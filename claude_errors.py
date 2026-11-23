"""claude CLI 실패 원인 분류와 로그용 가리기.

claude -p는 실패해도 stderr가 비어 있고 이유를 stdout(JSON의 result)에만 쓰는 경우가 많다.
그래서 stderr·stdout 끝부분을 합쳐서 원인을 나누고, 로그에는 토큰·키를 가린 채로만 남긴다.
"""

import os
import re

# 화면에 보여줄 문구와 모니터링에 남길 상태 코드 (원인별)
KINDS = {
    "auth": ("AI 검색 엔진 로그인(토큰)이 만료됐거나 잘못됐어요. 관리자에게 알려 주세요.", 401),
    "limit": ("AI 검색 사용량 한도에 걸렸어요. 잠시 후 다시 시도해 주세요.", 429),
    "timeout": ("AI 검색이 시간 안에 끝나지 않았어요. 다시 시도해 주세요.", 504),
    "other": ("AI 검색 엔진에서 오류가 났어요. 잠시 후 다시 시도해 주세요.", 502),
}

_PATTERNS = [   # 앞에서부터 먼저 맞는 것
    ("auth", r"invalid api key|/login|not logged in|log ?in|authenticat|unauthori[sz]ed|\b401\b"
             r"|oauth|token (has )?expired|expired token|invalid[^.\n]{0,20}token|credential|forbidden|\b403\b"),
    ("limit", r"usage limit|rate.?limit|limit reached|\b429\b|quota|credit balance|too many requests|limit exceeded"),
    ("timeout", r"timed? ?out|timeout|deadline"),
]

_SECRET_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
               "GEMINI_API_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY")
_SECRET_RE = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:token|key|secret|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),   # JWT
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),                                 # 긴 무작위 문자열
]


def error_kind(e: BaseException) -> str:
    """검색 실패 예외 → usage_logs.error_kind: token · limit · timeout · server(AI 서버 장애) · db(회원 DB) · other."""
    if type(e).__name__ == "MemberError":
        return "db"
    if "timeout" in type(e).__name__.lower() or "timeout" in type(e.__cause__).__name__.lower():
        return "timeout"
    code = getattr(e, "code", None)
    by_code = {401: "token", 403: "token", 429: "limit", 504: "timeout", 500: "server", 503: "server"}
    if code in by_code:
        return by_code[code]
    if "GEMINI_API_KEY" in str(e):   # 키가 없음
        return "token"
    kind = classify(str(e), str(e.__cause__ or ""))
    return "token" if kind == "auth" else kind


def mask(text: str) -> str:
    """토큰·키로 보이는 부분을 ***로 바꾼다."""
    text = text or ""
    for name in _SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, "***")
    for pat in _SECRET_RE:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


def tail(text: str, n: int = 500) -> str:
    """로그에 남길 끝부분 (가린 뒤 자름, 줄바꿈은 한 줄로)."""
    return " ".join(mask(text).split())[-n:]


def classify(*texts: str) -> str:
    """stderr·stdout 등에서 원인 종류(auth·limit·timeout·other)를 고른다."""
    joined = " ".join(t or "" for t in texts).lower()
    for kind, pat in _PATTERNS:
        if re.search(pat, joined):
            return kind
    return "other"
