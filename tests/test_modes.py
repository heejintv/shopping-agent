"""내 PC 모드·Render 모드에서 실제 is_local_request를 거쳐 API를 직접 호출하는 테스트.
(다른 테스트는 is_local_request를 가짜로 바꿔서, 이 함수 안의 오류를 못 잡았음 — PROXY_HEADERS 사건)
Supabase와 Claude·Gemini만 가짜로 바꾼다.

실행: python -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pickit-test-")

import app as server  # noqa: E402
import auth  # noqa: E402
import members  # noqa: E402

server.ASYNC_LOG = False

USER = {"id": "11111111-2222-3333-4444-555555555555", "email": "user@example.com"}
GOOD = {"Authorization": "Bearer good-token"}
NGROK = {"X-Forwarded-For": "203.0.113.5", "X-Forwarded-Proto": "https"}   # ngrok을 거쳐 온 요청
CHAT_BODY = {"question": "가성비 하나만 골라줘",
             "results": {"query": "무선 마우스", "products": [{"brand": "A", "model": "M1", "price": 10000}]}}


class ModeTestBase(unittest.TestCase):
    on_render = False

    def setUp(self):
        self.client = server.app.test_client()
        self.logs = []
        self.used = {}

        def fake_fetch_user(token):
            if token == "good-token":
                return USER
            raise auth.AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")

        patches = [
            mock.patch.object(server, "ON_RENDER", self.on_render),
            mock.patch.object(auth, "fetch_user", side_effect=fake_fetch_user),
            mock.patch.object(members, "get_role", return_value="user"),
            mock.patch.object(members, "count_today", side_effect=lambda uid, kind: self.used.get((uid, kind), 0)),
            mock.patch.object(members, "log_usage",
                              side_effect=lambda uid, email, action, status, **kw: self.logs.append(
                                  {"action": action, "status": status, **kw})),
            mock.patch.object(server.chat, "answer", return_value={"answer": "추천: A M1", "pick_id": 0, "fallback": False}),
            mock.patch.object(server, "perform_search", return_value={"query": "q", "products": [], "engine": "claude"}),
            mock.patch.object(server.stats, "record_request"),
            mock.patch.object(server, "DAILY_LIMIT", 5),
            mock.patch.dict(server.LIMITS, {"search": 5, "chat": 30}),
            mock.patch.object(server, "GLOBAL_SEARCH_LIMIT", 20),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.search = server.perform_search

    def get(self, path, headers=None, **qs):
        return self.client.get(path, query_string=qs, headers=headers or {})


class LocalModeTest(ModeTestBase):
    """내 PC 서버 (python app.py): 127.0.0.1에서 직접 온 요청은 무제한."""

    def test_health(self):
        res = self.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["unlimited"])

    def test_health_from_allowed_origin(self):
        res = self.get("/health", {"Origin": server.ALLOWED_ORIGIN})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["unlimited"])

    def test_health_via_ngrok_is_external(self):
        res = self.get("/health", NGROK)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["unlimited"])

    def test_health_from_other_site_is_external(self):
        res = self.get("/health", {"Origin": "https://evil.example"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["unlimited"])

    def test_me(self):
        res = self.get("/me", GOOD)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(res.get_json()["unlimited"])
        self.assertEqual(self.get("/me").status_code, 401)

    def test_search(self):
        self.used[(USER["id"], "search")] = 99   # 내 PC는 한도와 상관없이 통과
        res = self.get("/search", GOOD, q="내 PC 검색")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(res.get_json()["unlimited"])
        self.search.assert_called_once()
        self.assertTrue(self.logs[-1]["exempt"])   # 서버 전체 상한에 안 셈

    def test_search_still_needs_login(self):
        self.assertEqual(self.get("/search", q="x").status_code, 401)
        self.search.assert_not_called()

    def test_search_via_ngrok_is_limited(self):
        self.used[(USER["id"], "search")] = 5
        res = self.get("/search", {**GOOD, **NGROK}, q="ngrok 검색")
        self.assertEqual(res.status_code, 429)
        self.search.assert_not_called()

    def test_search_stream(self):
        res = self.get("/search/stream", GOOD, q="내 PC 스트림")
        self.assertIn("event: result", res.get_data(as_text=True))

    def test_chat(self):
        res = self.client.post("/chat", json=CHAT_BODY, headers=GOOD)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(res.get_json()["unlimited"])
        self.assertEqual(self.client.post("/chat", json=CHAT_BODY).status_code, 401)


class RenderModeTest(ModeTestBase):
    """Render (RENDER=true): 내 PC 구분 없이 모든 요청이 외부, 계정 한도 적용."""
    on_render = True

    def test_health(self):
        res = self.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["unlimited"])

    def test_me(self):
        self.used[(USER["id"], "search")] = 2
        d = self.get("/me", GOOD).get_json()
        self.assertFalse(d["unlimited"])
        self.assertEqual(d["search"]["remaining"], 3)

    def test_search_limited_by_account(self):
        res = self.get("/search", GOOD, q="렌더 검색")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["remaining"], 4)
        self.used[(USER["id"], "search")] = 5
        self.assertEqual(self.get("/search", GOOD, q="렌더 검색 2").status_code, 429)

    def test_chat(self):
        res = self.client.post("/chat", json=CHAT_BODY, headers=GOOD)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["remaining"], 29)


class RenderImportTest(unittest.TestCase):
    """실제로 RENDER=true 환경변수로 새 파이썬에서 app을 불러와 /health 호출 (gunicorn이 하는 것처럼)."""

    def test_health_under_render_env(self):
        code = textwrap.dedent("""
            import json, app
            res = app.app.test_client().get("/health")
            print(json.dumps({"status": res.status_code, "body": res.get_json(), "on_render": app.ON_RENDER}))
        """)
        env = {**os.environ, "RENDER": "true", "DATA_DIR": tempfile.mkdtemp(prefix="pickit-render-")}
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        d = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertTrue(d["on_render"])
        self.assertEqual(d["status"], 200)
        self.assertFalse(d["body"]["unlimited"])


if __name__ == "__main__":
    unittest.main()
