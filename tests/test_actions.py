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
PLAN = {"steps": [{"tool": "search", "query": "에어팟"}, {"tool": "pick", "by": "cheapest"}, {"tool": "add_favorite"}]}
RESULTS = {"query": "무선 마우스", "products": [{"brand": "A", "model": "M1", "price": 10000}]}
FAVS = [   # 화면이 보내는 찜 목록 (최신순)
    {"key": "k-pro2", "brand": "애플", "model": "에어팟 프로 2", "query": "에어팟", "price": 299000, "rating": 4.8},
    {"key": "k-kbd", "brand": "로지텍", "model": "K295", "query": "저소음 키보드", "price": 39000},
    {"key": "k-4", "brand": "애플", "model": "에어팟 4", "query": "무선 이어폰", "price": 199000, "rating": 4.6},
]
BODY = {"sentence": "저소음 키보드 찜해줘", "names": ["로지텍 저소음 키보드 K295", "앱코 K660"]}


def fake_fetch_user(token):
    if token in USERS:
        return USERS[token]
    raise auth.AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")


def plan(*steps):
    return {"steps": list(steps)}


class ValidateTest(unittest.TestCase):
    def test_single_steps(self):
        v = actions.validate
        self.assertEqual(v(plan({"tool": "add_favorite", "name": "  K295  키보드 "})),
                         [{"tool": "add_favorite", "args": {"name": "K295 키보드"}}])
        self.assertEqual(v(plan({"tool": "show_favorites", "x": 1})), [{"tool": "show_favorites", "args": {}}])
        self.assertEqual(v(plan({"tool": "search", "query": "a" * 500}))[0]["args"]["query"], "a" * actions.MAX_ARG)
        self.assertEqual(v(plan({"tool": "delete_account"})), [])     # 모르는 도구
        self.assertEqual(v(plan({"tool": "add_favorite"})), [])        # 앞에 pick 없이 이름도 없음
        self.assertEqual(v(plan()), [])
        self.assertEqual(v("not dict"), [])
        self.assertEqual(v({"steps": "x"}), [])

    def test_search_pick_add(self):
        """예: "에어팟 검색해서 제일 싼 거 찜해줘" → 3단계."""
        steps = actions.validate(plan({"tool": "search", "query": "에어팟"}, {"tool": "pick", "by": "cheapest"},
                                      {"tool": "add_favorite"}))
        self.assertEqual(steps, [{"tool": "search", "args": {"query": "에어팟"}},
                                 {"tool": "pick", "args": {"by": "cheapest"}},
                                 {"tool": "add_favorite", "args": {}}])

    def test_pick_criteria(self):
        one = lambda **kw: actions.validate(plan({"tool": "pick", **kw}))   # noqa: E731
        for by in ("cheapest", "top_rated", "first"):
            self.assertEqual(one(by=by), [{"tool": "pick", "args": {"by": by}}])
        self.assertEqual(one(by="brand", value=" 애플 ")[0]["args"], {"by": "brand", "value": "애플"})
        self.assertEqual(one(by="index", value="2")[0]["args"], {"by": "index", "value": 2})
        self.assertEqual(one(by="brand"), [])          # 브랜드 이름 없음
        self.assertEqual(one(by="index", value="둘"), [])
        self.assertEqual(one(by="index", value="0"), [])
        self.assertEqual(one(by="cheapest_ever"), [])

    def test_limits(self):
        s = {"tool": "show_favorites"}
        self.assertEqual(len(actions.validate(plan(s, s, s))), 3)
        self.assertEqual(actions.validate(plan(s, s, s, s)), [])   # 4단계 이상은 실행 안 함
        # 중간에 잘못된 단계가 있으면 전체를 실행하지 않음
        self.assertEqual(actions.validate(plan({"tool": "search", "query": "x"}, {"tool": "rm"})), [])
        # 새 검색 뒤에는 다시 pick 해야 이름 없는 찜이 됨
        self.assertEqual(actions.validate(plan({"tool": "pick", "by": "first"}, {"tool": "search", "query": "x"},
                                               {"tool": "add_favorite"})), [])

    def test_compare_favorites_step(self):
        self.assertEqual(actions.validate(plan({"tool": "compare_favorites", "condition": " 에어팟 "})),
                         [{"tool": "compare_favorites", "args": {"condition": "에어팟"}}])
        self.assertEqual(actions.validate(plan({"tool": "compare_favorites"}))[0]["args"], {"condition": ""})
        # 비교는 혼자만
        self.assertEqual(actions.validate(plan({"tool": "search", "query": "에어팟"}, {"tool": "compare_favorites"})), [])

    def test_match_favorites(self):
        picked, total = actions.match_favorites(FAVS, "에어팟")
        self.assertEqual(([p["key"] for p in picked], total), (["k-pro2", "k-4"], 2))
        self.assertEqual(actions.match_favorites(FAVS, "애플 에어팟")[1], 2)    # 단어가 모두 들어 있어야
        self.assertEqual(actions.match_favorites(FAVS, "에어 팟")[1], 2)        # 띄어쓰기 달라도
        self.assertEqual(actions.match_favorites(FAVS, "로지텍")[1], 1)
        self.assertEqual(actions.match_favorites(FAVS, "삼성")[1], 0)
        picked, total = actions.match_favorites(FAVS, "")                      # 조건 없으면 전체 중 최근 2개
        self.assertEqual(([p["key"] for p in picked], total), (["k-pro2", "k-kbd"], 3))
        self.assertEqual(actions.match_favorites("x", "a"), ([], 0))
        self.assertEqual(actions.match_favorites([{"brand": "x"}, 3], ""), ([], 0))

    def test_clean_names(self):
        self.assertEqual(actions.clean_names(["a  b", 3, "", None] + ["x"] * 20), ["a b"] + ["x"] * 9)
        self.assertEqual(actions.clean_names("not list"), [])

    def test_prompt_has_names(self):
        p = actions.build_prompt("찜해줘", ["A K1"])
        self.assertIn("- A K1", p)
        self.assertIn("찜해줘", p)


