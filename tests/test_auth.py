"""챗봇 로그인 확인 테스트. Supabase와 Gemini는 부르지 않고 가짜로 바꾼다.

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
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pickit-test-")   # 진짜 캐시·횟수 기록을 건드리지 않음

import app as server  # noqa: E402
import auth  # noqa: E402
from flask import g  # noqa: E402

GOOD_TOKEN = "good-token"
FAKE_USER = {"id": "11111111-2222-3333-4444-555555555555", "email": "tester@example.com"}
BODY = {"question": "가성비 하나만 골라줘",
        "results": {"query": "무선 마우스", "products": [{"brand": "A", "model": "M1", "price": 10000}]}}
ANSWER = {"answer": "추천: A M1", "pick_id": 0, "fallback": False}


def fake_fetch_user(token):
    """Supabase 대신: GOOD_TOKEN만 통과, 나머지는 401."""
    if token == GOOD_TOKEN:
        return FAKE_USER
    raise auth.AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")


class ChatAuthTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        patches = [
            mock.patch.object(auth, "fetch_user", side_effect=fake_fetch_user),
            mock.patch.object(server.chat, "answer", return_value=ANSWER),
            mock.patch.object(server.stats, "record_request"),
        ]
        self.fetch, self.answer, _ = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def post(self, headers=None):
        return self.client.post("/chat", json=BODY, headers=headers or {})

    def test_no_token_401(self):
        res = self.post()
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.get_json()["login_required"])
        self.fetch.assert_not_called()      # 토큰이 없으면 Supabase에 묻지도 않음
        self.answer.assert_not_called()     # Gemini 호출 전에 막힘

    def test_not_bearer_401(self):
        res = self.post({"Authorization": "Basic abc"})
        self.assertEqual(res.status_code, 401)
        self.answer.assert_not_called()

    def test_wrong_token_401(self):
        res = self.post({"Authorization": "Bearer wrong-token"})
        self.assertEqual(res.status_code, 401)
        self.fetch.assert_called_once_with("wrong-token")
        self.answer.assert_not_called()

    def test_good_token_passes(self):
        seen = {}
        self.answer.side_effect = lambda *a, **kw: (seen.update(uid=g.user_id), ANSWER)[1]
        res = self.post({"Authorization": f"Bearer {GOOD_TOKEN}"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["answer"], ANSWER["answer"])
        self.answer.assert_called_once()
        self.assertEqual(seen["uid"], FAKE_USER["id"])   # 확인된 사용자 id가 이후 처리에 넘어감

    def test_supabase_not_configured_503(self):
        self.fetch.side_effect = auth.AuthError("로그인 기능이 아직 설정되지 않았어요.", 503)
        res = self.post({"Authorization": f"Bearer {GOOD_TOKEN}"})
        self.assertEqual(res.status_code, 503)
        self.answer.assert_not_called()

    def test_search_routes_need_no_login(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertNotEqual(self.client.get("/history").status_code, 401)
        self.assertNotEqual(self.client.get("/cached?q=없는검색어").status_code, 401)

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
