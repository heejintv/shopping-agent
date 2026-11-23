"""일일 운영 보고서: 하루(KST, 기본 어제) 집계(aggregates)와 숫자 대조. Python만, AI 없음. (AI 부분은 report_crew.py)

- 데이터: Supabase usage_logs(members.usage_between) + 서버 모니터링 로그(logs/stats-*.json)
- aggregates = {"date", "metrics": [{"id", "label", "value", "unit", "source", "text"?}], "notes"}
  모든 숫자에 출처(source)가 있음. 이메일·user_id는 세는 데만 쓰고 결과에 넣지 않음. 문장·검색어는 개인정보를 가림
- 보고서는 숫자 바로 뒤에 근거를 적음: "검색 12회[search.total]" → check_numbers()가 하나씩 대조
"""

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta

import claude_errors
import stats
from admin_stats import CHAT_ACTIONS, GEMINI_CALLED, KST, _int, _when

ACTION_PREFIX = "[실행] "   # ⚡ 명령 기록의 query 앞머리 (app.chat_action_step)
TOP = 5
TEXT_MAX = 80
FAILED = ("error", "timeout")
FAIL_KINDS = {"token": "로그인·토큰", "limit": "AI 사용량 한도", "timeout": "시간 초과", "server": "AI 서버 장애",
              "db": "회원 DB 연결", "other": "기타(원인 로그 없음 포함)"}
# 잘 안 된 ⚡ 명령: usage_logs.outcome → 이름 (status=error·timeout도 error로 셈)
BAD_OUTCOMES = {"error": "오류", "rejected": "검사(validate) 거부", "ask_cancelled": "되물음 후 취소",
                "approve_cancelled": "승인 카드 취소", "cannot": "할 수 없다는 답변",
                "search_cancelled": "검색 확인에서 취소", "too_many": "반복 한도 초과"}
# ⚡ 기능 그래프에 보일 도구 (결과 찾기·찜 확인·되묻기는 중간 단계라 뺌)
TOOL_NAMES = {"add_favorite": "찜", "remove_favorite": "찜 빼기", "compare_favorites": "비교", "search": "검색",
              "open_favorites": "목록 열기", "logout": "로그아웃"}
# 여러 이상 징후 중 요약의 "할 일"에 쓸 순서 (앞이 더 중요)
PRIORITY = ("rule.fail_rate", "rule.gemini_rate", "rule.limited_users", "rule.avg_sec", "rule.other_rate")
NO_TODO = "특별히 할 일 없음"
CARDS = ("search.total", "action.total", "search.avg_sec", "search.fail_rate")   # 화면 맨 위 숫자 카드
# 요약 1줄(사용량)·2줄(성능)에 있어야 하는 근거
USAGE_KEYS = ("search.total", "search.ok", "search.cached", "chat.total", "action.total", "gemini.calls", "users.limited")
PERF_KEYS = ("search.avg_sec", "search.fail_rate", "search.failed")
# 이상 징후 규칙: (id, 이름, 보는 값, 비교, 기준값, 단위). 바꾸면 report-rules.md의 "이상 징후 기준"도 맞추기
RULES = (
    ("rule.fail_rate", "검색 실패율", "search.fail_rate", ">", 10, "%"),
    ("rule.avg_sec", "새 검색 평균 시간", "search.avg_sec", ">", 90, "초"),
    ("rule.other_rate", "실패 중 기타 비율", "search.other_rate", "≥", 50, "%"),
    ("rule.gemini_rate", "Gemini 하루 한도 사용률", "gemini.usage_rate", ">", 80, "%"),
    ("rule.limited_users", "한도 도달 사용자", "users.limited", "≥", 1, "명"),
)


# ---------- 개인정보 가리기 ----------

_PII = [
    (re.compile(r"https?://\S+|www\.\S+", re.I), "[링크]"),
    (re.compile(r"[\w.+\-]+\s*@\s*[\w\-]+(?:\.[\w\-]+)*"), "[이메일]"),
    (re.compile(r"(?<!\d)\d{6}\s*-\s*[1-8]\d{6}(?!\d)"), "[번호]"),
    (re.compile(r"(?<!\d)(?:\+?82[\s.\-]?0?|0)1[016789][\s.\-]?\d{3,4}[\s.\-]?\d{4}(?!\d)"), "[번호]"),
    (re.compile(r"(?<!\d)0\d{1,2}[\s.\-]\d{3,4}[\s.\-]\d{4}(?!\d)"), "[번호]"),
    (re.compile(r"(?<!\d)\d{4}(?: \d{4}){2,3}(?!\d)"), "[번호]"),
    (re.compile(r"(?<!\d)\d(?:-?\d){8,}(?!\d)"), "[번호]"),   # 카드·계좌처럼 9자리 이상
]


