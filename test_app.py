"""가짜 공급자로 작업 분리와 기록을 검증한다. 유료 API를 호출하지 않는다.

실행: python test_app.py
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading

data_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = data_dir
for name in ("HF_NAMESPACE", "HF_S3_ACCESS_KEY_ID", "HF_S3_SECRET_ACCESS_KEY", "HF_BUCKET"):
    os.environ.pop(name, None)

import app
from pipeline import history, storage


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def fake_video(client, url, prompt, duration, save):
    if "bad" in url:
        raise RuntimeError("fake failure")
    return write(save, f"video-{duration}")


def fake_merge(videos, output):
    with open(videos[0]) as f:
        return write(output, f.read())


class FakeGrok:
    @staticmethod
    def image_data_uri(path):
        return f"data:{path}"


app.GrokClient = FakeGrok
real_merge = app.merger.merge_videos
relayouts = []


def fake_relayout(client, path, prompt, save, ratio):
    relayouts.append(ratio)
    return write(save, "image"), path


app.analyzer.analyze_image = lambda path, ratio: {"expand_prompt": "x", "video_prompt": "y"}
app.expand.relayout_image = fake_relayout
app.video_gen.generate_video = fake_video
app.merger.merge_videos = fake_merge


def card(name):
    return write(os.path.join(tempfile.mkdtemp(), name), "card")


def run(order, seconds):
    return list(app.start_conversion(order, seconds))[-1]


def by_duration(seconds):
    return next(job for job in history.list_jobs() if job["duration"] == seconds)


# 동시에 돌린 작업은 서로의 결과를 건드리지 않는다
results = {}
threads = [
    threading.Thread(target=lambda s=s: results.__setitem__(s, run([card("a.png")], s)))
    for s in (3, 7)
]
for t in threads:
    t.start()
for t in threads:
    t.join()
assert results[3][3] != results[7][3]
for seconds, result in results.items():
    with open(result[3]) as f:
        assert f.read() == f"video-{seconds}"
    assert by_duration(seconds)["status"] == history.DONE

# 일부 카드만 실패하면 일부 실패로 남고 실패 사유가 기록된다
run([card("ok.png"), card("bad.png")], 4)
partial = by_duration(4)
assert partial["status"] == history.PARTIAL
assert partial["cards"][1]["error"] == "fake failure"
assert partial["final"] == "final.mp4"

# 전부 실패
run([card("bad.png")], 5)
assert by_duration(5)["status"] == history.FAILED

# 브라우저가 끊겨 제너레이터가 닫히면 중단으로 남는다
generator = app.start_conversion([card("a.png")], 6)
next(generator)
generator.close()
assert by_duration(6)["status"] == history.INTERRUPTED

# 서버 재시작 시 돌던 작업은 중단 처리된다
stale = history.create_job([card("a.png")], 8)
history.mark_interrupted()
assert history.load_job(stale["id"])["status"] == history.INTERRUPTED

# 손상된 기록은 목록을 깨지 않는다
os.makedirs(history.job_dir("00000000-broken"))
write(history.job_file("00000000-broken", "job.json"), "{broken")
assert all(job["id"] != "00000000-broken" for job in history.list_jobs())

# 기록 상세 화면
detail = app.job_detail_html(partial["id"])
assert "final.mp4" in detail and "fake failure" in detail and "bad.png" in detail
assert detail.count("<img") == 4  # 원본 2 + 16:9 2
assert "기록을 읽을 수 없습니다" in app.job_detail_html("missing")

# 목록 행: 파일명은 HTML로 해석되지 않고, 여러 장이면 "외 n장"으로 표시
row = app.history_row_html({
    "created_at": "2026-11-11 12:00", "duration": 5, "status": history.DONE,
    "cards": [{"name": "<img src=x>.png"}, {"name": "b.png"}],
})
assert "<img" not in row and "외 1장" in row

# 비율 선택: 원본은 재배치를 건너뛰고, 그 외에는 고른 비율로 재배치한다
relayouts.clear()
list(app.start_conversion([card("o.png")], 9, app.ORIGINAL))
original_job = by_duration(9)
assert relayouts == [] and original_job["ratio"] == app.ORIGINAL
assert original_job["cards"][0]["expanded"] is None and original_job["status"] == history.DONE
assert "원본 그대로" in app.job_detail_html(original_job["id"])
assert app.job_detail_html(original_job["id"]).count("<img") == 1
list(app.start_conversion([card("v.png")], 10, "9:16"))
assert relayouts == ["9:16"] and by_duration(10)["ratio"] == "9:16"
assert "9:16 변환" in app.job_detail_html(by_duration(10)["id"])

# 길이 값은 허용 범위로 제한한다
assert app.parse_seconds("7") == 7 and app.parse_seconds(99) == 10 and app.parse_seconds(0) == 3
assert app.parse_seconds(None) == 5 and app.parse_seconds("abc") == 5

assert app.parse_ratio("9:16 세로") == "9:16" and app.parse_ratio("원본 그대로") == app.ORIGINAL
assert app.parse_ratio("1:1") == "1:1" and app.parse_ratio("7:7") == "16:9" and app.parse_ratio(None) == "16:9"

# 비율별 재배치 지시문
landscape = app.analyzer._build_expand_prompt("bg", "subject", "t", "b", "", False, "16:9")
portrait = app.analyzer._build_expand_prompt("bg", "subject", "t", "b", "", False, "9:16")
assert "16:9 widescreen" in landscape and "right half" in landscape
assert "9:16 canvas" in portrait and "upper part" in portrait and "right half" not in portrait

# 크기가 다른 영상은 첫 영상 크기로 맞춰 합친다
clip_dir = tempfile.mkdtemp()


def make_clip(name, size):
    path = os.path.join(clip_dir, name)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=blue:s={size}:d=1",
                    "-pix_fmt", "yuv420p", path], check=True)
    return path


merged = real_merge([make_clip("a.mp4", "320x400"), make_clip("b.mp4", "640x360")], os.path.join(clip_dir, "out", "final.mp4"))
assert app.merger._video_size(merged) == (320, 400)
same = real_merge([make_clip("c.mp4", "320x180"), make_clip("d.mp4", "320x180")], os.path.join(clip_dir, "out", "same.mp4"))
assert app.merger._video_size(same) == (320, 180)

# 삭제 대상 선정: 오래된 순, 방금 올린 작업은 보호
assert storage.select_deletions({"a": 1, "b": 1, "c": 1}, "c", max_jobs=2, max_bytes=99) == ["a"]
assert storage.select_deletions({"a": 5, "b": 5, "c": 5}, "c", max_jobs=9, max_bytes=6) == ["a", "b"]
assert storage.select_deletions({"a": 50}, "a", max_jobs=9, max_bytes=6) == []
assert storage.select_deletions({"a": 1}, "a", max_jobs=1, max_bytes=1) == []


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.order = []
        self.fail_on = None
        self.deny_write = False

    def upload_file(self, path, bucket, key, ExtraArgs=None, Config=None):
        if self.fail_on and key.endswith(self.fail_on):
            raise RuntimeError("upload failed")
        with open(path, "rb") as f:
            self.objects[key] = f.read()
        self.order.append(key)

    def get_paginator(self, name):
        return self

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k, "Size": len(v)} for k, v in self.objects.items() if k.startswith(Prefix)]}

    def put_object(self, Bucket, Key, Body):
        if self.deny_write:
            raise type("E", (Exception,), {"response": {"Error": {"Code": "AccessDenied"}}})()
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        del self.objects[Key]

    def download_file(self, bucket, key, path, Config=None):
        with open(path, "wb") as f:
            f.write(self.objects[key])


fake = FakeS3()
storage._client = fake
os.environ.update(HF_NAMESPACE="a", HF_S3_ACCESS_KEY_ID="b", HF_S3_SECRET_ACCESS_KEY="c", HF_BUCKET="d")


def stored_ids():
    return sorted({k.split("/")[1] for k in fake.objects})


def make_local_job(job_id, payload="x"):
    write(history.job_file(job_id, "final.mp4"), payload)
    write(history.job_file(job_id, "job.json"), json.dumps({
        "id": job_id, "created_at": "2026-11-11 12:00", "duration": 5, "status": history.DONE,
        "final": "final.mp4", "thumbnail": None, "cards": [],
    }))


# 100건을 넘으면 오래된 것부터 지운다
for n in range(105):
    job_id = f"20260101-{n:06d}-aaaaaa"
    make_local_job(job_id)
    storage.upload_job(job_id, history.job_dir(job_id))
assert len(stored_ids()) == 100
assert stored_ids()[0] == "20260101-000005-aaaaaa"
assert fake.order[1].endswith("job.json")  # job.json은 마지막에 올라간다

# 용량 상한을 넘어도 오래된 것부터 지운다
storage.MAX_BYTES = sum(len(v) for v in fake.objects.values())
big_id = "20260102-000000-bbbbbb"
make_local_job(big_id, "y" * 500)
storage.upload_job(big_id, history.job_dir(big_id))
assert sum(len(v) for v in fake.objects.values()) <= storage.MAX_BYTES
assert big_id in stored_ids() and "20260101-000005-aaaaaa" not in stored_ids()
storage.MAX_BYTES = 90 * 1024 ** 3

# 서버 파일이 지워져도 저장소에서 받아 보여준다
shutil.rmtree(history.job_dir(big_id))
assert big_id in [job["id"] for job in history.all_jobs(100)]
assert not os.path.exists(history.job_file(big_id, "final.mp4"))  # 목록만 열면 영상은 안 받는다
remote_detail = app.job_detail_html(big_id)
assert "gradio_api/file=" in remote_detail and "final.mp4" in remote_detail
assert os.path.exists(history.job_file(big_id, "final.mp4"))

# 저장소에서 자동 삭제된 기록은 로컬 사본이 남아 있어도 목록에 안 나온다
evicted_id = "20260101-000000-aaaaaa"
assert os.path.exists(history.job_file(evicted_id, "job.json"))
assert evicted_id not in [job["id"] for job in history.all_jobs(200)]

# 업로드가 실패하면 화면에 안내하고, 깨진 기록은 목록에 안 나온다
fail_id = "20260103-000000-cccccc"
make_local_job(fail_id)
fake.fail_on = "job.json"
notice = app.finish_job(history.load_job(fail_id), history.DONE)
assert "저장 실패" in notice
assert fail_id not in [job["id"] for job in storage.list_jobs(100)]
assert fail_id in [job["id"] for job in history.all_jobs(200)]  # 서버에 있는 동안은 보여준다
fake.fail_on = None

# 저장소 점검: 정상, 쓰기 거부, 환경변수 누락을 구분한다
connected, message = storage.check()
assert connected and "건" in message and "health/ping.txt" not in fake.objects
fake.deny_write = True
assert storage.check() == (False, "AccessDenied")
fake.deny_write = False
assert "저장소 연결 안 됨 (AccessDenied)" in app.storage_status_html(False, "AccessDenied")
saved_bucket = os.environ.pop("HF_BUCKET")
assert storage.check() == (False, "환경변수 없음: HF_BUCKET")
os.environ["RENDER"] = "true"
assert "저장소 미설정" in app.finish_job(history.load_job(fail_id), history.DONE)
del os.environ["RENDER"]
os.environ["HF_BUCKET"] = "  d\n"
assert storage.enabled() and storage.bucket() == "d"  # 붙여넣기 공백 제거
os.environ["HF_BUCKET"] = saved_bucket

# 저장소 연결이 끊겨도 탭이 깨지지 않는다
storage._client = None
original_list = storage.list_jobs
storage.list_jobs = lambda limit: (_ for _ in ()).throw(RuntimeError("down"))
assert app.refresh_history()["error"] is True
storage.list_jobs = original_list

shutil.rmtree(data_dir)
print("OK")
