# 가격비교 에이전트

상품명을 입력하면 AI 에이전트(Claude)가 웹을 직접 검색해서 **브랜드·모델별 가격, 쇼핑몰, 별점, 장단점, 링크**를 비교표로 정리해 주는 웹서비스입니다.
API 키 없이 로컬에 설치된 Claude Code CLI(`claude -p`)를 검색 엔진처럼 사용합니다.

- 브랜드별 비교표
- 가격 막대그래프 (최저가 강조)
- 가성비 · 품질 · 최저가 추천 카드
- 확인하지 못한 값은 지어내지 않고 "확인 안 됨"으로 표시
- 같은 검색어는 캐시해서 다시 검색하지 않음

## 스크린샷

<!-- 스크린샷을 screenshots/ 폴더에 저장한 뒤 아래 줄의 파일명을 맞춰 주세요 -->
![검색 결과 화면](screenshots/main.png)

## 데모

GitHub Pages: https://heejintv.github.io/shopping-agent/

페이지가 열리면 `config.js`에 적힌 검색 서버로 자동 연결을 시도합니다.
- 연결되면 바로 검색 가능: "🟢 실시간 검색 가능 (오늘 남은 횟수 N회)"
- 연결되지 않으면 자동으로 **데모 모드**: 미리 검색해 둔 결과(`demo/`)를 보여줍니다.

## 구조

```
 브라우저 (index.html)
     │  GET /search?q=무선 마우스
     ▼
 Flask 서버 (app.py, localhost:5000)
     │  ① cache/에 같은 검색어가 있으면 바로 반환
     │  ② 없으면 subprocess로 실행 ↓
     ▼
 claude -p "…" --tools WebSearch,WebFetch --json-schema …
     │  웹 검색 → 쇼핑몰 페이지 읽기 → 정해진 JSON 형식으로 정리
     ▼
 Flask가 결과를 cache/에 저장하고 브라우저에 JSON 응답
```

| API | 설명 |
|---|---|
| `GET /search?q=&c=` | 검색. `c`는 원하는 조건(선택). 검색어+조건 조합으로 캐시하고, 캐시에 없으면 Claude 실행(하루 횟수 차감) |
| `GET /history` | `cache/`에 저장된 검색어 목록 (최근 순) |
| `GET /history/<파일명>` | 저장된 결과 하나 (Claude 실행·횟수 차감 없음) |
| `GET /health` | 서버 상태, 무제한 여부(`unlimited`)와 남은 검색 횟수(`remaining`) |

Claude에게는 **인기 상위 5개 모델만, 웹 검색은 최대 3번**까지 하도록 지시해서 검색 비용과 시간을 줄였습니다.

원하는 조건(예: 저소음, 화이트)을 입력하면 상품마다 **조건 적합도(높음/보통/낮음)와 이유**를 함께 받습니다. 맞는 상품이 없으면 지어내지 않고 "조건에 맞는 상품을 못 찾음"으로 표시합니다. 조건 없이 검색한 결과에서는 적합도 칸을 숨깁니다.

| 파일 | 역할 |
|---|---|
| `index.html` | 화면 (검색창, 원하는 조건, 최근 검색, 추천 카드, 가격 정렬 비교표, 데모 모드) |
| `config.js` | 검색 서버 주소 (ngrok 고정 도메인) |
| `app.py` | Flask 백엔드. `claude -p`를 웹 검색 도구만 허용해서 실행하고 결과를 캐시 (`SEARCH_ENGINE`으로 엔진 선택) |
| `gemini.py` | Gemini 검색 엔진. Google 검색 연동(grounding)으로 검색하고 결과를 같은 JSON 형식으로 정리 |
| `links.py` | 상품 링크 검증. 실제 접속해서 페이지 제목에 모델명이 있는 상세 페이지인지 확인 (`python links.py`로 기존 캐시 재검증, 재검색은 안 함) |
| `make_demo.py` | `cache/`의 검색 결과를 `demo/`로 복사 (GitHub Pages 데모용) |
| `demo/` | 공개용으로 저장해 둔 검색 결과 |
| `requirements.txt` | 파이썬 패키지 목록 |
| `.env.example` | 환경변수 예시 (`.env`로 복사해서 사용, `.env`는 올리지 않음) |

