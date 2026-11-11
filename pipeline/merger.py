import os
import subprocess
import tempfile


def _video_size(path):
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    width, height = output.split("x")
    return int(width), int(height)


def _fit_to_size(path, size, output_path):
    """크기가 다른 영상을 잘리지 않게 줄이고 남는 부분은 검은 여백으로 채운다."""
    width, height = size
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-vf", video_filter, "-r", "24", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-threads", "1", output_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 크기 맞춤 실패: {result.stderr}")


def merge_videos(video_paths, output_path):
    if not video_paths:
        raise ValueError("병합할 영상이 없습니다.")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    sizes = [_video_size(path) for path in video_paths]
    if len(set(sizes)) > 1:
        # 크기가 섞이면 그대로 이어 붙일 수 없어 첫 카드 크기에 맞춘다. 짝수여야 인코딩된다
        target = (sizes[0][0] // 2 * 2, sizes[0][1] // 2 * 2)
        with tempfile.TemporaryDirectory() as fit_dir:
            fitted = []
            for i, path in enumerate(video_paths):
                fitted_path = os.path.join(fit_dir, f"{i:03d}.mp4")
                _fit_to_size(path, target, fitted_path)
                fitted.append(fitted_path)
            return _concat(fitted, output_path)
    return _concat(video_paths, output_path)


def _concat(video_paths, output_path):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for path in video_paths:
            f.write(f"file '{os.path.abspath(path)}'\n")
        list_path = f.name

    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 병합 실패: {result.stderr}")
    finally:
        os.remove(list_path)

    return output_path
