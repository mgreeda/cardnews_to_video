import html
import os

import gradio as gr
from dotenv import load_dotenv

load_dotenv()

from pipeline import analyzer, expand, history, merger, storage, video_gen
from pipeline.grok import GrokClient

# 동시에 돌릴 수 있는 변환 작업 수. 초과분은 Gradio 큐에서 순서를 기다린다.
MAX_CONCURRENT_JOBS = 3
HISTORY_LIMIT = storage.MAX_JOBS
LAUNCH_KWARGS = {"allowed_paths": [history.DATA_DIR]}


def add_images(files, order):
    if files:
        order = order + [f.name if hasattr(f, "name") else f for f in files]
    return order


def move_card(index, direction, order):
    new_index = index + direction
    if 0 <= new_index < len(order):
        order[index], order[new_index] = order[new_index], order[index]
    return order


def finish_job(job, status):
    """최종 상태를 기록하고 저장소에 보관한다. 보관에 실패하면 화면에 덧붙일 안내를 돌려준다."""
    job["status"] = status
    history.save_job(job)
    if not storage.enabled():
        return ""
    try:
        storage.upload_job(job["id"], history.job_dir(job["id"]))
        return ""
    except Exception as e:
        print(f"storage upload failed for {job['id']}: {type(e).__name__}", flush=True)
        job["upload_failed"] = True
        history.save_job(job)
        return " (작업 기록 저장 실패: 영상은 지금 바로 받아두세요)"


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
        job = history.create_job(order, duration_per_card)
    except Exception as e:
        yield f"오류: {e}", gr.update(value=0), status_text(), None, expanded_paths, video_paths
        return

    job_id = job["id"]
    card_videos = []

    try:
        for i, path in enumerate(order):
            card_id = f"{i + 1:03d}"
            card = job["cards"][i]
            try:
                status_lines[i] = f"{card_id} ⏳ 분석중"
                yield f"{i + 1}/{total} 분석 중...", gr.update(value=(i / total) * 100), status_text(), None, expanded_paths, video_paths
                analysis = analyzer.analyze_image(path)

                status_lines[i] = f"{card_id} ✅ 분석완료 → ⏳ 16:9 확장중"
                yield f"{i + 1}/{total} 16:9 확장 중...", gr.update(value=(i / total) * 100), status_text(), None, expanded_paths, video_paths
                expanded_name = f"{card_id}_expanded.jpg"
                expanded_path, expanded_url = expand.expand_to_16x9(
                    client, path, analysis["expand_prompt"],
                    history.job_file(job_id, expanded_name),
                )
                expanded_paths = expanded_paths + [expanded_path]
                card["expanded"] = expanded_name
                history.save_job(job)

                status_lines[i] = f"{card_id} ✅ 분석완료 → ✅ 확장완료 → ⏳ 영상생성중"
                yield f"{i + 1}/{total} 영상 생성 중...", gr.update(value=(i / total) * 100), status_text(), None, expanded_paths, video_paths
                video_name = f"{card_id}_video.mp4"
                card_video_path = video_gen.generate_video(
                    client, expanded_url, analysis["video_prompt"], duration_per_card,
                    history.job_file(job_id, video_name),
                )
                card_videos.append(card_video_path)
                video_paths = video_paths + [card_video_path]
                card["video"] = video_name
                history.save_job(job)

                status_lines[i] = f"{card_id} ✅ 분석완료 → ✅ 확장완료 → ✅ 영상생성완료"
                yield f"{i + 1}/{total} 완료", gr.update(value=((i + 1) / total) * 100), status_text(), None, expanded_paths, video_paths
            except Exception as e:
                card["error"] = str(e)
                history.save_job(job)
                status_lines[i] = f"{card_id} ❌ 실패: {e}"
                yield f"{i + 1}/{total} 실패", gr.update(value=((i + 1) / total) * 100), status_text(), None, expanded_paths, video_paths

        if not card_videos:
            notice = finish_job(job, history.FAILED)
            yield f"모든 카드 변환에 실패했습니다.{notice}", gr.update(value=100), status_text(), None, expanded_paths, video_paths
            return

        try:
            final_path = merger.merge_videos(card_videos, history.job_file(job_id, "final.mp4"))
        except Exception as e:
            notice = finish_job(job, history.FAILED)
            yield f"영상 병합 실패: {e}{notice}", gr.update(value=100), status_text(), None, expanded_paths, video_paths
            return

        job["final"] = "final.mp4"
        notice = finish_job(job, history.DONE if len(card_videos) == total else history.PARTIAL)
        yield f"모든 카드 변환 완료{notice}", gr.update(value=100), status_text(), final_path, expanded_paths, video_paths
    finally:
        # 브라우저를 닫아 중간에 끊긴 경우
        if job["status"] == history.RUNNING:
            finish_job(job, history.INTERRUPTED)


STATUS_COLORS = {
    history.DONE: "#15803d",
    history.RUNNING: "#1d4ed8",
    history.PARTIAL: "#b45309",
    history.FAILED: "#b91c1c",
    history.INTERRUPTED: "#6b7280",
}
ROW_GRID = "display:grid;grid-template-columns:64px 150px minmax(0,1fr) 70px 70px 90px;gap:12px;align-items:center;"


def history_header_html():
    cells = "".join(f"<div>{title}</div>" for title in ("", "작업일시", "파일", "카드", "길이", "상태"))
    # 오른쪽 여백은 행의 보기 버튼 폭만큼
    return f'<div style="{ROW_GRID}padding-right:96px;font-size:13px;font-weight:600;opacity:.65;">{cells}</div>'


