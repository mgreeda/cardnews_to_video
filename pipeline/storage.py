"""작업 기록을 Hugging Face Storage Bucket에 보관한다. Render 무료 서버의 파일은 잠들 때 지워지기 때문."""
import json
import mimetypes
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

MAX_JOBS = 100
MAX_BYTES = 90 * 1024 ** 3  # 무료 비공개 저장공간 100GB 아래로 유지
PREFIX = "jobs/"
META_NAME = "job.json"
ENV_NAMES = ("HF_NAMESPACE", "HF_S3_ACCESS_KEY_ID", "HF_S3_SECRET_ACCESS_KEY", "HF_BUCKET")
# 메모리 512MB 서버라서 한 번에 한 조각만 올린다
TRANSFER = TransferConfig(multipart_threshold=256 * 1024 ** 2, multipart_chunksize=256 * 1024 ** 2, max_concurrency=1)

# ponytail: 프로세스 안의 잠금. 서버를 여러 대로 늘리면 저장소 쪽 잠금 필요
_upload_lock = threading.Lock()
_client_lock = threading.Lock()
_client = None


def enabled():
    return all(os.environ.get(name, "").strip() for name in ENV_NAMES)


def _env(name):
    # 붙여넣을 때 섞인 공백이나 줄바꿈 제거
    return os.environ[name].strip()


def bucket():
    return _env("HF_BUCKET")


def endpoint():
    return f"https://{_env('HF_NAMESPACE')}.s3.hf.co"


def describe_error(error):
    """비밀값 없이 원인만 담은 짧은 설명."""
    code = getattr(error, "response", {}).get("Error", {}).get("Code")
    return code or type(error).__name__


def check():
    """실제로 쓰고 지워보고 (연결 여부, 설명)을 돌려준다."""
    missing = [name for name in ENV_NAMES if not os.environ.get(name, "").strip()]
    if missing:
        return False, "환경변수 없음: " + ", ".join(missing)
    try:
        key = "health/ping.txt"
        client().put_object(Bucket=bucket(), Key=key, Body=b"ok")
        client().delete_object(Bucket=bucket(), Key=key)
        count = sum(1 for job in _scan().values() if job["has_meta"])
        return True, f"저장된 기록 {count}건"
    except Exception as e:
        return False, describe_error(e)


def client():
    global _client
    with _client_lock:
        if _client is None:
            _client = boto3.client(
                "s3",
                endpoint_url=endpoint(),
                aws_access_key_id=_env("HF_S3_ACCESS_KEY_ID"),
                aws_secret_access_key=_env("HF_S3_SECRET_ACCESS_KEY"),
                config=Config(
                    region_name="us-east-1",
                    s3={"addressing_style": "path"},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        return _client


def _key(job_id, name):
    return f"{PREFIX}{job_id}/{name}"


def _scan():
    """저장된 작업별 {size, keys, has_meta}."""
    jobs = {}
    pages = client().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=PREFIX)
    for page in pages:
        for item in page.get("Contents", []):
            job_id, _, name = item["Key"][len(PREFIX):].partition("/")
            job = jobs.setdefault(job_id, {"size": 0, "keys": [], "has_meta": False})
            job["size"] += item["Size"]
            job["keys"].append(item["Key"])
            job["has_meta"] = job["has_meta"] or name == META_NAME
    return jobs


def select_deletions(sizes, keep_id, max_jobs=None, max_bytes=None):
    """건수·용량 상한을 맞추기 위해 지울 작업을 오래된 순으로 고른다. 작업 ID는 시간순으로 정렬된다."""
    max_jobs = MAX_JOBS if max_jobs is None else max_jobs
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    count = len(sizes)
    total = sum(sizes.values())
    victims = []
    for job_id in sorted(sizes):
        if count <= max_jobs and total <= max_bytes:
            break
        if job_id == keep_id:
            continue
        victims.append(job_id)
        count -= 1
        total -= sizes[job_id]
    return victims


def _enforce_limits(keep_id):
    jobs = _scan()
    sizes = {job_id: job["size"] for job_id, job in jobs.items()}
    for job_id in select_deletions(sizes, keep_id):
        # job.json을 먼저 지워서 중간에 실패해도 깨진 기록이 목록에 안 나오게 한다
        for key in sorted(jobs[job_id]["keys"], key=lambda k: not k.endswith(f"/{META_NAME}")):
            client().delete_object(Bucket=bucket(), Key=key)


def upload_job(job_id, local_dir):
    """작업 폴더를 올린 뒤 상한을 적용한다. job.json을 마지막에 올려 완성된 기록만 목록에 나온다."""
    with _upload_lock:
        names = []
        for root, _, files in os.walk(local_dir):
            names += [os.path.relpath(os.path.join(root, f), local_dir) for f in files]
        for name in sorted(names, key=lambda n: n == META_NAME):
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            client().upload_file(
                os.path.join(local_dir, name), bucket(), _key(job_id, name),
                ExtraArgs={"ContentType": content_type}, Config=TRANSFER,
            )
        _enforce_limits(job_id)


def load_job(job_id):
    body = client().get_object(Bucket=bucket(), Key=_key(job_id, META_NAME))["Body"]
    return json.loads(body.read())


def list_jobs(limit):
    jobs = _scan()
    ids = sorted((job_id for job_id, job in jobs.items() if job["has_meta"]), reverse=True)[:limit]
    with ThreadPoolExecutor(max_workers=16) as pool:
        return list(pool.map(load_job, ids))


def download_file(job_id, name, local_path):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    client().download_file(bucket(), _key(job_id, name), local_path, Config=TRANSFER)


def download_job(job_id, local_dir):
    """job.json을 마지막에 받아서, 파일이 있으면 다운로드가 끝난 것으로 볼 수 있게 한다."""
    prefix = _key(job_id, "")
    keys = _scan()[job_id]["keys"]
    for key in sorted(keys, key=lambda k: k.endswith(f"/{META_NAME}")):
        name = key[len(prefix):]
        download_file(job_id, name, os.path.join(local_dir, name))
