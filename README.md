# 카드뉴스 영상 변환기

카드뉴스 이미지(비율 무관)를 업로드하면 Claude가 분석해 16:9 확장/영상 프롬프트를 자동으로 만들고,
Grok(xAI)으로 16:9 확장 → 모션 영상 생성 → ffmpeg 병합까지 한 번에 처리한다.
Render.com에 배포해두면 준현님 컴퓨터가 꺼져 있어도 팀원 누구나 링크 하나로 접속해서 쓸 수 있다.

## 최초 세팅 (담당자만)

1. [render.com](https://render.com) → New → **Web Service** → 이 깃허브 레포 연결
2. Root Directory: 이 레포 루트가 곧 프로젝트 폴더면 비워둠 (다른 레포 하위에 있으면 `card-to-video`)
3. Environment: **Docker** (레포에 있는 `Dockerfile`을 자동으로 사용)
4. Environment Variables에 아래 4개 등록:
   - `XAI_API_KEY`
   - `ANTHROPIC_API_KEY`
   - `APP_USERNAME`
   - `APP_PASSWORD`
5. 저장하면 Render가 자동으로 빌드/배포하고 URL이 생성됨
6. 이후로는 이 브랜치에 푸시할 때마다 Render가 자동으로 재배포함 (수동 업로드 불필요)
7. 생성된 URL을 팀원들에게 공유

## 매번 사용할 때 (팀원 누구나)

1. 공유받은 Render 링크 접속
2. 아이디/비밀번호 입력 (팀 공유 계정)
3. 카드뉴스 이미지 업로드 (비율 상관없음, 다중 업로드 가능)
4. 필요하면 ▲▼ 버튼으로 순서 조정, 카드당 영상 길이 슬라이더 조절
5. "영상 변환 시작" 클릭 → 카드별 진행 상황 확인
6. 완료되면 결과 영상 재생/다운로드 (중간에 막히면 섹션 5의 카드별 16:9 이미지/영상으로 어디서 실패했는지 확인 가능)

## 로컬에서 테스트하려면

```bash
cp .env.example .env   # 키 채워넣기
pip install -r requirements.txt
python app.py
```

로컬 테스트 시 영상 병합 단계(ffmpeg)를 쓰려면 `ffmpeg`가 시스템에 설치되어 있어야 한다.