def mask_text(text) -> str:
    text = claude_errors.mask(" ".join(str(text or "").split()))
    for pat, rep in _PII:
        text = pat.sub(rep, text)
    return text[:TEXT_MAX]


# ---------- 수집 ----------

def yesterday(now: datetime) -> tuple[datetime, datetime]:
    """어제 0시 ~ 오늘 0시 (KST)."""
    end = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=1), end


def period(now: datetime, day: date | None = None) -> tuple[datetime, datetime]:
    """day의 0시 ~ 다음 날 0시 (KST, 기본 어제). 오늘이면 지금 시각까지. 미래 날짜면 ValueError."""
    now = now.astimezone(KST)
    if day is None:
        return yesterday(now)
    if day > now.date():
        raise ValueError("미래 날짜는 고를 수 없어요.")
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    return start, now if day == now.date() else start + timedelta(days=1)


def parse_day(text) -> date | None:
    """"2026-11-19" → date, 비었으면 None(어제). 형식이 틀리면 ValueError."""
    if text is None or text == "":
        return None
    if not isinstance(text, str) or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", text):
        raise ValueError("날짜는 2026-11-19 모양으로 보내 주세요.")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError("없는 날짜예요.") from None


def load_events(start: datetime, end: datetime) -> list[dict]:
    """모니터링 로그에서 start~end 사이의 요청. 파일 날짜는 서버 시간대(Render는 UTC)라서 앞뒤 날짜도 읽음."""
    out = []
    for i in (-1, 0, 1):
        day = (start + timedelta(days=i)).date().isoformat()
        try:
            data = json.loads(stats._path(day).read_text(encoding="utf-8"))
            events = data.get("events") or []
            out += [e for e in events if isinstance(e, dict) and isinstance(e.get("t"), (int, float))
                    and start.timestamp() <= e["t"] < end.timestamp()]
        except (OSError, ValueError, AttributeError):
            continue
    return out


def _failure_kinds(failed: list[dict], events: list[dict]) -> dict:
    """실패 검색을 종류별로. usage_logs.error_kind가 있으면 그 값, 없는 예전 기록은 모니터링 로그의 코드로 맞춤.
    둘 다 없으면 기타. 합계는 항상 실패 수와 같음."""
    out = Counter("timeout" if r.get("status") == "timeout" else r["error_kind"] if r.get("error_kind") in FAIL_KINDS
                  else "" for r in failed)
    left = out.pop("", 0)   # 종류를 모르는 예전 기록
    codes = Counter(str(e.get("code")) for e in events if e.get("kind") == "search" and e.get("ok") is False)
    for kind, code in (("token", "401"), ("limit", "429"), ("timeout", "504"), ("server", "503")):
        n = min(left, max(0, codes[code] - out[kind]))   # error_kind로 이미 센 것은 빼고
        out[kind] += n
        left -= n
    out["other"] += left
    return {k: out[k] for k in FAIL_KINDS}


def _rounded(exact: float, rule: str) -> float:
    """소수 1자리. 반올림 때문에 기준값과 같아 보이면 더 자세히 (10.04% > 기준 10%)."""
    base = next(r[4] for r in RULES if r[0] == rule)
    v = round(exact, 1)
    return round(exact, 4) if v == base and exact != base else v


def judge(metrics: list[dict]) -> list[dict]:
    """이상 징후 규칙 판정. 값이 없는 규칙(기록 없음·한도 설정 없음)은 triggered=None."""
    known = {m["id"]: m["value"] for m in metrics}
    out = []
    for rid, label, mid, op, base, unit in RULES:
        v = known.get(mid)
        hit = None if v is None else v > base if op == ">" else v >= base
        out.append({"id": rid, "label": label, "metric": mid, "op": op, "threshold": base, "value": v, "unit": unit,
                    "triggered": hit, "text": f"{label} {v}{unit}[{mid}] {op} 기준 {base}{unit}[{rid}]" if hit else None})
    return out


