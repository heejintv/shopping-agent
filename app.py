"""쇼핑 가격비교 서버.

/search?q=상품명 요청이 오면 검색 엔진으로 브랜드·모델별 가격 정보를 JSON으로 받아 돌려준다.
결과는 cache/에 저장한다 (어떤 엔진으로 검색했는지 engine에 기록).

검색 엔진 (환경변수 SEARCH_ENGINE):
- claude (기본): `claude -p`를 웹 검색 전용으로 실행
- gemini: Gemini API + Google 검색 연동 (GEMINI_API_KEY, GEMINI_MODEL 사용, gemini.py)

인터넷에 공개할 때를 위한 보호 장치:
- 새 검색(/search, /search/stream)과 상담 챗봇(/chat)은 Supabase로 로그인한 회원만 (auth.py)
  최근 검색(/history)·공유 링크(/cached)는 로그인 없이 동작
- 회원은 계정당 하루 새 검색 DAILY_LIMIT회(기본 5), 상담 CHAT_DAILY_LIMIT회(기본 30). 캐시된 결과는 횟수 안 셈
- 서버 전체 새 검색도 하루 GLOBAL_SEARCH_LIMIT회(기본 20)까지
  profiles.role이 admin이거나 내 PC에서 직접 온 요청(is_local_request)은 둘 다 예외(무제한)
- 사용 기록은 Supabase usage_logs 표에 저장하고 하루 횟수도 거기서 셈 (members.py, SERVICE 키는 서버에서만)
- CORS는 GitHub Pages 주소(ALLOWED_ORIGIN)만 허용
- /search/stream: 새 검색의 진행 단계를 실시간으로 보냄 (Server-Sent Events). 취소하면 claude를 멈추고 횟수에서 빼지 않음
- /me: 로그인한 사람의 등급과 오늘 남은 횟수
- /chat/action: 챗봇 "문장으로 실행". Gemini 한 번으로 최대 3단계 계획(search·pick·찜)만 줌 (실행은 화면, 상담 한도 1회)
- /admin/stats: 관리자 대시보드 (admin.html). 토큰 확인 + profiles.role=admin일 때만, 아니면 403
- /admin/claude-check: admin 전용. claude --version + 짧은 claude -p로 토큰이 살아 있는지 (성공/실패·원인 종류만)
- Render 배포(환경변수 RENDER=true): 모든 요청을 외부로 보고 하루 제한 적용, 처음 켜질 때 demo/ 결과를 캐시로 불러옴
- /stats: 모니터링 요약 (stats.py, 화면은 status.html). 질문 원문·IP는 기록하지 않음
"""

import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_from_directory

import actions
import admin_stats
import auth
import chat
import claude_errors
import members
import stats
from gemini import EngineError, call_gemini
from gemini import model_name as gemini_model
import links
from links import verify_links
from polish import polish_result

BASE_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """.env 파일의 KEY=VALUE 줄을 환경변수로 읽는다 (이미 있는 환경변수는 덮어쓰지 않음)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# Render는 실행 환경에 RENDER=true를 자동으로 넣는다. 그때는 내 PC가 아니므로 모든 요청을 외부로 본다
ON_RENDER = os.environ.get("RENDER", "").lower() == "true"
if not ON_RENDER:   # Render에서는 설정을 전부 Render 환경변수로만 받음
    load_env(BASE_DIR / ".env")
# 캐시·모니터링 기록을 둘 폴더 (기본: 프로젝트 폴더)
DATA_DIR = Path(os.environ.get("DATA_DIR") or BASE_DIR)
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 회원 한 명의 하루 한도 (members.py가 Supabase usage_logs에서 셈)
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT") or "5")
CHAT_DAILY_LIMIT = int(os.environ.get("CHAT_DAILY_LIMIT") or "30")
LIMITS = {"search": DAILY_LIMIT, "chat": CHAT_DAILY_LIMIT}
# 서버 전체 새 검색 하루 상한 (모든 회원 합계, admin·내 PC 제외). 검색 비용 보호용
GLOBAL_SEARCH_LIMIT = int(os.environ.get("GLOBAL_SEARCH_LIMIT") or "20")
LIMIT_MESSAGES = {"search": "오늘 새 검색 {n}회를 모두 썼어요. 내일 다시 시도하세요.",
                  "chat": "오늘 상담 {n}회를 모두 썼어요. 내일 다시 시도하세요."}
# Gemini 하루 무료 호출 한도 (관리자 대시보드의 남은 양 막대용. 0이면 막대 없이 호출 수만)
GEMINI_DAILY_LIMIT = int(os.environ.get("GEMINI_DAILY_LIMIT") or "0")
GLOBAL_LIMIT_MESSAGE = "오늘 검색이 모두 소진됐어요. 내일 다시 시도해 주세요. (최근 검색은 계속 볼 수 있어요)"
MEMBER_DOWN = "회원 정보를 확인할 수 없어요. 잠시 후 다시 시도해주세요."
ASYNC_LOG = True   # 사용 기록을 응답과 따로(백그라운드) 남김. 테스트에서는 False
ALLOWED_ORIGIN = "https://heejintv.github.io"
# Render 주소로 페이지를 직접 열었을 때의 출처 (Render가 RENDER_EXTERNAL_URL을 자동으로 넣음)
PUBLIC_URL = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL") or "").rstrip("/")


def seed_cache_from_demo() -> int:
    """demo/의 검색 결과를 캐시에 없을 때만 복사한다 (Render는 재시작하면 캐시가 비므로 기본 결과를 채움)."""
    copied = 0
    for f in (BASE_DIR / "demo").glob("*.json"):
        if f.name == "index.json":
            continue
        target = CACHE_DIR / f.name
        if not target.exists():
            target.write_bytes(f.read_bytes())
            copied += 1
    return copied


if ON_RENDER or os.environ.get("SEED_DEMO") == "1":
    seed_cache_from_demo()

links.RESULT_HOOK = stats.record_link   # 링크 확인 결과를 사이트별로 /stats에 기록


def shop_links() -> bool:
    """Render(또는 SHOP_LINKS=1)에서는 다나와가 막힐 수 있어 다른 쇼핑몰 상품 링크(shop_url)도 같이 받는다."""
    return ON_RENDER or os.environ.get("SHOP_LINKS") == "1"


SHOP_URL_PROMPT = """- shop_url: 다나와가 아닌 쇼핑몰(G마켓, 11번가, 옥션, 컴퓨존, 브랜드 공식몰 등)의 같은 상품 상세 페이지 주소도 함께 찾아.
  웹페이지 읽기로 직접 열어서 확인한 주소만. 검색·목록 페이지는 안 됨. 못 찾으면 null.
