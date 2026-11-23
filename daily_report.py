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
# 이상 징후 규칙: (id, 이름, 보는 값, 비교, 기준값, 단위)
RULES = (
    ("rule.fail_rate", "검색 실패율", "search.fail_rate", ">", 10, "%"),
    ("rule.avg_sec", "새 검색 평균 시간", "search.avg_sec", ">", 45, "초"),
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
            "metrics": metrics, "anomalies": anomalies, "notes": notes}


# ---------- 초안 정리·숫자 대조 ----------

SECTIONS = {"summary": 3, "anomalies": 6, "suggestions": 6}   # 이름: 최대 줄 수


def clean_draft(data: dict) -> dict:
    """AI가 준 JSON → {summary, anomalies, suggestions} 글자 목록. 요약이 3줄이 아니면 ValueError."""
    out = {}
    for name, n in SECTIONS.items():
        v = data.get(name) if isinstance(data, dict) else None
        lines = v if isinstance(v, list) else [v] if isinstance(v, str) else []
        out[name] = [" ".join(str(x).split())[:400] for x in lines if isinstance(x, (str, int, float)) and str(x).strip()][:n]
    if len(out["summary"]) != SECTIONS["summary"]:
        raise ValueError("요약이 3줄이 아니에요.")
    return out


_TOKEN = re.compile(r"\[([a-z]+\.[a-z0-9_]+)\]|(\d[\d,]*(?:\.\d+)?)")
# 내부 키(aggregates id 모양). 바른 근거 "[search.total]"는 첫 갈래에서 건너뜀
_KEY = re.compile(r"\[[a-z]+\.[a-z0-9_]+\]|(\[[^\[\]]*\])|"
                  r"((?<![A-Za-z0-9_.])(?:search|chat|action|query|users|gemini|rule)(?:\.[a-z0-9_]+)+)", re.I)
_GAP = re.compile(r"[^\d\[\]\n]{0,4}")   # 숫자와 근거 사이에는 단위("회", "초", " %")만
# 숫자로 쓰지 않고 한글로 쓴 수는 대조할 수 없으므로 틀린 것으로 봄
_HANGUL_NUM = re.compile(r"(?<![가-힣])(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|스무|수십|수백)\s?(?:번|건|회|명|개|배)")


def missing_anomalies(draft: dict, agg: dict) -> list[str]:
    """판정된 이상 징후 중 초안의 anomalies에 (실제값·기준값 근거를 한 줄에 같이) 안 쓴 것."""
    return [f"이상 징후 빠짐: {a['text']}" for a in agg.get("anomalies") or [] if a["triggered"]
            and not any(f"[{a['metric']}]" in line and f"[{a['id']}]" in line for line in draft["anomalies"])]


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
