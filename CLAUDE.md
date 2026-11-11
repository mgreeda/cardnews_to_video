# 카드뉴스 영상 변환기 (card-to-video)

카드뉴스 이미지(비율 무관)를 업로드하면 16:9 영상으로 자동 변환하는 웹앱.
이 파일은 다음 작업자(사람 또는 AI)가 맥락을 이어받을 수 있도록 정리한 프로젝트 컨텍스트다.

## 무엇을 하는 앱인가

카드뉴스 이미지 업로드 → Claude가 이미지 분석 → xAI Grok으로 16:9 재배치 →
Grok으로 미세 모션 영상 생성 → ffmpeg로 병합 → 완성된 16:9 영상 다운로드.
팀 공유 로그인(아이디/비밀번호)으로 보호되며, 배포해두면 누구나 링크 하나로 사용 가능.

## 파이프라인 구조

업로드 → `pipeline/analyzer.py` (Claude `claude-sonnet-4-6` 이미지 분석)
       → `pipeline/expand.py` + `pipeline/grok.py` (Grok `grok-imagine-image-quality`로 16:9 재배치)
       → `pipeline/video_gen.py` + `pipeline/grok.py` (Grok `grok-imagine-video`로 모션 영상)
       → `pipeline/merger.py` (ffmpeg로 카드별 영상 병합)
       → `app.py` (Gradio UI + 전체 오케스트레이션)

## Grok API 스키마 (공식 문서로 확인 완료, `pipeline/grok.py`)

- base URL `https://api.x.ai/v1`, 인증 `Authorization: Bearer {XAI_API_KEY}`, `Content-Type: application/json`
  (OpenAI SDK처럼 multipart로 보내면 안 됨)
- `/images/edits`: `image` 필드는 평문 base64 문자열이 아니라
  `{"url": "data:image/jpeg;base64,...", "type": "image_url"}` 객체여야 함
  (평문 문자열로 보내면 `422 invalid type: string` 에러).
  `aspect_ratio`를 명시하지 않으면 **입력 이미지 비율을 그대로 유지**하므로 16:9 확장이 안 됨 —
  반드시 `aspect_ratio: "16:9"`를 넣어야 캔버스가 확장됨.
- `/videos/generations`: `image` 필드는 `{"url": "..."}` 형태 (image_edits와 달리 `type` 키 없음).
  `duration` 1~15초, `aspect_ratio` 16:9 등, `resolution` "720p"/"480p".
- 응답의 `request_id`로 `/videos/{request_id}` 폴링 → 완료 시
  `{"status": "done", "video": {"url": ...}}` (`videos` 리스트 아님).

## 16:9 재배치 접근 방식

단순 좌우 패딩이 아니라 요소별 재배치가 목표:
로고 좌상단 고정 / 메인비주얼 우측 확대 / 제목·본문 좌측 중단 이동 /
아이콘 줄 좌하단 유지 / 브랜드명 우하단 유지.

현재는 PIL 레이어 분리 대신 "Grok에게 상세한 재배치 프롬프트로 지시"하는 방식.
`analyzer.py`가 분석 단계에서 `main_visual_description`/`title_text`/`body_text`/`brand_text`를
추출하고, `EXPAND_PROMPT_TEMPLATE`이 위 재배치 규칙을 명시적으로 지시한다.
테스트 결과 재배치 + 16:9 확장 동작 확인됨.

## 영상 생성 주의사항

- **모션은 최소한으로**: 움직임이 과하면 안 됨. `VIDEO_PROMPT_BY_TYPE`이 타입별로
  "거의 정지된 이미지에 아주 미세한 생동감만" 수준의 프롬프트를 쓴다.
- **새 요소 추가 금지**: 원본에 없던 오브젝트/도형/사람/아이콘/파티클을 절대 추가하면 안 됨.
  `COMMON_SUFFIX`에 원본 구성 보존을 명시.
- **오디오 금지**: `COMMON_SUFFIX`의 프롬프트 문구로 무음 영상 생성을 지시 (ffmpeg 후처리는 안 함).
- 카드당 기본 영상 길이: 5초 (`app.py`의 슬라이더 기본값).

## 환경변수 (배포 시 4개 필요)

- `XAI_API_KEY` — xAI Grok API 키
- `ANTHROPIC_API_KEY` — Anthropic Claude API 키
- `APP_USERNAME` — 팀 공유 로그인 아이디
- `APP_PASSWORD` — 팀 공유 로그인 비밀번호

로컬 테스트는 `.env.example`을 `.env`로 복사해서 값을 채우면 된다.

## 배포 (Render)

- `Dockerfile`이 ffmpeg 설치 + 앱 실행까지 처리 (별도 packages.txt 불필요).
- Render: New → Web Service → 이 저장소 연결 → Root Directory `card-to-video`(저장소 루트가
  이 폴더면 비워둠) → Environment: Docker → 환경변수 4개 등록 → 배포.
- 이 저장소에 푸시할 때마다 Render가 자동 재배포.
- `app.py`는 `PORT` 환경변수와 `0.0.0.0` 바인딩을 이미 처리하므로 Render에서 바로 동작.

## 남은 검증/개선 여지

- Grok이 재배치 프롬프트를 못 따라줄 경우 PIL 레이어 분리 방식(요소별 크롭 후 재배치)으로
  전환하는 대안이 있음.

## 동시 사용과 작업 기록

- 변환은 최대 `MAX_CONCURRENT_JOBS`(3)개 동시 실행, 초과분은 Gradio 큐에서 대기.
- 작업마다 `DATA_DIR/jobs/<작업ID>/`에 원본·중간 결과·`final.mp4`·`job.json`을 저장 (`pipeline/history.py`).
- Render 무료는 잠들거나 재시작할 때 파일이 지워지므로, 끝난 작업은 Hugging Face 비공개 Storage Bucket에 보관
  (`pipeline/storage.py`, S3 호환 API). 환경변수 `HF_NAMESPACE`·`HF_S3_ACCESS_KEY_ID`·`HF_S3_SECRET_ACCESS_KEY`·`HF_BUCKET`.
  없으면 로컬 저장만 동작.
- 보관 상한: 100건 또는 90GB 중 먼저 닿는 쪽에서 오래된 기록부터 영구 삭제 (`MAX_JOBS`, `MAX_BYTES`).
- `job.json`은 마지막에 올려 완성된 기록만 목록에 나온다. 파일은 서버가 받아서 앱 로그인 뒤에서만 제공
  (S3 임시 링크는 Hugging Face 문서에 지원이 명시되어 있지 않아 쓰지 않음).
- 검증: `python test_app.py` (가짜 공급자·가짜 저장소, 유료 호출 없음).
