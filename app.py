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
        # Render 서버 파일은 재시작 때 지워지므로 미설정이면 알린다
        return " (저장소 미설정: 기록이 보관되지 않습니다. 영상은 지금 바로 받아두세요)" if os.environ.get("RENDER") else ""
    try:
        storage.upload_job(job["id"], history.job_dir(job["id"]))
        return ""
    except Exception as e:
        print(f"storage upload failed for {job['id']}: {storage.describe_error(e)}", flush=True)
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


PRIMARY_LINK = (
    "padding:12px 16px;border-radius:8px;background:var(--button-primary-background-fill);"
    "color:var(--button-primary-text-color);font-weight:700;text-decoration:none;text-align:center;"
)
SECONDARY_LINK = (
    "display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid var(--border-color-primary);"
    "color:var(--body-text-color);font-size:14px;font-weight:600;text-decoration:none;"
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

    def image_box(name, label, width, height):
        box = f"width:{width}px;height:{height}px;margin:0;border-radius:8px;background:var(--background-fill-secondary);display:block;"
        if name:
            content = (
                f'<a href="{url(name)}" target="_blank" rel="noopener">'
                f'<img src="{url(name)}" alt="{label}" loading="lazy" style="{box}object-fit:contain;"></a>'
            )
        else:
            content = f'<div style="{box}"></div>'
        return f'<div>{content}<div style="font-size:12px;opacity:.6;margin-top:4px;text-align:center;">{label}</div></div>'

    def fact(label, value):
        return (
            '<div style="display:flex;justify-content:space-between;gap:12px;padding:10px 0;'
            'border-bottom:1px solid var(--border-color-primary);font-size:14px;">'
            f'<span style="opacity:.65;">{label}</span><b>{value}</b></div>'
        )

    cards = job["cards"]
    title = html.escape(cards[0]["name"]) if cards else ""
    if len(cards) > 1:
        title += f" 외 {len(cards) - 1}장"
    color = STATUS_COLORS.get(job["status"], "#6b7280")

    if job["final"]:
        media = (
            f'<video controls preload="metadata" src="{url(job["final"])}" '
            'style="width:100%;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:10px;display:block;"></video>'
        )
        download = f'<a href="{url(job["final"])}" download style="{PRIMARY_LINK}margin-top:16px;display:block;">완성 영상 다운로드</a>'
    else:
        media = (
            '<div style="width:100%;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;'
            'background:var(--background-fill-secondary);border-radius:10px;opacity:.7;">완성된 영상이 없습니다</div>'
        )
        download = ""

    top = (
        '<div style="display:flex;flex-wrap:wrap;gap:24px;align-items:flex-start;">'
        f'<div style="flex:1 1 480px;min-width:0;max-width:680px;">{media}</div>'
        '<div style="flex:1 1 240px;min-width:0;">'
        f'<div style="color:{color};font-weight:700;font-size:14px;">{html.escape(job["status"])}</div>'
        f'<div style="font-size:18px;font-weight:700;margin:4px 0 12px;overflow-wrap:anywhere;">{title}</div>'
        f'{fact("작업일시", html.escape(job["created_at"]))}'
        f'{fact("카드 수", f"{len(cards)}장")}'
        f'{fact("카드당 길이", str(job["duration"]) + "초")}'
        f"{download}</div></div>"
    )

    rows = []
    for i, card in enumerate(cards):
        if card["error"]:
            state = f'<b style="color:{STATUS_COLORS[history.FAILED]};">실패</b>'
            detail = f'<div style="font-size:13px;opacity:.75;margin-top:4px;overflow-wrap:anywhere;">{html.escape(card["error"])}</div>'
        elif card["video"]:
            state = f'<a href="{url(card["video"])}" download style="{SECONDARY_LINK}">영상 받기</a>'
            detail = ""
        else:
            state = '<span style="opacity:.6;">미완료</span>'
            detail = ""
        rows.append(
            '<div style="display:flex;flex-wrap:wrap;align-items:center;gap:16px;padding:14px 0;'
            'border-top:1px solid var(--border-color-primary);">'
            f'<div style="flex:1 1 180px;min-width:0;"><div style="font-size:12px;opacity:.6;">카드 {i + 1}</div>'
            f'<div style="font-weight:600;overflow-wrap:anywhere;">{html.escape(card["name"])}</div>{detail}</div>'
            f'{image_box(card["original"], "원본", 90, 90)}'
            f'{image_box(card["expanded"], "16:9 변환", 160, 90)}'
            f'<div style="flex:0 0 96px;text-align:center;">{state}</div>'
            "</div>"
        )

    return (
        f"{top}"
        f'<div style="font-size:16px;font-weight:700;margin:32px 0 12px;">카드별 결과</div>'
        f'{"".join(rows)}'
    )


def refresh_history():
    """탭을 열 때마다 저장소 상태와 최신 목록을 보여준다."""
    connected, message = storage.check()
    state = {"selected": None, "connected": connected, "message": message, "error": False}
    try:
        return {**state, "jobs": history.all_jobs(HISTORY_LIMIT)}
    except Exception as e:
        print(f"history list failed: {storage.describe_error(e)}", flush=True)
        return {**state, "jobs": history.list_jobs()[:HISTORY_LIMIT], "error": True}


def storage_status_html(connected, message):
    if connected:
        color, text = STATUS_COLORS[history.DONE], f"저장소 연결됨 · {html.escape(message)}"
    else:
        color = STATUS_COLORS[history.FAILED]
        text = f"저장소 연결 안 됨 ({html.escape(message)}) · 지금 만든 기록은 서버가 재시작되면 사라집니다"
    return f'<div style="color:{color};font-size:14px;font-weight:700;padding:4px 0 8px;">{text}</div>'


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
                with gr.Row():
                    back_btn = gr.Button("← 목록으로", size="sm", scale=0, min_width=120)
                back_btn.click(refresh_history, outputs=[history_state])
                gr.HTML(job_detail_html(state["selected"]), padding=False)
                return

            if "connected" in state:
                gr.HTML(storage_status_html(state["connected"], state["message"]), padding=False)
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