def build_charts(rows: list[dict], searches, chats, cmds, metrics: list[dict], last_hour: int) -> dict:
    """화면 그래프 값 (AI에는 보내지 않음)."""
    def hourly(rs):
        n = Counter(_when(r).hour for r in rs)
        return [n[h] for h in range(last_hour + 1)]

    ms = [r["duration_ms"] for r in searches if r.get("status") == "ok" and isinstance(r.get("duration_ms"), (int, float))]
    tools = Counter(t for r in cmds if isinstance(r.get("tools"), list) for t in r["tools"] if t in TOOL_NAMES)
    bars = lambda pairs: [{"label": k, "value": n} for k, n in pairs]   # noqa: E731
    return {
        "hourly": {"title": "시간대별 사용량", "unit": "회", "source": "usage_logs created_at(KST), action",
                   "labels": [f"{h}시" for h in range(last_hour + 1)],
                   "series": [{"name": "검색", "values": hourly(searches)}, {"name": "상담", "values": hourly(chats)},
                              {"name": "⚡", "values": hourly(cmds)}]},
        "queries": {"title": "인기 검색어 TOP 5", "unit": "회", "source": "query.top1~5",
                    "bars": bars((m["text"], m["value"]) for m in metrics if m["id"].startswith("query.top"))},
        "tools": {"title": "많이 쓴 ⚡ 기능", "unit": "회", "source": "usage_logs tools",
                  "bars": bars((TOOL_NAMES[t], n) for t, n in tools.most_common()),
                  "missing": sum(1 for r in cmds if r.get("status") == "ok" and not isinstance(r.get("tools"), list))},
        "latency": {"title": "새 검색 응답 시간 분포", "unit": "회", "source": "usage_logs action=search, status=ok, duration_ms",
                    "bars": bars((("30초 미만", sum(v < 30000 for v in ms)),
                                  ("30~60초", sum(30000 <= v < 60000 for v in ms)),
                                  ("60초 이상", sum(v >= 60000 for v in ms))))},
    }


