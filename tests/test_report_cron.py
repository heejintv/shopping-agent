"""매일 자동 보고서 테스트: 토큰 검사, 만들기·저장·승인, 공개 로그에 내용이 안 나가는지. Gemini·Supabase는 가짜.

실행: python -m unittest discover -s tests -v
"""

import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))

import app as server  # noqa: E402
import auth  # noqa: E402
import claude_errors  # noqa: E402
import gemini  # noqa: E402
import members  # noqa: E402
import report_crew  # noqa: E402

TOKEN = "test-" + "x" * 40   # 가짜 값
CREW = {"draft": {"summary": ["비밀검색어"]}, "checks": [], "verdict": "검토 통과", "problems": [], "fixes": 0,
        "calls": 2, "tokens": 0, "reviewed": True}


class Base(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.db = {}

        def save(report_id, day, source, report):
            if source == "auto" and any(r["date"] == day and r["source"] == "auto" for r in self.db.values()):
                raise members.MemberError("x", 409)
            self.db[report_id] = {"id": report_id, "date": day, "source": source, "status": "draft", "report": report,
                                  "created_at": "2026-11-20T00:00:00+00:00", "approved_at": None}

        def auto(day):
            return next((r for r in self.db.values() if r["date"] == day and r["source"] == "auto"), None)

        def approve(report_id, source, at):
            r = self.db.get(report_id)
            if not r or r["source"] != source or r["status"] != "draft":
                return None
            r.update(status="approved", approved_at=at)
            return r

        users = {"user-token": {"id": "u-user", "email": "u@x.com"}, "admin-token": {"id": "u-admin", "email": "a@x.com"}}

        def fetch(token):
            if token in users:
                return users[token]
            raise auth.AuthError("로그인이 만료됐어요.")

        def start(p):
            m = p.start()
            self.addCleanup(p.stop)
            return m

        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        start(mock.patch.dict(os.environ, {"REPORT_CRON_TOKEN": TOKEN}))
        start(mock.patch.object(auth, "fetch_user", side_effect=fetch))
        start(mock.patch.object(members, "get_role", side_effect=lambda uid, fresh=False: "admin" if uid == "u-admin" else "user"))
        self.usage = start(mock.patch.object(members, "usage_between", return_value=([], False)))
        self.save = start(mock.patch.object(members, "save_report", side_effect=save))
        self.auto = start(mock.patch.object(members, "auto_report", side_effect=auto))
        start(mock.patch.object(members, "approve_report", side_effect=approve))
        self.latest = start(mock.patch.object(members, "latest_auto_report", side_effect=lambda: max(
            (r for r in self.db.values() if r["source"] == "auto"), key=lambda r: r["date"], default=None)))
        self.runs = []
        start(mock.patch.object(members, "log_run", side_effect=lambda *a: self.runs.append(a)))
        start(mock.patch.object(server, "_denied_at", float("-inf")))
        start(mock.patch.object(report_crew, "available", return_value=True))
        self.crew = start(mock.patch.object(report_crew, "run", side_effect=lambda agg: dict(CREW)))
        start(mock.patch.object(server, "ASYNC_LOG", False))
        start(mock.patch.object(server, "_report", {"state": "none"}))
        start(mock.patch.object(server, "_cron", {"state": "none"}))
        start(mock.patch.object(server, "REPORT_DIR", Path(d.name)))
        self.yesterday = (datetime.now(server.KST).date() - timedelta(days=1)).isoformat()

    def cron(self, method="post", token=TOKEN):
        headers = {"X-Report-Cron-Token": token} if token is not None else {}
        return getattr(self.client, method)("/admin/report/cron", headers=headers)

    def admin(self, method, path, token="admin-token", **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return getattr(self.client, method)("/admin/report" + path, headers=headers, **kw)


class CronTest(Base):
    def test_token(self):
        for method in ("post", "get"):
            for bad in (None, "", "wrong", TOKEN + "x", TOKEN[:-1], "한글".encode().decode("latin-1")):
                self.assertEqual(self.cron(method, bad).status_code, 401, bad)
        res = self.client.post("/admin/report/cron", headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(res.status_code, 401)
        self.auto.assert_not_called()
        self.crew.assert_not_called()

    def test_disabled_without_or_short_env(self):
        for env in ("", "short"):
            with mock.patch.dict(os.environ, {"REPORT_CRON_TOKEN": env}):
                self.assertEqual(self.cron(token=env).status_code, 503)
        self.crew.assert_not_called()

    def test_cron_token_is_not_admin(self):
        for path in ("", "/auto"):
            res = self.client.get("/admin/report" + path, headers={"X-Report-Cron-Token": TOKEN})
            self.assertEqual(res.status_code, 401)

    def test_makes_yesterday_and_saves_auto(self):
        res = self.cron()
        self.assertEqual((res.status_code, res.get_json()), (202, {"state": "done", "date": self.yesterday}))
        row = next(iter(self.db.values()))
        self.assertEqual((row["date"], row["source"], row["status"]), (self.yesterday, "auto", "draft"))
        self.assertEqual(row["report"]["verdict"], "검토 통과")
        self.assertEqual(server._report, {"state": "none"})   # 관리자가 보던 초안은 안 건드림

    def test_response_never_has_content(self):
        self.cron()
        for res in (self.cron(), self.cron("get")):
            self.assertLessEqual(set(res.get_json()), {"state", "date"})
            self.assertNotIn("비밀검색어", res.get_data(as_text=True))

    def test_second_call_does_not_use_gemini(self):
        self.cron()
        self.assertEqual(self.cron().status_code, 200)
        self.assertEqual(self.crew.call_count, 1)

    def test_running_and_busy(self):
        server._cron = {"state": "running", "date": "x"}
        self.assertEqual(self.cron().status_code, 202)
        self.assertEqual(self.admin("post", "").status_code, 409)
        server._cron, server._report = {"state": "none"}, {"state": "running", "id": "x"}
        self.assertEqual(self.cron().status_code, 409)
        self.crew.assert_not_called()

    def test_no_table_stops_before_gemini(self):
        self.auto.side_effect = members.MemberError("x", 404)
        self.assertEqual(self.cron().status_code, 503)
        self.crew.assert_not_called()

    def test_no_crewai(self):
        with mock.patch.object(report_crew, "available", return_value=False):
            self.assertEqual(self.cron().status_code, 503)

    def test_errors_then_retry(self):
        for target, err in ((self.crew, gemini.EngineError("한도")), (self.save, members.MemberError("x", 500)),
                            (self.usage, members.MemberError("x"))):
            old, target.side_effect = target.side_effect, err
            self.assertEqual(self.cron().get_json()["state"], "error")
            target.side_effect = old
        self.assertEqual(self.cron().get_json()["state"], "done")   # 다시 부르면 만들어짐

    def test_already_saved_meanwhile(self):
        self.save.side_effect = members.MemberError("x", 409)
        self.assertEqual(self.cron().get_json()["state"], "done")


class AutoCardTest(Base):
    def test_permissions(self):
        self.assertEqual(self.admin("get", "/auto", None).status_code, 401)
        self.assertEqual(self.admin("get", "/auto", "user-token").status_code, 403)
        self.auto.assert_not_called()

    def test_none_then_show_and_approve(self):
        self.assertEqual(self.admin("get", "/auto").get_json(), {"state": "none"})
        self.cron()
        d = self.admin("get", "/auto").get_json()
        self.assertEqual((d["state"], d["verdict"], d["aggregates"]["date"]), ("draft", "검토 통과", self.yesterday))
        self.assertEqual(self.admin("post", "/approve", "user-token", json={"id": d["id"]}).status_code, 403)
        res = self.admin("post", "/approve", json={"id": d["id"]}).get_json()
        self.assertEqual((res["state"], bool(res["approved_at"])), ("approved", True))
        self.assertEqual(self.admin("post", "/approve", json={"id": d["id"]}).status_code, 409)
        self.assertEqual(self.admin("get", "/auto").get_json()["state"], "approved")

    def test_bad_ids(self):
        for bad in (None, 3, "x", "eq.1&status=eq.draft", "0" * 16):
            self.assertEqual(self.admin("post", "/approve", json={"id": bad}).status_code, 409)

    def test_db_down(self):
        self.auto.side_effect = members.MemberError("x", 404)
        self.assertEqual(self.admin("get", "/auto").status_code, 503)


class ManualSaveTest(Base):
    def test_saved_and_approved(self):
        d = self.admin("post", "").get_json()
        self.assertTrue(d["saved"])
        self.assertEqual(self.db[d["id"]]["source"], "manual")
        self.admin("post", "/approve", json={"id": d["id"]})
        self.assertEqual(self.db[d["id"]]["status"], "approved")

    def test_save_failure_keeps_draft(self):
        self.save.side_effect = members.MemberError("x", 404)
        d = self.admin("post", "").get_json()
        self.assertEqual((d["state"], d["saved"]), ("draft", False))
        self.assertEqual(self.admin("post", "/approve", json={"id": d["id"]}).get_json()["state"], "approved")

    def test_old_manual_draft_cannot_be_approved(self):
        a = self.admin("post", "").get_json()["id"]
        self.admin("post", "")
        self.assertEqual(self.admin("post", "/approve", json={"id": a}).status_code, 409)


class RestTest(unittest.TestCase):
    """members의 reports 요청 모양."""

    def test_requests(self):
        with mock.patch.object(members, "_request", return_value=({}, b'[{"id":"a"}]')) as req:
            members.save_report("a", "2026-11-19", "auto", {"k": 1})
            self.assertEqual(members.auto_report("2026-11-19"), {"id": "a"})
            self.assertEqual(members.approve_report("a", "auto", "t"), {"id": "a"})
        save, get, patch = [c for c in req.call_args_list]
        self.assertEqual(save[1]["body"]["status"], "draft")
        self.assertEqual((get[0][2]["date"], get[0][2]["source"]), ("eq.2026-11-19", "eq.auto"))
        self.assertEqual(patch[0][2], {"id": "eq.a", "source": "eq.auto", "status": "eq.draft"})

    def test_empty(self):
        with mock.patch.object(members, "_request", return_value=({}, b"[]")):
            self.assertIsNone(members.auto_report("x"))
            self.assertIsNone(members.approve_report("a", "auto", "t"))


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.yml = (ROOT / ".github/workflows/daily-report.yml").read_text(encoding="utf-8")

    def test_workflow(self):
        for s in ('cron: "0 0 * * *"', "workflow_dispatch:", "permissions: {}", "/health", "for attempt in 1 2 3",
                  "${{ secrets.REPORT_CRON_TOKEN }}"):
            self.assertIn(s, self.yml)
        self.assertEqual(self.yml.count("secrets."), 1)
        for bad in ("git commit", "git push", "actions/checkout", "upload-artifact", "set -x", "curl -v", 'cat "$body"'):
            self.assertNotIn(bad, self.yml)

    def test_no_token_value_in_repo_files(self):
        self.assertRegex((ROOT / "render.yaml").read_text(encoding="utf-8"), r"REPORT_CRON_TOKEN[^\n]*\n\s+sync: false")
        for name in (".github/workflows/daily-report.yml", "render.yaml", "app.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"REPORT_CRON_TOKEN\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}", text), name)

    def test_token_masked_in_logs(self):
        with mock.patch.dict(os.environ, {"REPORT_CRON_TOKEN": TOKEN}):
            self.assertNotIn(TOKEN, claude_errors.mask(f"x {TOKEN} y"))

    def test_schema(self):
        sql = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")
        for s in ("create table if not exists public.reports", "alter table public.reports enable row level security",
                  "revoke all on public.reports from anon, authenticated"):
            self.assertIn(s, sql)

    def test_page_tabs(self):
        html = (ROOT / "admin.html").read_text(encoding="utf-8")
        at = [html.index(f'["{n}", ') for n in ("요약", "그래프", "이상·제안", "근거")]
        self.assertEqual(at, sorted(at))
        self.assertLess(html.index('id="autoPanel"'), html.index('id="period"'))   # 맨 위
        self.assertIn(report_crew.PASS_NO_AI, html)
        summary = html[at[0]:at[1]]
        self.assertNotIn("table(", summary)   # 요약 탭에는 표·그래프 없음
        self.assertNotIn("data-chart", summary)


if __name__ == "__main__":
    unittest.main()
