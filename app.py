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

PROMPT = """너는 쇼핑 가격비교 검색 엔진이야. 상품: "{q}"

1. 웹 검색으로 이 상품을 찾아. 한국 쇼핑몰(네이버쇼핑, 쿠팡, 다나와, 11번가, G마켓 등)을 우선해.
2. 브랜드·모델별로 서로 다른 상품을 최대 8개 정리해. 각 상품마다:
   - price: 확인한 판매가 숫자(원 단위, 쉼표 없이). currency는 "KRW" 등
   - store: 그 가격을 확인한 쇼핑몰 이름
   - rating: 별점(5점 만점 기준), review_count: 리뷰 수
   - pros / cons: 리뷰나 상품 정보에서 확인한 장점·단점 (각 1~3개, 짧게)
   - url: 실제로 확인한 상품/판매 페이지 링크
3. 확인하지 못한 값은 절대 지어내지 말고 null로 둬. pros/cons를 확인 못 하면 빈 배열.
4. value_pick: 가격 대비 성능이 가장 좋은 모델과 이유, quality_pick: 품질이 가장 좋은 모델과 이유.
   model은 products에 있는 model 이름을 그대로 써. 판단 근거가 부족하면 null.
5. notes: 가격 확인 시점, 주의사항 등 한두 문장 (없으면 null).
"""

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


def cache_path(q: str) -> Path:
    key = hashlib.sha256(normalize(q).encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{key}.json"


def run_claude(q: str) -> dict:
    cmd = [
        "claude", "-p", PROMPT.format(q=q),
        "--tools", WEB_TOOLS,            # 쓸 수 있는 도구를 웹 검색·웹페이지 읽기로만 제한
        "--allowedTools", WEB_TOOLS,     # 그 두 도구는 확인 없이 허용
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
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


@app.get("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(error="검색어(q)를 입력하세요."), 400
    if len(q) > 100:
        return jsonify(error="검색어가 너무 길어요 (100자 이하)."), 400

    path = cache_path(q)
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
            data = run_claude(q)
        except subprocess.TimeoutExpired:
            return jsonify(error=f"검색 시간이 {CLAUDE_TIMEOUT}초를 넘었어요. 다시 시도해 주세요."), 504
        except Exception:  # 실패한 결과는 캐시에 저장하지 않음
            app.logger.exception("search failed")
            return jsonify(error="검색 중 오류가 났어요. 서버 로그를 확인하세요."), 502

        data["query"] = q
        data["searched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data["elapsed_sec"] = round(time.time() - started, 1)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        data.pop("cost_usd", None)
        data.update(cached=False, remaining=left, limit=DAILY_LIMIT)
        return jsonify(data)


if __name__ == "__main__":
    # 127.0.0.1에서만 받음. 외부 공개는 ngrok 터널이 대신 연결해 줌
    # threaded=True: 검색 하나가 오래 걸려도 다른 요청을 동시에 처리
    app.run(host="127.0.0.1", port=5000, threaded=True)