"""

CLAUDE_TIMEOUT = 600  # 초. 웹 검색을 여러 번 하므로 넉넉하게 10분
WEB_TOOLS = "WebSearch,WebFetch"

# Claude가 반드시 이 모양으로 답하도록 강제하는 JSON 스키마
NULLABLE_NUM = {"type": ["number", "null"]}
NULLABLE_STR = {"type": ["string", "null"]}
PICK = {
    "type": ["object", "null"],
    "properties": {"model": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["model", "reason"],
}
SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "brand": {"type": "string"},
                    "model": {"type": "string"},
                    "price": NULLABLE_NUM,
                    "currency": NULLABLE_STR,
                    "store": NULLABLE_STR,
                    "rating": NULLABLE_NUM,
                    "review_count": NULLABLE_NUM,
                    "pros": {"type": "array", "items": {"type": "string"}},
                    "cons": {"type": "array", "items": {"type": "string"}},
                    "url": NULLABLE_STR,
                },
                "required": ["brand", "model", "price", "currency", "store",
                             "rating", "review_count", "pros", "cons", "url"],
            },
        },
        "value_pick": PICK,
        "quality_pick": PICK,
        "notes": NULLABLE_STR,
    },
    "required": ["products", "value_pick", "quality_pick", "notes"],
}

PROMPT = """상품 "{q}"의 한국 쇼핑몰(다나와·네이버쇼핑·쿠팡 등) 가격을 비교해.
- 웹 검색은 최대 3번. 다나와 같은 가격비교 사이트 위주로 찾아.
- 인기 상위 5개 모델만: price(원, 숫자), store, rating(5점), review_count, pros/cons(각 1~2개), url.
- url: 다나와 상품 상세(prod.danawa.com/info/?pcode=...) 또는 쇼핑몰의 그 상품 상세 페이지만. 검색·목록 페이지는 안 됨.
  웹페이지 읽기로 실제로 열어서 확인한 주소만 쓰고, 추측해서 만들지 마. 확인 못 하면 null.
