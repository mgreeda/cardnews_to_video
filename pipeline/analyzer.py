import base64
import json
import mimetypes
import os

import anthropic

MODEL = "claude-sonnet-4-6"

VISUAL_TYPES = ["3d_medical", "2d_illustration", "photo_person", "text_only"]

VIDEO_PROMPT_BY_TYPE = {
    "3d_medical": (
        "Barely perceptible glow pulse on the 3D medical figure, pain area very slowly "
        "brightening and dimming. Everything else completely frozen and static."
    ),
    "2d_illustration": (
        "Barely perceptible soft glow shift on the illustration only. "
        "No floating, no drifting. Background, text, and all shapes completely frozen."
    ),
    "photo_person": (
        "Barely perceptible ambient breathing on the person, almost imperceptible. "
        "No other movement. Background completely static."
    ),
    "text_only": (
        "Almost completely still. At most an extremely faint background light shimmer. "
        "No element movement whatsoever."
    ),
}

COMMON_SUFFIX = (
    "Keep motion absolutely minimal and slow — this should look like a nearly still image "
    "with only the faintest hint of life. No camera movement. No zoom. No pan. No text "
    "modification. Do NOT add, generate, or introduce any new objects, shapes, people, "
    "icons, particles, or elements that are not already present in the source image — "
    "preserve the original composition exactly. Cinematic still. 16:9. "
    "Do not generate any audio, sound effects, music, or dialogue — completely silent video."
)


ANALYSIS_SYSTEM_PROMPT = """당신은 카드뉴스 이미지를 분석해서, 이 이미지를 16:9로 재배치하고
영상으로 만드는 데 필요한 정보를 추출하는 어시스턴트입니다.
주어진 이미지를 보고 아래 JSON 형식으로만 답변하세요. 다른 설명은 추가하지 마세요.

{
  "background_description": "배경 색상과 분위기를 영어로 짧게 설명 (예: 'soft light blue gradient, calm and clean')",
  "visual_type": "3d_medical | 2d_illustration | photo_person | text_only 중 하나",
  "main_visual_description": "메인 비주얼(사람/일러스트/사진 등)을 영어로 짧게 설명 (예: 'glowing blue 3D anatomical figure of a person holding their head in pain')",
  "title_text": "강조 배경바가 있는 제목 텍스트를 원문 그대로 (여러 줄이면 줄바꿈으로 구분)",
  "body_text": "제목 아래 본문 설명 텍스트를 원문 그대로",
  "brand_text": "우하단 또는 좌상단에 있는 브랜드/로고 영문 워드마크 (예: 'ANAPA CLINIC'), 없으면 빈 문자열",
  "has_icon_row": true 또는 false (원본 이미지에 아이콘/뱃지 줄이 실제로 존재하는지)
}

visual_type 분류 기준:
- 3d_medical: 3D 인체/의료 그래픽 (장기, 뼈, 통증 부위 표시 등)
- 2d_illustration: 2D 일러스트/아이콘 중심
- photo_person: 실사 인물 사진
- text_only: 텍스트 중심, 그래픽 요소가 거의 없음
"""


def _build_expand_prompt(background_description, main_visual_description,
                         title_text, body_text, brand_text, has_icon_row):
    lines = [
        "Completely redesign this card's layout for a 16:9 widescreen canvas. "
        "Do NOT simply pad or crop the original square image — this must be a full layout "
        "re-composition that reuses ONLY the exact elements already present in the original, "
        "repositioning them as described below. Do NOT invent or add any element that does not "
        "exist in the original image.",
        "- Keep the logo/brand badge fixed in the top-left corner, same size and position.",
        f"- Take the main visual subject ({main_visual_description}) and enlarge it, moving it to "
        "fill the right half of the new wider canvas, extending from top to bottom.",
        f"- Move the highlighted title bar(s) (\"{title_text}\") and the body paragraph "
        f"(\"{body_text}\") to the left-center area of the canvas, vertically centered, keeping the "
        "exact same Korean text, font style, and highlight colors.",
    ]
    if has_icon_row:
        lines.append("- Keep the row of circular icon badges along the bottom-left area, same style.")
    if brand_text:
        lines.append(f"- Keep the brand wordmark (\"{brand_text}\") fixed in the bottom-right corner.")
    lines.append(
        f"- Extend the background seamlessly across the new wide canvas, matching the existing "
        f"background color, style, and atmosphere ({background_description})."
    )
    lines.append("Do not add any new graphic elements, objects, people, or text that are not in the original.")
    return "\n".join(lines)


def _load_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
    return anthropic.Anthropic(api_key=api_key)


def _encode_image(image_path):
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type not in ("image/jpeg", "image/png"):
        mime_type = "image/jpeg"
    with open(image_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return mime_type, data


def analyze_image(image_path):
    client = _load_client()
    mime_type, image_data = _encode_image(image_path)

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=ANALYSIS_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime_type, "data": image_data},
                    },
                    {"type": "text", "text": "이 카드뉴스 이미지를 분석해주세요."},
                ],
            }
        ],
    )

    raw_text = response.content[0].text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[len("json"):]
        raw_text = raw_text.strip()
    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"분석 응답 JSON 파싱 실패: {e}") from e

    background_description = result.get("background_description", "neutral background")
    visual_type = result.get("visual_type", "text_only")
    if visual_type not in VISUAL_TYPES:
        visual_type = "text_only"
    main_visual_description = result.get("main_visual_description", "the main visual subject")
    title_text = result.get("title_text", "")
    body_text = result.get("body_text", "")
    brand_text = result.get("brand_text", "")
    has_icon_row = bool(result.get("has_icon_row", False))

    expand_prompt = _build_expand_prompt(
        background_description=background_description,
        main_visual_description=main_visual_description,
        title_text=title_text,
        body_text=body_text,
        brand_text=brand_text,
        has_icon_row=has_icon_row,
    )
    video_prompt = f"{VIDEO_PROMPT_BY_TYPE[visual_type]} {COMMON_SUFFIX}"

    return {
        "background_description": background_description,
        "visual_type": visual_type,
        "main_visual_description": main_visual_description,
        "title_text": title_text,
        "body_text": body_text,
        "brand_text": brand_text,
        "has_icon_row": has_icon_row,
        "expand_prompt": expand_prompt,
        "video_prompt": video_prompt,
    }
