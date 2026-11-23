"""일일 운영 보고서 테스트: 집계·마스킹·숫자 대조·Crew 흐름·관리자 API. Gemini·Supabase는 가짜.
Crew 테스트는 crewai가 있을 때만 (Python 3.14에는 설치되지 않음).

실행: python -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))

import app as server  # noqa: E402
import auth  # noqa: E402
import claude_errors  # noqa: E402
import daily_report as dr  # noqa: E402
import gemini  # noqa: E402
import make_report  # noqa: E402
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


class DayTest(unittest.TestCase):
    TODAY = date(2026, 11, 20)

    def test_period(self):
        self.assertEqual(dr.period(NOW), dr.yesterday(NOW))
        self.assertEqual(dr.period(NOW, date(2026, 11, 19)), dr.yesterday(NOW))
        self.assertEqual(dr.period(NOW, self.TODAY), (datetime(2026, 11, 20, tzinfo=KST), NOW))   # 지금까지만
        with self.assertRaises(ValueError):
            dr.period(NOW, date(2026, 11, 21))

    def test_kst_date_from_utc_now(self):
        """Render(UTC)에서 전날 저녁이어도 한국 날짜가 기준."""
        now = datetime.fromisoformat("2026-11-19T20:00:00+00:00")   # KST 11-20 05:00
        self.assertEqual(dr.build([], [], now, day=self.TODAY)["label"], "오늘 5시 기준 (진행 중)")

    def test_today_build(self):
        rows = ROWS + [row("11-20 08:59"), row("11-20 09:01")]
        agg = dr.build(rows, [], NOW, day=self.TODAY)
        self.assertEqual((agg["date"], agg["label"]), ("2026-11-20", "오늘 9시 기준 (진행 중)"))
        self.assertEqual(values(agg)["search.total"], 2)   # 00:00, 08:59 (지금 이후는 제외)

    def test_past_day_has_no_label(self):
        agg = dr.build(ROWS, [], NOW, day=date(2026, 11, 18))
        self.assertEqual((agg["date"], agg["label"], values(agg)["search.total"]), ("2026-11-18", None, 1))
        self.assertIsNone(dr.build(ROWS, [], NOW)["label"])

    def test_label_hour_is_not_a_metric(self):
        agg = dr.build([], [], NOW, day=self.TODAY)
        ok = dr.check_numbers(draft("오늘 9시 기준 (진행 중) 검색 0회[search.total]"), agg)
        self.assertEqual([c["number"] for c in ok if c["line"] == 1 and c["section"] == "summary"], ["0"])
        bad = dr.check_numbers(draft("오늘 8시 기준 (진행 중)"), agg)
        self.assertFalse(bad[0]["ok"])

    def test_parse_day(self):
        self.assertIsNone(dr.parse_day(None))
        self.assertEqual(dr.parse_day("2026-11-19"), date(2026, 11, 19))
        for bad in ("2026-13-01", "20261119", "../../x", 5, ["2026-11-19"]):
            with self.assertRaises(ValueError):
                dr.parse_day(bad)


def draft(line="검색은 9회[search.total]예요", extra=None):
    return {"summary": [line, "실패 4회[search.failed]", "평균 3초[search.avg_sec]"],
            "anomalies": ['2026-11-19 "아이폰 16" 1회[query.top2]', *(extra or [])], "suggestions": ["제안 없음"]}


class RuleTest(unittest.TestCase):
    def judged(self, rows, **kw):
        agg = dr.build(rows, [], NOW, **kw)
        return {a["id"]: a for a in agg["anomalies"]}, agg

    def searches(self, ok, fail=0, **kw):
        return [row("11-19 10:00", **kw)] * ok + [row("11-19 10:00", status="error", error_kind="limit")] * fail

    def test_fail_rate(self):
        j, agg = self.judged(self.searches(6, 1))
        self.assertEqual(j["rule.fail_rate"]["text"], "검색 실패율 14.3%[search.fail_rate] > 기준 10%[rule.fail_rate]")
        self.assertFalse(self.judged(self.searches(9, 1))[0]["rule.fail_rate"]["triggered"])   # 딱 10%는 아님
        v = self.judged(self.searches(2240, 250))[0]["rule.fail_rate"]   # 10.04%: 반올림해도 기준과 구별
        self.assertTrue(v["triggered"])
        self.assertNotEqual(v["value"], 10)

    def test_avg_sec(self):
        self.assertFalse(self.judged(self.searches(1, duration_ms=45000))[0]["rule.avg_sec"]["triggered"])
        self.assertTrue(self.judged(self.searches(1, duration_ms=45001))[0]["rule.avg_sec"]["triggered"])

    def test_other_rate(self):
        other, known = row("11-19 10:00", status="error", error_kind="other"), self.searches(0, 1)
        self.assertTrue(self.judged([other] + known)[0]["rule.other_rate"]["triggered"])        # 50% 이상
        self.assertFalse(self.judged([other] + known * 2)[0]["rule.other_rate"]["triggered"])

    def test_gemini_rate(self):
        g = [row("11-19 10:00", "chat", engine="gemini")]
        self.assertFalse(self.judged(g * 8, gemini_limit=10)[0]["rule.gemini_rate"]["triggered"])
        self.assertTrue(self.judged(g * 9, gemini_limit=10)[0]["rule.gemini_rate"]["triggered"])
        self.assertIsNone(self.judged(g * 9)[0]["rule.gemini_rate"]["triggered"])   # 한도 설정 없음

    def test_limited_user_and_empty(self):
        self.assertTrue(self.judged([row("11-19 10:00", status="limited")])[0]["rule.limited_users"]["triggered"])
        self.assertFalse(any(a["triggered"] for a in self.judged([])[0].values()))

    def test_rule_text_passes_number_check(self):
        _, agg = self.judged(self.searches(6, 1) + [row("11-19 10:00", status="limited")])
        d = draft(extra=triggered(agg))
        self.assertTrue(all(c["ok"] for c in dr.check_numbers({**d, "summary": [], "anomalies": triggered(agg)}, agg)))
        self.assertEqual(dr.missing_anomalies(d, agg), [])
        self.assertEqual(len(dr.missing_anomalies(draft(), agg)), 2)


class BadCommandTest(unittest.TestCase):
    def cmd(self, outcome, text="찜해줘", **kw):
        return row("11-19 10:00", "chat", query="[실행] " + text, outcome=outcome, **kw)

    def test_counts_and_top(self):
        rows = [self.cmd(k, "kim@a.com 찜") for k in dr.BAD_OUTCOMES] + [
            self.cmd("ok"), self.cmd("waiting"), self.cmd(None), self.cmd("cannot", "비교"), self.cmd(None, status="error")]
        agg = dr.build(rows, [], NOW)
        v = values(agg)
        self.assertEqual((v["action.bad"], v["action.bad_cannot"], v["action.bad_error"], v["action.unfinished"]), (9, 2, 2, 1))
        top = {m["id"]: m for m in agg["metrics"]}["action.bad_top1"]
        self.assertEqual((top["text"], top["value"]), ("[이메일] 찜", 7))
        self.assertIn("승인 카드 취소", top["label"])
        self.assertTrue(any("outcome" in n for n in agg["notes"]))

    def test_top_five(self):
        agg = dr.build([self.cmd("cannot", f"명령{c}") for c in "가나다라마바"], [], NOW)
        self.assertEqual(sum(m["id"].startswith("action.bad_top") for m in agg["metrics"]), 5)


class ErrorKindTest(unittest.TestCase):
    def test_saved_kind_first(self):
        rows = [row("11-19 10:00", status="error", error_kind=k) for k in ("token", "limit", "server", "db", "timeout")]
        v = values(dr.build(rows + [row("11-19 10:00", status="error")], EVENTS, NOW))
        # 저장된 종류가 EVENTS의 401·429·504와 같은 실패라서 두 번 세지 않음 → 종류 없는 1건은 기타
        self.assertEqual([v[f"search.fail_{k}"] for k in dr.FAIL_KINDS], [1, 1, 1, 1, 1, 1])

    def test_classify(self):
        E = gemini.EngineError
        for e, want in ((E("x", 401), "token"), (E("x", 429), "limit"), (E("x", 504), "timeout"), (E("x", 503), "server"),
                        (members.MemberError("x"), "db"), (subprocess.TimeoutExpired("c", 1), "timeout"),
                        (E("GEMINI_API_KEY 환경변수가 설정되지 않았어요."), "token"), (E("JSON을 찾지 못했어요."), "other"),
                        (KeyError("x"), "other")):
            self.assertEqual(claude_errors.error_kind(e), want, e)
        e = E("너무 오래")
        e.__cause__ = TimeoutError()
        self.assertEqual(claude_errors.error_kind(e), "timeout")


class NewColumnTest(unittest.TestCase):
    """schema.sql을 아직 실행하지 않은 DB에서도 기록(하루 횟수)과 보고서가 동작."""

    def test_log_falls_back(self):
        sent = []

        def req(method, path, params=None, body=None, extra=None):
            sent.append(body)
            if "outcome" in body:
                raise members.MemberError("x", 400)

        with mock.patch.object(members, "_request", side_effect=req), self.assertLogs(members.log, "WARNING"):
            members.log_usage("u", "e", "chat", "ok", outcome="waiting", log_id="L")
        self.assertEqual(len(sent), 2)
        self.assertNotIn("log_id", sent[1])

    def test_other_errors_not_retried(self):
        with mock.patch.object(members, "_request", side_effect=members.MemberError("x", 500)) as req, \
                self.assertLogs(members.log, "WARNING"):
            members.log_usage("u", "e", "chat", "ok", outcome="ok")
        self.assertEqual(req.call_count, 1)

    def test_read_falls_back(self):
        with mock.patch.object(members, "select_rows", side_effect=[members.MemberError("x", 400), ([], False)]) as sel, \
                self.assertLogs(members.log, "WARNING"):
            members.usage_between(*dr.yesterday(NOW))
        self.assertNotIn("outcome", sel.call_args[0][1]["select"])

    def test_update(self):
        with mock.patch.object(members, "_request") as req:
            members.update_outcome("L", "cannot")
        self.assertEqual(req.call_args[0], ("PATCH", "usage_logs", {"log_id": "eq.L"}))
        self.assertEqual(req.call_args[1]["body"], {"outcome": "cannot"})


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

    def test_key_exposed(self):
        for line, n in (("action.bad_top1 명령이 문제", 1), ("search.total은 9회[search.total]", 1), ("Action.Failed_top3 확인", 1)):
            bad = [c for c in dr.check_numbers(draft(line), self.agg) if not c["ok"]]
            self.assertEqual(len(bad), n, bad)                       # 키 속 숫자는 따로 세지 않음
            self.assertIn("내부 키 노출", bad[0]["reason"])
            self.assertEqual(bad[0]["number"], "")
        self.assertEqual(self.bad("검색 9회[search.total], naver.com 확인"), [])   # 근거 안의 키·일반 주소는 괜찮음

    def test_key_in_quoted_user_text_ok(self):
        agg = dr.build([row("11-19 10:00", query="search.total 2")], [], NOW)
        self.assertEqual([c for c in dr.check_numbers(draft('"search.total 2" 1회[query.top1]'), agg)
                          if not c["ok"] and c["section"] == "summary" and c["line"] == 1], [])

    def test_two_keys_in_one_bracket(self):
        reasons = self.bad("9회[search.total, search.ok]")
        self.assertIn("근거 형식이 잘못됨", reasons[0])

    def test_suggestion_format(self):
        ok = ["찜 요청이 취소됨 → 비슷한 결과 먼저 보여주기", "제안 없음"]
        self.assertEqual(dr.bad_suggestions({"suggestions": ok}), [])
        for s in ("문구를 바꾸기", "현상 →", "→ 할 일", "가 → 나 → 다"):
            self.assertEqual(len(dr.bad_suggestions({"suggestions": [s]})), 1, s)

    def test_null_metric(self):
        agg = dr.build([], [], NOW)
        self.assertFalse(dr.check_numbers(draft("평균 0초[search.avg_sec]"), agg)[0]["ok"])

    def test_summary_must_be_three(self):
        with self.assertRaises(ValueError):
            dr.clean_draft({"summary": ["한 줄"]})


def triggered(agg):
    return [a["text"] for a in agg["anomalies"] if a["triggered"]]


HITS = triggered(dr.build(ROWS, EVENTS, NOW))   # 이 날의 이상 징후: 초안에 꼭 있어야 함
class RankTest(unittest.TestCase):
    """"N위"는 같은 줄의 근거 키 순위와 맞으면 통과. SUGGESTIONS는 실제로 받은 초안의 개선 제안 3줄."""

    COMMANDS = (("에어팟 찜해줘", "search_cancelled", 1), ("키보드 찜해줘", "approve_cancelled", 1),
                ("제일 싼 거 찜해줘", "approve_cancelled", 1))
    SUGGESTIONS = [
        '⚡ 명령 개선 제안. 잘 안 된 ⚡ 명령 1위 (검색 확인에서 취소)인 "에어팟 찜해줘" 1회[action.bad_top1] → 검색 횟수 안내 문구 대신 비슷한 결과를 먼저 보여주기 검토',
        '⚡ 명령 개선 제안. 잘 안 된 ⚡ 명령 2위 (승인 카드 취소)인 "키보드 찜해줘" 1회[action.bad_top2] → 승인 단계에서 상품 정보를 더 명확하게 확인하도록 UI 개선 검토',
        '⚡ 명령 개선 제안. 잘 안 된 ⚡ 명령 3위 (승인 카드 취소)인 "제일 싼 거 찜해줘" 1회[action.bad_top3] → 최저가 정렬 결과를 즉시 제시하고 바로 승인할 수 있게 연결 개선 검토',
    ]

    def setUp(self):
        rows = [row("11-19 10:00", "chat", query="[실행] " + t, outcome=o) for t, o, n in self.COMMANDS for _ in range(n)]
        self.agg = dr.build(rows, [], NOW)

    def checks(self, *lines):
        return dr.check_numbers({"summary": [], "anomalies": [], "suggestions": list(lines)}, self.agg)

    def bad(self, line):
        return [c for c in self.checks(line) if not c["ok"]]

    def test_aggregates_match_real_report(self):
        got = {m["id"]: (m["label"], m["text"], m["value"]) for m in self.agg["metrics"] if m["id"].startswith("action.bad_top")}
        self.assertEqual(got, {
            "action.bad_top1": ("잘 안 된 ⚡ 명령 1위 (검색 확인에서 취소)", "에어팟 찜해줘", 1),
            "action.bad_top2": ("잘 안 된 ⚡ 명령 2위 (승인 카드 취소)", "키보드 찜해줘", 1),
            "action.bad_top3": ("잘 안 된 ⚡ 명령 3위 (승인 카드 취소)", "제일 싼 거 찜해줘", 1)})

    def test_three_suggestions_pass(self):
        checks = self.checks(*self.SUGGESTIONS)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        self.assertEqual([c["number"] for c in checks if c["label"] == "순위"], ["1위", "2위", "3위"])
        self.assertEqual(dr.bad_suggestions({"suggestions": self.SUGGESTIONS}), [])

    def test_rank_right_before_key(self):
        self.assertEqual(self.bad("1위[action.bad_top1]"), [])

    def test_wrong_rank(self):
        for line in ("2위 1회[action.bad_top1]", "1위 명령", "1위 9회[action.bad_top9]", "2위[action.bad_top1]",
                     "1위와 2위 1회[action.bad_top1]", "1위 3회[action.bad]"):
            self.assertTrue(self.bad(line), line)

    def test_not_a_rank(self):
        self.assertTrue(self.bad("1 위치 1회[action.bad_top1]"))

    def test_value_still_checked(self):
        self.assertTrue(self.bad("1위 4회[action.bad_top1]"))

    def test_json_mime_type(self):
        seen = []
        types = mock.Mock()
        types.GenerateContentConfig = lambda **kw: seen.append(kw)
        client = mock.Mock()
        client.models.generate_content.return_value = mock.Mock(text="{}", usage_metadata=None)
        with mock.patch.object(gemini, "_client", return_value=(client, types)):
            gemini.chat_gemini("s", "p", 5, json_mode=True)
            gemini.chat_gemini("s", "p", 5)
        self.assertEqual(seen[0]["response_mime_type"], "application/json")
        self.assertNotIn("response_mime_type", seen[1])   # 상담 챗봇은 그대로


OK = json.dumps(draft(extra=HITS), ensure_ascii=False)
BAD = json.dumps(draft("검색은 8회[search.total]", HITS), ensure_ascii=False)
MISSING = json.dumps(draft(), ensure_ascii=False)
PASS, REJECT = '{"pass": true, "problems": []}', '{"pass": false, "problems": ["항목이 다름"]}'


@unittest.skipUnless(HAS_CREWAI, "crewai 없음")
class CrewTest(unittest.TestCase):
    def run_crew(self, answers):
        self.prompts = []
        it = iter(answers)

        def fake(system, prompt, timeout, usage=None, json_mode=False):
            self.assertTrue(json_mode)   # 보고서는 항상 JSON 형식 강제
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

    def test_today_label_sent_to_ai(self):
        agg = dr.build(ROWS, EVENTS, NOW, day=date(2026, 11, 20))
        line = f'{agg["label"]} 검색 1회[search.total]'
        text = json.dumps({"summary": [line, "a", "b"], "anomalies": [], "suggestions": []}, ensure_ascii=False)
        with mock.patch.object(gemini, "chat_gemini", side_effect=[text, PASS]) as chat, mock.patch.object(stats, "record_engine"):
            out = report_crew.run(agg)
        self.assertEqual(out["verdict"], "검토 통과")
        self.assertIn(agg["label"], chat.call_args_list[0][0][1])

    def test_wrong_number_fixed(self):
        out = self.run_crew([BAD, OK, PASS])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"]), ("검토 통과", 3, 1))
        self.assertIn("aggregates 값은 9", self.prompts[1])

    def test_bad_json_retried_once_not_a_fix(self):
        out = self.run_crew(["{깨진", OK, PASS])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"]), ("검토 통과", 3, 0))
        self.assertEqual(self.prompts[0], self.prompts[1])   # 같은 요청

    def test_review_json_retried_once(self):
        out = self.run_crew([OK, "{깨진", PASS])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"]), ("검토 통과", 3, 0))
        self.assertEqual(self.prompts[1], self.prompts[2])

    def test_twice_broken_then_counts_as_fix(self):
        out = self.run_crew(["글", "글", OK, PASS])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"]), ("검토 통과", 4, 1))

    def test_retry_stays_within_five_calls(self):
        out = self.run_crew([OK, REJECT, OK, REJECT, "글"])
        self.assertEqual((out["verdict"], out["calls"]), ("수정 필요", 5))

    def test_ranked_suggestions_pass_whole_flow(self):
        agg_rows = [row("11-19 10:00", "chat", query="[실행] " + t, outcome=o)
                    for t, o, n in RankTest.COMMANDS for _ in range(n)]
        agg = dr.build(agg_rows, [], NOW)
        d = {"summary": ["잘 안 된 명령 3회[action.bad]", "가", "나"], "anomalies": ["특이 사항 없음"],
             "suggestions": RankTest.SUGGESTIONS}
        with mock.patch.object(gemini, "chat_gemini", side_effect=[json.dumps(d, ensure_ascii=False), PASS]), \
                mock.patch.object(stats, "record_engine"):
            out = report_crew.run(agg)
        self.assertEqual((out["verdict"], out["calls"]), ("검토 통과", 2), out["problems"])

    def test_prompt_examples(self):
        self.run_crew([OK, PASS])
        for s in ("내부 키", "나쁜 예: action.bad_top1", "현상 → 할 일"):
            self.assertIn(s, self.prompts[0])

    def test_review_comments_passed_verbatim_and_kept(self):
        key = json.dumps(draft("action.bad_top1이 문제", HITS), ensure_ascii=False)
        out = self.run_crew([OK, REJECT, key, OK, PASS])
        self.assertEqual((out["verdict"], out["fixes"]), ("검토 통과", 2))
        self.assertIn("1차 검토: 항목이 다름", self.prompts[2])          # 검토 Agent의 문장 그대로
        self.assertIn("1차 검토: 항목이 다름", self.prompts[3])          # 두 번째 수정에도 앞선 지적을 함께
        self.assertIn("2차 검토: summary 1번째 줄 action.bad_top1: 내부 키 노출", self.prompts[3])

    def test_bad_suggestion_sent_back(self):
        d = {**draft(extra=HITS), "suggestions": ["문구를 바꾸기"]}
        out = self.run_crew([json.dumps(d, ensure_ascii=False), OK, PASS])
        self.assertEqual((out["verdict"], out["fixes"]), ("검토 통과", 1))
        self.assertIn("형식이 아님", self.prompts[1])

    def test_missing_anomaly_fixed(self):
        out = self.run_crew([MISSING, OK, PASS])
        self.assertEqual((out["verdict"], out["calls"], out["fixes"]), ("검토 통과", 3, 1))
        self.assertIn("이상 징후 빠짐: " + HITS[0], self.prompts[1])
        self.assertIn("triggered가 true인 항목이 초안 anomalies에 빠짐없이", self.prompts[2])   # 검토 Agent도 확인

    def test_missing_anomaly_never_passes(self):
        out = self.run_crew([MISSING] * 3)
        self.assertEqual(out["verdict"], "수정 필요")
        self.assertIn("이상 징후 빠짐", out["problems"][0])

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
        self.assertEqual(self.run_crew([OK, "좋아요", "좋아요"])["verdict"], "수정 필요")

    def test_never_valid(self):
        with self.assertRaises(gemini.EngineError):
            self.run_crew(["글"] * 5)
        self.assertEqual(len(self.prompts), 5)


class NoCrewaiTest(unittest.TestCase):
    """crewai가 없는 환경 (sys.modules에 None을 넣으면 import가 ImportError)."""

    def test_unavailable_message(self):
        with mock.patch.dict(sys.modules, {"crewai": None}), mock.patch.dict(os.environ), \
                mock.patch.object(gemini, "chat_gemini") as chat:
            self.assertFalse(report_crew.available())
            with self.assertRaises(gemini.EngineError) as cm:
                report_crew.run(dr.build([], [], NOW))
        self.assertEqual(str(cm.exception), "이 환경에서는 보고서 기능을 쓸 수 없어요 (Python 3.12 필요)")
        chat.assert_not_called()

    def run_server(self, before, after):
        """새 파이썬에서 앱을 불러와 /health까지 확인."""
        code = (f"import sys\n{before}\nimport app\n"
                f"assert app.app.test_client().get('/health').status_code == 200\n{after}")
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                               env={**os.environ, "DATA_DIR": d, "RENDER": "true"})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_server_starts_without_crewai(self):
        self.run_server("sys.modules['crewai'] = None", "")

    def test_crewai_not_loaded_at_start(self):
        self.run_server("", "assert 'crewai' not in sys.modules")


class CliTest(unittest.TestCase):
    def run_cli(self, available, state):
        def fake(report_id):
            server._report = {"state": state, "id": report_id}

        out = io.StringIO()
        with mock.patch.object(report_crew, "available", return_value=available), \
                mock.patch.object(server, "_report", {"state": "none"}), \
                mock.patch.object(server, "_make_report", side_effect=fake) as make, contextlib.redirect_stdout(out):
            code = make_report.main()
        return code, out.getvalue(), make

    def test_no_crewai(self):
        code, out, make = self.run_cli(False, "draft")
        self.assertEqual((code, out.strip()), (2, report_crew.UNAVAILABLE))
        make.assert_not_called()

    def test_result_and_error(self):
        code, out, _ = self.run_cli(True, "draft")
        self.assertEqual((code, json.loads(out)["state"]), (0, "draft"))
        self.assertEqual(self.run_cli(True, "error")[0], 1)


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
              mock.patch.object(report_crew, "available", return_value=True),
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

    def test_date_choice_and_file_names(self):
        today = datetime.now(KST).date()
        for day, partial in ((today - timedelta(days=5), False), (today, True)):
            d = self.call("post", json={"date": day.isoformat()}).get_json()
            self.assertEqual((d["aggregates"]["date"], bool(d["aggregates"]["label"])), (day.isoformat(), partial))
            self.call("post", "/approve", json={"id": d["id"]})
        names = sorted(p.name for p in Path(self.dir.name).iterdir())
        self.assertIn(f"report-{today - timedelta(days=5)}.json", names)
        self.assertRegex(names[-1], rf"^report-{today}-진행중-\d{{4}}\.json$")
        # 날짜를 안 보내면 어제
        self.assertEqual(self.call("post").get_json()["aggregates"]["date"], (today - timedelta(days=1)).isoformat())

    def test_bad_date_400(self):
        future = (datetime.now(KST).date() + timedelta(days=1)).isoformat()
        for bad in (future, "어제", 3):
            self.assertEqual(self.call("post", json={"date": bad}).status_code, 400)
        self.usage.assert_not_called()

    def test_redo_changes_id(self):
        a = self.call("post").get_json()["id"]
        self.assertNotEqual(a, self.call("post").get_json()["id"])
        self.assertEqual(self.call("post", "/approve", json={"id": a}).status_code, 409)

    def test_running_409(self):
        server._report = {"state": "running", "id": "x"}
        self.assertEqual(self.call("post").status_code, 409)

    def test_no_crewai_503(self):
        with mock.patch.object(report_crew, "available", return_value=False):
            res = self.call("post")
            self.assertEqual(self.call("post", token="user-token").status_code, 403)   # 권한 검사가 먼저
        self.assertEqual((res.status_code, res.get_json()), (503, {"error": report_crew.UNAVAILABLE}))
        self.usage.assert_not_called()
        self.run_crew.assert_not_called()
        self.assertEqual(self.call("get").get_json(), {"state": "none"})

    def test_errors_shown(self):
        self.usage.side_effect = members.MemberError("down")
        self.assertEqual(self.call("post").get_json()["error"], server.MEMBER_DOWN)
        self.usage.side_effect = None
        self.run_crew.side_effect = gemini.EngineError("한도")
        self.assertEqual(self.call("post").get_json(), {"state": "error", "id": mock.ANY, "error": "한도"})


if __name__ == "__main__":
    unittest.main()