- 확인 못 한 값은 null(목록은 빈 배열). 지어내지 마.
- value_pick(가성비)·quality_pick(품질): products의 model 이름 그대로 + 이유 한 줄. 근거 없으면 null.
- 이유(reason, fit_reason)는 "~해요", "~예요"로 끝나는 문장으로 써. "~있음", "~함", "~다", "~입니다" 같은 끝맺음은 쓰지 마.
- notes: 주의사항 한 문장 또는 null.
"""

# 사용자가 조건을 입력했을 때만 프롬프트 뒤에 붙임
CONDITION_PROMPT = """
사용자 조건: "{c}"
- 조건에 맞는 모델을 우선 찾아. 각 상품마다 fit(조건 적합도: "높음"/"보통"/"낮음")과 fit_reason(이유 한 줄).
- 조건에 맞는 상품이 하나도 없으면 지어내지 말고 products는 빈 배열, notes는 "조건에 맞는 상품을 못 찾음".
"""


# 검증에 실패한 상품만 모아서 링크를 다시 찾게 하는 지시 (재시도는 한 번만)
RETRY_PROMPT = """아래 상품들의 상세 페이지 주소를 찾아.
- 다나와 상품 상세(prod.danawa.com/info/?pcode=...) 또는 쇼핑몰의 그 상품 상세 페이지만. 검색·목록 페이지는 안 됨.
- 웹페이지 읽기로 직접 열어서 제목에 모델명이 있는 걸 확인한 주소만. 추측해서 만들지 마. 못 찾으면 null.
- 웹 검색은 상품당 1번 이내.
{items}
"""
RETRY_SCHEMA = {
    "type": "object",
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"no": {"type": "integer"}, "url": NULLABLE_STR},
                "required": ["no", "url"],
            },
        },
    },
    "required": ["links"],
}
RETRY_TIMEOUT = 300  # 초


def build_schema(with_condition: bool) -> dict:
    """조건이 있으면 상품마다 fit·fit_reason을 필수로, Render에서는 shop_url을 추가한 스키마."""
    if not with_condition and not shop_links():
        return SCHEMA
    schema = json.loads(json.dumps(SCHEMA))  # 깊은 복사
    item = schema["properties"]["products"]["items"]
    if shop_links():
        item["properties"]["shop_url"] = NULLABLE_STR
        item["required"] = item["required"] + ["shop_url"]
    if not with_condition:
        return schema
    item["properties"]["fit"] = {"type": "string", "enum": ["높음", "보통", "낮음"]}
    item["properties"]["fit_reason"] = {"type": "string"}
    item["required"] += ["fit", "fit_reason"]
    return schema

app = Flask(__name__)
if not app.debug:
    import logging
    app.logger.setLevel(logging.INFO)   # 단계별 시간 같은 info 로그도 남김 (gunicorn·Render 로그)

# 같은 검색어가 동시에 들어오면 claude를 한 번만 실행하도록 검색어별 잠금
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


# ngrok은 요청을 127.0.0.1로 전달하면서 이런 헤더를 붙인다. 하나라도 있으면 외부 요청
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded")
LOCAL_ORIGINS = {ALLOWED_ORIGIN, "http://localhost:5000", "http://127.0.0.1:5000"} | ({PUBLIC_URL} if PUBLIC_URL else set())


def is_local_request() -> bool:
    """내 PC에서 직접 온 요청이면 True (무제한). Render에서 돌 때는 항상 False (모두 외부 요청)."""
    if ON_RENDER:
        return False
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return False
    if any(h in request.headers for h in PROXY_HEADERS):  # ngrok을 거쳐 왔음
        return False
    # 내 PC 브라우저로 아무 사이트나 보다가 그 사이트가 몰래 localhost:5000을 부르는 경우 막기:
    # 다른 사이트에서 온 요청은 무제한으로 치지 않는다 (curl처럼 헤더가 없으면 내 PC로 봄)
    origin = request.headers.get("Origin")
    if origin:
        return origin in LOCAL_ORIGINS
    return request.headers.get("Sec-Fetch-Site", "none") in ("same-origin", "none")


def limit_info() -> dict:
    """로그인 없이 부르는 API(/health 등)에 붙이는 횟수 정보. 계정별 남은 횟수는 /me에서."""
    if is_local_request():
        return {"unlimited": True, "remaining": None, "limit": None}
    return {"unlimited": False, "remaining": None, "limit": DAILY_LIMIT}


# ---------- 회원 (로그인·등급·하루 한도·사용 기록) ----------

def login_user():
    """Authorization 토큰 확인. (사용자, None) 또는 (None, 오류 응답). 확인되면 g.user_id에 넣어 둠."""
    try:
        user = auth.verify(request.headers.get("Authorization"))
    except auth.AuthError as e:
        return None, (jsonify(error=str(e), login_required=e.status == 401), e.status)
    g.user_id = user["id"]
    g.user_email = user["email"]
    return user, None


def is_unlimited(user: dict) -> bool:
    """내 PC에서 직접 온 요청이거나 admin이면 무제한. Supabase에 못 물어보면 MemberError."""
    if is_local_request():
        return True
    return members.get_role(user["id"]) == "admin"


def limit_body(kind: str, scope: str) -> dict:
    """한도 초과 응답 내용. scope: "user"(내 계정) 또는 "global"(서버 전체)."""
    if scope == "global":
        return {"error": GLOBAL_LIMIT_MESSAGE, "scope": "global", "unlimited": False, "remaining": 0,
                "limit": LIMITS[kind]}
    n = LIMITS[kind]
    return {"error": LIMIT_MESSAGES[kind].format(n=n), "scope": "user", "unlimited": False, "remaining": 0, "limit": n}


def take_quota(user_id: str, kind: str) -> members.Ticket:
    """한도 칸 잡기 (새 검색은 서버 전체 상한도). LimitReached·MemberError는 부른 쪽에서 처리."""
    return members.quota.reserve(user_id, kind, LIMITS[kind], GLOBAL_SEARCH_LIMIT if kind == "search" else None)


def reserve(user: dict, kind: str, unlimited: bool):
    """하루 한도에서 한 칸 잡기. (잡은 칸 또는 None, 오류 응답 또는 None)"""
    if unlimited:
        return None, None
    try:
        return take_quota(user["id"], kind), None
    except members.LimitReached as e:
        return None, (jsonify(limit_body(kind, e.scope)), 429)
    except members.MemberError as e:
        app.logger.warning("quota check failed: %s", e)
        return None, (jsonify(error=MEMBER_DOWN), 503)


def finish(user: dict, action: str, status: str, t0: float, query=None, engine=None,
           ticket: "members.Ticket | None" = None, exempt: bool = False, tokens: int | None = None):
    """사용 기록을 남기고, 잡아 둔 한도 칸(ticket)을 놓는다. 기록이 끝난 뒤 놓아야 횟수가 정확함.
    exempt: admin·내 PC 사용 (서버 전체 상한에 안 셈). tokens: Gemini 사용 토큰 수."""
    def run():
        try:
            members.log_usage(user["id"], user.get("email"), action, status, query=query, engine=engine,
                              duration_ms=(time.time() - t0) * 1000, exempt=exempt, tokens=tokens)
        finally:
            if ticket:
                ticket.release()
    if ASYNC_LOG:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()


def quota_info(unlimited: bool, kind: str, left) -> dict:
    return {"unlimited": unlimited, "remaining": None if unlimited else left,
            "limit": None if unlimited else LIMITS[kind]}


# ---------- 검색 ----------

def normalize(q: str) -> str:
    return " ".join(q.split()).lower()


def cache_path(q: str, c: str = "") -> Path:
    """검색어+조건 조합별 캐시 파일. 조건이 없으면 예전과 같은 키라 기존 캐시를 그대로 쓴다."""
    raw = normalize(q) if not c else normalize(q) + "\n" + normalize(c)
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{key}.json"


def search_engine() -> str:
    """SEARCH_ENGINE 환경변수 (claude 또는 gemini). 모르는 값이면 claude."""
    engine = (os.environ.get("SEARCH_ENGINE") or "claude").strip().lower()
    return engine if engine in ("claude", "gemini") else "claude"


class Cancelled(Exception):
    """사용자가 검색을 취소함."""


class Job:
    """진행 상황을 보내는 검색 하나. 취소 신호와 이벤트 줄(queue)을 가진다."""

    def __init__(self):
        self.id = secrets.token_hex(8)
        self.cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()

    def emit(self, event: str | None, data: dict | None = None) -> None:
        self.events.put(None if event is None else (event, data or {}))

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


_jobs: dict[str, Job] = {}


def call_engine(prompt: str, schema: dict, timeout: int, engine: str, job: Job | None = None) -> dict:
    """검색 엔진 호출. 성공·실패(429/503/504 구분)를 모니터링에 기록한다. 취소는 실패로 치지 않는다."""
    try:
        out = call_gemini(prompt, schema, timeout) if engine == "gemini" else call_claude(prompt, schema, timeout, job)
    except Cancelled:
        raise
    except subprocess.TimeoutExpired:
        stats.record_engine(engine, False, 504)
        raise
    except EngineError as e:
        stats.record_engine(engine, False, e.code)
        raise
    except Exception:
        stats.record_engine(engine, False)
        raise
    stats.record_engine(engine, True)
    return out


def _kill(proc: subprocess.Popen) -> None:
    """claude와 그 아래에서 뜬 프로세스까지 한꺼번에 종료."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    proc.communicate()


def claude_bin() -> str:
    """claude 실행 파일: CLAUDE_BIN 환경변수 → PATH → 프로젝트의 bin/claude (Render 빌드에서 복사해 둔 것)."""
    local = BASE_DIR / "bin" / "claude"
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or (str(local) if local.exists() else "claude")


