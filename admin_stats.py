"""관리자 대시보드(/admin/stats) 집계. Supabase에서 읽어 온 usage_logs 행을 정리하는 순수 함수만 둔다.

- 날짜는 한국 시간(KST) 기준
- 숫자 카드·Gemini 사용량·서버 전체 새 검색·7일 그래프는 전체 기준
- 기간(오늘/7일/30일)과 이메일 검색은 사용자별 순위와 최근 기록에 적용
- "새 검색 수", "상담 수"는 성공(status=ok)한 것만 셈 (하루 한도와 같은 기준)
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
PERIODS = {"today": 1, "7d": 7, "30d": 30}   # 오늘 포함 며칠
CHART_DAYS = 7
RECENT_MAX = 100
RANK_MAX = 100
CHAT_ACTIONS = ("chat", "compare")
GEMINI_CALLED = ("ok", "error", "timeout")   # Gemini를 실제로 부른 기록 (캐시·한도 초과·취소는 안 부름)


def day_start(now: datetime, days: int) -> datetime:
    """오늘 포함 days일 전 0시 (KST)."""
    today = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1)


def fetch_start(now: datetime, period: str) -> datetime:
    """읽어 와야 하는 시작 시각: 기간과 7일 그래프 중 더 긴 쪽."""
    return day_start(now, max(PERIODS[period], CHART_DAYS))


def _when(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00")).astimezone(KST)
    except ValueError:
        return None


def _ok(row: dict) -> bool:
    return row.get("status") == "ok"


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def build(rows: list[dict], members_total: int, now: datetime, period: str = "today", q: str = "",
          gemini_limit: int = 0, global_limit: int = 0, truncated: bool = False) -> dict:
    period = period if period in PERIODS else "today"
    q = (q or "").strip().lower()
    today0 = day_start(now, 1)
    period0 = day_start(now, PERIODS[period])
    chart0 = day_start(now, CHART_DAYS)

    timed = [(w, r) for r in rows if (w := _when(r)) is not None]
    today = [r for w, r in timed if w >= today0]

    # 숫자 카드 (오늘, 전체)
    cards = {
        "members": members_total,
        "today_users": len({r.get("user_id") for r in today if r.get("user_id")}),
        "today_searches": sum(1 for r in today if r.get("action") == "search" and _ok(r)),
        "today_chats": sum(1 for r in today if r.get("action") in CHAT_ACTIONS and _ok(r)),
    }

    # Gemini 사용량 (오늘)
    g_rows = [r for r in today if r.get("engine") == "gemini" and r.get("status") in GEMINI_CALLED]
    g_calls = len(g_rows)
    gemini = {
        "calls": g_calls,
        "tokens": sum(_int(r.get("tokens")) for r in g_rows),
        "limit": gemini_limit or None,
        "remaining": max(0, gemini_limit - g_calls) if gemini_limit else None,
    }

    # 서버 전체 새 검색 (오늘, admin·내 PC 제외 — 하루 상한과 같은 기준)
    g_used = sum(1 for r in today if r.get("action") == "search" and _ok(r) and not r.get("exempt"))
    global_search = {"used": g_used, "limit": global_limit,
                     "remaining": max(0, global_limit - g_used) if global_limit else None}

    # 7일 일별 사용량 (전체)
    days = [chart0 + timedelta(days=i) for i in range(CHART_DAYS)]
    daily = {d.date(): {"date": d.strftime("%m-%d"), "searches": 0, "chats": 0} for d in days}
    for w, r in timed:
        slot = daily.get(w.date())
        if slot is None or not _ok(r):
            continue
        if r.get("action") == "search":
            slot["searches"] += 1
        elif r.get("action") in CHAT_ACTIONS:
            slot["chats"] += 1

    # 기간 + 이메일 검색 → 사용자별 순위, 최근 기록
    picked = [(w, r) for w, r in timed if w >= period0 and (not q or q in str(r.get("email") or "").lower())]
    users: dict[str, dict] = {}
    for w, r in picked:
        uid = r.get("user_id") or "?"
        u = users.setdefault(uid, {"email": r.get("email") or "", "searches": 0, "chats": 0, "last_used": None})
        if not u["email"] and r.get("email"):
            u["email"] = r["email"]
        if _ok(r):
            if r.get("action") == "search":
                u["searches"] += 1
            elif r.get("action") in CHAT_ACTIONS:
                u["chats"] += 1
        if u["last_used"] is None or w > u["last_used"]:
            u["last_used"] = w
    ranking = sorted(users.values(), key=lambda u: (u["searches"] + u["chats"], u["last_used"]), reverse=True)
    ranking = [{**u, "last_used": u["last_used"].isoformat()} for u in ranking[:RANK_MAX]]

    picked.sort(key=lambda x: x[0], reverse=True)
    recent = [{
        "time": w.isoformat(), "email": r.get("email") or "", "action": r.get("action"),
        "query": r.get("query") or "", "status": r.get("status"), "duration_ms": r.get("duration_ms"),
        "engine": r.get("engine"), "tokens": r.get("tokens"),
    } for w, r in picked[:RECENT_MAX]]

    return {
        "period": period, "q": q, "generated_at": now.astimezone(KST).isoformat(),
        "cards": cards, "gemini": gemini, "global_search": global_search,
        "daily": list(daily.values()), "ranking": ranking, "recent": recent,
        "matched": len(picked), "truncated": truncated,
    }
