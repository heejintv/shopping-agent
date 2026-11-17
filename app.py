"""쇼핑 가격비교 서버.

/search?q=상품명 요청이 오면 `claude -p`를 웹 검색 전용으로 실행해서
브랜드·모델별 가격 정보를 JSON으로 받아 돌려준다. 결과는 cache/에 저장한다.

인터넷에 공개할 때를 위한 보호 장치:
- 새 검색(claude 실행)은 하루 DAILY_LIMIT회(기본 10회)까지. 캐시된 결과는 제한 없음
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

def read_usage() -> dict:
    today = date.today().isoformat()
    try:
        usage = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        usage = {}
    if usage.get("date") != today:  # 날짜가 바뀌면 0부터
        usage = {"date": today, "count": 0}
    return usage


def try_use_one() -> int | None:
    """검색 1회를 차감하고 남은 횟수를 돌려준다. 다 썼으면 None."""
    with _usage_lock:
        usage = read_usage()
        if usage["count"] >= DAILY_LIMIT:
            return None
        usage["count"] += 1
        USAGE_FILE.write_text(json.dumps(usage), encoding="utf-8")
        return DAILY_LIMIT - usage["count"]


def remaining() -> int:
    with _usage_lock:
        return max(0, DAILY_LIMIT - read_usage()["count"])


# ---------- 검색 ----------

def normalize(q: str) -> str:
    return " ".join(q.split()).lower()


def cache_path(q: str, c: str = "") -> Path:
    """검색어+조건 조합별 캐시 파일. 조건이 없으면 예전과 같은 키라 기존 캐시를 그대로 쓴다."""
    raw = normalize(q) if not c else normalize(q) + "\n" + normalize(c)
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{key}.json"


def run_claude(q: str, c: str = "") -> dict:
    prompt = PROMPT.format(q=q) + (CONDITION_PROMPT.format(c=c) if c else "")
    cmd = [
        "claude", "-p", prompt,
        "--tools", WEB_TOOLS,            # 쓸 수 있는 도구를 웹 검색·웹페이지 읽기로만 제한
        "--allowedTools", WEB_TOOLS,     # 그 두 도구는 확인 없이 허용
        "--output-format", "json",
        "--json-schema", json.dumps(build_schema(bool(c))),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=BASE_DIR,
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


# ---------- 라우트 ----------

@app.after_request
def cors(resp):
    # GitHub Pages(heejintv.github.io)에서 온 요청만 허용. 다른 사이트는 브라우저가 차단
    if request.headers.get("Origin") == ALLOWED_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        # ngrok 무료 도메인의 경고 페이지를 건너뛰는 헤더를 브라우저가 보낼 수 있게 허용
        resp.headers["Access-Control-Allow-Headers"] = "ngrok-skip-browser-warning"
        resp.headers["Access-Control-Allow-Methods"] = "GET"
        resp.headers["Access-Control-Max-Age"] = "600"
    resp.headers["Vary"] = "Origin"
    return resp


@app.get("/health")
def health():
    return jsonify(ok=True, remaining=remaining(), limit=DAILY_LIMIT)


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
    data.update(cached=True, remaining=remaining(), limit=DAILY_LIMIT)
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
            data.update(cached=True, remaining=remaining(), limit=DAILY_LIMIT)
            return jsonify(data)

        left = try_use_one()
        if left is None:
            return jsonify(error=f"오늘 새 검색 {DAILY_LIMIT}회를 모두 썼어요. 내일 다시 시도하세요.",
                           remaining=0, limit=DAILY_LIMIT), 429

        started = time.time()
        try:
            data = run_claude(q, c)
        except subprocess.TimeoutExpired:
            return jsonify(error=f"검색 시간이 {CLAUDE_TIMEOUT}초를 넘었어요. 다시 시도해 주세요."), 504
        except Exception:  # 실패한 결과는 캐시에 저장하지 않음
            app.logger.exception("search failed")
            return jsonify(error="검색 중 오류가 났어요. 서버 로그를 확인하세요."), 502

        data["query"] = q
        data["condition"] = c or None
        data["searched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data["elapsed_sec"] = round(time.time() - started, 1)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        data.pop("cost_usd", None)
        data.update(cached=False, remaining=left, limit=DAILY_LIMIT)
        return jsonify(data)


if __name__ == "__main__":
    # 127.0.0.1에서만 받음. 외부 공개는 ngrok 터널이 대신 연결해 줌
    # threaded=True: 검색 하나가 오래 걸려도 다른 요청을 동시에 처리
    # use_reloader=True: .py 파일이 바뀌면 서버 자동 재시작 (.env도 감시)
    # debug=False: 외부에 공개 중이라 디버그 모드(브라우저에서 코드 실행 가능한 디버거)는 끔
    env_file = BASE_DIR / ".env"
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False,
            use_reloader=True, extra_files=[str(env_file)] if env_file.exists() else None)