def claude_failed(why: str, stderr: str, stdout: str):
    """claude 실패를 로그에 남기고(stderr·stdout 끝부분, 토큰·키는 가림) 원인별 EngineError를 낸다."""
    kind = claude_errors.classify(stderr, stdout)
    app.logger.warning("claude 실행 실패 (%s, 종류 %s) stderr=[%s] stdout=[%s]",
                       why, kind, claude_errors.tail(stderr), claude_errors.tail(stdout))
    message, code = claude_errors.KINDS[kind]
    raise EngineError(message, code)


def call_claude(prompt: str, schema: dict, timeout: int, job: Job | None = None) -> dict:
    """claude -p를 웹 도구만 허용해서 실행하고, 스키마 모양의 결과(dict)를 돌려준다.
    job이 있으면 0.5초마다 취소 여부를 확인해서, 취소되면 프로세스를 멈추고 Cancelled를 낸다."""
    cmd = [
        claude_bin(), "-p", prompt,
        "--tools", WEB_TOOLS,            # 쓸 수 있는 도구를 웹 검색·웹페이지 읽기로만 제한
        "--allowedTools", WEB_TOOLS,     # 그 두 도구는 확인 없이 허용
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=BASE_DIR,
                            start_new_session=True)   # 취소할 때 프로세스 그룹째 멈출 수 있게
    deadline = time.time() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if job and job.cancel.is_set():
                _kill(proc)
                raise Cancelled()
            if time.time() > deadline:
                _kill(proc)
                raise subprocess.TimeoutExpired(cmd[:2], timeout)
    if proc.returncode != 0:
        claude_failed(f"코드 {proc.returncode}", stderr, stdout)
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        claude_failed("JSON 아님", stderr, stdout)
    if out.get("is_error"):
        claude_failed("is_error", stderr, stdout)
    data = out.get("structured_output")
    if data is None:  # 혹시 structured_output이 없으면 result 텍스트를 JSON으로 파싱
        data = json.loads(out["result"])
    data["cost_usd"] = out.get("total_cost_usd")
    return data


def run_search(q: str, c: str, engine: str, job: Job | None = None) -> dict:
    prompt = PROMPT.format(q=q) + (SHOP_URL_PROMPT if shop_links() else "") + (CONDITION_PROMPT.format(c=c) if c else "")
    return call_engine(prompt, build_schema(bool(c)), CLAUDE_TIMEOUT, engine, job)


def retry_links(failed: list[dict], engine: str, job: Job | None = None) -> float | None:
    """검증 실패한 상품들만 같은 엔진으로 한 번 더 상세 페이지를 찾고 검증한다. 비용(USD)을 돌려준다."""
    items = "\n".join(f"{i}. {p['brand']} {p['model']}" for i, p in enumerate(failed, 1))
    out = call_engine(RETRY_PROMPT.format(items=items), RETRY_SCHEMA, RETRY_TIMEOUT, engine, job)
    found = {x.get("no"): x.get("url") for x in out.get("links", [])}
    retried = []
    for i, p in enumerate(failed, 1):
        if found.get(i):
            p["url"] = found[i]
            retried.append(p)
    verify_links(retried)  # 다시 받은 주소도 똑같이 검증. 실패하면 링크 확인 안 됨으로 남음
    return out.get("cost_usd")


STEP_LABELS = {"cache": "캐시", "search": "AI 검색", "links": "링크 확인", "retry": "링크 재검색", "finalize": "정리"}


def log_steps(q: str, timings: dict) -> None:
    app.logger.info("검색 단계 시간 [%s] %s", q, " · ".join(f"{STEP_LABELS[k]} {v / 1000:.1f}초" for k, v in timings.items()))