class PlanJsonTest(unittest.TestCase):
    """gemini.plan_json: 도구 없이 한 번, 응답 스키마를 강제하고 JSON만 꺼냄."""

    def test_one_call_with_schema(self):
        from google.genai import types
        client = mock.MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(
            text='{"steps":[{"tool":"show_favorites"}]}', usage_metadata=SimpleNamespace(total_token_count=7))
        usage = {}
        with mock.patch.object(gemini, "_client", return_value=(client, types)):
            out = gemini.plan_json("sys", "p", actions.SCHEMA, 5, usage)
        self.assertEqual(out, {"steps": [{"tool": "show_favorites"}]})
        self.assertEqual(usage["tokens"], 7)
        self.assertEqual(client.models.generate_content.call_count, 1)
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(config.response_json_schema, actions.SCHEMA)
        self.assertFalse(config.tools)   # 웹 검색 등 도구 없음 (실행은 화면에서)

    def test_bad_json_is_engine_error(self):
        from google.genai import types
        client = mock.MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text="nope", usage_metadata=None)
        with mock.patch.object(gemini, "_client", return_value=(client, types)), self.assertRaises(EngineError):
            gemini.plan_json("s", "p", actions.SCHEMA, 5)


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
            mock.patch.object(actions, "plan_json", return_value=PLAN),
            mock.patch.object(server.chat, "answer",
                              return_value={"answer": "추천: A M1", "pick_id": 0, "fallback": False, "engine": "gemini",
                                            "tokens": 3}),
        ]
        started = [p.start() for p in patches]
        self.pick, self.answer = started[-2], started[-1]
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
        self.assertEqual([x["tool"] for x in d["steps"]], ["search", "pick", "add_favorite"])
        self.assertEqual(d["steps"][1]["args"], {"by": "cheapest"})
        self.assertEqual(self.pick.call_count, 1)                    # Gemini는 한 번만
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

    def test_plan_fail_no_results_message(self):
        """계획 실패 + 검색 결과 없음 → 상담할 결과도 없으니 안내만 (한 번 기록)."""
        self.pick.side_effect = EngineError("Gemini API 사용 한도를 초과했어요.", 429)
        res = self.post(GOOD)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["message"], actions.NO_RESULTS)
        self.answer.assert_not_called()
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(members.quota._pending, {})

    # ---------- 계획을 못 세우면 상담으로 ----------

    def test_no_plan_goes_to_chat_once(self):
        self.pick.return_value = {"steps": []}
        res = self.post(GOOD, {**BODY, "results": RESULTS, "history": [{"role": "user", "text": "안녕"}]})
        d = res.get_json()
        self.assertEqual(res.status_code, 200, d)
        self.assertEqual(d["chat"], {"answer": "추천: A M1", "pick_id": 0})
        self.assertEqual(d["remaining"], 29)                       # 계획 + 상담 답변인데 한 번만 셈
        args = self.answer.call_args.args
        self.assertEqual(args[0], BODY["sentence"])                 # 같은 문장을 그대로 상담으로
        self.assertEqual(args[2], [{"role": "user", "text": "안녕"}])
        self.assertEqual([(x["action"], x["status"]) for x in self.logs], [("chat", "ok")])

    def test_plan_error_goes_to_chat(self):
        self.pick.side_effect = EngineError("Gemini 서버에 일시적인 문제가 있어요.", 503)
        d = self.post(GOOD, {**BODY, "results": RESULTS}).get_json()
        self.assertEqual(d["chat"]["answer"], "추천: A M1")

    def test_chat_answer_fails_502(self):
        self.pick.return_value = {"steps": []}
        self.answer.side_effect = EngineError("답변 중 오류가 났어요.", 500)
        res = self.post(GOOD, {**BODY, "results": RESULTS})
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.get_json()["error"], "답변 중 오류가 났어요.")
        self.assertEqual(self.logs[-1]["status"], "error")
        self.assertEqual(members.quota._pending, {})

    # ---------- compare_favorites ----------

    def test_compare_favorites(self):
        self.pick.return_value = {"steps": [{"tool": "compare_favorites", "condition": "에어팟"}]}
        self.answer.return_value = {"answer": "추천: 에어팟 4", "pick_id": 1, "fallback": False, "engine": "gemini",
                                    "tokens": 5, "reasons_fixed": 0}
        res = self.post(GOOD, {"sentence": "에어팟 찜한 거 두 개 비교해서 나은 상품 추천해", "favorites": FAVS})
        d = res.get_json()
        self.assertEqual(res.status_code, 200, d)
        self.assertEqual(d["compare"]["keys"], ["k-pro2", "k-4"])   # 키보드는 빠지고 에어팟 2개
        self.assertEqual(d["compare"]["pick_id"], 1)
        self.assertEqual(d["compare"]["matched"], 2)
        a = self.answer.call_args
        self.assertTrue(a.args[3])                                   # 기존 1:1 비교 모드 (compare.py 검증)
        self.assertEqual([p["model"] for p in a.args[1]["products"]], ["에어팟 프로 2", "에어팟 4"])
        self.assertEqual([(x["action"], x["status"]) for x in self.logs], [("compare", "ok")])
        self.assertEqual(d["remaining"], 29)

    def test_compare_needs_two(self):
        self.pick.return_value = {"steps": [{"tool": "compare_favorites", "condition": "키보드"}]}
        d = self.post(GOOD, {**BODY, "favorites": FAVS}).get_json()
        self.assertEqual(d["compare"], {"error": actions.NEED_TWO, "matched": 1})
        self.answer.assert_not_called()                              # 2개 미만이면 답변 엔진을 부르지 않음
        d = self.post(GOOD, {**BODY}).get_json()                     # 찜 목록을 안 보냄
        self.assertEqual(d["compare"]["matched"], 0)

    def test_no_results_still_works(self):
        """검색 결과가 없어도(names 없음) 계획을 받음."""
        res = self.post(GOOD, {"sentence": "에어팟 검색해서 제일 싼 거 찜해줘"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("(없음)", self.pick.call_args.args[1])
        self.assertEqual(len(res.get_json()["steps"]), 3)

    def test_unknown_tool_empty(self):
        self.pick.return_value = {"steps": [{"tool": "rm_rf"}]}
        d = self.post(GOOD).get_json()
        self.assertEqual(d["steps"], [])
        self.assertEqual(d["message"], actions.NO_RESULTS)   # 결과가 없어 상담으로도 못 넘김

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