def build(rows: list[dict], events: list[dict], now: datetime, truncated: bool = False, gemini_limit: int = 0,
          day: date | None = None) -> dict:
    start, end = period(now, day)
    label = f"오늘 {end.hour}시 기준 (진행 중)" if start.date() == now.astimezone(KST).date() else None
    rows = [r for r in rows if (w := _when(r)) is not None and start <= w < end]
    searches = [r for r in rows if r.get("action") == "search"]
    talk = [r for r in rows if r.get("action") in CHAT_ACTIONS]
    cmds = [r for r in talk if str(r.get("query") or "").startswith(ACTION_PREFIX)]
    chats = [r for r in talk if r not in cmds]
    by = lambda rs, *st: sum(1 for r in rs if r.get("status") in st)   # noqa: E731
    failed = [r for r in searches if r.get("status") in FAILED]
    ok = by(searches, "ok")
    ms = [r["duration_ms"] for r in searches if r.get("status") == "ok" and isinstance(r.get("duration_ms"), (int, float))]
    kinds = _failure_kinds(failed, events)
    gem = [r for r in rows if r.get("engine") == "gemini" and r.get("status") in GEMINI_CALLED]

    metrics = []

    def add(mid, label, value, unit, source, **kw):
        metrics.append({"id": mid, "label": label, "value": value, "unit": unit, "source": source, **kw})

    s = "usage_logs action=search"
    log = "logs/stats-*.json events.code"
    add("search.total", "검색 요청 수", len(searches), "회", s)
    add("search.ok", "새 검색 성공", ok, "회", f"{s}, status=ok")
    add("search.cached", "저장된 결과로 응답", by(searches, "cached"), "회", f"{s}, status=cached")
    add("search.failed", "검색 실패", len(failed), "회", f"{s}, status=error·timeout")
    add("search.limited", "한도 초과로 막힌 검색", by(searches, "limited"), "회", f"{s}, status=limited")
    add("search.cancelled", "취소된 검색", by(searches, "cancelled"), "회", f"{s}, status=cancelled")
    add("search.fail_rate", "검색 실패율 (실패 ÷ (성공+실패))",
        _rounded(len(failed) * 100 / (ok + len(failed)), "rule.fail_rate") if ok + len(failed) else None, "%",
        "계산: search.failed ÷ (search.ok + search.failed)")
    for kind, name in FAIL_KINDS.items():
        add(f"search.fail_{kind}", f"실패: {name}", kinds[kind], "회", f"usage_logs error_kind={kind}, 없으면 {log}")
    add("search.other_rate", "실패 중 기타 비율",
        _rounded(kinds["other"] * 100 / len(failed), "rule.other_rate") if failed else None, "%",
        "계산: search.fail_other ÷ search.failed")
    add("search.avg_sec", "새 검색 평균 시간", _rounded(sum(ms) / len(ms) / 1000, "rule.avg_sec") if ms else None, "초",
        f"{s}, status=ok, duration_ms 평균")
    add("chat.total", "상담·비교 요청 수 (⚡ 제외)", len(chats), "회", "usage_logs action=chat·compare")
    add("chat.failed", "상담·비교 실패", by(chats, *FAILED), "회", "usage_logs action=chat·compare, status=error·timeout")
    c = 'usage_logs action=chat, query가 "[실행]"으로 시작'
    add("action.total", "⚡ 명령 수", len(cmds), "회", c)
    add("action.failed", "⚡ 명령 실패", by(cmds, *FAILED), "회", f"{c}, status=error·timeout")
    top = Counter(t for r in cmds if r.get("status") in FAILED
                  and (t := mask_text(str(r["query"])[len(ACTION_PREFIX):])))
    for i, (text, n) in enumerate(top.most_common(TOP), 1):
        add(f"action.failed_top{i}", f"⚡ 실패 명령 {i}위", n, "회", f"{c}, status=error·timeout, 문장별 수", text=text)
    result = lambda r: "error" if r.get("status") in FAILED else r.get("outcome")   # noqa: E731
    bad = [r for r in cmds if result(r) in BAD_OUTCOMES]
    o = f"{c}, outcome"
    add("action.bad", "잘 안 된 ⚡ 명령", len(bad), "회", f"{o} (status=error·timeout 포함)")
    for kind, name in BAD_OUTCOMES.items():
        add(f"action.bad_{kind}", f"잘 안 된 ⚡ 명령: {name}", sum(1 for r in bad if result(r) == kind), "회", f"{o}={kind}")
    add("action.unfinished", "⚡ 도중에 떠났거나 진행 중인 명령", sum(1 for r in cmds if result(r) == "waiting"), "회", f"{o}=waiting")
    texts: dict[str, Counter] = {}
    for r in bad:
        if t := mask_text(str(r["query"])[len(ACTION_PREFIX):]):
            texts.setdefault(t, Counter())[BAD_OUTCOMES[result(r)]] += 1
    for i, (text, why) in enumerate(sorted(texts.items(), key=lambda x: -x[1].total())[:TOP], 1):
        add(f"action.bad_top{i}", f"잘 안 된 ⚡ 명령 {i}위 ({', '.join(k for k, _ in why.most_common())})", why.total(), "회",
            f"{o}, 문장별 수", text=text)
    top = Counter(t for r in searches if r.get("status") in ("ok", "cached")
                  and (t := mask_text(str(r.get("query") or "").lower())))
    for i, (text, n) in enumerate(top.most_common(TOP), 1):
        add(f"query.top{i}", f"인기 검색어 {i}위", n, "회", f"{s}, status=ok·cached, 검색어별 수", text=text)
    add("users.limited", "한도 도달 사용자 수", len({r.get("user_id") for r in rows
        if r.get("status") == "limited" and r.get("user_id")}), "명", "usage_logs status=limited, 서로 다른 user_id 수")
    add("gemini.calls", "Gemini 사용 요청 수", len(gem), "회", "usage_logs engine=gemini, status=ok·error·timeout")
    add("gemini.tokens", "Gemini 토큰", sum(_int(r.get("tokens")) for r in gem), "개", "usage_logs engine=gemini, tokens 합계")
    if gemini_limit:
        add("gemini.limit", "Gemini 하루 한도", gemini_limit, "회", "환경변수 GEMINI_DAILY_LIMIT")
        add("gemini.usage_rate", "Gemini 하루 한도 사용률", _rounded(len(gem) * 100 / gemini_limit, "rule.gemini_rate"), "%",
            "계산: gemini.calls ÷ gemini.limit")
    anomalies = judge(metrics)
    for a in anomalies:
        add(a["id"], f"기준: {a['label']}", a["threshold"], a["unit"], "이상 징후 규칙 (daily_report.RULES)")

    notes = ["⚡ 명령 하나는 Gemini를 여러 번 불러도 사용 요청 한 건으로 집계됨"]
    if label:
        notes.append("하루가 끝나지 않아 지금 시각까지만 집계됨 (다른 날과 바로 비교할 수 없음)")
    if truncated:
        notes.append("기록이 너무 많아 일부만 집계됨")
    if any(r.get("status") == "error" and r.get("error_kind") not in FAIL_KINDS for r in failed):
        notes.append("실패 종류를 저장하지 않은 기록이 있어, 모니터링 로그로 못 찾은 원인은 기타로 집계됨")
    if any(r.get("status") == "ok" and not r.get("outcome") for r in cmds):
        notes.append("결과 종류(outcome)가 없는 ⚡ 기록이 있어 잘 안 된 명령이 실제보다 적게 집계될 수 있음")
    if not gemini_limit:
        notes.append("GEMINI_DAILY_LIMIT이 없어 Gemini 사용률은 판정하지 않음")
    return {"date": start.date().isoformat(), "label": label, "generated_at": now.astimezone(KST).isoformat(timespec="seconds"),
            "metrics": metrics, "anomalies": anomalies, "top_anomaly": top_anomaly(anomalies),
            "notes": notes,
            "no_usage": not (searches or talk), "cards": list(CARDS),
            "charts": build_charts(rows, searches, chats, cmds, metrics, end.hour if label else 23)}


# ---------- 근거에서 원본 열기 ----------

EVIDENCE_MAX = 10
EVIDENCE_TEXT = 30
TIME_KEYS = ("search.avg_sec", "rule.avg_sec")   # 걸린 시간 칸을 보여 줄 숫자
_ACTION_NAMES = {"search": "검색", "chat": "상담", "compare": "비교"}


def _is_cmd(r: dict) -> bool:
    return r.get("action") in CHAT_ACTIONS and str(r.get("query") or "").startswith(ACTION_PREFIX)