def perform_search(q: str, c: str, path: Path, engine: str, step=None, job: Job | None = None,
                   timings: dict | None = None) -> dict:
    """새 검색 본체: 엔진 검색 → 링크 검증(실패한 것만 한 번 재검색) → 문구 정리 → 캐시 저장.
    step(단계, 글자, done, total)로 진행 상황을 알린다. 예외(시간 초과·엔진 오류·취소)는 호출한 쪽에서 처리."""
    step = step or (lambda *a, **k: None)
    check = job.check if job else (lambda: None)
    timings = timings if timings is not None else {}
    started = time.time()
    t = time.time()
    step("search", "AI가 웹 검색 중")
    data = run_search(q, c, engine, job)
    timings["search"] = (time.time() - t) * 1000
    check()
    products = data.get("products") or []
    t = time.time()
    step("links", "링크 확인 중", 0, len(products))
    # 링크마다 실제 접속해서 확인 → 진짜 틀린 링크(404·다른 상품 페이지)만 한 번 더 찾기 → 그래도 실패하면 링크 없음
    failed = verify_links(products, on_progress=lambda d, n: step("links", "링크 확인 중", d, n))
    timings["links"] = (time.time() - t) * 1000
    check()
    t = time.time()
    if failed:
        stats.bump("link_retries")
        step("links", "링크 다시 찾는 중", len(products) - len(failed), len(products))
        try:
            cost = retry_links(failed, engine, job)
            if cost and data.get("cost_usd") is not None:
                data["cost_usd"] = round(data["cost_usd"] + cost, 4)
        except Cancelled:
            raise
        except Exception:  # 재시도가 실패해도 검색 결과는 그대로 돌려줌 (해당 상품은 링크 확인 안 됨)
            app.logger.exception("link retry failed")
    elif any(p.get("url_verified") is False for p in products):
        stats.bump("link_retry_skipped")   # 확인 못 한 링크는 있지만 차단·시간 초과라 재검색 안 함
    timings["retry"] = (time.time() - t) * 1000
    check()
    t = time.time()
    step("finalize", "결과 정리 중")
    polish_result(data)   # 추천 이유 끝맺음을 "~해요/~예요"로
    data["link_retry"] = len(failed)
    data["engine"] = engine
    if engine == "gemini":
        data["engine_model"] = gemini_model()
    data["query"] = q
    data["condition"] = c or None
    data["searched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["elapsed_sec"] = round(time.time() - started, 1)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    data.pop("cost_usd", None)
    timings["finalize"] = (time.time() - t) * 1000
    return data


def read_query():
    """q·c를 읽어서 검사. (q, c, 오류 응답 또는 None)"""
    q = (request.args.get("q") or "").strip()
    c = " ".join((request.args.get("c") or "").split())  # 원하는 조건 (선택)
    if not q:
        return q, c, (jsonify(error="검색어(q)를 입력하세요."), 400)
    if len(q) > 100:
        return q, c, (jsonify(error="검색어가 너무 길어요 (100자 이하)."), 400)
    if len(c) > 100:
        return q, c, (jsonify(error="조건이 너무 길어요 (100자 이하)."), 400)
    return q, c, None


def query_lock(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path.name, threading.Lock())


# ---------- 라우트 ----------

@app.after_request
def cors(resp):
    # GitHub Pages(heejintv.github.io)에서 온 요청만 허용. 다른 사이트는 브라우저가 차단
    if request.headers.get("Origin") == ALLOWED_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        # ngrok 무료 도메인의 경고 페이지를 건너뛰는 헤더를 브라우저가 보낼 수 있게 허용
        # Content-Type: 챗봇(/chat)이 JSON을 POST로 보냄, Authorization: 챗봇에 Supabase 로그인 토큰을 같이 보냄
        resp.headers["Access-Control-Allow-Headers"] = "ngrok-skip-browser-warning, Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST"
        resp.headers["Access-Control-Max-Age"] = "600"
        # GitHub Pages(공개 사이트)에서 http://localhost:5000 을 부를 때 크롬이 요구하는 헤더
        if request.headers.get("Access-Control-Request-Private-Network") == "true":
            resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Vary"] = "Origin"
    return resp


@app.get("/health")
def health():
    return jsonify(ok=True, **limit_info())


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/status.html")
def status_page():
    return send_from_directory(BASE_DIR, "status.html")


@app.get("/admin.html")
def admin_page():
    """화면 틀만 보냄 (데이터는 없음). 숫자는 /admin/stats가 admin에게만 줌."""
    return send_from_directory(BASE_DIR, "admin.html")


@app.get("/stats")
def stats_api():
    """모니터링 요약. 검색 API와 같은 CORS 규칙 + 허용하지 않은 출처는 거절."""
    origin = request.headers.get("Origin")
    if origin and origin not in LOCAL_ORIGINS:
        return jsonify(error="허용되지 않은 출처예요."), 403
    return jsonify(stats.summary())


@app.get("/config.js")
def config_js():
    return send_from_directory(BASE_DIR, "config.js")


@app.get("/intent.js")
def intent_js():
    return send_from_directory(BASE_DIR, "intent.js")


@app.get("/demo/<path:name>")
def demo_files(name):
    return send_from_directory(BASE_DIR / "demo", name)


HISTORY_ID = re.compile(r"^[0-9a-f]{32}$")


@app.get("/history")
def history():
    """cache/에 저장된 검색어 목록 (최근 검색 순). 형식은 demo/index.json과 같다."""
    items = []
    for f in CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not data.get("query") or not isinstance(data.get("products"), list):
            continue
        if not data["products"] and not data.get("condition"):
            continue
        items.append({
            "query": data["query"],
            "condition": data.get("condition"),
            "file": f.name,
            "searched_at": data.get("searched_at"),
            "count": len(data["products"]),
        })
    items.sort(key=lambda m: m["searched_at"] or "", reverse=True)
    return jsonify(items[:30])


@app.get("/history/<name>")
def history_item(name):
    """저장된 검색 결과 하나. claude를 실행하지 않고 횟수도 차감하지 않는다."""
    key = name.removesuffix(".json")
    path = CACHE_DIR / f"{key}.json"
    if not HISTORY_ID.match(key) or not path.exists():
        return jsonify(error="저장된 검색 결과가 없어요."), 404
    data = polish_result(json.loads(path.read_text(encoding="utf-8")))
    data.pop("cost_usd", None)
    data.update(cached=True, **limit_info())
    return jsonify(data)


@app.get("/cached")
def cached():
    """공유 링크용: 검색어+조건의 저장된 결과만 돌려준다. 없으면 404 (새 검색은 절대 하지 않고 횟수도 차감 안 함)."""
    q = (request.args.get("q") or "").strip()
    c = " ".join((request.args.get("c") or "").split())
    if not q or len(q) > 100 or len(c) > 100:
        return jsonify(error="검색어를 확인해 주세요."), 400
    path = cache_path(q, c)
    if not path.exists():
        return jsonify(error="저장된 결과가 없어요.", **limit_info()), 404
    data = polish_result(json.loads(path.read_text(encoding="utf-8")))
    data.pop("cost_usd", None)
    data.update(cached=True, **limit_info())
    return jsonify(data)


@app.get("/search")
def search():
    """새 검색 (회원 전용). 로그인 확인과 하루 한도 확인을 claude 실행 전에 한다."""
    origin = request.headers.get("Origin")
    if origin and origin not in LOCAL_ORIGINS:
        return jsonify(error="허용되지 않은 출처예요."), 403
    user, err = login_user()
    if err:
        return err
    q, c, err = read_query()
    if err:
        return err
    try:
        unlimited = is_unlimited(user)
    except members.MemberError as e:
        app.logger.warning("role check failed: %s", e)
        return jsonify(error=MEMBER_DOWN), 503
    path = cache_path(q, c)
    lock = query_lock(path)

    t0 = time.time()
    timings = {}
    record = lambda **kw: stats.record_request("search", (time.time() - t0) * 1000, query=q, **kw)  # noqa: E731
    done = lambda status, **kw: finish(user, "search", status, t0, query=q, exempt=unlimited, **kw)  # noqa: E731
    with lock:
        timings["cache"] = (time.time() - t0) * 1000
        if path.exists():  # 캐시된 결과는 횟수 차감 없음
            data = polish_result(json.loads(path.read_text(encoding="utf-8")))   # 예전 캐시의 "~있음" 같은 끝맺음 다듬기
            data.pop("cost_usd", None)
            data.update(cached=True, **quota_info(unlimited, "search", None))
            record(cached=True)
            done("cached", engine=data.get("engine"))
            return jsonify(data)

        ticket, err = reserve(user, "search", unlimited)
        if err:
            if err[1] == 429:
                record(limited=True)
                done("limited")
            return err
        left = ticket.remaining if ticket else None

        engine = search_engine()
        try:
            data = perform_search(q, c, path, engine, timings=timings)
        except subprocess.TimeoutExpired:
            record(ok=False, code=504)
            done("timeout", engine=engine, ticket=ticket)
            return jsonify(error=f"검색 시간이 {CLAUDE_TIMEOUT}초를 넘었어요. 다시 시도해 주세요."), 504
        except EngineError as e:  # 엔진(Gemini·claude) 한도 초과·로그인 오류 등은 이유를 화면에 보여줌
            app.logger.warning("search engine failed: %s (%s)", e, e.__cause__)
            record(ok=False, code=e.code)
            done("error", engine=engine, ticket=ticket)
            return jsonify(error=str(e)), 502
        except Exception:  # 실패한 결과는 캐시에 저장하지 않음
            app.logger.exception("search failed")
            record(ok=False)
            done("error", engine=engine, ticket=ticket)
            return jsonify(error="검색 중 오류가 났어요. 서버 로그를 확인하세요."), 502
        data.update(cached=False, **quota_info(unlimited, "search", left))
        log_steps(q, timings)
        record(steps=timings)
        done("ok", engine=engine, ticket=ticket, tokens=data.get("tokens"))
        return jsonify(data)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/search/stream")
def search_stream():
    """새 검색의 진행 단계를 실시간으로 보낸다 (Server-Sent Events).
    이벤트: start{id} → step{step, label, done, total} … → result{검색 결과} | error{error, status} | cancelled
    단계: cache(캐시 확인) → search(AI가 웹 검색 중) → links(링크 확인 중 3/5) → finalize(결과 정리 중)
    취소: POST /search/cancel/<id> 또는 연결을 끊으면 claude를 멈추고 하루 횟수에서 빼지 않는다.
    회원 전용: 로그인·등급 확인은 스트림을 열기 전에 (실패하면 JSON 401/503)."""
    origin = request.headers.get("Origin")
    if origin and origin not in LOCAL_ORIGINS:
        return jsonify(error="허용되지 않은 출처예요."), 403
    user, err = login_user()
    if err:
        return err
    q, c, err = read_query()
    if err:
        return err
    # 요청 정보가 필요한 판단은 여기서 (작업 스레드에는 request가 없음)
    try:
        unlimited = is_unlimited(user)
    except members.MemberError as e:
        app.logger.warning("role check failed: %s", e)
        return jsonify(error=MEMBER_DOWN), 503
    path = cache_path(q, c)
    job = Job()
    _jobs[job.id] = job

    def step(name, label, done=None, total=None):
        job.emit("step", {"step": name, "label": label, "done": done, "total": total})

    def worker():
        t0 = time.time()
        record = lambda **kw: stats.record_request("search", (time.time() - t0) * 1000, query=q, **kw)  # noqa: E731
        ticket = None
        engine = None
        tokens = None
        status = "error"   # 사용 기록에 남길 결과 (ok·cached·limited·cancelled·timeout·error)
        timings = {}
        try:
            with query_lock(path):
                timings["cache"] = (time.time() - t0) * 1000
                step("cache", "캐시 확인")
                if path.exists():
                    data = polish_result(json.loads(path.read_text(encoding="utf-8")))
                    data.pop("cost_usd", None)
                    data.update(cached=True, **quota_info(unlimited, "search", None))
                    record(cached=True)
                    status, engine = "cached", data.get("engine")
                    job.emit("result", data)
                    return
                if job.cancel.is_set():
                    raise Cancelled()
                left = None
                if not unlimited:
                    try:
                        ticket = take_quota(user["id"], "search")   # Supabase 오류면 MemberError
                    except members.LimitReached as e:
                        record(limited=True)
                        status = "limited"
                        job.emit("error", {**limit_body("search", e.scope), "status": 429})
                        return
                    left = ticket.remaining
                engine = search_engine()
                data = perform_search(q, c, path, engine, step, job, timings)
                data.update(cached=False, **quota_info(unlimited, "search", left))
                log_steps(q, timings)
                record(steps=timings)
                status = "ok"
                tokens = data.get("tokens")
                job.emit("result", data)
        except Cancelled:
            status = "cancelled"   # 취소된 검색은 status가 ok가 아니라 하루 횟수에 안 셈
            job.emit("cancelled", {"refunded": ticket is not None})
        except members.MemberError as e:
            app.logger.warning("quota check failed: %s", e)
            job.emit("error", {"error": MEMBER_DOWN, "status": 503})
        except subprocess.TimeoutExpired:
            record(ok=False, code=504)
            status = "timeout"
            job.emit("error", {"error": f"검색 시간이 {CLAUDE_TIMEOUT}초를 넘었어요. 다시 시도해 주세요.", "status": 504})
        except EngineError as e:
            app.logger.warning("search engine failed: %s (%s)", e, e.__cause__)
            record(ok=False, code=e.code)
            job.emit("error", {"error": str(e), "status": 502})
        except Exception:
            app.logger.exception("search stream failed")
            record(ok=False)
            job.emit("error", {"error": "검색 중 오류가 났어요. 서버 로그를 확인하세요.", "status": 502})
        finally:
            finish(user, "search", status, t0, query=q, engine=engine, ticket=ticket, exempt=unlimited, tokens=tokens)
            job.emit(None)   # 끝 표시
            _jobs.pop(job.id, None)

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        finished = False
        try:
            yield sse("start", {"id": job.id})
            while True:
                try:
                    item = job.events.get(timeout=1)
                except queue.Empty:
                    yield ": ping\n\n"   # 연결 유지 + 끊겼는지 확인
                    continue
                if item is None:
                    finished = True
                    return
                yield sse(*item)
        finally:
            if not finished:   # 브라우저가 연결을 끊음 → 검색 취소
                job.cancel.set()

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/search/cancel/<job_id>")
def search_cancel(job_id):
    """진행 중인 스트림 검색 취소."""
    origin = request.headers.get("Origin")
    if origin and origin not in LOCAL_ORIGINS:
        return jsonify(error="허용되지 않은 출처예요."), 403
    job = _jobs.get(job_id)
    if job:
        job.cancel.set()
    return jsonify(ok=bool(job))


@app.post("/chat")
def chat_api():
    """상담 챗봇. 받은 검색 결과 안에서만 답한다 (웹 검색 안 함)."""
    # 다른 사이트가 몰래 보내는 요청 막기: 허용한 출처만, JSON만 받음
    # (JSON POST는 브라우저가 CORS 사전 확인을 하므로 허용 안 된 사이트는 요청 자체가 안 옴)
    origin = request.headers.get("Origin")
    if origin and origin not in LOCAL_ORIGINS:
        return jsonify(error="허용되지 않은 출처예요."), 403
    # 로그인 확인: Gemini를 부르기 전에 막는다. 확인된 사용자는 g.user_id에 넣어 둠
    user, err = login_user()
    if err:
        return err
    if not request.is_json:
        return jsonify(error="JSON으로 보내 주세요."), 415
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="요청 형식이 잘못됐어요."), 400

    question = " ".join(str(body.get("question") or "").split())
    if not question:
        return jsonify(error="질문을 입력하세요."), 400
    if len(question) > chat.MAX_QUESTION:
        return jsonify(error=f"질문이 너무 길어요 ({chat.MAX_QUESTION}자 이하)."), 400
    compare = body.get("mode") == "compare"   # 찜 목록 1:1 비교: 두 상품만 받아서 답함
    if compare:
        results = chat.clean_compare(body.get("results"))
        if results is None:
            return jsonify(error="비교할 상품 2개를 보내주세요."), 400
        history = []   # 비교 질문은 이전 대화와 섞지 않음
    else:
        results = chat.clean_results(body.get("results"))
        if results is None:
            return jsonify(error="먼저 상품을 검색해주세요."), 400
        history = chat.clean_history(body.get("history"))

    kind = "compare" if compare else "chat"
    t0 = time.time()
    record = lambda **kw: stats.record_request(kind, (time.time() - t0) * 1000, **kw)  # noqa: E731
    # 사용 기록의 query에는 질문 원문(300자까지)을 남김 (서비스 개선용, 로그인 창에서 안내)
    topic = question
    try:
        unlimited = is_unlimited(user)
    except members.MemberError as e:
        app.logger.warning("role check failed: %s", e)
        return jsonify(error=MEMBER_DOWN), 503
    ticket, err = reserve(user, "chat", unlimited)
    if err:
        if err[1] == 429:
            record(limited=True)
            finish(user, kind, "limited", t0, query=topic, exempt=unlimited)
        return err
    info = quota_info(unlimited, "chat", ticket.remaining if ticket else None)

    try:
        out = chat.answer(question, results, history, compare)
    except EngineError as e:
        app.logger.warning("chat failed: %s (%s)", e, e.__cause__)
        status = 503 if str(e) == chat.BUSY_MESSAGE else 502
        record(ok=False, code=e.code)
        finish(user, kind, "error", t0, query=topic, engine=chat.chat_engine(), ticket=ticket, exempt=unlimited)
        return jsonify(error=str(e), **info), status
    except Exception:
        app.logger.exception("chat failed")
        record(ok=False)
        finish(user, kind, "error", t0, query=topic, engine=chat.chat_engine(), ticket=ticket, exempt=unlimited)
        return jsonify(error="답변 중 오류가 났어요. 잠시 후 다시 시도해주세요.", **info), 502
    if out.get("reasons_fixed"):
        app.logger.info("chat compare: 이유 %d줄을 일반 장점 문장으로 바꿈", out["reasons_fixed"])
    if out["fallback"]:
        app.logger.info("chat: gemini busy → claude로 재시도함")
    record()
    finish(user, kind, "ok", t0, query=topic, engine=out.get("engine") or chat.chat_engine(),
           ticket=ticket, exempt=unlimited, tokens=out.get("tokens"))
    return jsonify(**out, **info)


