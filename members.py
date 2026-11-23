"""회원 등급(profiles.role)과 사용 기록(usage_logs) — Supabase REST를 서버의 SERVICE 키로 부른다.

- 하루 사용 횟수는 usage_logs에서 계산하므로 서버를 다시 켜도 유지된다
  (한국 시간 자정 기준, status='ok'인 기록만 셈 — 캐시·취소·실패·한도 초과는 안 셈)
- 같은 사람이 동시에 여러 번 보내도 한도를 넘지 않게, 처리 중인 요청 수(pending)도 같이 센다
- 서버 전체 하루 상한(새 검색)도 같은 방식으로 셈. admin·내 PC 기록은 exempt=true라 상한에 안 들어감
- SERVICE 키는 요청 헤더에만 쓰고 로그·응답에 절대 내보내지 않는다
- 테스트에서는 get_role / count_today / log_usage를 가짜로 바꾼다
"""

import json
import logging
import os
import threading
import time
import urllib.error
from dataclasses import dataclass
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TIMEOUT = 5          # 초
ROLE_CACHE_SEC = 60  # 등급은 자주 안 바뀌므로 1분 동안 기억
KST = ZoneInfo("Asia/Seoul")
QUERY_MAX = 300

# 하루 한도를 셀 기록 종류: 검색 / 상담(일반 대화 + 찜 1:1 비교)
KIND_ACTIONS = {"search": ("search",), "chat": ("chat", "compare")}