def _cmd_result(r: dict):
    return "error" if r.get("status") in FAILED else r.get("outcome")


def _cmd_text(r: dict) -> str:
    return mask_text(str(r.get("query") or "")[len(ACTION_PREFIX):])


def _evidence_filter(key: str, text: str | None):
    """근거 키 → 그 숫자에 들어간 기록인지 보는 함수. 원본 기록이 없는 키(설정값 등)면 None."""
    key = next((r[2] for r in RULES if r[0] == key), key)   # 기준값(rule.*)은 그 규칙이 본 값의 기록
    search = lambda r: r.get("action") == "search"   # noqa: E731
    st = lambda r, *s: r.get("status") in s   # noqa: E731
    cmd, chat = _is_cmd, lambda r: r.get("action") in CHAT_ACTIONS and not _is_cmd(r)
    gem = lambda r: r.get("engine") == "gemini" and st(r, *GEMINI_CALLED)   # noqa: E731
    kind = lambda r: "timeout" if st(r, "timeout") else r.get("error_kind") if r.get("error_kind") in FAIL_KINDS else None   # noqa: E731
    table = {
        "search.total": search, "search.ok": lambda r: search(r) and st(r, "ok"),
        "search.cached": lambda r: search(r) and st(r, "cached"), "search.failed": lambda r: search(r) and st(r, *FAILED),
        "search.limited": lambda r: search(r) and st(r, "limited"),
        "search.cancelled": lambda r: search(r) and st(r, "cancelled"),
        "search.fail_rate": lambda r: search(r) and st(r, "ok", *FAILED),
        "search.other_rate": lambda r: search(r) and st(r, *FAILED),
        "search.avg_sec": lambda r: search(r) and st(r, "ok") and isinstance(r.get("duration_ms"), (int, float)),
        "chat.total": chat, "chat.failed": lambda r: chat(r) and st(r, *FAILED),
        "action.total": cmd, "action.failed": lambda r: cmd(r) and st(r, *FAILED),
        "action.bad": lambda r: cmd(r) and _cmd_result(r) in BAD_OUTCOMES,
        "action.unfinished": lambda r: cmd(r) and _cmd_result(r) == "waiting",
        "users.limited": lambda r: st(r, "limited") and bool(r.get("user_id")),
        "gemini.calls": gem, "gemini.tokens": gem, "gemini.usage_rate": gem,
    }
    for k in FAIL_KINDS:   # 종류를 모르는 예전 기록은 기타에서 보여 줌
        table[f"search.fail_{k}"] = lambda r, k=k: search(r) and st(r, *FAILED) and (kind(r) or "other") == k
    for k in BAD_OUTCOMES:
        table[f"action.bad_{k}"] = lambda r, k=k: cmd(r) and _cmd_result(r) == k
    if key in table:
        return table[key]
    if not text:
        return None
    if re.fullmatch(r"action\.failed_top[1-5]", key):
        return lambda r: cmd(r) and st(r, *FAILED) and _cmd_text(r) == text
    if re.fullmatch(r"action\.bad_top[1-5]", key):
        return lambda r: cmd(r) and _cmd_result(r) in BAD_OUTCOMES and _cmd_text(r) == text
    if re.fullmatch(r"query\.top[1-5]", key):
        return lambda r: search(r) and st(r, "ok", "cached") and mask_text(str(r.get("query") or "").lower()) == text
    return None


def evidence(rows: list[dict], key: str, start: datetime, end: datetime, text: str | None = None) -> dict | None:
    """숫자를 만든 원본 기록 최대 10건 (최신순, 개인정보 없음). 원본이 없는 키면 None."""
    match = _evidence_filter(key, text)
    if match is None:
        return None
    hit = sorted(((w, r) for r in rows if (w := _when(r)) is not None and start <= w < end and match(r)),
                 key=lambda x: x[0], reverse=True)

    timed = key in TIME_KEYS

    def view(w, r):
        shown = _cmd_text(r) if _is_cmd(r) else mask_text(r.get("query"))   # 가린 다음 자름
        return {"time": w.isoformat(timespec="seconds"), "kind": "⚡ 명령" if _is_cmd(r) else _ACTION_NAMES.get(r.get("action"), "기타"),
                "status": r.get("status"), "detail": FAIL_KINDS.get(r.get("error_kind")) or BAD_OUTCOMES.get(r.get("outcome")),
                "text": shown[:EVIDENCE_TEXT], **({"sec": round(r["duration_ms"] / 1000, 1)} if timed else {})}
    return {"total": len(hit), "timed": timed, "rows": [view(w, r) for w, r in hit[:EVIDENCE_MAX]]}