@app.post("/chat/action")
def chat_action():
    """챗봇 "문장으로 실행": Gemini 한 번으로 최대 3단계 계획 {"steps": [{tool, args}]}만 돌려줌 (실행은 화면이 함).
    /chat과 같은 검사(출처·로그인·상담 하루 한도)를 거치고, 사용 기록은 상담(chat) 한 번으로 남김."""
    origin = request.headers.get("Origin")
    if origin and origin not in LOCAL_ORIGINS:
        return jsonify(error="허용되지 않은 출처예요."), 403
    user, err = login_user()
    if err:
        return err
    if not request.is_json:
        return jsonify(error="JSON으로 보내 주세요."), 415
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="요청 형식이 잘못됐어요."), 400
    sentence = " ".join(str(body.get("sentence") or "").split())
    if not sentence:
        return jsonify(error="문장을 입력하세요."), 400
    if len(sentence) > actions.MAX_SENTENCE:
        return jsonify(error=f"문장이 너무 길어요 ({actions.MAX_SENTENCE}자 이하)."), 400
    names = actions.clean_names(body.get("names"))

    t0 = time.time()
    record = lambda **kw: stats.record_request("chat", (time.time() - t0) * 1000, **kw)  # noqa: E731
    topic = "[실행] " + sentence
    try:
        unlimited = is_unlimited(user)
    except members.MemberError as e:
        app.logger.warning("role check failed: %s", e)
        return jsonify(error=MEMBER_DOWN), 503
    ticket, err = reserve(user, "chat", unlimited)
    if err:
        if err[1] == 429:
            record(limited=True)
            finish(user, "chat", "limited", t0, query=topic, exempt=unlimited)
        return err
    info = quota_info(unlimited, "chat", ticket.remaining if ticket else None)
    try:
        out = actions.choose(sentence, names)
    except Exception as e:   # Gemini 실패(한도·키·시간 초과 등)는 모두 "버튼으로 해 주세요"
        app.logger.warning("chat action failed: %s (%s)", e, e.__cause__)
        record(ok=False, code=getattr(e, "code", None))
        finish(user, "chat", "error", t0, query=topic, engine="gemini", ticket=ticket, exempt=unlimited)
        return jsonify(error=actions.FAIL_MESSAGE, **info), 502
    record()
    finish(user, "chat", "ok", t0, query=topic, engine="gemini", ticket=ticket, exempt=unlimited,
           tokens=out.pop("tokens", None))
    if not out["steps"]:
        out["message"] = actions.UNKNOWN_MESSAGE
    return jsonify(**out, **info)


