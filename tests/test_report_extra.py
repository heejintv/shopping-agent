"""일일 보고서: 실행 로그(report_runs), 근거에서 원본 열기, 규칙 파일(report-rules.md). Gemini·Supabase는 가짜.

실행: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))

import app as server  # noqa: E402
import daily_report as dr  # noqa: E402
import gemini  # noqa: E402
import members  # noqa: E402
import report_crew  # noqa: E402
import test_report as tr  # noqa: E402
import test_report_cron as tc  # noqa: E402

KST, NOW, row = dr.KST, tr.NOW, tr.row
START, END = dr.yesterday(NOW)
USED = [row("11-19 10:00")]


class RunLogTest(tc.Base):
    def setUp(self):
        super().setUp()
        t = datetime.now(KST) - timedelta(days=1)
        self.usage.return_value = ([row("11-19 10:00", created_at=t.isoformat())], False)

    def test_auto_ok_then_exists(self):
        self.cron()
        self.cron()
        self.assertEqual(self.runs, [("auto", "ok", None, self.yesterday), ("auto", "skipped", "exists", self.yesterday)])

    def test_no_data(self):
        self.usage.return_value = ([], False)
        self.cron()
        self.assertEqual(self.runs, [("auto", "ok", "no_data", self.yesterday)])

    def test_failures(self):
        E = gemini.EngineError
        for target, err, reason in ((self.crew, E("한도", 429), "gemini"), (self.crew, E("키", 401), "gemini_key"),
                                    (self.crew, E("GEMINI_API_KEY 없음"), "gemini_key"), (self.crew, KeyError("x"), "other"),
                                    (self.crew, E("report-rules.md를 읽을 수 없어요."), "other"),
                                    (self.usage, members.MemberError("x"), "db"), (self.auto, members.MemberError("x"), "db"),
                                    (self.save, members.MemberError("x", 500), "save")):
            old, target.side_effect = target.side_effect, err
            self.runs.clear()
            self.cron()
            target.side_effect = old
            self.assertEqual(self.runs, [("auto", "failed", reason, self.yesterday)], err)

    def test_saved_meanwhile(self):
        self.save.side_effect = members.MemberError("x", 409)
        self.cron()
        self.assertEqual(self.runs[0][1:3], ("skipped", "exists"))

    def test_wrong_token_logged_once(self):
        for _ in range(5):
            self.assertEqual(self.cron(token="wrong").status_code, 401)
        self.cron("get", "wrong")
        self.assertEqual(self.runs, [("auto", "failed", "token", self.yesterday)])
        self.assertNotIn("wrong", str(self.runs))

    def test_busy_and_crewai(self):
        server._cron = {"state": "running", "date": "x"}
        self.assertEqual(self.cron().status_code, 202)
        self.admin("post", "")
        self.assertEqual([r[:3] for r in self.runs], [("auto", "skipped", "busy"), ("manual", "skipped", "busy")])
        server._cron = {"state": "none"}
        self.runs.clear()
        with mock.patch.object(report_crew, "available", return_value=False):
            self.cron()
            self.admin("post", "")
        self.assertEqual([r[:3] for r in self.runs], [("auto", "failed", "crewai"), ("manual", "failed", "crewai")])

    def test_manual(self):
        self.admin("post", "", json={"date": "2026-11-01"})
        self.assertEqual(self.runs[0], ("manual", "ok", "no_data", "2026-11-01"))
        self.crew.side_effect = gemini.EngineError("한도", 429)
        d = self.admin("post", "").get_json()
        self.assertEqual(self.runs[1][:3], ("manual", "failed", "gemini"))
        self.assertNotIn("reason", d)
        self.crew.side_effect = lambda agg: dict(tc.CREW)
        self.save.side_effect = members.MemberError("x", 404)
        self.admin("post", "")
        self.assertEqual(self.runs[2][:3], ("manual", "ok", "save"))

    def test_cli_not_logged(self):
        server._make_report("cli")
        self.assertEqual(self.runs, [])
        self.save.assert_not_called()

    def test_runs_api(self):
        self.assertEqual(self.admin("get", "/runs", None).status_code, 401)
        self.assertEqual(self.admin("get", "/runs", "user-token").status_code, 403)
        ok = {"id": 1, "at": (datetime.now(KST) - timedelta(days=1)).isoformat(), "status": "ok", "reason": None}
        bad = {"id": 2, "at": datetime.now(KST).isoformat(), "status": "failed", "reason": "gemini"}
        with mock.patch.object(members, "last_run", side_effect=lambda s, st=None: None if s == "manual" else ok if st else bad):
            d = self.admin("get", "/runs").get_json()
        self.assertTrue(d["auto"]["warn"])
        self.assertIn("실패 · Gemini 오류 → 아래는 어제 성공본", d["auto"]["text"])
        self.assertFalse(d["manual"]["warn"])
        with mock.patch.object(members, "last_run", side_effect=members.MemberError("x", 404)):
            self.assertEqual(self.admin("get", "/runs").status_code, 503)

    def test_card_shows_last_success(self):
        self.db["a" * 16] = {"id": "a" * 16, "date": "2026-01-01", "source": "auto", "status": "draft", "report": {},
                             "created_at": None, "approved_at": None}
        self.assertTrue(self.admin("get", "/auto").get_json()["stale"])
        self.cron()
        d = self.admin("get", "/auto").get_json()
        self.assertEqual((d["stale"], d["aggregates"]["date"]), (False, self.yesterday))


class RunSummaryTest(unittest.TestCase):
    def run_(self, i, hhmm, status, reason=None, day="20"):
        return {"id": i, "at": f"2026-11-{day}T{hhmm}:00+09:00", "status": status, "reason": reason}

    def test_text(self):
        now = datetime(2026, 11, 20, 12, 0, tzinfo=KST)
        ok = self.run_(1, "09:01", "ok", day="19")
        S = lambda a, b: dr.run_summary(a, b, now)   # noqa: E731
        self.assertEqual(S(self.run_(2, "09:15", "failed", "gemini"), ok),
                         {"text": "09:15 실패 · Gemini 오류 → 아래는 어제 성공본", "warn": True})
        self.assertEqual(S(ok, ok), {"text": "어제 09:01 성공", "warn": False})
        self.assertFalse(S(self.run_(2, "09:15", "skipped", "exists"), ok)["warn"])
        self.assertTrue(S(self.run_(2, "09:15", "skipped", "busy"), ok)["warn"])
        self.assertIn("성공한 보고서 없음", S(self.run_(2, "09:15", "failed", "token"), None)["text"])
        self.assertIn("11월 3일 성공본", S(self.run_(2, "09:15", "failed"), self.run_(1, "09:00", "ok", day="03"))["text"])
        self.assertIn("마지막 성공은 어제", dr.run_summary(self.run_(2, "09:15", "failed"), ok, now, below=False)["text"])
        self.assertEqual(S(None, None)["warn"], False)

    def test_utc_time(self):
        r = {"id": 1, "at": "2026-11-20T00:15:00+00:00", "status": "ok", "reason": None}
        self.assertEqual(dr.run_summary(r, r, NOW)["text"], "09:15 성공")


class RunRestTest(unittest.TestCase):
    def test_log_and_read(self):
        with mock.patch.object(members, "_request", return_value=({}, b'[{"id":1}]')) as req:
            members.log_run("auto", "failed", "token", "2026-11-19")
            self.assertEqual(members.last_run("auto", "ok"), {"id": 1})
        self.assertEqual(req.call_args_list[0][0][1], "report_runs")
        self.assertEqual(req.call_args_list[1][0][2]["status"], "eq.ok")

    def test_log_never_raises(self):
        with mock.patch.object(members, "_request", side_effect=members.MemberError("x", 404)), \
                self.assertLogs(members.log, "WARNING"):
            members.log_run("auto", "ok")

    def test_schema_same_rights_as_reports(self):
        sql = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")
        for t in ("reports", "report_runs"):
            for s in (f"alter table public.{t} enable row level security", f"revoke all on public.{t} from anon, authenticated",
                      f"grant select, insert, update on public.{t} to service_role"):
                self.assertIn(s, sql)


class EvidenceTest(unittest.TestCase):
    def ev(self, key, rows=tr.ROWS, text=None):
        return dr.evidence(rows, key, START, END, text)

    def test_count_matches_every_number(self):
        """횟수인 숫자는 원본 기록 수와 같아야 함."""
        rows = [r if r.get("status") != "error" or r["action"] != "search" else {**r, "error_kind": "limit"} for r in tr.ROWS]
        rows += [row("11-19 12:00", "chat", query="[실행] " + t, outcome=o) for t, o in
                 (("에어팟 찜", "search_cancelled"), ("에어팟 찜", "cannot"), ("x", "waiting"))]
        agg = dr.build(rows, [], NOW)
        for m in agg["metrics"]:
            if m["unit"] == "회" and not m["id"].startswith("rule."):
                self.assertEqual(self.ev(m["id"], rows, m.get("text"))["total"], m["value"], m["id"])

    def test_rate_and_rule(self):
        self.assertEqual(self.ev("search.fail_rate")["total"], 6)          # 성공 2 + 실패 4
        self.assertEqual(self.ev("rule.fail_rate"), self.ev("search.fail_rate"))
        self.assertEqual(self.ev("search.avg_sec")["total"], 2)
        self.assertEqual(self.ev("users.limited")["total"], 2)

    def test_no_source(self):
        for key in ("gemini.limit", "nope", "query.top1", "query.top9", "search.__class__"):
            self.assertIsNone(self.ev(key), key)
        self.assertIsNone(self.ev("query.top9", text="x"))

    def test_max_ten_newest_first(self):
        rows = [row(f"11-19 10:{i:02}") for i in range(15)]
        e = self.ev("search.total", rows)
        self.assertEqual((e["total"], len(e["rows"])), (15, 10))
        self.assertTrue(e["rows"][0]["time"].startswith("2026-11-19T10:14"))

    def test_privacy_and_cut(self):
        secret = "kim@a.com 010-1234-5678 " + "가" * 50
        rows = [row("11-19 10:00", query=secret), row("11-19 10:01", "chat", query="[실행] " + secret)]
        for key in ("search.total", "action.total"):
            e = self.ev(key, rows)
            self.assertEqual(set(e["rows"][0]), {"time", "kind", "status", "detail", "text"})
            self.assertLessEqual(len(e["rows"][0]["text"]), 30)
            for s in ("kim@", "1234", "secret@", "u-a", "[실행]"):
                self.assertNotIn(s, json.dumps(e, ensure_ascii=False))

    def test_mask_before_cut(self):
        e = self.ev("search.total", [row("11-19 10:00", query="가" * 25 + " 010-1234-5678")])   # 30자에서 잘려도 번호 안 나옴
        self.assertNotIn("010", e["rows"][0]["text"])

    def test_period(self):
        day = date(2026, 11, 20)
        start, end = dr.evidence_period(day, "2026-11-20T08:00:00+09:00", NOW)
        self.assertEqual(end, datetime(2026, 11, 20, 8, 0, 1, tzinfo=KST))
        for bad in ("x", "2026-11-20T08:00:00", "2026-11-19T08:00:00+09:00", "2026-11-20T23:00:00+09:00"):
            with self.assertRaises(ValueError):
                dr.evidence_period(day, bad, NOW)


class EvidenceApiTest(tc.Base):
    def post(self, token="admin-token", **body):
        return self.admin("post", "/evidence", token, json={"date": "2026-11-19", "key": "search.total", **body})

    def test_admin_only(self):
        self.assertEqual(self.post(None).status_code, 401)
        self.assertEqual(self.post("user-token").status_code, 403)
        res = self.client.post("/admin/report/evidence", headers={"X-Report-Cron-Token": tc.TOKEN}, json={})
        self.assertEqual(res.status_code, 401)
        self.usage.assert_not_called()

    def test_ok(self):
        self.usage.return_value = (tr.ROWS, False)
        with mock.patch.object(dr, "period", return_value=(START, END)):
            res = self.post()
        d = res.get_json()
        self.assertEqual((res.status_code, d["total"], d["truncated"]), (200, 9, False))
        self.assertEqual(res.headers["Cache-Control"], "no-store")
        self.assertNotIn("secret@", res.get_data(as_text=True))

    def test_bad_request(self):
        for body in ({"date": None}, {"date": "x"}, {"key": 3}, {"text": 3}, {"until": "x"}):
            self.assertEqual(self.post(**{**body}).status_code, 400, body)
        self.assertEqual(self.post(key="gemini.limit").status_code, 404)
        self.usage.assert_not_called()

    def test_db_down(self):
        self.usage.side_effect = members.MemberError("x")
        self.assertEqual(self.post(date="2020-01-01").status_code, 503)


class RulesFileTest(unittest.TestCase):
    def test_sections(self):
        rules = report_crew.load_rules()
        for name, words in {"작성": ("사용량 → 성능 → 할 일", "현상 → 할 일"), "근거 표시": ("search.total", "나쁜 예"),
                            "사용 없음": ("추측",), "검토": ("no_usage가 true면", "빠짐없이")}.items():
            for w in words:
                self.assertIn(w, rules[name])

    def test_no_usage_sentence_same_as_python_check(self):
        self.assertIn(json.dumps(dr.NO_USAGE_DRAFT, ensure_ascii=False), report_crew.load_rules()["사용 없음"])
        self.assertIn(dr.NO_USAGE_DRAFT["summary"][0], report_crew.load_rules()["검토"])

    def test_missing_file_or_section(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "r.md"
            with mock.patch.object(report_crew, "RULES_FILE", f):
                with self.assertRaises(gemini.EngineError):
                    report_crew.load_rules()
                f.write_text("## 작성\n\n내용\n", encoding="utf-8")
                with self.assertRaisesRegex(gemini.EngineError, "검토"):
                    report_crew.load_rules()

    def test_daily_report_command_reads_it(self):
        self.assertIn("@report-rules.md", (ROOT / ".claude/commands/daily-report.md").read_text(encoding="utf-8"))

    def test_page(self):
        html = (ROOT / "admin.html").read_text(encoding="utf-8")
        for s in ('id="autoRun"', 'id="manualRun"', "/admin/report/runs", "/admin/report/evidence", "data-ev="):
            self.assertIn(s, html)

    @unittest.skipUnless(tr.HAS_CREWAI, "crewai 없음")
    def test_agents_use_the_file(self):
        text = report_crew.RULES_FILE.read_text(encoding="utf-8")
        text = text.replace("## 작성\n", "## 작성\n\n작성표시XYZ").replace("## 검토\n", "## 검토\n\n검토표시XYZ")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "r.md"
            f.write_text(text, encoding="utf-8")
            with mock.patch.object(report_crew, "RULES_FILE", f), mock.patch.object(report_crew.stats, "record_engine"), \
                    mock.patch.object(gemini, "chat_gemini", side_effect=[tr.OK, tr.PASS]) as chat:
                report_crew.run(dr.build(tr.ROWS, tr.EVENTS, NOW))
        prompts = [c[0][1] for c in chat.call_args_list]
        self.assertIn("작성표시XYZ", prompts[0])
        self.assertNotIn("검토표시XYZ", prompts[0])
        self.assertIn("검토표시XYZ", prompts[1])


if __name__ == "__main__":
    unittest.main()