### 안전장치
- **하루 검색 제한**: ngrok을 거쳐 들어온 새 검색(Claude 실행)만 하루 10회(`DAILY_LIMIT`)까지. 내 PC에서 직접 온 요청은 무제한이고, 이미 검색한 단어(캐시)는 누구나 제한 없음.
  - ngrok 요청도 서버에는 127.0.0.1로 보이므로 `X-Forwarded-For` 같은 전달 헤더가 있으면 외부로 판단합니다.
  - 페이지는 `http://localhost:5000` → `config.js`의 ngrok 주소 → 데모 모드 순서로 연결을 시도합니다.
- **CORS**: `https://heejintv.github.io`에서 온 요청만 허용합니다.
- Claude가 쓸 수 있는 도구를 **웹 검색(WebSearch)과 웹페이지 읽기(WebFetch)로만 제한**해서 파일 수정이나 명령 실행이 불가능합니다.
- JSON 스키마로 출력 형식을 강제하고, 확인 못 한 값은 `null`로 두도록 지시합니다.
- 화면에 표시할 때 모든 값을 HTML 이스케이프하고, `http(s)` 링크만 표시합니다.
- **링크 검증**: Claude가 준 상품 링크에 서버가 직접 접속해 정상 응답이고 페이지 제목에 모델명이 있는지 확인합니다. 실패한 상품만 모아 `claude -p`로 상세 페이지를 **한 번만** 다시 찾게 하고, 그래도 실패하면 버튼 대신 "링크 확인 안 됨"을 표시합니다. 검색 결과 페이지 같은 대체 링크는 쓰지 않으며, 내 PC·내부망 주소(localhost, 192.168.x.x 등)에는 접속하지 않습니다.

### 검색 엔진 선택 (Claude / Gemini)

환경변수(`.env`)로 고릅니다. 화면·JSON 형식·링크 검증 규칙은 엔진과 관계없이 같습니다.

| 변수 | 설명 |
|---|---|
| `SEARCH_ENGINE` | `claude`(기본, `claude -p`) 또는 `gemini`(Gemini API + Google 검색 연동) |
| `GEMINI_API_KEY` | Gemini API 키 (`gemini`일 때 필요) |
| `GEMINI_MODEL` | Gemini 모델 이름 (기본 `gemini-2.5-flash`) |

- Gemini 호출이 실패하면(한도 초과, 키 오류 등) 이유를 화면에 보여주고 캐시에는 저장하지 않습니다.
- 캐시 파일의 `engine`(그리고 Gemini는 `engine_model`)에 어떤 엔진으로 검색했는지 기록합니다.

## 실행 방법

필요한 것: Linux/WSL 또는 macOS, Python 3.10 이상, 로그인된 [Claude Code](https://claude.com/claude-code) CLI

```bash
git clone https://github.com/<사용자명>/<저장소명>.git
cd <저장소명>

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 필요하면 DAILY_LIMIT 조정
python app.py
```

브라우저에서 http://localhost:5000 을 엽니다.
검색 한 번에 보통 1~3분이 걸리고, Claude 계정 사용량이 차감됩니다.

### 외부 공개 (ngrok 고정 도메인)

```bash
# 1. 설치 (Ubuntu/WSL)
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt update && sudo apt install ngrok

# 2. 토큰 등록 (https://dashboard.ngrok.com/get-started/your-authtoken)
ngrok config add-authtoken <내 토큰>

# 3. 고정 도메인 확인 (https://dashboard.ngrok.com/domains, 무료 계정에 1개 제공)

# 4. 서버와 터널 실행
python app.py                                   # 터미널 1
ngrok http --url=<내도메인>.ngrok-free.app 5000  # 터미널 2
```

`config.js`의 `SERVER_URL`을 내 고정 도메인으로 바꿔 push하면, GitHub Pages가 그 주소로 자동 연결합니다.
도메인이 고정이라 터널을 다시 켜도 설정을 바꿀 필요가 없습니다.

### 데모 데이터 갱신

```bash
python make_demo.py   # cache/ → demo/ 복사 (검색 비용 등 내부 정보는 제외)
```

## 주의

검색 결과는 AI가 웹에서 수집한 정보이며 가격은 수시로 바뀝니다. 구매 전에 반드시 링크에서 다시 확인하세요.