def history_row_html(job):
    cards = job["cards"]
    title = html.escape(cards[0]["name"]) if cards else ""
    if len(cards) > 1:
        title += f" 외 {len(cards) - 1}장"
    color = STATUS_COLORS.get(job["status"], "#6b7280")
    thumbnail = ""
    if job.get("thumbnail") and os.path.exists(history.job_file(job["id"], job["thumbnail"])):
        src = html.escape(history.file_url(job["id"], job["thumbnail"]), quote=True)
        thumbnail = f'<img src="{src}" alt="" loading="lazy" style="width:64px;height:64px;object-fit:cover;border-radius:6px;display:block;">'
    return (
        f'<div style="{ROW_GRID}font-size:14px;">'
        f"<div>{thumbnail}</div>"
        f"<div>{html.escape(job['created_at'])}</div>"
        f'<div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;">{title}</div>'
        f"<div>{len(cards)}장</div>"
        f"<div>{job['duration']}초</div>"
        f'<div style="color:{color};font-weight:700;">{html.escape(job["status"])}</div>'
        "</div>"
    )


def job_detail_html(job_id):
    """선택한 기록의 상세 화면."""
    try:
        job = history.read_job(job_id)
    except Exception as e:
        print(f"history read failed for {job_id}: {type(e).__name__}", flush=True)
        return "<p>기록을 읽을 수 없습니다.</p>"

    def url(name):
        return html.escape(history.file_url(job_id, name), quote=True)

    def heading(text):
        return f'<h3 style="margin:24px 0 8px;font-size:16px;">{text}</h3>'

    def image_grid(names):
        items = "".join(
            f'<a href="{url(name)}" target="_blank" rel="noopener">'
            f'<img src="{url(name)}" alt="" loading="lazy" style="width:100%;height:160px;object-fit:contain;'
            'background:rgba(127,127,127,.12);border-radius:6px;display:block;"></a>'
            for name in names
        )
        return f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;">{items}</div>'

    cards = job["cards"]
    parts = [
        f'<p style="font-size:15px;"><b>{html.escape(job["created_at"])}</b> · 카드 {len(cards)}장 · '
        f'카드당 {job["duration"]}초 · 상태: <b>{html.escape(job["status"])}</b></p>'
    ]
    if job["final"]:
        parts.append(heading("완성된 영상"))
        parts.append(f'<video controls preload="metadata" src="{url(job["final"])}" style="width:100%;max-height:420px;background:#000;border-radius:6px;"></video>')
        parts.append(f'<p><a href="{url(job["final"])}" download>완성 영상 다운로드</a></p>')

    parts.append(heading("원본 카드"))
    parts.append(image_grid([card["original"] for card in cards]))

    expanded = [card["expanded"] for card in cards if card["expanded"]]
    if expanded:
        parts.append(heading("16:9 변환 이미지"))
        parts.append(image_grid(expanded))

    rows = []
    for i, card in enumerate(cards):
        if card["error"]:
            state = f'실패: {html.escape(card["error"])}'
        elif card["video"]:
            state = f'완료 · <a href="{url(card["video"])}" download>영상 다운로드</a>'
        else:
            state = "미완료"
        rows.append(f'<li style="margin:4px 0;">{i + 1:03d} {html.escape(card["name"])} — {state}</li>')
    parts.append(heading("카드별 결과"))
    parts.append(f'<ul style="list-style:none;padding:0;margin:0;">{"".join(rows)}</ul>')
    return "".join(parts)


def refresh_history():
    """탭을 열 때마다 최신 목록을 보여준다."""
    try:
        return {"jobs": history.all_jobs(HISTORY_LIMIT), "selected": None, "error": False}
    except Exception as e:
        print(f"history list failed: {type(e).__name__}", flush=True)
        return {"jobs": history.list_jobs()[:HISTORY_LIMIT], "selected": None, "error": True}


with gr.Blocks(title="카드뉴스 영상 변환기") as demo:
    gr.Markdown("# 카드뉴스 영상 변환기")

    with gr.Tab("영상 변환"):
        order_state = gr.State([])

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
            concurrency_limit=MAX_CONCURRENT_JOBS,
        )

    with gr.Tab("작업 기록") as history_tab:
        history_state = gr.State({"jobs": [], "selected": None})

        @gr.render(inputs=history_state)
        def history_view(state):
            if state["selected"]:
                back_btn = gr.Button("← 목록으로", size="sm")
                back_btn.click(refresh_history, outputs=[history_state])
                gr.HTML(job_detail_html(state["selected"]), padding=False)
                return

            if state.get("error"):
                gr.Markdown("**저장소에 연결하지 못했습니다. 잠시 후 탭을 다시 열어주세요.**")
            jobs = state["jobs"]
            if not jobs:
                gr.Markdown("_아직 작업 기록이 없습니다. 영상 변환을 실행하면 여기에 쌓입니다._")
                return
            gr.HTML(history_header_html(), padding=False)
            for job in jobs:
                with gr.Row(equal_height=True):
                    gr.HTML(history_row_html(job), scale=1, padding=False)
                    view_btn = gr.Button("보기", scale=0, min_width=80)
                    view_btn.click(
                        lambda current, job_id=job["id"]: {**current, "selected": job_id},
                        inputs=[history_state],
                        outputs=[history_state],
                    )

        history_tab.select(refresh_history, outputs=[history_state])

if __name__ == "__main__":
    history.mark_interrupted()

    app_username = os.environ.get("APP_USERNAME")
    app_password = os.environ.get("APP_PASSWORD")
    auth = (app_username, app_password) if app_username and app_password else None

    demo.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 7860)),
        auth=auth,
        auth_message="카드뉴스 영상 변환기 - 팀 공유 아이디/비밀번호를 입력하세요.",
        **LAUNCH_KWARGS,
    )