@app.get("/me")
def me():
    """로그인한 사람의 등급과 오늘 남은 횟수 (헤더의 "오늘 검색 3/5")."""
    origin = request.headers.get("Origin")
    if origin and origin not in LOCAL_ORIGINS:
        return jsonify(error="허용되지 않은 출처예요."), 403
    user, err = login_user()
    if err:
        return err
    try:
        role = members.get_role(user["id"])
        unlimited = is_local_request() or role == "admin"
        out = {"email": user["email"], "role": role, "unlimited": unlimited}
        for kind, limit in LIMITS.items():
            used = members.quota.used(user["id"], kind)
            out[kind] = {"used": used, "limit": None if unlimited else limit,
                         "remaining": None if unlimited else max(0, limit - used)}
        if not unlimited:   # 서버 전체 새 검색이 다 떨어졌는지 (헤더에 "오늘 검색 소진")
            g_used = members.quota.used(None, "search")
            out["global_search"] = {"used": g_used, "limit": GLOBAL_SEARCH_LIMIT,
                                    "remaining": max(0, GLOBAL_SEARCH_LIMIT - g_used)}
    except members.MemberError as e:
        app.logger.warning("me failed: %s", e)
        return jsonify(error=MEMBER_DOWN), 503
    return jsonify(out)


