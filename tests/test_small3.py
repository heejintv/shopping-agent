"""작은 기능 3개: 오류 응답 JSON(404/405/500)·화면 안내, ⚡ logout(확인 [예/아니요] 필수), 관리자 'Claude 연결 확인' 버튼.
Gemini는 가짜 응답. 실행: python -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))

import actions  # noqa: E402
import app as server  # noqa: E402
import react_graph  # noqa: E402
from tests.test_actions import FakeLLM, GraphBase, call, text  # noqa: E402

server.ASYNC_LOG = False
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
ADMIN = (ROOT / "admin.html").read_text(encoding="utf-8")


class JsonErrorTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_404_json(self):
        res = self.client.get("/no-such-path-xyz")
        self.assertEqual(res.status_code, 404)
        self.assertTrue(res.is_json)
        self.assertEqual(res.get_json()["error"], "요청 주소를 찾지 못했어요.")

    def test_405_json(self):
        res = self.client.delete("/health")
        self.assertEqual(res.status_code, 405)
        self.assertTrue(res.is_json)
        self.assertIn("error", res.get_json())
        self.assertIn("GET", res.headers.get("Allow", ""))

    def test_500_json(self):
        app = server.app
        with mock.patch.dict(app.config, {"TESTING": False, "PROPAGATE_EXCEPTIONS": False}), \
             mock.patch.dict(app.view_functions, {"health": lambda: 1 / 0}), \
             mock.patch.object(app.logger, "error"):
            res = app.test_client().get("/health")
        self.assertEqual(res.status_code, 500)
        self.assertTrue(res.is_json)
        self.assertEqual(res.get_json()["error"], "서버 오류가 났어요.")
        self.assertNotIn("ZeroDivision", res.get_data(as_text=True))   # 내부 오류 내용은 안 보냄


class ReadJsonTest(unittest.TestCase):
    """화면 readJson: JSON이 아니면 상태 코드별 안내 + 콘솔에 상태 코드·앞 200자."""

    def run_js(self, status, body):
        start = HTML.index("const HTTP_TEXT =")
        end = HTML.index("const safeUrl =")
        js = HTML[start:end] + f"""
const logs = []; console.warn = m => logs.push(m);
const res = {{ status: {status}, ok: {str(200 <= status < 300).lower()}, text: async () => {json.dumps(body)} }};
readJson(res).then(d => process.stdout.write(JSON.stringify({{ d, logs }})));"""
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20, check=True).stdout
        return json.loads(out)

    def test_status_messages(self):
        for status, want in ((502, "서버가 깨어나는 중이에요. 30초 뒤 다시 시도해 주세요"),
                             (503, "서버가 깨어나는 중이에요. 30초 뒤 다시 시도해 주세요"),
                             (404, "요청 주소를 찾지 못했어요"), (500, "서버 오류가 났어요"), (418, "서버 오류가 났어요")):
            r = self.run_js(status, "<html>" + "x" * 500)
            self.assertEqual(r["d"]["error"], want, status)
            self.assertIn(f"상태 {status}", r["logs"][0])
            self.assertEqual(len(r["logs"][0].split(": ", 1)[1]), 200)          # 응답 앞 200자만

    def test_json_passes(self):
        r = self.run_js(429, json.dumps({"error": "오늘 검색을 다 썼어요."}))
        self.assertEqual(r["d"], {"error": "오늘 검색을 다 썼어요."})
        self.assertEqual(r["logs"], [])

    def test_used_by_api_calls(self):
        self.assertGreaterEqual(HTML.count("await readJson(res)"), 6)


class LogoutToolTest(GraphBase):
    def test_registered(self):
        self.assertIn("logout", actions.TOOLS)
        self.assertEqual(actions.check_call("logout", {}, "u"), ({}, None))
        node = subprocess.run(["node", "-e", "const i=require('./intent.js');process.stdout.write(String(i.isActionCommand('로그아웃해줘')))"],
                              cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertEqual(node, "true")

    def test_confirm_required_then_logout(self):
        out = self.start(FakeLLM(call("logout"), text("안 쓰임")), sentence="로그아웃해줘")
        p = out["pending"]
        self.assertEqual(out["type"], "interrupt")
        self.assertEqual((p["kind"], p["action"], p["question"], p["options"]),
                         ("approve", "logout", "로그아웃할까요?", ["예", "아니요"]))   # 실행 전 확인 필수
        out = self.resume({"approved": True})
        self.assertEqual(out["type"], "text")
        self.assertEqual(out["text"], react_graph.LOGOUT_DONE)
        self.assertEqual(out["effects"], ["logout"])                             # 화면이 기존 로그아웃 함수 실행
        self.assertEqual(len(self.llm.seen), 1)

    def test_no_keeps_login(self):
        self.start(FakeLLM(call("logout")), sentence="로그아웃해줘")
        out = self.resume({"approved": False})
        self.assertEqual((out["text"], out["effects"]), (react_graph.LOGOUT_KEPT, []))

    def test_already_logged_out(self):
        self.start(FakeLLM(call("logout")), sentence="로그아웃해줘")
        out = self.resume({"logged_out": True})
        self.assertEqual((out["text"], out["effects"]), ("이미 로그아웃 상태예요", []))

    def test_cancel(self):
        self.start(FakeLLM(call("logout")), sentence="로그아웃해줘")
        out = self.resume({"cancelled": True})
        self.assertTrue(out["cancelled"])
        self.assertEqual(out["effects"], [])

    def test_page_uses_existing_logout(self):
        self.assertIn('$("logoutBtn").addEventListener("click", logout);', HTML)
        self.assertIn('data.effects.includes("logout")', HTML)
        self.assertIn("await logout();", HTML)
        self.assertIn('"이미 로그아웃 상태예요"', HTML)


class AdminClaudeCheckTest(unittest.TestCase):
    def test_button_and_labels(self):
        self.assertIn('id="claudeCheck"', ADMIN)
        self.assertIn("/admin/claude-check", ADMIN)
        for label in ("로그인·토큰", "사용량 한도", "시간 초과", "기타", "확인 시각", "확인 중…"):
            self.assertIn(label, ADMIN)
        body = ADMIN[ADMIN.index("async function claudeCheck("):]
        self.assertIn("btn.disabled = true;", body)
        self.assertIn("btn.disabled = false;", body)


if __name__ == "__main__":
    unittest.main()
