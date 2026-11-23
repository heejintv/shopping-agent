"""claude 실패 원인 분류·로그 가리기, /admin/claude-check 테스트. claude 실행은 가짜로 바꾼다.

실행: python -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))

import app as server  # noqa: E402
import auth  # noqa: E402
import claude_errors  # noqa: E402
import members  # noqa: E402
from gemini import EngineError  # noqa: E402

server.ASYNC_LOG = False
USERS = {"user-token": {"id": "u-user", "email": "u@example.com"},
         "admin-token": {"id": "u-admin", "email": "a@example.com"}}
SECRET = "sk-ant-oat01-" + "x" * 60


class ClassifyMaskTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(claude_errors.classify("", '{"is_error":true,"result":"Invalid API key · Please run /login"}'), "auth")
        self.assertEqual(claude_errors.classify("OAuth token has expired", ""), "auth")
        self.assertEqual(claude_errors.classify("", "Claude AI usage limit reached|1764000000"), "limit")
        self.assertEqual(claude_errors.classify("API Error: 429 rate_limit_error", ""), "limit")
        self.assertEqual(claude_errors.classify("Request timed out", ""), "timeout")
        self.assertEqual(claude_errors.classify("", ""), "other")
        # usage 안의 input_tokens 때문에 로그인 문제로 잘못 분류하지 않음
        self.assertEqual(claude_errors.classify("", '{"result":"invalid schema","usage":{"input_tokens":3}}'), "other")

    def test_mask(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "my-env-secret-123"}):
            text = claude_errors.mask(f"key {SECRET} Bearer abc.def token=zzz9 env my-env-secret-123")
        for s in (SECRET, "abc.def", "zzz9", "my-env-secret-123"):
            self.assertNotIn(s, text)


class CallClaudeTest(unittest.TestCase):
    def fake_popen(self, code, stdout, stderr=""):
        proc = mock.MagicMock(returncode=code, pid=1)
        proc.communicate.return_value = (stdout, stderr)
        return mock.patch.object(server.subprocess, "Popen", return_value=proc)

    def test_empty_stderr_logs_stdout_and_masks(self):
        out = json.dumps({"is_error": True, "result": f"Invalid API key {SECRET} · Please run /login"})
        with self.fake_popen(1, out, ""), self.assertLogs(server.app.logger, "WARNING") as logs:
            with self.assertRaises(EngineError) as cm:
                server.call_claude("p", {}, 5)
        self.assertEqual(cm.exception.code, 401)
        self.assertIn("로그인", str(cm.exception))
        log = "\n".join(logs.output)
        self.assertIn("Please run /login", log)   # stdout 끝부분이 로그에 남음
        self.assertNotIn(SECRET, log)             # 토큰은 가려짐

    def test_limit_and_other(self):
        with self.fake_popen(1, "", "usage limit reached"), self.assertLogs(server.app.logger, "WARNING"):
            with self.assertRaises(EngineError) as cm:
                server.call_claude("p", {}, 5)
        self.assertEqual(cm.exception.code, 429)
        with self.fake_popen(0, "not json"), self.assertLogs(server.app.logger, "WARNING"):
            with self.assertRaises(EngineError) as cm:
                server.call_claude("p", {}, 5)
        self.assertEqual(cm.exception.code, 502)

    def test_search_shows_reason(self):
        """화면(/search)에도 원인별 문구가 그대로 감."""
        err = EngineError(claude_errors.KINDS["limit"][0], 429)
        with mock.patch.object(server, "perform_search", side_effect=err), \
             mock.patch.object(server, "is_local_request", return_value=True), \
             mock.patch.object(server, "login_user", return_value=(USERS["admin-token"], None)), \
             mock.patch.object(server, "is_unlimited", return_value=True), \
             mock.patch.object(server, "reserve", return_value=(None, None)), \
             mock.patch.object(server, "finish"):
            res = server.app.test_client().get("/search", query_string={"q": "없는검색어zz"})
        self.assertEqual(res.status_code, 502)
        self.assertIn("사용량 한도", res.get_json()["error"])


class AdminClaudeCheckTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

        def fake_fetch_user(token):
            if token in USERS:
                return USERS[token]
            raise auth.AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")

        for p in (mock.patch.object(auth, "fetch_user", side_effect=fake_fetch_user),
                  mock.patch.object(members, "get_role",
                                    side_effect=lambda uid, fresh=False: "admin" if uid == "u-admin" else "user")):
            p.start()
            self.addCleanup(p.stop)

    def get(self, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.get("/admin/claude-check", headers=headers)

    def run_with(self, results):
        return mock.patch.object(server.subprocess, "run", side_effect=results)

    def test_permissions(self):
        with self.run_with([]) as run:
            self.assertEqual(self.get().status_code, 401)
            self.assertEqual(self.get("user-token").status_code, 403)
        run.assert_not_called()   # admin이 아니면 claude를 실행하지도 않음

    def test_ok_no_content(self):
        answer = json.dumps({"is_error": False, "result": "비밀스러운 답변 내용"})
        with self.run_with([SimpleNamespace(returncode=0, stdout="2.1.5 (Claude Code)\n", stderr=""),
                            SimpleNamespace(returncode=0, stdout=answer, stderr="")]):
            res = self.get("admin-token")
        d = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["version"]["value"], "2.1.5")
        self.assertNotIn("비밀스러운", res.get_data(as_text=True))   # 답변 내용은 안 줌
        self.assertEqual(res.headers["Cache-Control"], "no-store")

    def test_auth_fail(self):
        bad = json.dumps({"is_error": True, "result": f"Invalid API key {SECRET}"})
        with self.run_with([SimpleNamespace(returncode=0, stdout="2.1.5", stderr=""),
                            SimpleNamespace(returncode=1, stdout=bad, stderr="")]), \
             self.assertLogs(server.app.logger, "WARNING") as logs:
            d = self.get("admin-token").get_json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["prompt"]["error"], "auth")
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_timeout_and_not_installed(self):
        with self.run_with([SimpleNamespace(returncode=0, stdout="2.1.5", stderr=""),
                            subprocess.TimeoutExpired("claude", 60)]):
            self.assertEqual(self.get("admin-token").get_json()["prompt"]["error"], "timeout")
        with self.run_with([FileNotFoundError()]) as run:
            d = self.get("admin-token").get_json()
        self.assertEqual(d["version"]["error"], "not_installed")
        self.assertEqual(run.call_count, 1)   # 설치가 안 됐으면 claude -p는 건너뜀


if __name__ == "__main__":
    unittest.main()
