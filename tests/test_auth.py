"""회원 전용 기능 테스트 (/chat, /search, /search/stream, /me).
Supabase(토큰 확인·등급·사용 기록)와 Gemini·Claude는 부르지 않고 가짜로 바꾼다.

실행: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pickit-test-")   # 진짜 캐시·기록을 건드리지 않음

import app as server  # noqa: E402
import auth  # noqa: E402
import members  # noqa: E402
from flask import g  # noqa: E402

server.ASYNC_LOG = False   # 사용 기록을 바로 남겨서 테스트에서 확인

GOOD = {"Authorization": "Bearer good-token"}
ADMIN = {"Authorization": "Bearer admin-token"}
USERS = {
    "good-token": {"id": "11111111-2222-3333-4444-555555555555", "email": "user@example.com"},
    "admin-token": {"id": "99999999-2222-3333-4444-555555555555", "email": "admin@example.com"},
}
ROLES = {USERS["admin-token"]["id"]: "admin"}
CHAT_BODY = {"question": "가성비 하나만 골라줘",
             "results": {"query": "무선 마우스", "products": [{"brand": "A", "model": "M1", "price": 10000}]}}
ANSWER = {"answer": "추천: A M1", "pick_id": 0, "fallback": False}
SEARCH_RESULT = {"query": "무선 마우스", "products": [{"brand": "A", "model": "M1"}], "engine": "claude"}


def fake_fetch_user(token):
    """Supabase 토큰 확인 대신: USERS에 있는 토큰만 통과, 나머지는 401."""
    if token in USERS:
        return USERS[token]
    raise auth.AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")


class MemberApiTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.used = {}   # (user_id, kind) → 오늘 성공한 횟수 (가짜 usage_logs)
        self.logs = []

        def fake_log(user_id, email, action, status, **kw):
            self.logs.append({"user_id": user_id, "action": action, "status": status, **kw})

        patches = [
            mock.patch.object(auth, "fetch_user", side_effect=fake_fetch_user),
            mock.patch.object(members, "get_role", side_effect=lambda uid: ROLES.get(uid, "user")),
            mock.patch.object(members, "count_today", side_effect=lambda uid, kind: self.used.get((uid, kind), 0)),
            mock.patch.object(members, "log_usage", side_effect=fake_log),
            mock.patch.object(server, "is_local_request", return_value=False),   # 외부 요청처럼
            mock.patch.object(server.chat, "answer", return_value=ANSWER),
            mock.patch.object(server, "perform_search", return_value=dict(SEARCH_RESULT)),
            mock.patch.object(server.stats, "record_request"),
            mock.patch.object(server, "DAILY_LIMIT", 5),   # .env 값과 상관없이 기본 한도로 테스트
            mock.patch.dict(server.LIMITS, {"search": 5, "chat": 30}),
        ]
        started = [p.start() for p in patches]
        self.fetch, self.answer, self.search = started[0], started[5], started[6]
        for p in patches:
            self.addCleanup(p.stop)

    def chat(self, headers=None, body=None):
        return self.client.post("/chat", json=body or CHAT_BODY, headers=headers or {})

    def search_get(self, headers=None, q="무선 마우스"):
        return self.client.get("/search", query_string={"q": q}, headers=headers or {})

    # ---------- /chat 로그인 ----------

    def test_chat_no_token_401(self):
        res = self.chat()
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.get_json()["login_required"])
        self.fetch.assert_not_called()      # 토큰이 없으면 Supabase에 묻지도 않음
        self.answer.assert_not_called()     # Gemini 호출 전에 막힘

    def test_chat_not_bearer_401(self):
        self.assertEqual(self.chat({"Authorization": "Basic abc"}).status_code, 401)
        self.answer.assert_not_called()

    def test_chat_wrong_token_401(self):
        res = self.chat({"Authorization": "Bearer wrong-token"})
        self.assertEqual(res.status_code, 401)
        self.fetch.assert_called_once_with("wrong-token")
        self.answer.assert_not_called()

    def test_chat_good_token_passes_and_logs(self):
        seen = {}
        self.answer.side_effect = lambda *a, **kw: (seen.update(uid=g.user_id), ANSWER)[1]
        res = self.chat(GOOD)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["remaining"], 29)
        self.assertEqual(seen["uid"], USERS["good-token"]["id"])   # 확인된 사용자 id가 이후 처리에 넘어감
        self.assertEqual(self.logs[-1]["action"], "chat")
        self.assertEqual(self.logs[-1]["status"], "ok")
        self.assertEqual(self.logs[-1]["query"], "무선 마우스")      # 질문 원문이 아니라 검색어
        self.assertEqual(members.quota._pending, {})                 # 잡아 둔 칸을 놓음

    def test_supabase_not_configured_503(self):
        self.fetch.side_effect = auth.AuthError("로그인 기능이 아직 설정되지 않았어요.", 503)
        self.assertEqual(self.chat(GOOD).status_code, 503)
        self.answer.assert_not_called()

    def test_chat_limit_30(self):
        self.used[(USERS["good-token"]["id"], "chat")] = 30
        res = self.chat(GOOD)
        self.assertEqual(res.status_code, 429)
        self.answer.assert_not_called()
        self.assertEqual(self.logs[-1]["status"], "limited")

    def test_chat_admin_unlimited(self):
        self.used[(USERS["admin-token"]["id"], "chat")] = 999
        res = self.chat(ADMIN)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["unlimited"])

    def test_member_db_down_503(self):
        with mock.patch.object(members, "get_role", side_effect=members.MemberError("down")):
            self.assertEqual(self.chat(GOOD).status_code, 503)
        self.answer.assert_not_called()

    # ---------- /search 회원 전용 ----------

    def test_search_no_token_401(self):
        res = self.search_get()
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.get_json()["login_required"])
        self.search.assert_not_called()     # Claude 실행 전에 막힘

    def test_search_wrong_token_401(self):
        self.assertEqual(self.search_get({"Authorization": "Bearer nope"}).status_code, 401)
        self.search.assert_not_called()

    def test_search_member_ok_and_logs(self):
        res = self.search_get(GOOD, q="회원 검색 1")
        self.assertEqual(res.status_code, 200, res.get_json())
        d = res.get_json()
        self.assertEqual((d["remaining"], d["limit"]), (4, 5))
        self.search.assert_called_once()
        log = self.logs[-1]
        self.assertEqual((log["action"], log["status"], log["query"]), ("search", "ok", "회원 검색 1"))
        self.assertEqual(log["user_id"], USERS["good-token"]["id"])

    def test_search_limit_5(self):
        self.used[(USERS["good-token"]["id"], "search")] = 5
        res = self.search_get(GOOD, q="회원 검색 2")
        self.assertEqual(res.status_code, 429)
        self.search.assert_not_called()
        self.assertEqual(self.logs[-1]["status"], "limited")

    def test_search_admin_unlimited(self):
        self.used[(USERS["admin-token"]["id"], "search")] = 50
        res = self.search_get(ADMIN, q="관리자 검색")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["unlimited"])

    def test_pending_counts_toward_limit(self):
        """동시에 들어온 요청(아직 기록 전)도 한도에 셈."""
        uid = USERS["good-token"]["id"]
        self.used[(uid, "search")] = 4
        self.assertEqual(members.quota.reserve(uid, "search", 5), 0)
        self.assertIsNone(members.quota.reserve(uid, "search", 5))
        members.quota.release(uid, "search")
        self.assertEqual(members.quota._pending, {})

    def test_stream_no_token_401(self):
        res = self.client.get("/search/stream", query_string={"q": "x"})
        self.assertEqual(res.status_code, 401)
        self.search.assert_not_called()

    def test_stream_member_gets_result(self):
        res = self.client.get("/search/stream", query_string={"q": "스트림 검색"}, headers=GOOD)
        self.assertIn("text/event-stream", res.headers["Content-Type"])
        body = res.get_data(as_text=True)
        self.assertIn("event: result", body)
        self.search.assert_called_once()
        self.assertEqual(self.logs[-1]["status"], "ok")

    def test_public_routes_need_no_login(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertNotEqual(self.client.get("/history").status_code, 401)
        self.assertNotEqual(self.client.get("/cached?q=없는검색어").status_code, 401)

    # ---------- /me ----------

    def test_me(self):
        self.used[(USERS["good-token"]["id"], "search")] = 2
        d = self.client.get("/me", headers=GOOD).get_json()
        self.assertEqual(d["role"], "user")
        self.assertEqual(d["search"], {"used": 2, "limit": 5, "remaining": 3})
        self.assertEqual(d["chat"]["remaining"], 30)
        self.assertEqual(self.client.get("/me").status_code, 401)

    def test_me_admin(self):
        d = self.client.get("/me", headers=ADMIN).get_json()
        self.assertTrue(d["unlimited"])
        self.assertIsNone(d["search"]["limit"])

    def test_cors_allows_authorization_header(self):
        res = self.client.options("/chat", headers={
            "Origin": server.ALLOWED_ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type"})
        self.assertIn("Authorization", res.headers.get("Access-Control-Allow-Headers", ""))


class BearerTokenTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(auth.bearer_token("Bearer abc"), "abc")
        self.assertEqual(auth.bearer_token("bearer  abc "), "abc")
        self.assertIsNone(auth.bearer_token(None))
        self.assertIsNone(auth.bearer_token("Bearer "))
        self.assertIsNone(auth.bearer_token("Token abc"))


if __name__ == "__main__":
    unittest.main()
