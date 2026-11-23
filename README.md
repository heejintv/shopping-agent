# Pickit (픽잇) — AI 쇼핑 비교

[![test](https://github.com/heejintv/shopping-agent/actions/workflows/test.yml/badge.svg)](https://github.com/heejintv/shopping-agent/actions/workflows/test.yml)

> **고민은 짧게, 선택은 정확하게.** AI가 웹을 검색해 인기 모델 5개를 비교하고, 딱 맞는 하나를 골라드려요.

상품명을 입력하면 AI 에이전트가 웹을 직접 검색해서 **브랜드·모델별 가격, 쇼핑몰, 별점, 장단점, 링크**를 비교표로 정리해 주는 웹서비스입니다.
결과를 본 뒤에는 💬 상담 챗봇에게 "자취생한테 뭐가 좋아?"처럼 물어보면 **그 검색 결과 안에서** 하나를 골라 줍니다.

- **검색은 Claude**: 로컬에 설치된 Claude Code CLI(`claude -p`)가 웹 검색 → 쇼핑몰 페이지 확인 → JSON 정리
- **상담은 Gemini**: Gemini API가 검색 결과만 보고 답변 (혼잡·한도 초과면 Claude로 자동 재시도)
- **회원은 Supabase**: 이메일 회원가입·로그인, 계정별 하루 사용 횟수·찜 목록·사용 기록 저장, 관리자 대시보드

## 주요 기능

- Pickit 첫 화면: 그라데이션 히어로, 타이핑되듯 바뀌는 검색 안내 문구, 기능 소개 카드, 로고 파비콘·링크 미리보기(og 태그와 1200×630 미리보기 이미지)
- 인기 모델 5개 비교표 (가격 낮은순·높은순 정렬, 가격 막대그래프)
- 비교표 필터: 가격 범위 슬라이더, 별점 4.5 이상, 브랜드, 해외구매 제외 (받아온 결과 안에서만, 새 검색 없음)
- 🌏 해외구매 배지: 상품명·쇼핑몰에 해외구매·해외직구·병행수입·구매대행이 있으면 표시, A/S·배송 기간·관부가세 안내 (챗봇도 확인할 점에 반영)
- 가성비 · 품질 · 최저가 추천 카드 (같은 상품이면 카드 하나에 배지 여러 개, 정보가 부족하면 회색 안내 카드)
- 원하는 조건(저소음, 화이트, 5만원 이하 등) 입력 → 상품마다 조건 적합도(높음/보통/낮음)와 이유
- 상품 링크는 서버가 직접 접속해서 진짜 상품 페이지인지 확인한 것만 표시
- 별점·리뷰 수는 링크 확인 때 연 상품 페이지에서 직접 읽음 (JSON-LD aggregateRating → 다나와 페이지 데이터, 5점 만점으로 환산, AI 값보다 우선, `rating_source`에 page/ai 기록)
- 확인하지 못한 값은 지어내지 않고 "확인 안 됨"으로 표시
- 같은 검색어는 캐시해서 다시 검색하지 않음, 최근 검색 바로 보기
- 새 검색은 진행 단계(캐시 확인 → 웹 검색 → 링크 확인 3/5 → 결과 정리)와 경과 시간을 실시간으로 보여주고, "검색 취소" 가능 (취소한 검색은 하루 횟수에 안 셈)
- 상담 챗봇: 추천 1개 / 이유 두 줄 / 사기 전에 확인할 점 하나
- **챗봇 ↔ 결과 화면 연결**: 챗봇이 추천한 상품 카드로 스크롤하고 시머·글로우 효과와 "✨ AI 추천" 배지로 강조
- **찜하기와 1:1 비교**: ♡로 찜(회원은 내 계정에, 비회원은 이 기기에 저장, 검색어별로 모아 보기), 찜한 상품 2개를 나란히 비교하고 "AI에게 물어보기"
- **비교 답변 서버 검증**: 서버가 두 상품의 숫자를 직접 비교하고, AI가 이유에 틀린 비교를 쓰면 고쳐서 보여줌
- **모니터링 대시보드**: 📊 서버 상태 화면에서 요청 수, 응답 시간, 엔진별 성공·실패, 정상/주의/장애 상태를 10초마다 확인
- **결과 공유**: "🔗 공유"로 링크(`?q=검색어&c=조건`)나 요약 텍스트 복사, 휴대폰은 공유 시트. 링크로 들어오면 저장된 결과만 바로 보여주고 새 검색은 하지 않음
- 서버가 꺼져 있으면 저장된 결과를 보여주는 데모 모드
- **회원가입·로그인 (Supabase Auth)**: 헤더의 "로그인"에서 이메일+비밀번호로 가입·로그인. 로그인하면 이메일 앞부분, "오늘 검색 3/5", 로그아웃 표시
- **회원 전용 새 검색·상담**: 비회원은 최근 검색·공유 링크·데모·필터·정렬만. 새 검색을 누르면 "새 검색은 회원 전용이에요", 챗봇은 자물쇠 안내 카드
- **계정별 하루 제한 + 서버 전체 상한**: 회원 한 명당 새 검색 5회·상담 30회, 모든 회원 합쳐 새 검색 20회. admin은 무제한
- **찜 목록 동기화**: 로그인하면 내 계정 찜(Supabase `favorites`)만 보이고 다른 기기에서도 같은 목록. 로그아웃하면 이 기기의 기록을 모두 지움
- **🛠 관리자 대시보드**: admin 계정에만 보이는 버튼. 회원 수, 오늘 사용량, Gemini 토큰·한도, 사용자별 순위, 최근 기록, 7일 그래프
- **관리자 "Claude 연결 확인"**: 버튼 하나로 `/admin/claude-check` 호출 → 성공/실패, 에러 종류(로그인·토큰 / 사용량 한도 / 시간 초과 / 기타), 확인 시각. 확인 중에는 버튼이 "확인 중…"으로 잠김
- **⚡ 문장으로 실행**: 챗봇에 "제일 싼 거 찜해줘"처럼 쓰면 AI가 등록된 도구를 한 단계씩 골라 실제로 실행 (찜·찜 취소·찜 목록·검색·찜한 상품 비교·로그아웃)
  - **자동 ⚡**: "찜/저장/빼줘/검색해/비교/vs/로그아웃" 같은 행동 키워드면 Gemini 호출 없이 바로 ⚡로, 질문(물음표·"어때")은 상담으로 (`intent.js`)
  - **되묻기·여러 개 선택**: 후보가 여럿인데 기준이 없으면 버튼으로 되물음. 비교처럼 2개를 골라야 하면 목록형 카드에서 여러 개 선택 (개수가 맞을 때만 [비교하기])
  - **찜 전 승인 카드**: 상품명·가격을 보여 주고 [저장/취소], 로그아웃도 "로그아웃할까요?" [예/아니요] 확인 후에만
  - **중복 찜 방지**: 이미 찜한 상품(상품 ID 기준)이면 "이미 찜한 상품이에요"라고만 답하고 아무것도 실행하지 않음 (찜 버튼이 토글이라 실수로 빠지지 않게)
- **링크 없을 때 "쇼핑몰에서 찾기"**: 상품 링크가 절반 미만이면 같은 검색 안에서 한 번 더 찾고(횟수 차감 1회 그대로), 끝까지 없는 상품은 회색 "쇼핑몰에서 찾기"(네이버쇼핑 검색 + 상품명) 버튼으로 진짜 상품 링크와 구분

## 스크린샷

<!--
  스크린샷 넣는 법
  1. 화면을 캡처해서 screenshots/ 폴더에 아래 파일명으로 저장
  2. git add screenshots/ 후 커밋·push 하면 이 자리에 이미지가 보입니다
-->

| 검색 결과 | 상담 챗봇 | 휴대폰 화면 |
|---|---|---|
| ![검색 결과 화면 — 추천 카드와 모델별 비교표](screenshots/main.png) | ![상담 챗봇 — 오른쪽 아래 💬 채팅창](screenshots/chat.png) | ![휴대폰 화면 — 채팅창이 아래쪽 전체로 열림](screenshots/mobile.png) |
| `screenshots/main.png` | `screenshots/chat.png` | `screenshots/mobile.png` |

| 찜 목록 | 1:1 비교 + AI 답변 |
|---|---|
| ![찜 목록 — 검색어별로 묶인 찜한 상품](screenshots/wishlist.png) | ![1:1 비교 — 두 상품을 나란히, AI가 고른 쪽 강조](screenshots/compare.png) |
| `screenshots/wishlist.png` | `screenshots/compare.png` |

| 모니터링 대시보드 |
|---|
| ![서버 상태 — 숫자 카드, 응답 시간 그래프, 엔진별 성공·실패](screenshots/status.png) |
| `screenshots/status.png` |

#### 회원 기능

| 로그인 · 회원가입 창 | 로그인 후 헤더 | 새 검색 회원 전용 안내 |
|---|---|---|
| ![로그인 창 — 이메일·비밀번호, 회원가입 전환, 질문 기록 안내](screenshots/login.png) | ![헤더 — 이메일 앞부분, 오늘 검색 3/5, 로그아웃, 관리자 버튼](screenshots/header-member.png) | ![비회원이 검색을 누르면 — 자물쇠와 로그인/회원가입 버튼](screenshots/search-gate.png) |
| `screenshots/login.png` | `screenshots/header-member.png` | `screenshots/search-gate.png` |

| 챗봇 로그인 전 화면 | 비회원 찜 가져오기 확인창 |
|---|---|
| ![챗봇 — 자물쇠 아이콘, 로그인하고 상담받기, 회원 혜택 3줄](screenshots/chat-guest.png) | ![이 기기에 저장된 찜 N개를 내 계정에 추가할까요?](screenshots/wish-import.png) |
| `screenshots/chat-guest.png` | `screenshots/wish-import.png` |

#### 관리자 대시보드

| 관리자 대시보드 (PC) | 관리자 대시보드 (휴대폰) | 관리자가 아닐 때 |
|---|---|---|
| ![관리자 — 숫자 카드, Gemini·서버 전체 한도 막대, 7일 그래프, 순위·최근 기록](screenshots/admin.png) | ![관리자 — 휴대폰에서 카드 2열, 표는 옆으로 밀어 보기](screenshots/admin-mobile.png) | ![관리자만 볼 수 있어요 안내 카드](screenshots/admin-denied.png) |
| `screenshots/admin.png` | `screenshots/admin-mobile.png` | `screenshots/admin-denied.png` |

## 데모

GitHub Pages: https://heejintv.github.io/shopping-agent/

페이지가 열리면 내 PC 서버(`localhost:5000`) → `config.js`의 ngrok 주소 순서로 연결을 시도합니다.
- 연결되면 로그인한 회원은 바로 새 검색 가능 (헤더에 "오늘 검색 N/5"). 비회원은 최근 검색·공유 링크만
- 연결되지 않으면 자동으로 **데모 모드**: 미리 검색해 둔 결과(`demo/`)만 볼 수 있고 챗봇은 쓸 수 없습니다.

## 전체 구조

```
 ┌──────────────────────────────┐  로그인·찜(내 행만, RLS)  ┌──────────────────────────┐
 │ 브라우저 (index.html)          │ ───────────────────────▶ │ Supabase                  │
 │ admin.html (관리자)            │      supabase-js          │  Auth (회원가입·로그인)    │
 └──────────────┬───────────────┘      (anon 키)            │  profiles · favorites     │
                │  Authorization: Bearer <로그인 토큰>        │  usage_logs               │
                │  ① localhost:5000 직접 (내 PC, 무제한)       └────────────▲─────────────┘
                │  ② Render · ngrok (계정별 하루 제한)                      │
                ▼                                                          │ 토큰 확인 · 등급 ·
 ┌──────────────────────────────┐                                         │ 사용 기록 · 하루 횟수
 │ Flask 서버 (app.py)           │ ───────────────────────────────────────┘ (SERVICE 키, 서버에서만)
 │  출처 확인 · 로그인 확인       │
 │  하루 제한 · 캐시              │
 └───────┬──────────────┬───────┘
         │              │
  GET /search      POST /chat
         │              │
         ▼              ▼
 ┌───────────────┐  ┌──────────────────────────┐
 │ 검색: Claude   │  │ 상담: Gemini API          │
 │ claude -p      │  │ (도구 없음, 검색 결과만)   │
 │ 웹 검색·읽기만  │  └────────────┬─────────────┘
 └───────┬───────┘               │ 429 · 503 · 504
         │                       ▼
         │              ┌──────────────────────────┐
         │              │ Claude로 한 번 자동 재시도 │
         │              │ claude -p (도구 전부 끔)   │
         │              └────────────┬─────────────┘
         ▼                           │ 그래도 실패
 ┌───────────────────────┐           ▼
 │ 링크 검증 (links.py)    │   "잠시 후 다시 시도해주세요"
 │ 실패한 상품만 1번 재검색 │
 └───────┬───────────────┘
         ▼
 cache/에 저장 (검색 엔진 기록) → 브라우저에 JSON 응답
```

### 검색 (`GET /search`)
1. `cache/`에 같은 검색어+조건이 있으면 바로 돌려줌 (횟수 차감 없음)
2. 없으면 `claude -p`를 **웹 검색(WebSearch)·웹페이지 읽기(WebFetch) 도구만** 허용해서 실행. JSON 스키마로 출력 모양을 강제
3. 상품 링크마다 서버가 직접 접속해서 페이지 제목에 모델명이 있는지 확인. 실패한 상품(링크 있는 상품이 절반 미만이면 링크 없는 상품도)을 모아 **한 번만** 다시 찾고, 그래도 없으면 회색 "쇼핑몰에서 찾기" 버튼
4. 결과를 `cache/`에 저장 (`engine`에 어떤 엔진으로 검색했는지 기록)

Claude에게는 **인기 상위 5개 모델만, 웹 검색은 최대 3번**까지 하도록 지시해서 비용과 시간을 줄였습니다.
`SEARCH_ENGINE=gemini`로 바꾸면 Gemini API + Google 검색 연동(grounding)으로도 검색할 수 있습니다. 화면·JSON 형식·링크 검증 규칙은 같습니다. (Google 검색 연동은 Gemini 유료 요금제가 필요할 수 있습니다.)

### 상담 챗봇 (`POST /chat`)
- 브라우저가 **질문 + 지금 화면의 검색 결과 + 최근 대화 6개**를 보냄. 서버는 필요한 값만 꺼내 크기를 제한한 뒤 전달
- 웹 검색을 하지 않고 받은 검색 결과 안에서만 답함. 없는 상품·가격·정보는 지어내지 않고 "모른다"고 답함
- 쇼핑과 상관없는 질문은 정중히 돌려보냄, 항상 한국어
- `CHAT_ENGINE=gemini`(기본)이면 Gemini, `claude`면 `claude -p`(도구 전부 끔, 답변만)
- Gemini가 **429(한도 초과)·503(혼잡)·504(시간 초과)** 이면 Claude로 한 번 자동 재시도
- 새 상품을 검색하면 대화 초기화

### ⚡ 문장으로 실행 (`POST /chat/action/step`, `react_graph.py`)

**Gemini Function Calling**으로 도구를 고르고, **LangGraph `StateGraph`**가 반복(ReAct)을 서버에서 관리합니다. 명령 하나 = 상담 1회 차감, Gemini 호출은 최대 5번.

```
START → agent ──(functionCall)──→ validate ──→ server_tool ──→ agent          서버에서 바로 실행
          │                          │            └(add_favorite)→ approve → wait_client → agent
          │                          ├──→ approve (logout: [예/아니요]) → END
          │                          ├──→ wait_client → agent                  화면에서 실행 후 결과로 이어 감
          │                          └(거부: 사유를 함수 결과로)→ agent
          └(최종 답 / 호출 한도)→ END
```

- `agent`: Gemini 한 번 호출 → 함수 하나 또는 최종 답
- `validate`: 등록된 함수인지, 입력값 형식·범위·로그인, 검색 확인 여부를 서버가 검사
- `server_tool`: 검색 결과·찜 목록에서 찾기, 비교 (서버에서 숫자로 정렬·필터)
- `approve` / `wait_client`: `interrupt()`로 멈추고 화면(승인 카드·버튼·실행 결과)을 기다림 → 화면이 `Command(resume=…)`로 이어 감. 상태는 `MemorySaver`에 명령 ID(`thread_id`)별로

| 등록 도구 | 실행 위치 | 하는 일 |
|---|---|---|
| `find_in_results` | 서버 | 지금 검색 결과에서 후보 찾기 (`sort` 가격·평점, `max_price`·`min_price`, `brand`) |
| `get_favorites` | 서버 | 찜 목록 데이터 (최근 찜한 순) |
| `compare_favorites` | 서버 → 화면 | 찜·검색 결과에서 2개를 골라 1:1 비교 후 AI 비교 창 열기 (3개 이상이면 고르기 카드) |
| `add_favorite` | 승인 → 화면 | 찜하기 (승인 카드, 중복이면 실행 안 함) |
| `remove_favorite` | 화면 | 찜 빼기 |
| `open_favorites` | 화면 | 찜 목록 창 열기 (열어 달라고 할 때만) |
| `search` | 화면 | 새 검색 (검색창 제출과 같은 경로) |
| `ask_user` | 화면 | 되묻기 버튼, `min_select`·`max_select`로 여러 개 선택 |
| `logout` | 확인 → 화면 | 기존 로그아웃 함수 실행 |

**사용 예시**

- "제일 싼 거 찜해줘" → `find_in_results(sort=price_asc)` → 1등을 `add_favorite` → 승인 카드 [저장]
- "버즈 비교해줘" → `compare_favorites(condition="버즈")` → 2개면 바로, 여럿이면 2개 고르기 → AI 비교 창
- "평점 제일 높은 로지텍 찜해줘" → `find_in_results(sort=rating_desc, brand="로지텍")` → `add_favorite`
- "방금 찜한 거 빼줘" → `get_favorites` → 찜 번호 1을 `remove_favorite`
- "무선 청소기 검색해줘" → `search` (직접 "검색"이라고 하지 않았으면 먼저 "새로 검색할까요? (하루 N회 중 1회)" 확인)

**안전장치**

- **검색 확인 강제**: 사용자가 "검색/찾아"를 직접 말했거나 [검색] 버튼을 누른 기록이 없으면 서버(`validate`)가 `search`를 거부 → 하루 검색 횟수를 몰래 쓰지 않음
- **등록 함수·로그인 검사**: 등록되지 않은 함수, 모르는 입력값, 형식·범위가 틀린 값, 로그인이 필요한 도구는 서버가 막고 사유를 AI에 돌려줌
- **실행 결과로만 답**: 화면이 돌려준 실제 결과로 답함 (예: 비교 창이 실제로 열렸을 때(`opened: true`)만 "열었어요", 못 열면 이유를 그대로). 함수 결과 안의 지시문은 따르지 않음
- **되돌리기 어려운 기능은 도구로 안 만듦**: 회원 탈퇴·찜 전체 삭제·결제 같은 기능은 등록하지 않음. 찜과 로그아웃은 실행 전에 반드시 확인

### 챗봇 ↔ 결과 화면 연결
- **상품 매칭은 id로**: 서버가 상품마다 id(검색 결과 안의 순서)를 붙여 챗봇에 보내고, 챗봇은 답 끝에 `추천ID: 3`처럼 적습니다. 서버가 그 줄을 떼어 내고, 실제로 보낸 id일 때만 `pick_id`로 돌려줍니다. 상품명 글자 비교는 하지 않으며, 검색 결과에 없는 상품이면 강조하지 않습니다.
- **링크와 자동 강조**: 답변의 "추천:" 줄 상품명이 링크가 됩니다. 답변이 오면 한 번, 링크를 누를 때마다 그 상품 카드(카드가 없으면 표의 행)로 부드럽게 스크롤합니다.
- **강조 효과 (메인 색 하나로)**: 비스듬한 흰 빛 줄기(시머)가 한 번 지나가고, 가까운 진한 빛 + 멀리 퍼지는 연한 빛의 글로우가 숨 쉬듯 두 번 퍼지며, 1.02배로 살짝 튀어나옵니다. 효과가 끝나면 1px 메인 색 테두리와 은은한 글로우, "✨ AI 추천" 배지가 다음 질문이나 새 검색 전까지 남습니다.
- 휴대폰에서는 강조하는 동안 채팅창을 잠깐 내려서 카드가 보이게 합니다. 움직임 줄이기(`prefers-reduced-motion`) 설정이면 시머와 튀어나오기 없이 글로우만 보여줍니다.

### 찜하기와 1:1 비교
- **찜하기**: 추천 카드와 표의 ♡를 누르면 ♥로 바뀝니다. 상품명, 가격, 쇼핑몰, 별점, 장단점, 확인된 링크, 검색어, 찜한 날짜를 같이 저장하고, 저장에 실패해도 화면은 그대로 동작합니다.
  - 회원: Supabase `favorites` 표의 **내 행만** 읽고 씁니다 (브라우저에는 저장하지 않음). 다른 기기에서도 같은 목록이 보입니다.
  - 비회원: 이 기기의 브라우저 저장소(`pickit:guest:wishlist`)에만 저장합니다. 자세한 규칙은 [찜 목록 저장 방식](#찜-목록-저장-방식)
- **찜 목록**: 위쪽 "♥ 찜 목록 (N)"에서 다른 검색어 상품까지 검색어별로 묶어 보고, 하나씩 지우거나 전체 비울 수 있습니다.
- **1:1 비교**: 2개를 체크하면 "비교하기"가 켜집니다 (3개째는 체크 안 됨). 가격·쇼핑몰·별점·장단점·링크를 나란히 보여주고, 가격은 낮은 쪽, 별점은 높은 쪽을 메인 색으로 표시합니다.
- **AI에게 물어보기**: 두 상품만 `/chat`(`mode: "compare"`)에 보내 "이 두 개 중에 뭐가 나아?"를 묻습니다. 답변은 비교 화면 위쪽에 바로 나오고, AI가 고른 칸에 같은 강조 효과가 붙습니다. 채팅창 대화 기록에도 남고, 하루 제한은 일반 질문과 같은 횟수를 씁니다.
- 휴대폰에서는 찜 목록과 비교 화면이 아래에서 올라오는 시트로 열리고, 비교는 위아래로 배치됩니다.

### 비교 답변 서버 검증 (`compare.py`)
AI가 비교 이유에 "같은 점"이나 "상대가 더 나은 점"을 섞어 쓰는 경우가 있어서, 서버가 숫자로 직접 확인합니다.

1. **숫자 비교**: 가격·별점·리뷰 수(상품 값)와 무게·보증 기간·폴링 속도·DPI·사용 시간(장단점 문장 속 숫자)을 뽑아 두 상품을 비교합니다. 가격·무게는 낮을수록, 나머지는 높을수록 낫다고 봅니다.
2. **프롬프트에 정답 목록**: 상품별 "확실히 나은 점"과 "같은 점"을 프롬프트에 넣고, 이유 두 줄은 추천 상품의 "확실히 나은 점"에서만 고르게 합니다. 나은 점이 1개뿐이면 둘째 줄은 "가격 차이에 비해 기본 성능은 충분해요"처럼 숫자 없는 일반적인 장점으로 씁니다.
3. **답변 검사**: 답을 받은 뒤 이유 줄에 추천 상품 기준 "같은 점"이나 "상대가 나은 점"이 들어갔는지 검사하고, 들어갔으면 그 줄을 일반적인 장점 문장으로 바꿉니다.

```
예) G304(39,900원, 99g) vs G PRO X SUPERLIGHT 2(149,000원, 60g) → AI가 G304 추천
 AI 원래 답변   이유: 99g 경량 무게와 HERO 12000DPI를 탑재하여 일상적인 용도로 쓰기에 무난해요.
 서버가 고친 답 이유: 가격 차이에 비해 기본 성능은 충분해요.      ← 상대가 더 가벼우므로 교체
```
숫자로 적힌 항목만 비교하므로, "3모드 지원"처럼 숫자가 없는 기능 차이는 검사하지 못합니다.

### 모니터링 대시보드 (`GET /stats`, `status.html`)
메인 화면 위쪽 **📊 서버 상태**를 누르면 열립니다. 10초마다 `/stats`를 다시 불러오고, 서버에 연결되지 않으면 "서버 오프라인"을 표시합니다 (마지막 값은 흐리게 유지).

| 영역 | 내용 |
|---|---|
| 상태 표시 | ✅ 정상(초록) / ⚠️ 주의(노랑, 최근 1시간 에러율 20% 이상) / ⛔ 장애(빨강, 최근 3번 연속 실패) / 🔌 서버 오프라인 |
| 숫자 카드 | 가동 시간, 오늘 요청 수(검색·채팅·비교), 평균 응답 시간(캐시 적중 제외), 에러율 |
| 보조 카드 | 캐시 적중률, Gemini→Claude 재시도 횟수, 비교 답변에서 서버가 교체한 줄 수, 하루 제한에 걸린 횟수 |
| 최근 1시간 응답 시간 | 선 그래프 (실패한 요청은 빨간 테두리 점, 마우스를 올리면 시각·종류·응답 시간), "표로 보기" 제공 |
| 엔진별 성공 / 실패 | claude·gemini 누적 막대 + 실패 내역(429 / 503 / 504 / 기타) |
| 인기 검색어 TOP 5 | 오늘 검색한 검색어 (캐시 적중 포함) |

- **기록 방식** (`stats.py`): `logs/stats-YYYY-MM-DD.json`에 날짜별로 저장하고 30일이 지난 파일은 지웁니다. `logs/`는 `.gitignore`에 있어 저장소에 올라가지 않습니다.
- **개인정보 없음**: 요청마다 종류·시각·응답 시간·성공 여부·오류 코드·검색어만 남깁니다. 챗봇 질문 원문, 답변, IP는 저장하지 않습니다. (회원 사용 기록은 이와 별도로 Supabase `usage_logs`에 남고, 여기에는 질문 원문이 300자까지 들어갑니다 — [아래](#supabase-표와-rls-규칙) 참고)
- **에러로 치는 것**: 검색·챗봇이 실패해서 오류를 돌려준 경우만. 하루 제한(429 응답)은 서버 오류가 아니라 따로 셉니다.
- `/stats`도 검색 API와 같은 CORS 규칙을 따르고, 허용하지 않은 출처(Origin)는 403으로 거절합니다.

## 사용 기술

| 구분 | 기술 | 쓰는 곳 |
|---|---|---|
| 실행 환경 | **WSL2** (Windows 위 Ubuntu) | 서버·Claude Code CLI·ngrok을 Linux 환경에서 실행 |
| 백엔드 | Python 3, **Flask** | API 서버 (`app.py`), 캐시·하루 제한·출처 확인 |
| 검색 AI | **Claude Code CLI** (`claude -p`) | 웹 검색으로 상품 정보 수집, JSON 스키마 출력 |
| 상담 AI | **Gemini API** (`google-genai`) | 상담 챗봇 답변, ⚡ Function Calling, (선택) Google 검색 연동 검색 |
| 에이전트 | **LangGraph** (`langgraph>=1.2,<2`) | ⚡ ReAct 반복: `StateGraph`·`interrupt`·`MemorySaver` (`react_graph.py`) |
| 배포 | **Render** (무료 웹 서비스) + **gunicorn** | 내 PC 없이 서버 실행 (`render.yaml`, `gunicorn.conf.py`) |
| 외부 공개 | **ngrok** (무료 고정 도메인) | 내 PC의 `localhost:5000`을 인터넷에 연결 |
| 프론트엔드 | **GitHub Pages**, HTML·CSS·JavaScript | 화면 호스팅. 프레임워크 없이 한 파일(`index.html`) |
| 링크 검증 | Python 표준 라이브러리 (`urllib`) | 상품 페이지에 직접 접속해 확인 (`links.py`) |
| 회원·DB | **Supabase** (Auth + Postgres + RLS), `supabase-js` 2.45.4 | 회원가입·로그인, 찜 목록, 사용 기록, 관리자 권한 (`auth.py`, `members.py`, `supabase/`) |

## 보안 장치

### 1. API 키는 `.env`에만 (저장소에 올리지 않음)
- `GEMINI_API_KEY` 같은 비밀 값은 `.env`에만 두고, 코드는 **환경변수로만** 읽습니다.
- `.env`는 `.gitignore`에 들어 있어 커밋되지 않습니다. 저장소에는 값이 빈 `.env.example`만 올립니다.
- 검색 비용 같은 내부 정보는 `make_demo.py`로 공개용 결과를 만들 때 빠집니다.
- Supabase 키: 화면(`config.js`)에는 공개용 URL·anon(publishable) 키만 둡니다. **SERVICE 키는 서버 환경변수에만** 있고 화면·응답·로그에 나오지 않습니다. 커밋 전에 서비스 키 모양(`sb_secret_`, JWT)이 섞였는지 검사합니다.

### 2. 출처 확인
- **CORS**: GitHub Pages(`https://heejintv.github.io`)에서 온 요청만 브라우저가 응답을 읽을 수 있습니다.
- **챗봇 POST**: 허용한 출처(Origin)가 아니면 403, JSON이 아니면 415로 거절합니다. JSON POST는 브라우저가 사전 확인(preflight)을 하므로 다른 사이트는 요청 자체를 보낼 수 없습니다.
- **내 PC 판별**: ngrok을 거친 요청도 서버에는 127.0.0.1로 보여서, `X-Forwarded-For` 같은 전달 헤더가 있으면 외부 요청으로 봅니다. 내 브라우저에서 다른 사이트가 몰래 `localhost:5000`을 부르는 경우도 무제한으로 치지 않습니다.
- 서버는 `127.0.0.1`에서만 받고, 외부 공개는 ngrok 터널로만 합니다. 디버그 모드는 끕니다.

### 3. 회원 전용 기능과 하루 사용 제한 (계정 기준)

| 항목 | 비회원 | 회원 | admin · 내 PC | 설정 |
|---|---|---|---|---|
| 새 검색 (AI 실행) | 불가 (로그인 안내) | 계정당 하루 5회 | 무제한 | `DAILY_LIMIT` |
| 최근 검색·공유 링크·데모·필터·정렬 | 가능 | 가능 | 가능 | — |
| 상담 챗봇 (1:1 비교 포함) | 불가 (로그인 안내) | 계정당 하루 30회 | 무제한 | `CHAT_DAILY_LIMIT` |
| 서버 전체 새 검색 | — | 모든 회원 합쳐 하루 20회 ("오늘 검색이 모두 소진됐어요") | 예외 | `GLOBAL_SEARCH_LIMIT` |
| 찜 목록 | 이 기기에만 (`pickit:guest:*`) | Supabase `favorites`의 내 행만 | 같음 | — |

- 하루 횟수는 Supabase `usage_logs` 표에서 셉니다 (한국 시간 자정 기준, 성공한 것만). 서버를 다시 켜도 유지돼요.
- 저장된 결과(캐시)·취소·실패는 횟수에 넣지 않습니다. Render에서는 "내 PC" 구분 없이 모두 계정 기준입니다.
- admin·내 PC 사용은 `usage_logs.exempt=true`로 기록돼 서버 전체 상한에 들어가지 않아요.
- 브라우저 저장소는 비회원(`pickit:guest:*`)과 계정별(`pickit:u:<user_id>:*`)로 나뉘고, 로그아웃하면 모두 지웁니다.
  로그인할 때 비회원 찜이 있으면 "이 기기에 저장된 찜 N개를 내 계정에 추가할까요?"라고 묻고, 예일 때만 추가합니다.
- 상담 기록(`usage_logs.query`)에는 질문 원문을 300자까지 남깁니다 (로그인 창에 안내).

### 4. 그 밖의 장치
- **AI 도구 제한**: 검색용 Claude는 웹 검색·웹페이지 읽기만, 챗봇용 Claude는 도구 없이 답변만 합니다. 파일 수정이나 명령 실행이 불가능합니다.
- **입력 제한**: 검색어·조건 100자, 챗봇 질문 300자, 대화 기록 6개. 챗봇은 받은 검색 결과·대화 안의 지시문을 따르지 않도록 지시합니다.
- **링크 검증**: 내 PC·내부망 주소(localhost, 192.168.x.x 등)에는 접속하지 않습니다.
- **화면 표시**: 모든 값을 HTML 이스케이프하고, `http(s)` 링크만 표시합니다. 챗봇 답변은 글자로만 넣어 HTML이 실행되지 않습니다.
- **지어내지 않기**: JSON 스키마로 형식을 강제하고, 확인 못 한 값은 `null`로 둡니다.

## API

| API | 설명 |
|---|---|
| `GET /search?q=&c=` | 🔒 검색. `c`는 원하는 조건(선택). 검색어+조건 조합으로 캐시하고, 캐시에 없으면 AI 실행(계정·서버 전체 하루 횟수 차감) |
| `GET /search/stream?q=&c=` | 🔒 새 검색의 진행 단계를 실시간으로 보냄 (Server-Sent Events: 캐시 확인 → AI가 웹 검색 중 → 링크 확인 중 3/5 → 결과 정리 중). 연결을 끊으면 취소 |
| `POST /search/cancel/<id>` | 진행 중인 스트림 검색 취소. claude 프로세스를 멈추고, 취소된 검색은 하루 횟수에 안 셈 |
| `GET /cached?q=&c=` | 공유 링크용. 저장된 결과만 돌려주고 없으면 404 (새 검색·횟수 차감 없음) |
| `GET /history` | `cache/`에 저장된 검색어 목록 (최근 순) |
| `GET /history/<파일명>` | 저장된 결과 하나 (AI 실행·횟수 차감 없음) |
| `POST /chat/action/step` | 🔒 ⚡ 문장으로 실행. `{sentence}`로 시작(상담 1회 차감) → 멈출 때마다 `{cmd, resume}`으로 이어 감. 응답은 `interrupt`(화면 실행·승인 대기) 또는 `text`(끝) + 지난 노드 순서 |
| `GET /admin/claude-check` | 🔒👑 claude CLI 연결 확인. 성공/실패와 에러 종류(auth·limit·timeout·other)만 |
| `POST /chat` | 🔒 상담 챗봇. `{question, results, history}`를 받아 검색 결과 안에서만 답함. `mode: "compare"`면 찜한 상품 정확히 2개만 받아 둘 중 하나를 추천 (대화 기록은 무시, 하루 제한은 같은 횟수) |
| `GET /stats` | 모니터링 요약 (가동 시간, 오늘 요청 수, 응답 시간, 엔진별 성공·실패, 캐시 적중률, 인기 검색어, 사이트별 링크 확인 `danawa 0/5`, 상태) |
| `GET /health` | 서버 상태, 내 PC 직접 접속이면 `unlimited: true` (계정별 남은 횟수는 `/me`) |
| `GET /me` | 🔒 로그인한 사람의 등급(`role`)과 오늘 남은 새 검색·상담 횟수, 서버 전체 남은 새 검색 |
| `POST /admin/report` · `GET /admin/report` | 🔒👑 일일 운영 보고서 만들기(`{date}` 기본 어제, 오늘은 지금까지, 뒤에서 실행) · 상태와 초안 보기 |
| `POST /admin/report/approve` | 🔒👑 `{id}` 초안 승인 → `logs/reports/report-날짜.json` (오늘은 `report-날짜-진행중-시각.json`) 저장 (Render 무료는 재시작하면 사라짐) |
| `GET /admin/stats?period=today\|7d\|30d&q=이메일` | 🔒👑 관리자 대시보드 데이터. admin이 아니면 403 |

🔒 = `Authorization: Bearer <Supabase 로그인 토큰>` 필요 (없거나 틀리면 401, AI 호출 전에 막음). 👑 = `profiles.role = 'admin'`만.

## 파일

| 파일 | 역할 |
|---|---|
| `index.html` | 화면 (Pickit 로고·첫 화면, 검색창, 원하는 조건, 최근 검색, 추천 카드, 비교표, 상담 챗봇, 찜 목록·1:1 비교, 데모 모드) |
| `config.js` | 검색 서버 주소 (`RENDER_URL`, `SERVER_URL`=ngrok), Supabase URL·anon 키. 연결 순서: 내 PC → Render → ngrok → 데모 |
| `render.yaml` | Render 배포 설정 (빌드 때 Claude CLI 설치, 시작 명령, 헬스체크, 환경변수) |
| `gunicorn.conf.py` | 배포용 서버 설정 (워커 1 + 스레드 8, timeout 300초) |
| `app.py` | Flask 백엔드. 검색·챗봇·관리자 API, 캐시, 로그인 확인, 하루 제한, 출처 확인 |
| `auth.py` | Supabase 로그인 토큰 확인 (`Authorization: Bearer`) |
| `members.py` | SERVICE 키로 Supabase 읽기·쓰기: 등급(`profiles.role`), 사용 기록(`usage_logs`), 계정별·서버 전체 하루 한도 |
| `admin_stats.py` | 관리자 대시보드 집계 (숫자 카드, Gemini 사용량, 순위, 최근 기록, 7일 그래프) |
| `daily_report.py` | 일일 보고서 집계(어제 KST, 출처 포함, 개인정보 가림)와 숫자 대조. AI 없음 |
| `report_crew.py` | CrewAI 작성·검토 Agent (Gemini만, 보고서 하나에 최대 5번, 수정 최대 2번). Python 3.13 이하 필요 |
| `make_report.py` | 일일 보고서를 명령으로 만들기 (Claude Code `/daily-report`에서 사용) |
| `admin.html` | 관리자 대시보드 화면 (데이터는 `/admin/stats`에서만 받음) |
| `supabase/schema.sql` | Supabase 표 3개·가입 트리거·RLS 규칙. SQL Editor에서 실행 |
| `supabase/check_favorites.sql` | 찜이 계정끼리 섞였는지 점검하는 SQL (읽기 전용, 삭제문은 주석) |
| `tests/` | 서버 단위 테스트 (`python -m unittest discover -s tests -v`) |
| `e2e/` | Playwright 화면 테스트, 데모 모드 (`npm ci && npx playwright install chromium && npm run test:e2e`) |
| `actions.py` | ⚡ 등록 도구(Function Declarations)·시스템 지시문·입력값 검사·명령(cmd) 주인과 만료 |
| `react_graph.py` | ⚡ LangGraph `StateGraph` (agent → validate → server_tool/approve/wait_client → END) |
| `intent.js` | 자동 ⚡ 판단(행동 키워드), 조사(을/를), 선택지 맞추기 (화면과 테스트가 같이 씀) |
| `claude_errors.py` | claude CLI 실패 원인 분류(로그인·토큰/한도/시간 초과/기타)와 로그 마스킹 |
| `chat.py` | 상담 챗봇. 지시문, 입력 정리, Gemini/Claude 호출과 자동 재시도 |
| `polish.py` | 추천 이유 끝맺음을 "~해요/~예요"로 다듬기 (응답할 때 적용, `python polish.py demo`로 데모 파일 정리) |
| `stats.py` | 모니터링 기록. 요청·엔진 결과를 `logs/`에 저장하고 `/stats` 요약 계산 |
| `status.html` | 모니터링 대시보드 화면 |
| `compare.py` | 1:1 비교. 두 상품의 숫자를 직접 비교하고, AI 답변의 이유 줄을 검사·교체 |
| `gemini.py` | Gemini API 호출. 챗봇 답변, (선택) Google 검색 연동 검색 결과를 같은 JSON 형식으로 정리 |
| `links.py` | 상품 링크 검증과 별점 읽기 (`python links.py`로 기존 캐시 재검증, `python links.py ratings cache`로 별점만 다시 채움. 재검색은 안 함) |
| `make_demo.py` | `cache/`의 검색 결과를 `demo/`로 복사 (GitHub Pages 데모용) |
| `demo/` | 공개용으로 저장해 둔 검색 결과 |
| `screenshots/` | README 스크린샷 |
| `og-image.png` | 링크 미리보기 이미지 (1200×630). 원본은 `og-image.svg` |
| `requirements.txt` | 파이썬 패키지 목록 |
| `.env.example` | 환경변수 예시 (`.env`로 복사해서 사용, `.env`는 올리지 않음) |

## 환경변수

`.env.example`을 `.env`로 복사한 뒤 값을 채웁니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SEARCH_ENGINE` | `claude` | 검색 엔진. `claude` 또는 `gemini` |
| `CHAT_ENGINE` | `gemini` | 상담 챗봇 엔진. `gemini` 또는 `claude` |
| `GEMINI_API_KEY` | — | Gemini API 키 ([발급](https://aistudio.google.com/apikey)) |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini 모델 이름 |
| `DAILY_LIMIT` | `5` | 회원 한 명의 새 검색 하루 최대 횟수 |
| `CHAT_DAILY_LIMIT` | `30` | 회원 한 명의 상담 하루 최대 횟수 |
| `GEMINI_DAILY_LIMIT` | — | 관리자 대시보드의 Gemini 하루 무료 호출 한도 (남은 양 막대) |
| `GLOBAL_SEARCH_LIMIT` | `20` | 서버 전체(모든 회원 합계) 새 검색 하루 최대 횟수. admin·내 PC 제외 |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | — | 로그인 확인 |
| `SUPABASE_SERVICE_KEY` | — | 서버 전용. 등급 확인·사용 기록 (화면·GitHub·로그에 절대 넣지 않음) |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | (Render) `claude -p` 로그인 토큰. 내 PC에서 `claude setup-token`으로 발급 |
| `DATA_DIR` | 프로젝트 폴더 | 캐시·모니터링 기록을 둘 폴더 (하루 횟수는 Supabase `usage_logs`) |
| `SEED_DEMO` | — | `1`이면 처음 켜질 때 `demo/` 결과를 캐시로 불러옴 (Render에서는 자동) |
| `RENDER`, `RENDER_EXTERNAL_URL` | (Render가 자동 설정) | Render에서 도는지 판별, Render 주소를 허용 출처에 추가 |

Render에서는 `.env`를 읽지 않고 Render 대시보드의 환경변수만 씁니다.

## 실행 방법

필요한 것: WSL(Ubuntu) 또는 Linux·macOS, Python 3.10 이상, 로그인된 [Claude Code](https://claude.com/claude-code) CLI, Gemini API 키(챗봇용)

```bash
git clone https://github.com/<사용자명>/<저장소명>.git
cd <저장소명>

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # GEMINI_API_KEY 등 값 채우기
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

### Render 배포 (내 PC 없이)

`render.yaml`(블루프린트)로 무료 웹 서비스를 만듭니다.

- **빌드:** Claude Code CLI 설치(`curl -fsSL https://claude.ai/install.sh | bash`) → 실행 파일을 프로젝트의 `bin/`으로 복사 → `pip install -r requirements.txt`
- **시작:** `PATH="$PWD/bin:$PATH" gunicorn app:app` (설정은 `gunicorn.conf.py`: 워커 1개 + 스레드 8개, timeout 300초. 진행 상황 스트림은 1초마다 ping을 보내 연결을 유지)
- **헬스체크:** `/health`
- **Render에서 달라지는 점:** 모든 요청을 외부로 보고 계정별 하루 제한 적용(새 검색 5회, 상담 30회, 서버 전체 새 검색 20회). 처음 켜질 때 `demo/` 결과를 캐시로 불러와서 재시작해도 기본 결과가 보임. CORS는 `https://heejintv.github.io` 그대로

직접 해야 하는 단계:

1. **Claude 토큰 발급 (내 PC):** 터미널에서 `claude setup-token` 실행 → 브라우저에서 로그인 → 출력된 토큰(`sk-ant-oat…`)을 복사해 둡니다. 비밀번호처럼 다루고 저장소·채팅에 붙여 넣지 마세요.
2. **Render 가입:** https://render.com 에서 GitHub 계정으로 가입하고, 이 저장소 접근을 허용합니다.
3. **블루프린트로 만들기:** 대시보드 → **New → Blueprint** → 이 저장소 선택 → `render.yaml`을 읽어 `pickit` 서비스가 나옵니다.
4. **비밀 환경변수 입력:** 만드는 화면(또는 서비스 → **Environment**)에서 `CLAUDE_CODE_OAUTH_TOKEN`(1번 토큰), `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`를 입력합니다. 나머지(`SEARCH_ENGINE`, `CHAT_ENGINE`, `GEMINI_MODEL`, 하루 제한)는 `render.yaml` 값이 들어가 있고 필요하면 바꿉니다.
5. **배포 확인:** 빌드 로그에 `claude --version` 결과가 찍히고, 서비스 주소(`https://pickit-xxxx.onrender.com`)의 `/health`가 `{"ok": true, ...}`를 돌려주면 성공입니다.
6. **화면 연결:** `config.js`의 `RENDER_URL`에 그 주소(끝에 `/` 없이)를 넣고 push하면 GitHub Pages가 Render로 연결합니다.

쇼핑몰 차단 대응 (Render에서만):
- 링크를 확인할 때마다 결과(성공 / 403·401·429·캡차·시간 초과 = 차단 의심 / 404·제목 불일치 등)를 로그와 `/stats`(서버 상태 화면의 "사이트별 링크 확인")에 남깁니다.
- 어떤 사이트가 최근 성공 없이 차단 신호가 2번 이상이면 차단으로 보고, 그 사이트 링크는 지우지 않고 **확인 불가**로 남깁니다. 화면에는 회색 "상품 보기 (미확인)"으로 보이고, 누르면 "서버에서 확인하지 못한 링크예요" 안내 후 엽니다.
- 검색 지시문에 다나와 외 다른 쇼핑몰 상품 링크(`shop_url`)도 찾게 해서, 다나와가 막히면 그 링크로 확인하고 별점도 거기서 읽습니다. 별점은 확인된(차단되지 않은) 페이지에서만 읽습니다.
- 내 PC에서는 기존처럼 확인 안 되는 링크를 "링크 확인 안 됨"으로 처리합니다.
- 속도: 링크는 5개씩 동시에, 하나당 5초까지만 확인하고 차단·시간 초과는 다시 시도하지 않습니다. 같은 검색에서 차단된 사이트의 나머지 링크는 건너뛰고, 차단된 사이트는 30분 동안 기억해 바로 "확인 불가" 처리합니다. 링크 재검색(Claude 두 번째 호출)은 404·다른 상품 페이지처럼 링크가 진짜 틀린 경우에만 합니다.
- 검색 단계별 시간(캐시 확인·AI 검색·링크 확인·링크 재검색·정리)을 로그(`검색 단계 시간 [...]`)와 `/stats`, 서버 상태 화면에 기록합니다.

알아둘 점 (무료 플랜):
- 15분 동안 요청이 없으면 잠들고, 다음 접속 때 깨는 데 최대 1분 걸립니다. 화면에 "서버를 깨우는 중이에요 (최대 1분)"이 뜹니다.
- 디스크가 유지되지 않아서 다시 켜지면 새로 검색한 캐시·모니터링 기록이 초기화됩니다 (`demo/` 결과는 다시 불러옴). 하루 사용 횟수는 Supabase `usage_logs`에서 세므로 유지됩니다.
- 메모리가 512MB라 검색을 동시에 여러 개 돌리면 부족할 수 있습니다.
- 검색은 내 Claude 구독 사용량을 씁니다. 토큰을 공개 서비스에 써도 되는지 Anthropic 이용 약관을 확인하세요.

### 데모 데이터 갱신

```bash
python make_demo.py   # cache/ → demo/ 복사 (검색 비용 등 내부 정보는 제외)
```

## 회원가입·로그인 (Supabase Auth)

### 로그인 흐름
1. 헤더의 **로그인** → 이메일+비밀번호로 로그인 또는 회원가입 (`supabase-js` 2.45.4, CDN 버전 고정). 가입 확인 메일의 링크를 누르면 로그인할 수 있어요.
2. 로그인하면 헤더에 **이메일 앞부분 · 오늘 검색 3/5 · 로그아웃**이 보이고, admin이면 **🛠 관리자** 버튼도 보여요.
3. 새 검색·상담 요청에는 `Authorization: Bearer <로그인 토큰>`이 붙어요.
4. 서버(`auth.py`)는 토큰을 Supabase(`/auth/v1/user`)에 물어 확인하고, 없거나 틀리면 **Claude·Gemini를 부르기 전에 401**로 막아요. 확인된 사용자 id는 사용 기록에 씁니다.
5. 로그인 창에는 "서비스 개선을 위해 상담 질문 내용이 기록돼요" 안내가 있어요.

### 회원 전용 기능과 하루 제한
표는 [보안 장치 3](#3-회원-전용-기능과-하루-사용-제한-계정-기준)에 있어요. 요약하면:

- **비회원**: 최근 검색, 공유 링크, 데모, 필터·정렬은 그대로. 새 검색은 "🔒 새 검색은 회원 전용이에요" + 로그인/회원가입 버튼, 챗봇은 가운데 안내 카드(자물쇠 · 로그인하고 상담받기 · 회원 혜택 3줄)만 보이고 예시 질문·입력창은 숨김
- **회원**: 계정당 하루 새 검색 5회(`DAILY_LIMIT`), 상담 30회(`CHAT_DAILY_LIMIT`, 1:1 비교 포함)
- **서버 전체 상한**: 모든 회원 합쳐 새 검색 하루 20회(`GLOBAL_SEARCH_LIMIT`). 넘으면 "오늘 검색이 모두 소진됐어요"
- **admin**(`profiles.role = 'admin'`)과 내 PC 직접 접속: 둘 다 무제한, 서버 전체 상한에도 안 들어감(`exempt`)
- 횟수는 **한국 시간 자정 기준, 성공한 것만** 셉니다 (저장된 결과·취소·실패·한도 초과는 안 셈). 같은 사람이 동시에 여러 번 보내도 넘지 않게 처리 중인 요청까지 셉니다.

### Supabase 표와 RLS 규칙
`supabase/schema.sql`에 모두 들어 있어요 (여러 번 실행해도 됨). 화면은 anon 키 + 로그인 토큰으로, 서버는 SERVICE 키로 접근합니다. SERVICE 키는 RLS를 건너뛰므로 서버에만 둡니다.

| 표 | 내용 | 화면(로그인한 사람)이 할 수 있는 것 | 누가 쓰나 |
|---|---|---|---|
| `profiles` | `id`(auth 사용자 id), `email`, `role`(`user` 기본 / `admin`), `created_at` | 자기 행만 읽기·수정. **`role`·`id`는 못 바꿈** (열 단위 권한으로 `email`만 수정 허용) | 가입하면 트리거(`handle_new_user`)가 자동으로 한 줄 생성. admin 지정은 SQL Editor에서만 |
| `favorites` | `user_id`, `item_key`(브랜드+모델), `item`(상품 정보 JSON, 8KB 이하), `query`(검색어), `created_at` | 자기 행만 읽기·추가·삭제. 넣을 수 있는 열도 제한 (`created_at`은 서버 시각만) | 화면이 `supabase-js`로 직접 |
| `usage_logs` | `user_id`, `email`, `action`(search/chat/compare), `query`(검색어 또는 질문 원문, 300자), `engine`, `status`(ok·cached·limited·cancelled·timeout·error), `duration_ms`, `tokens`(Gemini 사용 토큰), `outcome`(⚡ 결과 종류), `error_kind`(검색 실패 종류), `log_id`, `exempt`, `created_at` | 자기 기록만 읽기, admin은 전체 읽기. **쓰기·수정·삭제 불가** | 서버만 (SERVICE 키) |

- admin 확인은 `public.is_admin()` 함수(security definer)로 해서 RLS 안에서 `profiles`를 다시 읽다가 꼬이지 않게 했어요.
- 탈퇴(auth 사용자 삭제)하면 세 표의 행이 함께 지워집니다 (`on delete cascade`).

### 찜 목록 저장 방식
- **브라우저 저장소를 계정별로 분리**: 비회원은 `pickit:guest:*`, 회원은 `pickit:u:<user_id>:*` (숨긴 최근 검색어 등). 회원의 찜은 브라우저에 저장하지 않고 `favorites`의 내 행만 씁니다.
- **로그아웃·계정 변경**: 브라우저의 `pickit:` 저장소 전부와 화면의 찜·대화·1:1 비교 답변·검색 결과·주소창 검색어를 지웁니다.
- **로그인할 때 자동으로 합치지 않음**: 이 기기에 비회원 찜이 있으면 "이 기기에 저장된 찜 N개를 내 계정에 추가할까요?"라고 묻고, **예**일 때만 추가한 뒤 비회원 찜을 비웁니다. 아니오면 그 로그인 동안 다시 묻지 않습니다.
- 이미 로그인된 상태로 페이지를 열면, 로그인 확인이 끝날 때까지 비회원 찜을 보여주지 않습니다.

### 관리자 대시보드 (`admin.html`, `GET /admin/stats`)
admin 계정으로 로그인하면 헤더에 **🛠 관리자** 버튼이 보입니다.

| 영역 | 내용 |
|---|---|
| 조건 | 기간(오늘 / 7일 / 30일), 이메일 검색 (순위·최근 기록에 적용), 새로고침 |
| 숫자 카드 | 전체 회원 수, 오늘 사용자 수, 오늘 새 검색 수, 오늘 상담 수 (성공한 것만) |
| Gemini 사용량 | 오늘 호출 수·토큰 합계, `GEMINI_DAILY_LIMIT` 대비 남은 양 막대 (80% 주황, 100% 빨강) |
| 서버 전체 새 검색 | 오늘 사용 수 / `GLOBAL_SEARCH_LIMIT` 막대 (admin·내 PC 제외) |
| 최근 7일 사용량 | 일별 새 검색·상담 막대그래프 |
| 사용자별 순위 | 이메일, 새 검색 수, 상담 수, 마지막 사용 시각 |
| 최근 기록 | 시간, 이메일, 종류(검색/상담/비교), 내용(검색어나 질문), 결과, 응답 시간. 실패는 빨간 행, 한도 초과는 노란 행 (결과 칸에도 글자로 표시) |

- **서버에서 막기**: `/admin/stats`는 토큰 확인 → `profiles.role`을 **캐시 없이 매번** 확인 → admin이 아니면 403 (기록을 읽지도 않음). `admin.html` 파일에는 숫자가 하나도 없어서, 일반 사용자가 주소를 직접 열어도 "관리자만 볼 수 있어요"만 보입니다. 헤더 버튼을 숨기는 건 편의일 뿐입니다.
- 응답은 `Cache-Control: no-store`, 날짜는 한국 시간 기준. 기간 안의 기록은 최근 20,000개까지 집계합니다.

### DB 준비 순서
1. Supabase 대시보드 → **SQL Editor** → `supabase/schema.sql` 전체를 붙여 넣고 **Run**
2. **Authentication → URL Configuration**: Site URL과 Redirect URLs에 `https://heejintv.github.io/shopping-agent/`
3. **Project Settings → API Keys**: URL·anon(publishable) 키는 `config.js`에, 세 값(`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`)은 `.env`와 Render 환경변수에
4. 관리자 지정: `update public.profiles set role = 'admin' where email = '내이메일';`
5. (점검) 찜이 섞였는지: `supabase/check_favorites.sql`을 한 덩어리씩 실행

## 고친 버그 (2026-11-20)

### 1. 계정끼리 찜 목록이 섞임
- **증상**: A로 로그인 → 목도리 2개 찜 → 로그아웃해도 찜 2개가 그대로 보임 → B로 가입·로그인하면 B에게도 목도리 2개가 보임
- **원인**: 로그인 중 DB(`favorites`) 동기화가 실패하면(예: 표를 아직 안 만듦) 찜이 **모든 계정이 같이 쓰는 브라우저 키**(`shopping-agent:wishlist`)에 저장됐어요. 로그아웃하면 그 키를 다시 읽어 화면에 남았고, B가 로그인할 때 **브라우저 찜을 DB에 자동으로 합치는** 코드가 A의 찜을 B 계정에 넣었어요.
- **해결**
  - 로그인 중에는 찜을 브라우저에 쓰지 않고 `favorites`의 내 `user_id` 행만 읽고 씀
  - 브라우저 저장소 키를 비회원용(`pickit:guest:*`)·계정별(`pickit:u:<user_id>:*`)로 분리, 예전 공용 찜 키는 섞였을 수 있어 버림
  - 로그아웃·계정 변경 시 브라우저 저장소와 화면 기록(찜·대화·검색 결과·주소창 검색어)을 모두 지움
  - 자동 합치기를 없애고 "이 기기에 저장된 찜 N개를 내 계정에 추가할까요?" 확인 후에만 추가
  - `favorites`는 `created_at`을 화면에서 넣지 못하게 막음. 이미 섞인 행은 `check_favorites.sql`의 "가입 전 시각으로 저장된 찜" 쿼리로 찾을 수 있음 (예전 코드가 브라우저의 찜 시각을 그대로 넣었기 때문)

### 2. 내 PC 서버에서 `/health` 500 → 화면이 데모 모드로 빠짐
- **증상**: `NameError: name 'PROXY_HEADERS' is not defined` (`is_local_request`), `GET /health 500`
- **원인**: 하루 횟수를 `usage.json`(IP 기준)에서 Supabase(계정 기준)로 옮기면서 옛 코드 묶음을 지울 때, 바로 아래에 있던 `PROXY_HEADERS` 정의(ngrok 경유 요청을 구분하는 헤더 목록)까지 같이 지웠어요. 테스트는 `is_local_request`를 가짜로 바꿔 두어 이 함수가 실제로 실행되지 않아 못 잡았어요. Render에서는 이 줄에 닿기 전에 함수가 끝나서 문제가 없었어요.
- **해결**
  - `PROXY_HEADERS` 복원, pyflakes로 전체 `.py`를 검사해 정의 안 된 이름이 더 없음을 확인
  - `tests/test_modes.py` 추가: `is_local_request`를 가짜로 바꾸지 않고 내 PC 모드(직접·허용 출처·ngrok 경유·다른 사이트)와 Render 모드에서 `/health`, `/me`, `/search`, `/search/stream`, `/chat`을 직접 호출. `RENDER=true`로 앱을 새로 불러와 `/health`도 확인. 고치기 전 코드로 돌리면 내 PC 모드 테스트가 바로 실패함

## 테스트

```bash
python -m unittest discover -s tests -v
```

Supabase·Claude·Gemini는 가짜로 바꿔서 네트워크 없이 돌아갑니다. 현재 **265개** 통과 (crewai가 없는 Python 3.14에서는 Crew 테스트 15개를 건너뜀).

| 파일 | 확인하는 것 |
|---|---|
| `tests/test_auth.py` | 새 검색·스트림·상담: 토큰 없음/틀림 401, 계정 한도 429, 서버 전체 상한(admin 예외), 동시 요청, `/me`, 사용 기록 |
| `tests/test_members.py` | Supabase REST 요청 모양 (키 헤더, 개수 세기, `exempt` 필터), 기록 실패해도 응답은 정상 |
| `tests/test_modes.py` | 내 PC·Render 모드에서 실제 `is_local_request`를 거쳐 API 호출 |
| `tests/test_admin.py` | `/admin/stats` 401·403·200, 대시보드 집계, Gemini 토큰 기록 |
| `tests/test_report.py` | 일일 보고서 집계·마스킹·숫자 대조, Crew 호출 상한(가짜 Gemini), 권한·승인 |
| `tests/test_actions.py` | ⚡ 입력값 검사, 그래프 흐름(interrupt/resume), 승인 카드, 중복 찜, 검색 확인, 여러 개 선택, 비교 창 열림 확인 (Gemini 가짜 응답) |
| `tests/test_intent.py` | 자동 ⚡ 키워드, 조사, 선택지 맞추기 (`node`로 `intent.js` 실행) |
| `tests/test_recent.py` | 최근 검색 순서 (한국 시간, 저장된 결과로 다시 검색해도 맨 앞) |
| `tests/test_link_fallback.py` | 링크 절반 미만이면 1번 다시 찾기, 요약 줄 개발 용어 정리, "쇼핑몰에서 찾기" |
| `tests/test_claude_check.py` | claude 실패 원인 분류·마스킹, `/admin/claude-check` 권한 |
| `tests/test_small3.py` | 404/405/500 JSON, 화면 오류 안내(`readJson`), ⚡ logout 확인, 관리자 연결 확인 버튼 |

화면(브라우저) 동작은 직접 확인합니다.

## 알려진 한계

- **Render 재시작 시 초기화**: ⚡ 진행 중인 명령은 메모리(`MemorySaver`)에만 있어서 서버가 다시 켜지면 사라지고(다시 입력해야 함), 최근 검색·캐시도 임시 디스크라 초기화됩니다 (`demo/` 결과만 다시 불러옴).
- **비교는 2개까지**: 1:1 비교만 지원해요. 후보가 3개 이상이면 2개를 골라야 합니다.

## 주의

검색 결과와 챗봇 답변은 AI가 웹에서 수집한 정보를 바탕으로 하며, 가격은 수시로 바뀝니다. 구매 전에 반드시 링크에서 다시 확인하세요.