def evidence_period(day: date, until: str | None, now: datetime) -> tuple[datetime, datetime]:
    """보고서가 집계한 기간. until: 진행 중 보고서를 만든 시각(그날 안이어야 함). 틀리면 ValueError."""
    start, end = period(now, day)
    if until:
        try:
            u = datetime.fromisoformat(str(until))
        except ValueError:
            raise ValueError("기준 시각 형식이 잘못됐어요.") from None
        if u.tzinfo is None or not start <= u <= end:
            raise ValueError("기준 시각이 보고서 날짜와 맞지 않아요.")
        end = min(end, u + timedelta(seconds=1))   # 초 아래는 잘려서 저장됨
    return start, end


# ---------- 숫자 불일치 ----------

MISMATCH_MAX = 20


def mismatches(checks: list[dict], agg: dict) -> list[dict]:
    """틀린 항목: 초안 몇 → 실제 몇 (실행 로그·화면용)."""
    unit = {m["id"]: m["unit"] for m in agg["metrics"]}
    where = {"summary": "요약", "anomalies": "이상 징후", "suggestions": "개선 제안"}
    out = []
    for c in checks:
        if c["ok"]:
            continue
        none = "근거 없음" if c["label"] is None else "기록 없음"
        out.append({"item": c["label"] or mask_text(c["id"]) or "근거 없는 숫자", "draft": c["number"] or "숫자 없음",
                    "actual": "해당 없음" if not c["number"] else none if c["expected"] is None else c["expected"], "unit": unit.get(c["id"], ""),
                    "why": c["reason"], "section": where[c["section"]], "line": c["line"]})
    return out[:MISMATCH_MAX]


def has_mismatch(report: dict) -> bool:
    return any(not c.get("ok") for c in report.get("checks") or [])


# ---------- 실행 로그 (report_runs) ----------

RUN_REASONS = {"no_data": "자료 없음", "gemini": "Gemini 오류", "gemini_key": "Gemini 키 오류", "token": "토큰 오류",
               "exists": "이미 있음", "busy": "다른 보고서를 만드는 중", "db": "Supabase 오류", "save": "저장 실패",
               "crewai": "CrewAI 없음", "mismatch": "숫자 불일치", "other": "기타 오류"}
RUN_STATUS = {"ok": "성공", "failed": "실패", "skipped": "건너뜀"}


def run_summary(latest: dict | None, last_ok: dict | None, now: datetime, below: bool = True) -> dict:
    """"가장 최근 실행" 표시 {text, warn}. 최근 시도가 마지막 성공과 다르면 warn (이미 있어서 건너뛴 것은 정상).
    below: 카드 아래에 마지막 성공본이 보이는지 (자동 보고서만)."""
    def when(run):
        try:
            w = datetime.fromisoformat(str(run.get("at")).replace("Z", "+00:00")).astimezone(KST)
        except ValueError:
            return "시각 모름", "이전"
        ago = (now.astimezone(KST).date() - w.date()).days
        name = {0: "오늘", 1: "어제"}.get(ago, f"{w.month}월 {w.day}일")
        return (f"{w:%H:%M}" if ago == 0 else f"{name} {w:%H:%M}"), name

    if not latest:
        return {"text": "실행 기록이 없어요", "warn": False}
    text = f"{when(latest)[0]} {RUN_STATUS.get(latest.get('status'), '알 수 없음')}"
    if latest.get("reason"):
        text += f" · {RUN_REASONS.get(latest['reason'], RUN_REASONS['other'])}"
    detail = latest.get("detail")
    if latest.get("reason") == "mismatch" and isinstance(detail, list) and detail and isinstance(detail[0], dict):
        d = detail[0]
        text += f" ({d.get('item')} {d.get('draft')} → {d.get('actual')}{f' 외 {len(detail) - 1}건' if len(detail) > 1 else ''})"
    same = bool(last_ok) and last_ok.get("id") == latest.get("id")
    warn = not same and not (latest.get("status") == "skipped" and latest.get("reason") == "exists")
    if warn:
        day = when(last_ok)[1] if last_ok else None
        text += (f" → 아래는 {day} 성공본" if below else f" → 마지막 성공은 {day}") if day else " → 성공한 보고서 없음"
    return {"text": text, "warn": warn}


# ---------- 초안 정리·숫자 대조 ----------

SECTIONS = {"summary": 3, "anomalies": 6, "suggestions": 6}   # 이름: 최대 줄 수


def clean_draft(data: dict, no_usage: bool = False) -> dict:
    """AI가 준 JSON → {summary, anomalies, suggestions} 글자 목록. 요약이 3줄이 아니면 ValueError."""
    out = {}
    for name, n in SECTIONS.items():
        v = data.get(name) if isinstance(data, dict) else None
        lines = v if isinstance(v, list) else [v] if isinstance(v, str) else []
        out[name] = [" ".join(str(x).split())[:400] for x in lines if isinstance(x, (str, int, float)) and str(x).strip()][:n]
    if no_usage:   # "사용 기록이 없는 날이에요" 한 줄 (내용은 no_usage_problems에서 확인)
        return out
    if len(out["summary"]) != SECTIONS["summary"]:
        raise ValueError("요약이 3줄이 아니에요.")
    return out


