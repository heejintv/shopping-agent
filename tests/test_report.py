"""일일 운영 보고서 테스트: 집계·마스킹·숫자 대조·Crew 흐름·관리자 API. Gemini·Supabase는 가짜.
Crew 테스트는 crewai가 있을 때만 (Python 3.14에는 설치되지 않음).

실행: python -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))

import app as server  # noqa: E402
import auth  # noqa: E402
import daily_report as dr  # noqa: E402
import gemini  # noqa: E402
import members  # noqa: E402
import report_crew  # noqa: E402
import stats  # noqa: E402

KST = dr.KST
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=KST)
HAS_CREWAI = importlib.util.find_spec("crewai") is not None


def row(when, action="search", status="ok", query="마우스", uid="u-a", **kw):
    t = datetime.strptime(f"2026-{when}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    return {"created_at": t.isoformat(), "user_id": uid, "email": "secret@example.com", "action": action,
            "status": status, "query": query, "engine": "claude", "duration_ms": 2000, "tokens": None, **kw}


ROWS = [
    row("11-19 00:00"), row("11-19 23:59", duration_ms=4000),
    row("11-18 23:59"), row("11-20 00:00"),   # 어제가 아님
    row("11-19 10:00", status="cached", query="아이폰 16"),
    row("11-19 10:00", status="error"), row("11-19 10:01", status="error"), row("11-19 10:02", status="error"),
    row("11-19 10:03", status="timeout"),
    row("11-19 10:04", status="limited", uid="u-b"), row("11-19 10:05", status="limited", uid="u-b"),
    row("11-19 11:00", "chat", engine="gemini", tokens=100, query="뭐가 좋아?"),
    row("11-19 11:00", "compare", query="비교"),
    row("11-19 12:00", "chat", query="[실행] 찜해줘", engine="gemini", tokens=50),
    row("11-19 12:00", "chat", "error", query="[실행] kim@a.com 010-1234-5678로 보내", engine="gemini"),
]
T = datetime(2026, 11, 19, 10, 0, tzinfo=KST).timestamp()
EVENTS = [{"t": T, "kind": "search", "ok": False, "code": c} for c in ("401", "429", "504")]


def values(agg):
    return {m["id"]: m["value"] for m in agg["metrics"]}


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.agg = dr.build(ROWS, EVENTS, NOW, gemini_limit=1000)

    def test_counts(self):
        v = values(self.agg)
        self.assertEqual(self.agg["date"], "2026-11-19")
        for k, n in {"search.total": 9, "search.ok": 2, "search.cached": 1, "search.failed": 4, "search.limited": 2,
                     "search.fail_rate": 66.7, "search.fail_token": 1, "search.fail_limit": 1, "search.fail_timeout": 1,
                     "search.fail_other": 1, "search.avg_sec": 3.0, "chat.total": 2, "action.total": 2,
                     "action.failed": 1, "users.limited": 1, "gemini.calls": 3, "gemini.tokens": 150}.items():
            self.assertEqual(v[k], n, k)

    def test_failure_kinds_never_exceed_total(self):
        agg = values(dr.build([row("11-19 10:00", status="error")], EVENTS * 9, NOW))
        self.assertEqual(sum(agg[f"search.fail_{k}"] for k in ("token", "limit", "timeout", "other")), 1)

    def test_every_number_has_source_and_no_pii(self):
        self.assertTrue(all(m["source"] for m in self.agg["metrics"]))
        text = json.dumps(self.agg, ensure_ascii=False)
        for s in ("secret@", "kim@", "1234", "u-a", "u-b"):
            self.assertNotIn(s, text)
        self.assertIn("[이메일] [번호]로 보내", text)

    def test_mask(self):
        for s in ("900101-1234567", "1234-5678-1234-5678", "01012345678", "+82 10-1234-5678", "02-123-4567"):
            self.assertEqual(dr.mask_text(s), "[번호]", s)
        self.assertEqual(dr.mask_text("아이폰 16 1500000원 이하"), "아이폰 16 1500000원 이하")

    def test_empty_day(self):
        v = values(dr.build([], [], NOW))
        self.assertEqual((v["search.total"], v["search.avg_sec"], v["search.fail_rate"]), (0, None, None))

    def test_load_events_across_utc_files(self):
        start, end = dr.yesterday(NOW)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(stats, "LOG_DIR", Path(d)):
            for day, ts in (("2026-11-18", start.timestamp()), ("2026-11-19", end.timestamp() - 1), ("2026-11-20", end.timestamp())):
                stats._path(day).write_text(json.dumps({"events": [{"t": ts}]}))
            self.assertEqual(len(dr.load_events(start, end)), 2)

    def test_token_code_recorded(self):
        with mock.patch.object(stats, "_day", stats._empty(stats.date.today().isoformat())), mock.patch.object(stats, "_save"):
            stats.record_request("search", 1, ok=False, code=401)
            self.assertEqual(stats._day["events"][-1]["code"], "401")

    def test_query_has_range_and_no_email(self):
        with mock.patch.object(members, "select_rows", return_value=([], False)) as sel:
            members.usage_between(*dr.yesterday(NOW))
        p = sel.call_args[0][1]
        self.assertNotIn("email", p["select"])
        self.assertEqual(p["and"], '(created_at.gte."2026-11-19T00:00:00+09:00",created_at.lt."2026-11-20T00:00:00+09:00")')


def draft(line="검색은 9회[search.total]예요"):
    return {"summary": [line, "실패 4회[search.failed]", "평균 3초[search.avg_sec]"],
            "anomalies": ['2026-11-19 "아이폰 16" 1회[query.top2]'], "suggestions": ["제안 없음"]}


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.agg = dr.build(ROWS, EVENTS, NOW)

    def bad(self, line):
        return [c["reason"] for c in dr.check_numbers(draft(line), self.agg) if not c["ok"]]

    def test_ok(self):
        checks = dr.check_numbers(draft(), self.agg)
        self.assertEqual(len(checks), 4)
        self.assertTrue(all(c["ok"] and c["source"] for c in checks), checks)
        self.assertEqual(self.bad("실패율 66.7%[search.fail_rate]"), [])

    def test_bad(self):
        for line in ("검색 8회[search.total]", "검색 9회", "검색 9회[search.nope]", "9회와 2회[search.ok]",
                     "검색 9명[search.total]", "아이폰 16", "검색 세 번 실패", "9회[search.total] 중 10%"):
            self.assertTrue(self.bad(line), line)

    def test_null_metric(self):
        agg = dr.build([], [], NOW)
        self.assertFalse(dr.check_numbers(draft("평균 0초[search.avg_sec]"), agg)[0]["ok"])

    def test_summary_must_be_three(self):
        with self.assertRaises(ValueError):
            dr.clean_draft({"summary": ["한 줄"]})


OK, BAD = json.dumps(draft(), ensure_ascii=False), json.dumps(draft("검색은 8회[search.total]"), ensure_ascii=False)
PASS, REJECT = '{"pass": true, "problems": []}', '{"pass": false, "problems": ["항목이 다름"]}'


@unittest.skipUnless(HAS_CREWAI, "crewai 없음")
class CrewTest(unittest.TestCase):
    def run_crew(self, answers):
        self.prompts = []
        it = iter(answers)

        def fake(system, prompt, timeout, usage=None):
            self.prompts.append(system + prompt)
            a = next(it)
            if isinstance(a, Exception):
                raise a
            usage["tokens"] = 10
            return a

        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with mock.patch.object(gemini, "chat_gemini", side_effect=fake), mock.patch.object(stats, "record_engine"), \
                mock.patch.dict(os.environ, env, clear=True):
            return report_crew.run(dr.build(ROWS, EVENTS, NOW))

    def test_pass(self):
        out = self.run_crew([OK, PASS])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"], out["tokens"]), ("검토 통과", 2, 0, 20))
        self.assertIn('"search.total"', self.prompts[0])      # 집계값이 AI에 전달됨
        self.assertNotIn("secret@example.com", "".join(self.prompts))

    def test_wrong_number_fixed(self):
        out = self.run_crew([BAD, OK, PASS])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"]), ("검토 통과", 3, 1))
        self.assertIn("aggregates 값은 9", self.prompts[1])

    def test_reviewer_rejects_then_pass(self):
        out = self.run_crew([OK, REJECT, OK, PASS])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"]), ("검토 통과", 4, 1))

    def test_max_two_fixes(self):
        out = self.run_crew([BAD, BAD, BAD])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"]), ("수정 필요", 3, 2))

    def test_max_five_calls(self):
        out = self.run_crew([OK, REJECT, OK, REJECT, OK])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"], out["reviewed"]), ("수정 필요", 5, 2, False))

    def test_rate_limit_not_retried(self):
        err = gemini.EngineError("한도를 초과했어요 429 rate limit", 429)
        with self.assertRaises(gemini.EngineError) as cm:
            self.run_crew([err])
        self.assertEqual(cm.exception.code, 429)
        self.assertEqual(len(self.prompts), 1)

    def test_review_call_failure_is_not_pass(self):
        out = self.run_crew([OK, gemini.EngineError("x", 503)])
        self.assertEqual((out["verdict"], out["calls"]), ("수정 필요", 2))

    def test_unreadable_review_is_not_pass(self):
        self.assertEqual(self.run_crew([OK, "좋아요"])["verdict"], "수정 필요")

    def test_never_valid(self):
        with self.assertRaises(gemini.EngineError):
            self.run_crew(["글", "글", "글"])


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        users = {"user-token": {"id": "u-user", "email": "u@x.com"}, "admin-token": {"id": "u-admin", "email": "a@x.com"}}

        def fetch(token):
            if token in users:
                return users[token]
            raise auth.AuthError("로그인이 만료됐어요.")

        self.result = {"draft": draft(), "checks": [], "verdict": "검토 통과", "problems": [], "fixes": 0,
                       "calls": 2, "tokens": 0, "reviewed": True}
        ps = [mock.patch.object(auth, "fetch_user", side_effect=fetch),
              mock.patch.object(members, "get_role", side_effect=lambda uid, fresh=False: "admin" if uid == "u-admin" else "user"),
              mock.patch.object(members, "usage_between", return_value=([], False)),
              mock.patch.object(report_crew, "run", side_effect=lambda agg: dict(self.result)),
              mock.patch.object(server, "ASYNC_LOG", False),
              mock.patch.object(server, "_report", {"state": "none"}),
              mock.patch.object(server, "REPORT_DIR", Path(self.dir.name))]
        started = [p.start() for p in ps]
        self.usage, self.run_crew = started[2], started[3]
        for p in ps:
            self.addCleanup(p.stop)

    def call(self, method, path="", token="admin-token", **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return getattr(self.client, method)("/admin/report" + path, headers=headers, **kw)

    def test_permissions(self):
        for method, path in (("get", ""), ("post", ""), ("post", "/approve")):
            self.assertEqual(self.call(method, path, None).status_code, 401)
            self.assertEqual(self.call(method, path, "user-token").status_code, 403)
        self.usage.assert_not_called()
        self.run_crew.assert_not_called()

    def test_make_and_approve(self):
        res = self.call("post")
        self.assertEqual(res.status_code, 202)
        d = self.call("get").get_json()
        self.assertEqual((d["state"], d["verdict"]), ("draft", "검토 통과"))
        self.assertEqual(self.call("post", "/approve", json={"id": "다른것"}).status_code, 409)
        d = self.call("post", "/approve", json={"id": d["id"]}).get_json()
        self.assertEqual(d["state"], "approved")
        self.assertEqual(len(list(Path(self.dir.name).glob("report-*.json"))), 1)
        self.assertEqual(self.call("post", "/approve", json={"id": d["id"]}).status_code, 409)

    def test_redo_changes_id(self):
        a = self.call("post").get_json()["id"]
        self.assertNotEqual(a, self.call("post").get_json()["id"])
        self.assertEqual(self.call("post", "/approve", json={"id": a}).status_code, 409)

    def test_running_409(self):
        server._report = {"state": "running", "id": "x"}
        self.assertEqual(self.call("post").status_code, 409)

    def test_errors_shown(self):
        self.usage.side_effect = members.MemberError("down")
        self.assertEqual(self.call("post").get_json()["error"], server.MEMBER_DOWN)
        self.usage.side_effect = None
        self.run_crew.side_effect = gemini.EngineError("한도")
        self.assertEqual(self.call("post").get_json(), {"state": "error", "id": mock.ANY, "error": "한도"})


if __name__ == "__main__":
    unittest.main()
