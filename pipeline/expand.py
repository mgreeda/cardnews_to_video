import os

from pipeline.grok import GrokClient


def relayout_image(client: GrokClient, image_path, prompt, save_path, aspect_ratio):
    """지정 비율로 재배치한 이미지를 로컬에 저장하고 (로컬 경로, 원본 URL)을 반환한다.

    image_url은 영상 생성 단계(video_gen)에서 재사용한다 — Grok 영상 API가
    image_url을 입력으로 받기 때문에 로컬 파일을 다시 업로드할 필요가 없다.
    """
    image_url = client.relayout_image(image_path, prompt, aspect_ratio)
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    client.download_file(image_url, save_path)
    return save_path, image_url