_TOKEN = re.compile(r"\[([a-z]+\.[a-z0-9_]+)\]|(\d[\d,]*(?:\.\d+)?)")
# 내부 키(aggregates id 모양). 바른 근거 "[search.total]"는 첫 갈래에서 건너뜀
_KEY = re.compile(r"\[[a-z]+\.[a-z0-9_]+\]|(\[[^\[\]]*\])|"
                  r"((?<![A-Za-z0-9_.])(?:search|chat|action|query|users|gemini|rule)(?:\.[a-z0-9_]+)+)", re.I)
_RANK = re.compile(r"\s?위(?![해치험한원])")   # "1위", "1위는" ("3 위치"는 아님)
_GAP = re.compile(r"[^\d\[\]\n]{0,4}")   # 숫자와 근거 사이에는 단위("회", "초", " %")만
# 숫자로 쓰지 않고 한글로 쓴 수는 대조할 수 없으므로 틀린 것으로 봄
_HANGUL_NUM = re.compile(r"(?<![가-힣])(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|스무|수십|수백)\s?(?:번|건|회|명|개|배)")


def missing_anomalies(draft: dict, agg: dict) -> list[str]:
    """판정된 이상 징후 중 초안의 anomalies에 (실제값·기준값 근거를 한 줄에 같이) 안 쓴 것."""
    return [f"이상 징후 빠짐: {a['text']}" for a in agg.get("anomalies") or [] if a["triggered"]
            and not any(f"[{a['metric']}]" in line and f"[{a['id']}]" in line for line in draft["anomalies"])]


def top_anomaly(anomalies: list[dict]) -> dict | None:
    """판정된 이상 징후 중 가장 중요한 것 {id, label} (없으면 None)."""
    hit = {a["id"]: a for a in anomalies if a["triggered"]}
    return next(({"id": i, "label": hit[i]["label"]} for i in PRIORITY if i in hit), None)


def _numbers(line: str, date_text: str) -> list[tuple[float, str | None]]:
    """줄에 쓴 숫자와 바로 뒤의 근거 키 (없으면 None)."""
    line = line.replace(date_text, "")
    tokens = list(_TOKEN.finditer(line))
    out = []
    for i, t in enumerate(tokens):
        if t.group(2):
            n = tokens[i + 1] if i + 1 < len(tokens) else None
            key = n.group(1) if n and n.group(1) and _GAP.fullmatch(line[t.end():n.start()]) else None
            out.append((float(t.group(2).replace(",", "")), key))
    return out


def bad_todo(draft: dict, agg: dict) -> list[str]:
    """요약 3번째 줄: 이상 징후가 없으면 NO_TODO, 있으면 할 일 문장. 2번째 줄 숫자를 반복하면 안 됨."""
    second, third = draft["summary"][1:3]
    top = top_anomaly(agg["anomalies"])
    plain = third.strip().rstrip(". ")
    if not top:
        return [] if plain == NO_TODO else [f'summary 3번째 줄: 이상 징후가 없으니 "{NO_TODO}"이라고만 써야 함']
    out = [f"summary 3번째 줄: 이상 징후({top['label']})가 있으니 할 일을 써야 함"] if plain == NO_TODO else []
    used = _numbers(second, agg["date"])
    keys, values = {k for _, k in used if k}, {v for v, _ in used}
    for v, k in _numbers(third, agg["date"]):
        if k in keys or (k is None and v in values):
            out.append(f"summary 3번째 줄: 2번째 줄 숫자({v:,g})를 반복함 (할 일 문장만 쓰기)")
    return out


def bad_summary(draft: dict, agg: dict) -> list[str]:
    """요약 순서(사용량 → 성능 → 할 일)와 잘 안 된 명령을 요약에 쓰지 않았는지."""
    lines, known = draft["summary"], {m["id"]: m["value"] for m in agg["metrics"]}
    cited = [set(re.findall(r"\[([a-z]+\.[a-z0-9_]+)\]", line)) for line in lines]
    out = []
    if not cited[0] & set(USAGE_KEYS):
        out.append("summary 1번째 줄: 사용량(검색·상담·⚡ 명령 수)이 근거와 함께 있어야 함")
    if any(known.get(k) is not None for k in PERF_KEYS[:2]) and not cited[1] & set(PERF_KEYS):
        out.append("summary 2번째 줄: 성능(평균 응답 시간·실패율)이 근거와 함께 있어야 함")
    out += [f"summary {i}번째 줄: 잘 안 된 명령은 이상 징후·개선 제안에만 쓰기" for i, c in enumerate(cited, 1)
            if any(k.startswith(("action.bad", "action.failed")) for k in c)]
    return out + bad_todo(draft, agg)


