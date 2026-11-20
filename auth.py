"""Supabase 로그인 토큰 검증.

화면(supabase-js)이 로그인하면 access token(JWT)을 받는다. 챗봇(/chat) 요청은
`Authorization: Bearer <토큰>` 헤더로 이 토큰을 같이 보내고, 서버는 Supabase에 물어서 확인한다.

환경변수 (.env 또는 Render 대시보드):
- SUPABASE_URL: 프로젝트 주소 (예: https://xxxx.supabase.co)
- SUPABASE_ANON_KEY: 공개 키. 토큰 확인 요청의 apikey로 씀 (화면 config.js에도 들어가는 키)
- SUPABASE_SERVICE_KEY: 관리자 키. 서버에서만 쓰고, 화면·로그·응답에 절대 내보내지 않는다
  (지금은 토큰 확인에 필요 없음. 나중에 관리자 기능에서 씀)

토큰·키 값은 로그에 남기지 않는다.
"""

import json
import os
import urllib.error
import urllib.request

VERIFY_TIMEOUT = 5  # 초


class AuthError(Exception):
    """로그인 확인 실패. status: 401(토큰 없음·틀림) 또는 503(Supabase 설정 없음·연결 실패)."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def bearer_token(header: str | None) -> str | None:
    """'Bearer xxx' 헤더에서 토큰만 꺼낸다. 없거나 모양이 틀리면 None."""
    if not header:
        return None
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 4096:
        return None
    return token


def fetch_user(token: str) -> dict:
    """Supabase에 토큰을 보내 로그인한 사용자 정보를 받는다 (GET /auth/v1/user).
    토큰이 틀리거나 만료됐으면 AuthError(401). 테스트에서는 이 함수를 가짜로 바꾼다."""
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    if not url or not key:
        raise AuthError("로그인 기능이 아직 설정되지 않았어요.", 503)
    req = urllib.request.Request(
        url + "/auth/v1/user",
        headers={"apikey": key, "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=VERIFY_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.") from None
        raise AuthError("로그인 확인 중 오류가 났어요. 잠시 후 다시 시도해주세요.", 503) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise AuthError("로그인 확인 중 오류가 났어요. 잠시 후 다시 시도해주세요.", 503) from None
    return data if isinstance(data, dict) else {}


def verify(header: str | None) -> dict:
    """Authorization 헤더를 확인하고 {"id", "email"}을 돌려준다. 실패하면 AuthError."""
    token = bearer_token(header)
    if not token:
        raise AuthError("로그인하면 상담을 받을 수 있어요.")
    user = fetch_user(token)
    uid = user.get("id")
    if not isinstance(uid, str) or not uid:
        raise AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")
    return {"id": uid, "email": user.get("email")}
