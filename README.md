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

검색 서버에 연결되지 않으면 **데모 모드**로 동작합니다.
화면에 "데모 모드: 저장된 검색 결과를 보여주는 중"이 표시되고, 미리 검색해 둔 결과(`demo/`)를 보여줍니다.
화면의 **서버 설정**에 터널 주소와 비밀번호를 넣으면 실제 검색을 할 수 있습니다.

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

| 파일 | 역할 |
|---|---|
| `index.html` | 화면 (검색창, 로딩, 비교표, 막대그래프, 추천 카드, 데모 모드) |
| `app.py` | Flask 백엔드. `claude -p`를 웹 검색 도구만 허용해서 실행하고 결과를 캐시 |
| `make_demo.py` | `cache/`의 검색 결과를 `demo/`로 복사 (GitHub Pages 데모용) |
| `demo/` | 공개용으로 저장해 둔 검색 결과 |
| `requirements.txt` | 파이썬 패키지 목록 |
| `.env.example` | 환경변수 예시 (`.env`로 복사해서 사용, `.env`는 올리지 않음) |

### 안전장치
- **접속 비밀번호**: `.env`의 `APP_PASSWORD`와 요청 헤더 `X-App-Password`가 같아야 검색할 수 있습니다. 15분 안에 5번 틀리면 15분 동안 잠깁니다.
- **하루 검색 제한**: 새 검색(Claude 실행)은 하루 20회(`DAILY_LIMIT`)까지. 이미 검색한 단어(캐시)는 제한 없음.
- **CORS**: `https://heejintv.github.io`에서 온 요청만 허용합니다.
- Claude가 쓸 수 있는 도구를 **웹 검색(WebSearch)과 웹페이지 읽기(WebFetch)로만 제한**해서 파일 수정이나 명령 실행이 불가능합니다.
- JSON 스키마로 출력 형식을 강제하고, 확인 못 한 값은 `null`로 두도록 지시합니다.
- 화면에 표시할 때 모든 값을 HTML 이스케이프하고, `http(s)` 링크만 표시합니다.

## 실행 방법

필요한 것: Linux/WSL 또는 macOS, Python 3.10 이상, 로그인된 [Claude Code](https://claude.com/claude-code) CLI

```bash
git clone https://github.com/<사용자명>/<저장소명>.git
cd <저장소명>

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # .env를 열어 APP_PASSWORD를 긴 비밀번호로 바꾸기
python app.py
```

브라우저에서 http://localhost:5000 을 엽니다.
검색 한 번에 보통 1~3분이 걸리고, Claude 계정 사용량이 차감됩니다.

### 휴대폰에서 쓰기 (Cloudflare 빠른 터널)

```bash
# 터미널 1: 서버
python app.py

# 터미널 2: 터널
cloudflared tunnel --url http://localhost:5000
```

터널 로그에 나오는 `https://xxxx.trycloudflare.com` 주소를 복사해서,
휴대폰으로 https://heejintv.github.io/shopping-agent/ 를 열고 **서버 설정**에 주소와 비밀번호를 넣습니다.
터널 주소는 켤 때마다 바뀌므로 다시 켜면 설정도 다시 입력합니다.

### 데모 데이터 갱신

```bash
python make_demo.py   # cache/ → demo/ 복사 (검색 비용 등 내부 정보는 제외)
```

## 주의

검색 결과는 AI가 웹에서 수집한 정보이며 가격은 수시로 바뀝니다. 구매 전에 반드시 링크에서 다시 확인하세요.