def require_admin():
    """출처·토큰 확인 → profiles.role이 admin인지 새로 확인. 통과하면 None, 아니면 오류 응답."""
    origin = request.headers.get("Origin")
    if origin and origin not in LOCAL_ORIGINS:
        return jsonify(error="허용되지 않은 출처예요."), 403
    user, err = login_user()
    if err:
        return err
    try:
        role = members.get_role(user["id"], fresh=True)   # 권한이 막 바뀌었어도 바로 반영
    except members.MemberError as e:
        app.logger.warning("admin role check failed: %s", e)
        return jsonify(error=MEMBER_DOWN), 503
    if role != "admin":
        return jsonify(error="관리자만 볼 수 있어요."), 403
    return None


CHECK_TIMEOUT = 60   # 초. /admin/claude-check의 짧은 claude -p 호출


def _run_check(cmd: list[str], timeout: int) -> tuple[bool, str, str]:
    """명령 하나 실행 → (성공, 원인 종류, stdout). 실패는 가린 끝부분만 로그에 남김."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=BASE_DIR)
    except FileNotFoundError:
        return False, "not_installed", ""
    except subprocess.TimeoutExpired:
        return False, "timeout", ""
    ok = proc.returncode == 0
    if ok and "-p" in cmd:
        try:
            ok = not json.loads(proc.stdout).get("is_error")
        except (json.JSONDecodeError, AttributeError):
            ok = False
    if ok:
        return True, "", proc.stdout
    kind = claude_errors.classify(proc.stderr, proc.stdout)
    app.logger.warning("claude-check 실패 (%s, 코드 %s, 종류 %s) stderr=[%s] stdout=[%s]", cmd[1],
                       proc.returncode, kind, claude_errors.tail(proc.stderr), claude_errors.tail(proc.stdout))
    return False, kind, ""


@app.get("/admin/claude-check")
def admin_claude_check():
    """admin 전용: claude --version + 아주 짧은 claude -p 한 번으로 토큰이 살아 있는지 확인.
    답변 내용은 돌려주지 않고 성공/실패와 원인 종류(auth·limit·timeout·other·not_installed)만."""
    err = require_admin()
    if err:
        return err
    out = {}
    ok, kind, text = _run_check([claude_bin(), "--version"], 15)
    m = re.search(r"\d+\.\d+\.\d+", text)
    out["version"] = {"ok": ok, "error": kind or None, "value": m.group(0) if m else None}
    t = time.time()
    ok, kind, _ = _run_check([claude_bin(), "-p", "OK라고만 답해.", "--tools", "", "--output-format", "json"],
                             CHECK_TIMEOUT) if ok else (False, kind, "")
    out["prompt"] = {"ok": ok, "error": kind or None, "ms": round((time.time() - t) * 1000)}
    out["ok"] = out["version"]["ok"] and out["prompt"]["ok"]
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/admin/stats")
def admin_stats_api():
    """관리자 대시보드 데이터. 토큰 확인 → profiles.role이 admin인지 새로 확인 → SERVICE 키로 읽어서 정리.
    ?period=today|7d|30d (기본 today), ?q=이메일 일부 (순위·최근 기록에 적용)"""
    err = require_admin()
    if err:
        return err

    period = request.args.get("period") or "today"
    if period not in admin_stats.PERIODS:
        return jsonify(error="기간은 today, 7d, 30d 중 하나예요."), 400
    q = " ".join((request.args.get("q") or "").split())[:100]
    now = datetime.now(admin_stats.KST)
    try:
        rows, truncated = members.usage_since(admin_stats.fetch_start(now, period).isoformat())
        total = members.count_rows("profiles")
    except members.MemberError as e:
        app.logger.warning("admin stats failed: %s", e)
        return jsonify(error=MEMBER_DOWN), 503
    out = admin_stats.build(rows, total, now, period, q, gemini_limit=GEMINI_DAILY_LIMIT,
                            global_limit=GLOBAL_SEARCH_LIMIT, truncated=truncated)
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"   # 관리자 데이터는 브라우저·중간 서버에 남기지 않음
    return resp


if __name__ == "__main__":
    # 127.0.0.1에서만 받음. 외부 공개는 ngrok 터널이 대신 연결해 줌
    # threaded=True: 검색 하나가 오래 걸려도 다른 요청을 동시에 처리
    # use_reloader=True: .py 파일이 바뀌면 서버 자동 재시작 (.env도 감시)
    # debug=False: 외부에 공개 중이라 디버그 모드(브라우저에서 코드 실행 가능한 디버거)는 끔
    env_file = BASE_DIR / ".env"
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False,
            use_reloader=True, extra_files=[str(env_file)] if env_file.exists() else None)
