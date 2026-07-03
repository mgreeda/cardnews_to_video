from pipeline.grok import GrokClient


def generate_video(client: GrokClient, image_url, prompt, duration, save_path):
    request_id = client.generate_video(image_url, prompt, duration)
    video_url = client.poll_video(request_id)
    client.download_file(video_url, save_path)
    return save_path
