"""쇼핑 가격비교 서버.

/search?q=상품명 요청이 오면 검색 엔진으로 브랜드·모델별 가격 정보를 JSON으로 받아 돌려준다.
결과는 cache/에 저장한다 (어떤 엔진으로 검색했는지 engine에 기록).

검색 엔진 (환경변수 SEARCH_ENGINE):
- claude (기본): `claude -p`를 웹 검색 전용으로 실행
- gemini: Gemini API + Google 검색 연동 (GEMINI_API_KEY, GEMINI_MODEL 사용, gemini.py)

인터넷에 공개할 때를 위한 보호 장치:
- ngrok을 거쳐 들어온 새 검색(claude 실행)은 하루 DAILY_LIMIT회(기본 10회)까지. 캐시된 결과는 제한 없음
- 내 PC에서 직접 온 요청은 무제한 (is_local_request 참고)
- 상담 챗봇(/chat, chat.py)은 외부 요청만 하루 CHAT_DAILY_LIMIT회(기본 30회)까지
- CORS는 GitHub Pages 주소(ALLOWED_ORIGIN)만 허용
"""

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import chat
from gemini import EngineError, call_gemini
from gemini import model_name as gemini_model
from links import verify_links

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)
USAGE_FILE = BASE_DIR / "usage.json"


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


load_env(BASE_DIR / ".env")

DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "10"))
CHAT_DAILY_LIMIT = int(os.environ.get("CHAT_DAILY_LIMIT", "30"))
ALLOWED_ORIGIN = "https://heejintv.github.io"

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
    """조건이 있으면 상품마다 fit·fit_reason을 필수로 추가한 스키마."""
    if not with_condition:
        return SCHEMA
    schema = json.loads(json.dumps(SCHEMA))  # 깊은 복사
    item = schema["properties"]["products"]["items"]
    item["properties"]["fit"] = {"type": "string", "enum": ["높음", "보통", "낮음"]}
    item["properties"]["fit_reason"] = {"type": "string"}
    item["required"] += ["fit", "fit_reason"]
    return schema

app = Flask(__name__)

# 같은 검색어가 동시에 들어오면 claude를 한 번만 실행하도록 검색어별 잠금
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_usage_lock = threading.Lock()


# ---------- 하루 검색 횟수 ----------

# usage.json: {"date": 오늘, "count": 검색 횟수, "chat_count": 챗봇 횟수}
LIMITS = {"count": lambda: DAILY_LIMIT, "chat_count": lambda: CHAT_DAILY_LIMIT}


def read_usage() -> dict:
    today = date.today().isoformat()
    try:
        usage = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        usage = {}
    if usage.get("date") != today:  # 날짜가 바뀌면 0부터
        usage = {"date": today}
    for k in LIMITS:
        usage.setdefault(k, 0)
    return usage


def try_use_one(kind: str = "count") -> int | None:
    """검색(count) 또는 챗봇(chat_count) 1회를 차감하고 남은 횟수를 돌려준다. 다 썼으면 None."""
    limit = LIMITS[kind]()
    with _usage_lock:
        usage = read_usage()
        if usage[kind] >= limit:
            return None
        usage[kind] += 1
        USAGE_FILE.write_text(json.dumps(usage), encoding="utf-8")
        return limit - usage[kind]


def remaining(kind: str = "count") -> int:
    with _usage_lock:
        return max(0, LIMITS[kind]() - read_usage()[kind])


# ngrok은 요청을 127.0.0.1로 전달하면서 이런 헤더를 붙인다. 하나라도 있으면 외부 요청
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded")
LOCAL_ORIGINS = {ALLOWED_ORIGIN, "http://localhost:5000", "http://127.0.0.1:5000"}


def is_local_request() -> bool:
    """내 PC에서 직접 온 요청이면 True (무제한)."""
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
    """응답에 붙이는 횟수 정보. 무제한이면 remaining은 null."""
    if is_local_request():
        return {"unlimited": True, "remaining": None, "limit": None}
    return {"unlimited": False, "remaining": remaining(), "limit": DAILY_LIMIT}


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


