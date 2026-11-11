import json
import os
import shutil
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from PIL import Image

from pipeline import storage

DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", "data"))
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
KST = timezone(timedelta(hours=9))

RUNNING = "진행중"
DONE = "완료"
PARTIAL = "일부 실패"
FAILED = "실패"
INTERRUPTED = "중단됨"


def job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def job_file(job_id, relative_path):
    return os.path.join(job_dir(job_id), relative_path)


def save_job(job):
    path = job_file(job["id"], "job.json")
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=1)
    os.replace(temp_path, path)


def create_job(image_paths, duration):
    """작업 폴더를 만들고 원본을 복사한다. 작업끼리 파일이 겹치지 않도록 폴더를 분리한다."""
    now = datetime.now(KST)
    job_id = f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    os.makedirs(job_file(job_id, "originals"))

    cards = []
    for i, path in enumerate(image_paths):
        original = os.path.join("originals", f"{i + 1:03d}{os.path.splitext(path)[1].lower()}")
        shutil.copy(path, job_file(job_id, original))
        cards.append({
            "name": os.path.basename(path),
            "original": original,
            "expanded": None,
            "video": None,
            "error": None,
        })

    job = {
        "id": job_id,
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "duration": duration,
        "status": RUNNING,
        "final": None,
        "thumbnail": _make_thumbnail(job_id, image_paths[0]),
        "cards": cards,
    }
    save_job(job)
    return job


def _make_thumbnail(job_id, image_path):
    """목록에서 원본 전체를 받지 않도록 작은 이미지를 만든다."""
    try:
        with Image.open(image_path) as image:
            image = image.convert("RGB")
            image.thumbnail((160, 160))
            image.save(job_file(job_id, "thumb.jpg"), quality=80)
        return "thumb.jpg"
    except OSError:
        return None


def load_job(job_id):
    with open(job_file(job_id, "job.json"), encoding="utf-8") as f:
        return json.load(f)


def list_jobs():
    """최신 작업부터 돌려준다. 읽을 수 없는 기록은 건너뛴다."""
    if not os.path.isdir(JOBS_DIR):
        return []
    jobs = []
    for job_id in sorted(os.listdir(JOBS_DIR), reverse=True):
        try:
            jobs.append(load_job(job_id))
        except (OSError, ValueError):
            continue
    return jobs


def _ensure_thumbnail(job):
    """목록용 썸네일을 이 서버로 받아둔다. 실패해도 목록은 보여준다."""
    name = job.get("thumbnail")
    if not name or os.path.exists(job_file(job["id"], name)):
        return
    try:
        storage.download_file(job["id"], name, job_file(job["id"], name))
    except Exception as e:
        print(f"thumbnail download failed for {job['id']}: {type(e).__name__}", flush=True)


def all_jobs(limit):
    """저장소 기록과 이 서버에서 돌고 있거나 보관에 실패한 기록을 합쳐 최신순으로 돌려준다."""
    local = list_jobs()
    if not storage.enabled():
        return local[:limit]
    remote = storage.list_jobs(limit)
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_ensure_thumbnail, remote))
    # 저장소에서 자동 삭제된 기록의 로컬 사본은 다시 보여주지 않는다
    jobs = {job["id"]: job for job in local if job["status"] == RUNNING or job.get("upload_failed")}
    jobs.update({job["id"]: job for job in remote})
    return [jobs[job_id] for job_id in sorted(jobs, reverse=True)[:limit]]


def read_job(job_id):
    """기록을 읽는다. 이 서버에 없으면 저장소에서 받아온다."""
    try:
        return load_job(job_id)
    except OSError:
        if not storage.enabled():
            raise
    # ponytail: 받아둔 사본은 서버가 잠들 때 지워진다. 깨어 있는 시간이 길어져 디스크가 차면 정리 추가
    storage.download_job(job_id, job_dir(job_id))
    return load_job(job_id)


def file_url(job_id, name):
    """브라우저에서 열 주소. 앱 로그인을 거쳐서만 받을 수 있다."""
    return f"gradio_api/file={urllib.parse.quote(job_file(job_id, name))}"


def mark_interrupted():
    """서버가 재시작되면 돌던 작업은 이어지지 않으므로 중단으로 표시한다."""
    for job in list_jobs():
        if job["status"] == RUNNING:
            job["status"] = INTERRUPTED
            save_job(job)