class MemberError(Exception):
    """Supabase 회원 기록에 연결할 수 없음 (설정 없음·네트워크 오류). code: HTTP 상태 코드(모르면 None)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class LimitReached(Exception):
    """하루 한도를 다 씀. scope: "user"(내 계정) 또는 "global"(서버 전체)."""

    def __init__(self, scope: str):
        super().__init__(scope)
        self.scope = scope


def _config() -> tuple[str, str]:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        raise MemberError("SUPABASE_URL 또는 SUPABASE_SERVICE_KEY가 없어요.")
    return url, key


def _headers(key: str) -> dict:
    # 새 형식 키(sb_secret_...)는 apikey 헤더에만. 예전 JWT 형식(service_role)은 Authorization에도 넣어야 함
    h = {"apikey": key, "Content-Type": "application/json"}
    if not key.startswith("sb_"):
        h["Authorization"] = f"Bearer {key}"
    return h


def _request(method: str, path: str, params: dict | None = None, body=None, extra: dict | None = None):
    url, key = _config()
    full = f"{url}/rest/v1/{path}"
    if params:
        full += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(full, data=data, method=method, headers={**_headers(key), **(extra or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        raise MemberError(f"Supabase {path} {e.code}", e.code) from None   # 응답 본문·키는 남기지 않음
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MemberError(f"Supabase {path} 연결 실패") from None


# ---------- 등급 ----------

_role_cache: dict[str, tuple[float, str]] = {}


def get_role(user_id: str, fresh: bool = False) -> str:
    """profiles.role ('user' | 'admin'). 행이 없으면 'user'. fresh=True면 기억해 둔 값 없이 새로 확인."""
    hit = _role_cache.get(user_id)
    if hit and not fresh and time.time() - hit[0] < ROLE_CACHE_SEC:
        return hit[1]
    _, raw = _request("GET", "profiles", {"select": "role", "id": f"eq.{user_id}", "limit": "1"})
    rows = json.loads(raw.decode("utf-8") or "[]")
    role = rows[0].get("role") if rows and isinstance(rows[0], dict) else None
    role = "admin" if role == "admin" else "user"
    _role_cache[user_id] = (time.time(), role)
    return role


# ---------- 관리자 대시보드용 읽기 ----------

PAGE = 1000          # 한 번에 받는 행 수 (Supabase 기본 최대값)
MAX_ROWS = 20000     # 대시보드 한 번에 읽는 최대 행 수


def count_rows(table: str, params: dict | None = None) -> int:
    headers, _ = _request("GET", table, {"select": "id", "limit": "1", **(params or {})},
                          extra={"Prefer": "count=exact"})
    return parse_count(headers.get("Content-Range"))


def select_rows(table: str, params: dict, max_rows: int = MAX_ROWS) -> tuple[list[dict], bool]:
    """여러 쪽으로 나눠 읽는다. (행 목록, max_rows에서 잘렸는지)"""
    rows: list[dict] = []
    while len(rows) < max_rows:
        _, raw = _request("GET", table, {**params, "limit": str(PAGE), "offset": str(len(rows))})
        page = json.loads(raw.decode("utf-8") or "[]")
        if not isinstance(page, list):
            raise MemberError(f"Supabase {table} 응답을 읽을 수 없어요.")
        rows.extend(r for r in page if isinstance(r, dict))
        if len(page) < PAGE:
            return rows, False
    return rows[:max_rows], True


def usage_since(start_iso: str) -> tuple[list[dict], bool]:
    """start_iso 이후의 사용 기록 (최신순)."""
    return select_rows("usage_logs", {
        "select": "created_at,user_id,email,action,status,query,engine,duration_ms,tokens,exempt",
        "created_at": f"gte.{start_iso}", "order": "created_at.desc",
    })


def usage_between(start: datetime, end: datetime) -> tuple[list[dict], bool]:
    """start 이상 end 미만의 사용 기록 (최신순, 일일 보고서용)."""
    columns = "created_at,user_id,action,status,query,engine,duration_ms,tokens"   # 이메일은 읽지 않음
    params = {
        "and": f'(created_at.gte."{start.isoformat()}",created_at.lt."{end.isoformat()}")',
        "order": "created_at.desc",
    }
    return _first_ok([columns + ",outcome,error_kind,tools", columns + ",outcome,error_kind", columns],
                     lambda c: select_rows("usage_logs", {**params, "select": c}))


# ---------- 오늘 사용 횟수 ----------

def today_start() -> str:
    """한국 시간 오늘 0시 (ISO 형식)."""
    return datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def parse_count(content_range: str | None) -> int:
    """'0-0/3' 또는 '*/0' 에서 전체 개수."""
    try:
        return int((content_range or "").rsplit("/", 1)[1])
    except (IndexError, ValueError):
        raise MemberError("Supabase 개수 응답을 읽을 수 없어요.") from None


def count_today(user_id: str | None, kind: str) -> int:
    """오늘 성공한 검색(kind='search') 또는 상담(kind='chat') 횟수.
    user_id가 None이면 서버 전체 (admin·내 PC처럼 한도 예외인 기록은 빼고)."""
    actions = ",".join(KIND_ACTIONS[kind])
    params = {"select": "id", "action": f"in.({actions})", "status": "eq.ok",
              "created_at": f"gte.{today_start()}", "limit": "1"}
    if user_id:
        params["user_id"] = f"eq.{user_id}"
    else:
        params["exempt"] = "eq.false"
    headers, _ = _request("GET", "usage_logs", params, extra={"Prefer": "count=exact"})
    return parse_count(headers.get("Content-Range"))


# ---------- 기록 ----------

def _first_ok(options: list, call):
    """아직 없는 열(400) 때문에 실패하면 다음 것으로. 다른 오류는 그대로."""
    for i, option in enumerate(options):
        try:
            return call(option)
        except MemberError as e:
            if e.code != 400 or i == len(options) - 1:
                raise
            log.warning("usage_logs에 새 열이 없어요. supabase/schema.sql을 실행하세요.")


def log_usage(user_id: str, email: str | None, action: str, status: str, query: str | None = None,
              engine: str | None = None, duration_ms: float | None = None, exempt: bool = False,
              tokens: int | None = None, outcome: str | None = None, error_kind: str | None = None,
              log_id: str | None = None, tools: list[str] | None = None) -> None:
    """usage_logs에 한 줄 쓴다. 실패해도 응답은 그대로 (경고 로그만, 내용·키는 남기지 않음).
    exempt=True: 한도 예외(admin·내 PC) 사용이라 서버 전체 상한에 안 셈.
    outcome(⚡ 결과 종류)·error_kind(검색 실패 종류)·log_id·tools(⚡ 실행한 도구) 열이 아직 없는 DB면 그 값만 빼고 다시 씀 (하루 횟수는 꼭 세야 함)."""
    row = {
        "user_id": user_id, "email": email, "action": action, "status": status,
        "query": (query or "")[:QUERY_MAX] or None, "engine": engine,
        "duration_ms": None if duration_ms is None else int(duration_ms), "exempt": bool(exempt),
    }
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        row["tokens"] = tokens   # Gemini가 알려 준 사용 토큰 수 (모르면 열을 비워 둠)
    new = {k: v for k, v in (("outcome", outcome), ("error_kind", error_kind), ("log_id", log_id)) if v}
    bodies = [{**row, **new}] + ([row] if new else [])
    if tools is not None:   # tools 열(추가분 5)만 아직 없을 수 있음
        bodies.insert(0, {**row, **new, "tools": tools})
    try:
        _first_ok(bodies, lambda b: _request("POST", "usage_logs", body=b, extra={"Prefer": "return=minimal"}))
    except MemberError as e:
        log.warning("usage log failed: %s", e)


def update_outcome(log_id: str, outcome: str, tools: list[str] | None = None) -> None:
    """⚡ 명령이 끝났을 때 처음 남긴 줄의 outcome(·tools)을 고침. 실패해도 응답은 그대로."""
    bodies = ([{"outcome": outcome, "tools": tools}] if tools is not None else []) + [{"outcome": outcome}]
    try:
        _first_ok(bodies, lambda b: _request("PATCH", "usage_logs", {"log_id": f"eq.{log_id}"}, body=b,
                                             extra={"Prefer": "return=minimal"}))
    except MemberError as e:
        log.warning("outcome update failed: %s", e)


# ---------- 일일 보고서 (reports) ----------

def _rows(raw) -> list[dict]:
    rows = json.loads(raw.decode("utf-8") or "[]")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def save_report(report_id: str, day: str, source: str, report: dict) -> None:
    """초안 저장. 같은 날짜의 자동 보고서가 이미 있으면 MemberError(code=409)."""
    _request("POST", "reports", body={"id": report_id, "date": day, "source": source, "status": "draft", "report": report},
             extra={"Prefer": "return=minimal"})


def auto_report(day: str) -> dict | None:
    """그 날짜의 자동 보고서 한 줄 (없으면 None)."""
    _, raw = _request("GET", "reports", {"select": "id,date,status,report,created_at,approved_at",
                                         "date": f"eq.{day}", "source": "eq.auto", "limit": "1"})
    return next(iter(_rows(raw)), None)


def approve_report(report_id: str, source: str, at: str) -> dict | None:
    """초안을 승인으로. 초안이 없거나 이미 승인됐으면 None."""
    _, raw = _request("PATCH", "reports", {"id": f"eq.{report_id}", "source": f"eq.{source}", "status": "eq.draft"},
                      body={"status": "approved", "approved_at": at}, extra={"Prefer": "return=representation"})
    return next(iter(_rows(raw)), None)


def get_report(report_id: str) -> dict | None:
    _, raw = _request("GET", "reports", {"select": "id,source,status,report", "id": f"eq.{report_id}", "limit": "1"})
    return next(iter(_rows(raw)), None)


def recent_auto_reports() -> list[dict]:
    """최근 날짜순 자동 보고서 (어제 것이 없을 때 마지막 성공본을 찾음)."""
    _, raw = _request("GET", "reports", {"select": "id,date,status,report,created_at,approved_at",
                                         "source": "eq.auto", "order": "date.desc", "limit": "10"})
    return _rows(raw)


def log_run(source: str, status: str, reason: str | None = None, day: str | None = None,
            detail: list[dict] | None = None) -> bool:
    """보고서 실행 시도 한 줄 (report_runs). 실패해도 보고서는 그대로. detail: 숫자 불일치 내용. 남겼으면 True."""
    row = {"source": source, "status": status, "reason": reason, "report_date": day}
    try:
        _first_ok(([{**row, "detail": detail}] if detail else []) + [row],
                  lambda b: _request("POST", "report_runs", body=b, extra={"Prefer": "return=minimal"}))
    except MemberError as e:
        log.warning("report run log failed: %s", e)
        return False
    return True


def last_run(source: str, status: str | None = None) -> dict | None:
    """가장 최근 실행 시도 (status를 주면 그 결과 중 최근). 예전에 남은 연습 기록(practice)은 뺌."""
    columns = "id,at,source,status,reason,report_date"
    params = {"or": "(reason.is.null,reason.neq.practice)", "source": f"eq.{source}", "order": "id.desc", "limit": "1"}
    if status:
        params["status"] = f"eq.{status}"
    _, raw = _first_ok([columns + ",detail", columns],
                       lambda c: _request("GET", "report_runs", {**params, "select": c}))
    return next(iter(_rows(raw)), None)


# ---------- 한도 ----------

GLOBAL = "*"   # 서버 전체 칸의 이름


@dataclass
class Ticket:
    """reserve()로 잡은 칸. 기록을 남긴 뒤 release()."""
    quota: "Quota"
    user_id: str
    kind: str
    with_global: bool
    remaining: int   # 이번 것까지 뺀 내 계정의 남은 횟수

    def release(self) -> None:
        self.quota._dec((self.user_id, self.kind))
        if self.with_global:
            self.quota._dec((GLOBAL, self.kind))


class Quota:
    """계정별(+서버 전체) 하루 한도. reserve()로 칸을 잡고, 기록을 남긴 뒤 ticket.release()로 놓는다."""

    def __init__(self):
        self._pending: dict[tuple[str, str], int] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _inc(self, key):
        with self._guard:
            self._pending[key] = self._pending.get(key, 0) + 1

    def _dec(self, key):
        with self._guard:
            n = self._pending.get(key, 0) - 1
            if n > 0:
                self._pending[key] = n
            else:
                self._pending.pop(key, None)

    def used(self, user_id: str | None, kind: str) -> int:
        """오늘 쓴 횟수 + 지금 처리 중인 횟수. user_id=None이면 서버 전체."""
        return count_today(user_id, kind) + self._pending.get((user_id or GLOBAL, kind), 0)

    def reserve(self, user_id: str, kind: str, limit: int, global_limit: int | None = None) -> Ticket:
        """내 계정 한 칸(+ 서버 전체 한 칸)을 잡는다.
        다 썼으면 LimitReached("user"|"global"), Supabase 오류면 MemberError."""
        with self._lock((user_id, kind)):
            used = self.used(user_id, kind)
            if used >= limit:
                raise LimitReached("user")
            if global_limit is not None:
                with self._lock((GLOBAL, kind)):
                    if self.used(None, kind) >= global_limit:
                        raise LimitReached("global")
                    self._inc((GLOBAL, kind))
            self._inc((user_id, kind))
            return Ticket(self, user_id, kind, global_limit is not None, limit - used - 1)


quota = Quota()
