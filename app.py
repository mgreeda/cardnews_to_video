import os

import gradio as gr
from dotenv import load_dotenv

from pipeline import analyzer, expand, merger, video_gen
from pipeline.grok import GrokClient

load_dotenv()

TEMP_DIR = "temp"
OUTPUT_DIR = "output"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


def add_images(files, order):
    if files:
        order = order + [f.name if hasattr(f, "name") else f for f in files]
    return order


def move_card(index, direction, order):
    new_index = index + direction
    if 0 <= new_index < len(order):
        order[index], order[new_index] = order[new_index], order[index]
    return order


def start_conversion(order, duration_per_card):
    if not order:
        yield "업로드된 이미지가 없습니다.", gr.update(value=0), "", None, [], []
        return

    total = len(order)
    status_lines = {i: f"{i + 1:03d} ⏳ 대기중" for i in range(total)}
    expanded_paths = []
    video_paths = []

    def status_text():
        return "\n".join(status_lines[i] for i in range(total))

    try:
        client = GrokClient()
    except Exception as e:
        yield f"오류: {e}", gr.update(value=0), status_text(), None, expanded_paths, video_paths
        return

    card_videos = []

    for i, path in enumerate(order):
        card_id = f"{i + 1:03d}"
        try:
            status_lines[i] = f"{card_id} ⏳ 분석중"
            yield f"{i + 1}/{total} 분석 중...", gr.update(value=(i / total) * 100), status_text(), None, expanded_paths, video_paths
            analysis = analyzer.analyze_image(path)

            status_lines[i] = f"{card_id} ✅ 분석완료 → ⏳ 16:9 확장중"
            yield f"{i + 1}/{total} 16:9 확장 중...", gr.update(value=(i / total) * 100), status_text(), None, expanded_paths, video_paths
            expanded_path, expanded_url = expand.expand_to_16x9(
                client, path, analysis["expand_prompt"],
                os.path.join(TEMP_DIR, f"{card_id}_expanded.jpg"),
            )
            expanded_paths = expanded_paths + [expanded_path]

            status_lines[i] = f"{card_id} ✅ 분석완료 → ✅ 확장완료 → ⏳ 영상생성중"
            yield f"{i + 1}/{total} 영상 생성 중...", gr.update(value=(i / total) * 100), status_text(), None, expanded_paths, video_paths
            card_video_path = video_gen.generate_video(
                client, expanded_url, analysis["video_prompt"], duration_per_card,
                os.path.join(TEMP_DIR, f"{card_id}_video.mp4"),
            )
            card_videos.append(card_video_path)
            video_paths = video_paths + [card_video_path]

            status_lines[i] = f"{card_id} ✅ 분석완료 → ✅ 확장완료 → ✅ 영상생성완료"
            yield f"{i + 1}/{total} 완료", gr.update(value=((i + 1) / total) * 100), status_text(), None, expanded_paths, video_paths
        except Exception as e:
            status_lines[i] = f"{card_id} ❌ 실패: {e}"
            yield f"{i + 1}/{total} 실패", gr.update(value=((i + 1) / total) * 100), status_text(), None, expanded_paths, video_paths

    if not card_videos:
        yield "모든 카드 변환에 실패했습니다.", gr.update(value=100), status_text(), None, expanded_paths, video_paths
        return

    final_path = merger.merge_videos(card_videos, os.path.join(OUTPUT_DIR, "final.mp4"))
    yield "모든 카드 변환 완료", gr.update(value=100), status_text(), final_path, expanded_paths, video_paths


with gr.Blocks(title="카드뉴스 영상 변환기") as demo:
    order_state = gr.State([])

    gr.Markdown("# 카드뉴스 영상 변환기")

    gr.Markdown("## 1. 이미지 업로드")
    upload = gr.File(file_count="multiple", file_types=["image"], label="카드뉴스 이미지 업로드 (JPG, PNG)")

    @gr.render(inputs=order_state)
    def card_grid(order):
        if not order:
            gr.Markdown("_업로드된 이미지가 없습니다._")
            return
        for i, path in enumerate(order):
            with gr.Row():
                gr.Image(value=path, height=80, width=80, show_label=False, interactive=False)
                gr.Markdown(f"**{i + 1:03d}** &nbsp; {os.path.basename(path)}")
                up_btn = gr.Button("▲", scale=0, min_width=40, interactive=i > 0)
                down_btn = gr.Button("▼", scale=0, min_width=40, interactive=i < len(order) - 1)
                up_btn.click(
                    lambda o, idx=i: move_card(idx, -1, o),
                    inputs=[order_state],
                    outputs=[order_state],
                )
                down_btn.click(
                    lambda o, idx=i: move_card(idx, 1, o),
                    inputs=[order_state],
                    outputs=[order_state],
                )

    upload.change(add_images, inputs=[upload, order_state], outputs=[order_state])

    gr.Markdown("## 2. 옵션")
    duration_slider = gr.Slider(minimum=3, maximum=10, value=5, step=1, label="카드당 영상 길이 (초)")

    gr.Markdown("## 3. 실행 및 진행 상황")
    start_btn = gr.Button("영상 변환 시작", variant="primary")
    progress_text = gr.Textbox(label="진행 상황", interactive=False)
    progress_bar = gr.Slider(minimum=0, maximum=100, value=0, label="진행률(%)", interactive=False)
    card_status = gr.Textbox(label="카드별 단계 상태", lines=8, interactive=False)

    gr.Markdown("## 4. 결과")
    result_video = gr.Video(label="완성된 영상")

    gr.Markdown("## 5. 중간 결과 (디버깅용) — 어느 단계에서 잘못됐는지 확인")
    gr.Markdown("### 카드별 16:9 변환 이미지")
    expanded_gallery = gr.Gallery(label="변환된 이미지", columns=4, height=220)

    video_state = gr.State([])

    gr.Markdown("### 카드별 생성된 영상")

    @gr.render(inputs=video_state)
    def video_grid(videos):
        if not videos:
            gr.Markdown("_아직 생성된 영상이 없습니다._")
            return
        for i, vid_path in enumerate(videos):
            gr.Video(value=vid_path, label=f"{i + 1:03d} 카드 영상", height=220)

    start_btn.click(
        start_conversion,
        inputs=[order_state, duration_slider],
        outputs=[progress_text, progress_bar, card_status, result_video, expanded_gallery, video_state],
    )

if __name__ == "__main__":
    app_username = os.environ.get("APP_USERNAME")
    app_password = os.environ.get("APP_PASSWORD")
    auth = (app_username, app_password) if app_username and app_password else None

    demo.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 7860)),
        auth=auth,
        auth_message="카드뉴스 영상 변환기 - 팀 공유 아이디/비밀번호를 입력하세요.",
    )
