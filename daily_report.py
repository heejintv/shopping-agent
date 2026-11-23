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
    """실패 검색을 토큰/한도/시간 초과/기타로. usage_logs에는 오류 코드가 없어서 모니터링 로그의 코드를 씀.
    로그가 없으면(서버 재시작 등) 기타. 합계는 항상 실패 수와 같음."""
    codes = Counter(str(e.get("code")) for e in events if e.get("kind") == "search" and e.get("ok") is False)
    left = sum(1 for r in failed if r.get("status") == "error")
    timeout = len(failed) - left
    out = {}
    for kind, n in (("token", codes["401"]), ("limit", codes["429"]), ("timeout", max(0, codes["504"] - timeout))):
        out[kind] = min(n, left)
        left -= out[kind]
    out["timeout"] += timeout
    out["other"] = left
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
    log = "logs/stats-*.json events.code (usage_logs 실패 건과 맞춤)"
    add("search.total", "검색 요청 수", len(searches), "회", s)
    add("search.ok", "새 검색 성공", ok, "회", f"{s}, status=ok")
    add("search.cached", "저장된 결과로 응답", by(searches, "cached"), "회", f"{s}, status=cached")
    add("search.failed", "검색 실패", len(failed), "회", f"{s}, status=error·timeout")
    add("search.limited", "한도 초과로 막힌 검색", by(searches, "limited"), "회", f"{s}, status=limited")
    add("search.cancelled", "취소된 검색", by(searches, "cancelled"), "회", f"{s}, status=cancelled")
    add("search.fail_rate", "검색 실패율 (실패 ÷ (성공+실패))",
        round(len(failed) * 100 / (ok + len(failed)), 1) if ok + len(failed) else None, "%",
        "계산: search.failed ÷ (search.ok + search.failed)")
    add("search.fail_token", "실패: 로그인·토큰", kinds["token"], "회", f"{log} 401·403")
    add("search.fail_limit", "실패: AI 사용량 한도", kinds["limit"], "회", f"{log} 429")
    add("search.fail_timeout", "실패: 시간 초과", kinds["timeout"], "회", f"usage_logs status=timeout + {log} 504")
    add("search.fail_other", "실패: 기타(원인 로그 없음 포함)", kinds["other"], "회", "계산: search.failed − 나머지 실패 종류")
    add("search.avg_sec", "새 검색 평균 시간", round(sum(ms) / len(ms) / 1000, 1) if ms else None, "초",
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

    notes = ["⚡ 명령 하나는 Gemini를 여러 번 불러도 사용 요청 한 건으로 집계됨"]
    if label:
        notes.append("하루가 끝나지 않아 지금 시각까지만 집계됨 (다른 날과 바로 비교할 수 없음)")
    if truncated:
        notes.append("기록이 너무 많아 일부만 집계됨")
    if failed and not events:
        notes.append("모니터링 로그가 없어(서버 재시작 등) 시간 초과 외 실패 원인은 기타로 집계됨")
    return {"date": start.date().isoformat(), "label": label, "generated_at": now.astimezone(KST).isoformat(timespec="seconds"),
            "metrics": metrics, "notes": notes}


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
_GAP = re.compile(r"[^\d\[\]\n]{0,4}")   # 숫자와 근거 사이에는 단위("회", "초", " %")만
# 숫자로 쓰지 않고 한글로 쓴 수는 대조할 수 없으므로 틀린 것으로 봄
_HANGUL_NUM = re.compile(r"(?<![가-힣])(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|스무|수십|수백)\s?(?:번|건|회|명|개|배)")


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
            tokens = list(_TOKEN.finditer(text))

            def add(number, mid, reason):
                m = known.get(mid)
                out.append({"section": section, "line": i, "number": number, "id": mid,
                            "label": m["label"] if m else None, "expected": m["value"] if m else None,
                            "source": m["source"] if m else None, "ok": not reason, "reason": reason})

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