def call_engine(prompt: str, schema: dict, timeout: int, engine: str) -> dict:
    if engine == "gemini":
        return call_gemini(prompt, schema, timeout)
    return call_claude(prompt, schema, timeout)


def call_claude(prompt: str, schema: dict, timeout: int) -> dict:
    """claude -p를 웹 도구만 허용해서 실행하고, 스키마 모양의 결과(dict)를 돌려준다."""
    cmd = [
        "claude", "-p", prompt,
        "--tools", WEB_TOOLS,            # 쓸 수 있는 도구를 웹 검색·웹페이지 읽기로만 제한
        "--allowedTools", WEB_TOOLS,     # 그 두 도구는 확인 없이 허용
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=BASE_DIR,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude 실행 실패 (코드 {proc.returncode}): {proc.stderr[-500:]}")
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude 오류: {out.get('result')}")
    data = out.get("structured_output")
    if data is None:  # 혹시 structured_output이 없으면 result 텍스트를 JSON으로 파싱
        data = json.loads(out["result"])
    data["cost_usd"] = out.get("total_cost_usd")
    return data


def run_search(q: str, c: str, engine: str) -> dict:
    prompt = PROMPT.format(q=q) + (CONDITION_PROMPT.format(c=c) if c else "")
    return call_engine(prompt, build_schema(bool(c)), CLAUDE_TIMEOUT, engine)


def retry_links(failed: list[dict], engine: str) -> float | None:
    """검증 실패한 상품들만 같은 엔진으로 한 번 더 상세 페이지를 찾고 검증한다. 비용(USD)을 돌려준다."""
    items = "\n".join(f"{i}. {p['brand']} {p['model']}" for i, p in enumerate(failed, 1))
    out = call_engine(RETRY_PROMPT.format(items=items), RETRY_SCHEMA, RETRY_TIMEOUT, engine)
    found = {x.get("no"): x.get("url") for x in out.get("links", [])}
    retried = []
    for i, p in enumerate(failed, 1):
        if found.get(i):
            p["url"] = found[i]
            retried.append(p)
    verify_links(retried)  # 다시 받은 주소도 똑같이 검증. 실패하면 링크 확인 안 됨으로 남음
    return out.get("cost_usd")


# ---------- 라우트 ----------

@app.after_request
def cors(resp):
    # GitHub Pages(heejintv.github.io)에서 온 요청만 허용. 다른 사이트는 브라우저가 차단
    if request.headers.get("Origin") == ALLOWED_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        # ngrok 무료 도메인의 경고 페이지를 건너뛰는 헤더를 브라우저가 보낼 수 있게 허용
        # Content-Type: 챗봇(/chat)이 JSON을 POST로 보냄
        resp.headers["Access-Control-Allow-Headers"] = "ngrok-skip-browser-warning, Content-Type"
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


@app.get("/config.js")
def config_js():
    return send_from_directory(BASE_DIR, "config.js")


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
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("cost_usd", None)
    data.update(cached=True, **limit_info())
    return jsonify(data)


@app.get("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(error="검색어(q)를 입력하세요."), 400
    if len(q) > 100:
        return jsonify(error="검색어가 너무 길어요 (100자 이하)."), 400
    c = " ".join((request.args.get("c") or "").split())  # 원하는 조건 (선택)
    if len(c) > 100:
        return jsonify(error="조건이 너무 길어요 (100자 이하)."), 400

    path = cache_path(q, c)
    with _locks_guard:
        lock = _locks.setdefault(path.name, threading.Lock())

    with lock:
        if path.exists():  # 캐시된 결과는 횟수 차감 없음
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("cost_usd", None)
            data.update(cached=True, **limit_info())
            return jsonify(data)

        info = limit_info()
        left = None if info["unlimited"] else try_use_one()
        if not info["unlimited"] and left is None:
            return jsonify(error=f"오늘 새 검색 {DAILY_LIMIT}회를 모두 썼어요. 내일 다시 시도하세요.",
                           unlimited=False, remaining=0, limit=DAILY_LIMIT), 429

        started = time.time()
        engine = search_engine()
        try:
            data = run_search(q, c, engine)
        except subprocess.TimeoutExpired:
            return jsonify(error=f"검색 시간이 {CLAUDE_TIMEOUT}초를 넘었어요. 다시 시도해 주세요."), 504
        except EngineError as e:  # Gemini 한도 초과·키 오류 등은 이유를 화면에 보여줌
            app.logger.warning("gemini search failed: %s (%s)", e, e.__cause__)
            return jsonify(error=str(e)), 502
        except Exception:  # 실패한 결과는 캐시에 저장하지 않음
            app.logger.exception("search failed")
            return jsonify(error="검색 중 오류가 났어요. 서버 로그를 확인하세요."), 502

        # 링크마다 실제 접속해서 확인 → 실패한 상품만 claude로 한 번 더 찾기 → 그래도 실패하면 링크 없음
        failed = verify_links(data.get("products") or [])
        if failed:
            try:
                cost = retry_links(failed, engine)
                if cost and data.get("cost_usd") is not None:
                    data["cost_usd"] = round(data["cost_usd"] + cost, 4)
            except Exception:  # 재시도가 실패해도 검색 결과는 그대로 돌려줌 (해당 상품은 링크 확인 안 됨)
                app.logger.exception("link retry failed")
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
        info["remaining"] = left
        data.update(cached=False, **info)
        return jsonify(data)


@app.post("/chat")
def chat_api():
    """상담 챗봇. 받은 검색 결과 안에서만 답한다 (웹 검색 안 함)."""
    # 다른 사이트가 몰래 보내는 요청 막기: 허용한 출처만, JSON만 받음
    # (JSON POST는 브라우저가 CORS 사전 확인을 하므로 허용 안 된 사이트는 요청 자체가 안 옴)
    origin = request.headers.get("Origin")
    if origin and origin not in LOCAL_ORIGINS:
        return jsonify(error="허용되지 않은 출처예요."), 403
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

    unlimited = is_local_request()
    left = None if unlimited else try_use_one("chat_count")
    if not unlimited and left is None:
        return jsonify(error=f"오늘 챗봇 대화 {CHAT_DAILY_LIMIT}회를 모두 썼어요. 내일 다시 시도하세요.",
                       unlimited=False, remaining=0, limit=CHAT_DAILY_LIMIT), 429
    info = {"unlimited": unlimited, "remaining": left, "limit": None if unlimited else CHAT_DAILY_LIMIT}

    try:
        out = chat.answer(question, results, history, compare)
    except EngineError as e:
        app.logger.warning("chat failed: %s (%s)", e, e.__cause__)
        status = 503 if str(e) == chat.BUSY_MESSAGE else 502
        return jsonify(error=str(e), **info), status
    except Exception:
        app.logger.exception("chat failed")
        return jsonify(error="답변 중 오류가 났어요. 잠시 후 다시 시도해주세요.", **info), 502
    if out["fallback"]:
        app.logger.info("chat: gemini busy → claude로 재시도함")
    return jsonify(**out, **info)


if __name__ == "__main__":
    # 127.0.0.1에서만 받음. 외부 공개는 ngrok 터널이 대신 연결해 줌
    # threaded=True: 검색 하나가 오래 걸려도 다른 요청을 동시에 처리
    # use_reloader=True: .py 파일이 바뀌면 서버 자동 재시작 (.env도 감시)
    # debug=False: 외부에 공개 중이라 디버그 모드(브라우저에서 코드 실행 가능한 디버거)는 끔
    env_file = BASE_DIR / ".env"
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False,
            use_reloader=True, extra_files=[str(env_file)] if env_file.exists() else None)
