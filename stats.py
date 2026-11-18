"""서버 모니터링 기록 (/stats, status.html).

logs/stats-YYYY-MM-DD.json 에 날짜별로 저장한다.
- events: 요청 하나당 {t, kind(search/chat/compare), ms, ok, cached, limited, code, query(검색만)}
- engines: 엔진 호출 결과 {claude|gemini: {ok, fail: {429, 503, 504, other}}}
- counters: {fallbacks(Gemini→Claude 재시도), reasons_fixed(비교 답변에서 서버가 바꾼 줄 수)}

개인정보는 저장하지 않는다: 질문 원문·IP·답변은 기록하지 않고 횟수·시간·검색어만 남긴다.
"""

import json
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
KEEP_DAYS = 30          # 이보다 오래된 기록 파일은 지움
MAX_EVENTS = 20000      # 하루 이벤트 상한 (파일이 너무 커지지 않게)
RECENT_SEC = 3600       # 최근 1시간 (그래프·주의 판단)
WARN_ERROR_RATE = 0.2   # 최근 에러율 20% 이상이면 주의
DOWN_STREAK = 3         # 연속 3번 실패면 장애
FAIL_CODES = ("429", "503", "504")

STARTED_AT = time.time()
_lock = threading.Lock()
_day: dict | None = None


def _empty(today: str) -> dict:
    return {"date": today, "events": [],
            "engines": {e: {"ok": 0, "fail": {c: 0 for c in (*FAIL_CODES, "other")}} for e in ("claude", "gemini")},
            "counters": {"fallbacks": 0, "reasons_fixed": 0}}


def _path(day: str) -> Path:
    return LOG_DIR / f"stats-{day}.json"


def _load_today() -> dict:
    """오늘 기록 (메모리에 없으면 파일에서, 날짜가 바뀌면 새로). _lock 안에서 호출."""
    global _day
    today = date.today().isoformat()
    if _day is None or _day["date"] != today:
        try:
            _day = json.loads(_path(today).read_text(encoding="utf-8"))
            if _day.get("date") != today:
                raise ValueError
            base = _empty(today)      # 예전 형식 파일이어도 빠진 칸을 채움
            for k, v in base.items():
                _day.setdefault(k, v)
        except (OSError, ValueError):
            _day = _empty(today)
            _cleanup()
    return _day


def _save() -> None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        tmp = _path(_day["date"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(_day, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_path(_day["date"]))   # 쓰다가 끊겨도 파일이 깨지지 않게
    except OSError:
        pass   # 기록 실패가 서비스에 영향을 주지 않게


def _cleanup() -> None:
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    for f in LOG_DIR.glob("stats-*.json"):
        if f.stem.removeprefix("stats-") < cutoff:
            f.unlink(missing_ok=True)


def code_key(code) -> str:
    return str(code) if str(code) in FAIL_CODES else "other"


# ---------- 기록 ----------

def record_request(kind: str, ms: float, ok: bool = True, cached: bool = False,
                   limited: bool = False, code=None, query: str | None = None) -> None:
    """요청 하나를 기록. limited = 하루 제한에 걸림 (서버 오류로 치지 않음)."""
    ev = {"t": round(time.time(), 1), "kind": kind, "ms": round(ms), "ok": bool(ok)}
    if cached:
        ev["cached"] = True
    if limited:
        ev["limited"] = True
    if not ok and code is not None:
        ev["code"] = code_key(code)
    if query and kind == "search":
        ev["query"] = " ".join(query.split()).lower()[:100]
    with _lock:
        day = _load_today()
        day["events"].append(ev)
        del day["events"][:-MAX_EVENTS]
        _save()


def record_engine(engine: str, ok: bool, code=None) -> None:
    with _lock:
        day = _load_today()
        e = day["engines"].setdefault(engine, {"ok": 0, "fail": {c: 0 for c in (*FAIL_CODES, "other")}})
        if ok:
            e["ok"] += 1
        else:
            k = code_key(code)
            e["fail"][k] = e["fail"].get(k, 0) + 1
        _save()


def bump(counter: str, n: int = 1) -> None:
    if not n:
        return
    with _lock:
        day = _load_today()
        day["counters"][counter] = day["counters"].get(counter, 0) + n
        _save()


# ---------- 요약 (/stats) ----------

def _status(events: list[dict], now: float) -> dict:
    """정상 / 주의(최근 1시간 에러율 20% 이상) / 장애(최근 연속 3번 실패)."""
    real = [e for e in events if not e.get("cached") and not e.get("limited")]   # 실제로 엔진을 쓴 요청
    last = real[-DOWN_STREAK:]
    if len(last) == DOWN_STREAK and not any(e["ok"] for e in last):
        return {"level": "down", "label": "장애", "reason": f"최근 {DOWN_STREAK}번 연속 실패"}
    recent = [e for e in real if now - e["t"] <= RECENT_SEC]
    rate = sum(not e["ok"] for e in recent) / len(recent) if recent else 0
    if recent and rate >= WARN_ERROR_RATE:
        return {"level": "warn", "label": "주의", "reason": f"최근 1시간 에러율 {rate:.0%}", "recent_error_rate": round(rate, 3)}
    reason = f"최근 1시간 에러율 {rate:.0%}" if recent else "최근 1시간 요청 없음"
    return {"level": "ok", "label": "정상", "reason": reason, "recent_error_rate": round(rate, 3)}


def summary() -> dict:
    now = time.time()
    with _lock:
        day = json.loads(json.dumps(_load_today()))   # 복사본으로 계산
    events = day["events"]
    real = [e for e in events if not e.get("cached") and not e.get("limited")]
    searches = [e for e in events if e["kind"] == "search" and not e.get("limited")]
    cache_hits = sum(1 for e in searches if e.get("cached"))
    errors = sum(1 for e in events if not e["ok"])
    counted = [e for e in events if not e.get("limited")]
    top = Counter(e["query"] for e in searches if e.get("query")).most_common(5)
    return {
        "now": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "now_ts": round(now, 1),   # 그래프 기준 시각 (보는 사람의 시간대와 무관)
        "started_at": datetime.fromtimestamp(STARTED_AT).isoformat(timespec="seconds"),
        "uptime_sec": round(now - STARTED_AT),
        "today": {
            "date": day["date"],
            "total": len(counted),
            "search": sum(1 for e in counted if e["kind"] == "search"),
            "chat": sum(1 for e in counted if e["kind"] == "chat"),
            "compare": sum(1 for e in counted if e["kind"] == "compare"),
            "limited": sum(1 for e in events if e.get("limited")),
            "avg_ms": round(sum(e["ms"] for e in real) / len(real)) if real else None,   # 캐시 적중 제외
            "errors": errors,
            "error_rate": round(errors / len(counted), 3) if counted else 0,
            "cache_hits": cache_hits,
            "cache_rate": round(cache_hits / len(searches), 3) if searches else None,
            "fallbacks": day["counters"].get("fallbacks", 0),
            "reasons_fixed": day["counters"].get("reasons_fixed", 0),
        },
        "engines": day["engines"],
        "top_queries": [{"query": q, "count": n} for q, n in top],
        "recent": [{"t": e["t"], "ms": e["ms"], "kind": e["kind"], "ok": e["ok"]}
                   for e in real if now - e["t"] <= RECENT_SEC],
        "status": _status(events, now),
    }
