import base64
import mimetypes
import os
import time

import requests

BASE_URL = "https://api.x.ai/v1"

ENDPOINTS = {
    "image_edit": "/images/edits",
    "video_generate": "/videos/generations",
    "video_status": "/videos/{request_id}",
}

IMAGE_EDIT_MODEL = "grok-imagine-image-quality"
VIDEO_MODEL = "grok-imagine-video"


class GrokError(Exception):
    pass


class GrokClient:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("XAI_API_KEY")
        if not self.api_key:
            raise GrokError("XAI_API_KEY가 설정되지 않았습니다.")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    @staticmethod
    def image_data_uri(image_path):
        mime = mimetypes.guess_type(image_path)[0]
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            mime = "image/jpeg"
        with open(image_path, "rb") as f:
            image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
        return f"data:{mime};base64,{image_b64}"

    def relayout_image(self, image_path, prompt, aspect_ratio):
        payload = {
            "model": IMAGE_EDIT_MODEL,
            "image": {"url": self.image_data_uri(image_path), "type": "image_url"},
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
        }
        result = self._post(ENDPOINTS["image_edit"], payload)
        return self._extract_image_url(result)

    def generate_video(self, image_url, prompt, duration, resolution="720p"):
        # 이미지 기반 영상은 입력 이미지 비율을 그대로 따르므로 aspect_ratio를 보내지 않는다
        payload = {
            "model": VIDEO_MODEL,
            "image": {"url": image_url},
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
        }
        result = self._post(ENDPOINTS["video_generate"], payload)
        request_id = result.get("request_id") or result.get("id")
        if not request_id:
            raise GrokError(f"응답에서 request_id를 찾을 수 없습니다: {result}")
        return request_id

    def poll_video(self, request_id, timeout=600, interval=5):
        path = ENDPOINTS["video_status"].format(request_id=request_id)
        elapsed = 0
        while elapsed < timeout:
            result = self._get(path)
            status = result.get("status")
            if status == "done":
                return self._extract_video_url(result)
            if status == "failed":
                raise GrokError(f"영상 생성 실패: {result}")
            time.sleep(interval)
            elapsed += interval
        raise GrokError(f"영상 생성 타임아웃 ({timeout}초): request_id={request_id}")

    def download_file(self, url, save_path):
        resp = self.session.get(url, stream=True, timeout=120)
        resp.raise_for_status()
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return save_path

    def _post(self, path, payload):
        try:
            resp = self.session.post(f"{BASE_URL}{path}", json=payload, timeout=120)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            raise GrokError(f"{path} 요청 실패: status={e.response.status_code} body={e.response.text[:500]}") from e
        except requests.RequestException as e:
            raise GrokError(f"{path} 요청 실패: {e}") from e

    def _get(self, path):
        try:
            resp = self.session.get(f"{BASE_URL}{path}", timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            raise GrokError(f"{path} 요청 실패: status={e.response.status_code} body={e.response.text[:500]}") from e
        except requests.RequestException as e:
            raise GrokError(f"{path} 요청 실패: {e}") from e

    @staticmethod
    def _extract_image_url(result):
        data = result.get("data")
        if data and data[0].get("url"):
            return data[0]["url"]
        raise GrokError(f"응답에서 이미지 URL을 찾을 수 없습니다: {result}")

    @staticmethod
    def _extract_video_url(result):
        video = result.get("video")
        if video and video.get("url"):
            return video["url"]
        if result.get("url"):
            return result["url"]
        raise GrokError(f"응답에서 영상 URL을 찾을 수 없습니다: {result}")