NO_USAGE_DRAFT = {"summary": ["사용 기록이 없는 날이에요"], "anomalies": ["특이 사항 없음"], "suggestions": ["제안 없음"]}
_SECTION_NAMES = {"summary": "요약", "anomalies": "이상 징후", "suggestions": "개선 제안"}


def no_usage_problems(draft: dict) -> list[str]:
    """사용 기록이 없는 날의 초안이 정해진 문장만 썼는지 (추측 금지)."""
    return [f"{name}({_SECTION_NAMES[name]}): 사용 기록이 없는 날이라 \"{want[0]}\" 한 줄만 써야 함 (추측 금지)"
            for name, want in NO_USAGE_DRAFT.items() if [x.rstrip(". ") for x in draft[name]] != want]


NO_SUGGESTION = "제안 없음"


def bad_suggestions(draft: dict) -> list[str]:
    """"현상 → 할 일" 형식이 아닌 개선 제안."""
    return [f"suggestions {i}번째 줄: '현상 → 할 일' 형식이 아님" for i, line in enumerate(draft["suggestions"], 1)
            if line.rstrip(". ") != NO_SUGGESTION and not re.fullmatch(r"\s*[^→\s][^→]*→\s*[^→\s][^→]*", line)]


def check_numbers(draft: dict, agg: dict) -> list[dict]:
    """초안의 숫자마다 {section, line, number, id, label, expected, source, ok, reason}."""
    known = {m["id"]: m for m in agg["metrics"]}
    texts = sorted({m["text"] for m in agg["metrics"] if m.get("text")}, key=len, reverse=True)
    out = []
    for section in SECTIONS:
        for i, line in enumerate(draft[section], 1):
            text = line.replace(agg["date"], "날짜")
            if agg.get("label"):   # "오늘 15시 기준 (진행 중)"의 시각은 집계 숫자가 아님
                text = text.replace(agg["label"], "기준 시각")
            for t in texts:   # 따옴표로 인용한 검색어·명령 문장 속 숫자("아이폰 16")는 집계 숫자가 아님
                for a, b in ('""', "''", "“”", "‘’", "「」"):
                    text = text.replace(a + t + b, "인용")
            def add(number, mid, reason):
                m = known.get(mid)
                out.append({"section": section, "line": i, "number": number, "id": mid,
                            "label": m["label"] if m else None, "expected": m["value"] if m else None,
                            "source": m["source"] if m else None, "ok": not reason, "reason": reason})

            def key(m):
                """대괄호 밖의 키는 숫자로 세지 않고 따로 지적."""
                if m.group(2):
                    add("", m.group(2), "내부 키 노출 (문장에는 한국어 이름만, 키는 대괄호 근거 안에만)")
                    return "키"
                if m.group(1) and _KEY.search(m.group(1)[1:-1]):   # "[search.ok, search.total]"
                    add("", m.group(1)[:60], "근거 형식이 잘못됨 (대괄호 하나에 키 하나)")
                    return "키"
                return m.group(0)

            tokens = list(_TOKEN.finditer(text := _KEY.sub(key, text)))
            for j, tok in enumerate(tokens):
                if tok.group(1):
                    if tok.group(1) not in known:
                        add("", tok.group(1), "aggregates에 없는 근거")
                    continue
                number = tok.group(2)
                if _RANK.match(text, tok.end()):   # "1위": 같은 줄의 근거 키 순위(action.bad_top1 등)와 맞는지
                    key_id = next((t.group(1) for t in tokens if t.group(1) in known
                                   and re.search(r"top0*" + re.escape(number) + "$", t.group(1))), None)
                    if key_id:
                        out.append({"section": section, "line": i, "number": f"{number}위", "id": None, "label": "순위",
                                    "expected": number, "source": f"같은 줄의 근거 [{key_id}]", "ok": True, "reason": None})
                        continue
                nxt = tokens[j + 1] if j + 1 < len(tokens) else None
                if not (nxt and nxt.group(1) and _GAP.fullmatch(text[tok.end():nxt.start()])):
                    add(number, None, "근거 표시가 없음")
                    continue
                m = known.get(nxt.group(1))
                if not m:
                    add(number, nxt.group(1), "aggregates에 없는 근거")
                elif m["value"] is None or abs(float(number.replace(",", "")) - m["value"]) > 1e-9:
                    add(number, m["id"], f"aggregates 값은 {m['value']}")
                elif text[tok.end():nxt.start()].strip() not in ("", m["unit"]):
                    add(number, m["id"], f"단위는 {m['unit']}")
                else:
                    add(number, m["id"], None)
            for h in _HANGUL_NUM.finditer(text):
                add(h.group(0), None, "한글로 쓴 수는 대조할 수 없음 (숫자와 근거로 쓰기)")
    return out
