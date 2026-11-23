"""챗봇 "문장으로 실행" 테스트: /chat/action 권한·한도·실패 안내, 도구 검사, Gemini 함수 호출 결과 읽기.
Supabase·Gemini는 가짜로 바꾼다 (실제 찜·검색 실행은 화면 몫이라 여기서는 고른 결과 JSON만 확인).

실행: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))

import actions  # noqa: E402
import app as server  # noqa: E402
import auth  # noqa: E402
import gemini  # noqa: E402
import members  # noqa: E402
from gemini import EngineError  # noqa: E402

server.ASYNC_LOG = False
GOOD = {"Authorization": "Bearer good-token"}
ADMIN = {"Authorization": "Bearer admin-token"}
USERS = {"good-token": {"id": "u-good", "email": "u@example.com"},
         "admin-token": {"id": "u-admin", "email": "a@example.com"}}
BODY = {"sentence": "저소음 키보드 찜해줘", "names": ["로지텍 저소음 키보드 K295", "앱코 K660"]}


def fake_fetch_user(token):
    if token in USERS:
        return USERS[token]
    raise auth.AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")


class ValidateTest(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(actions.validate("add_favorite", {"name": "  K295  키보드 "}),
                         {"tool": "add_favorite", "args": {"name": "K295 키보드"}})
        self.assertEqual(actions.validate("show_favorites", {"x": 1}), {"tool": "show_favorites", "args": {}})
        self.assertEqual(actions.validate("search", {"query": "무선 마우스"})["args"], {"query": "무선 마우스"})
        self.assertEqual(actions.validate("delete_account", {}), {"tool": None, "args": {}})   # 모르는 도구
        self.assertEqual(actions.validate("add_favorite", {"name": ""}), {"tool": None, "args": {}})
        self.assertEqual(actions.validate(None, {}), {"tool": None, "args": {}})
        self.assertEqual(len(actions.validate("search", {"query": "a" * 500})["args"]["query"]), actions.MAX_ARG)

    def test_clean_names(self):
        self.assertEqual(actions.clean_names(["a  b", 3, "", None] + ["x"] * 20), ["a b"] + ["x"] * 9)
        self.assertEqual(actions.clean_names("not list"), [])

    def test_prompt_has_names(self):
        p = actions.build_prompt("찜해줘", ["A K1"])
        self.assertIn("- A K1", p)
        self.assertIn("찜해줘", p)


class PickToolTest(unittest.TestCase):
    """gemini.pick_tool: 함수 선언을 보내고, 응답의 function_calls에서 이름·입력값만 꺼냄."""

    def fake_client(self, resp):
        from google.genai import types
        client = mock.MagicMock()
        client.models.generate_content.return_value = resp
        return mock.patch.object(gemini, "_client", return_value=(client, types)), client

    def test_reads_function_call(self):
        call = SimpleNamespace(name="add_favorite", args={"name": "앱코 K660"})
        resp = SimpleNamespace(function_calls=[call], usage_metadata=SimpleNamespace(total_token_count=42))
        patch, client = self.fake_client(resp)
        usage = {}
        with patch:
            out = gemini.pick_tool("sys", "prompt", actions.TOOLS, 5, usage)
        self.assertEqual(out, ("add_favorite", {"name": "앱코 K660"}))
        self.assertEqual(usage["tokens"], 42)
        config = client.models.generate_content.call_args.kwargs["config"]
        names = [d.name for d in config.tools[0].function_declarations]
        self.assertEqual(names, list(actions.TOOLS))
        self.assertTrue(config.automatic_function_calling.disable)   # 서버에서 실행하지 않음

    def test_no_call(self):
        patch, _ = self.fake_client(SimpleNamespace(function_calls=None, usage_metadata=None))
        with patch:
            self.assertEqual(gemini.pick_tool("s", "p", actions.TOOLS, 5), (None, {}))


class ChatActionApiTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.used, self.logs = {}, []

        def fake_log(user_id, email, action, status, **kw):
            self.logs.append({"action": action, "status": status, **kw})

        patches = [
            mock.patch.object(auth, "fetch_user", side_effect=fake_fetch_user),
            mock.patch.object(members, "get_role",
                              side_effect=lambda uid, fresh=False: "admin" if uid == "u-admin" else "user"),
            mock.patch.object(members, "count_today", side_effect=lambda uid, kind: self.used.get((uid, kind), 0)),
            mock.patch.object(members, "log_usage", side_effect=fake_log),
            mock.patch.object(server, "is_local_request", return_value=False),
            mock.patch.object(server.stats, "record_request"),
            mock.patch.dict(server.LIMITS, {"search": 5, "chat": 30}),
            mock.patch.object(actions, "pick_tool", return_value=("add_favorite", {"name": "로지텍 저소음 키보드 K295"})),
        ]
        started = [p.start() for p in patches]
        self.pick = started[-1]
        for p in patches:
            self.addCleanup(p.stop)

    def post(self, headers=None, body=None):
        return self.client.post("/chat/action", json=BODY if body is None else body, headers=headers or {})

    def test_no_login_401(self):
        res = self.post()
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.get_json()["login_required"])
        self.pick.assert_not_called()

    def test_ok_returns_tool_only(self):
        res = self.post(GOOD)
        d = res.get_json()
        self.assertEqual(res.status_code, 200, d)
        self.assertEqual(d["tool"], "add_favorite")
        self.assertEqual(d["args"], {"name": "로지텍 저소음 키보드 K295"})
        self.assertEqual(d["remaining"], 29)
        self.assertNotIn("tokens", d)
        prompt = self.pick.call_args.args[1]
        self.assertIn("로지텍 저소음 키보드 K295", prompt)            # 현재 검색 결과 상품명을 넘김
        self.assertEqual(self.logs[-1]["action"], "chat")           # 상담 한도에서 한 번
        self.assertEqual(self.logs[-1]["status"], "ok")
        self.assertEqual(self.logs[-1]["query"], "[실행] 저소음 키보드 찜해줘")
        self.assertEqual(members.quota._pending, {})

    def test_limit_429(self):
        self.used[("u-good", "chat")] = 30
        res = self.post(GOOD)
        self.assertEqual(res.status_code, 429)
        self.pick.assert_not_called()
        self.assertEqual(self.logs[-1]["status"], "limited")

    def test_admin_unlimited(self):
        self.used[("u-admin", "chat")] = 999
        res = self.post(ADMIN)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["unlimited"])

    def test_gemini_fail_says_use_buttons(self):
        self.pick.side_effect = EngineError("Gemini API 사용 한도를 초과했어요.", 429)
        res = self.post(GOOD)
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.get_json()["error"], actions.FAIL_MESSAGE)
        self.assertEqual(self.logs[-1]["status"], "error")
        self.assertEqual(members.quota._pending, {})

    def test_unknown_tool_null(self):
        self.pick.return_value = ("rm_rf", {})
        d = self.post(GOOD).get_json()
        self.assertIsNone(d["tool"])
        self.assertEqual(d["message"], actions.UNKNOWN_MESSAGE)

    def test_bad_input_400(self):
        self.assertEqual(self.post(GOOD, {"sentence": " "}).status_code, 400)
        self.assertEqual(self.post(GOOD, {"sentence": "가" * 201}).status_code, 400)
        self.assertEqual(self.client.post("/chat/action", data="x", headers=GOOD).status_code, 415)
        self.pick.assert_not_called()

    def test_bad_origin_403(self):
        res = self.client.post("/chat/action", json=BODY, headers={**GOOD, "Origin": "https://evil.example"})
        self.assertEqual(res.status_code, 403)

    def test_member_db_down_503(self):
        with mock.patch.object(members, "get_role", side_effect=members.MemberError("down")):
            self.assertEqual(self.post(GOOD).status_code, 503)


if __name__ == "__main__":
    unittest.main()
