"""⚡ 문장으로 실행 (Gemini Function Calling + ReAct) 테스트. Gemini·Supabase는 가짜로 바꾼다.

- actions.check_call: 등록된 함수·입력값·권한 검사
- actions.clean_contents: 화면이 보낸 대화 기록(functionCall/functionResponse) 검사
- actions.Commands: 명령 하나에 상담 1회, Gemini 최대 5번
- gemini.step_call: 함수 선언 등록, functionCall·최종 답·thoughtSignature 읽기
- /chat/action/step, /chat/action/compare: 로그인·한도·반복 상한·거부 사유

실행: python -m unittest discover -s tests -v
"""

import base64
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
OTHER = {"Authorization": "Bearer other-token"}
ADMIN = {"Authorization": "Bearer admin-token"}
USERS = {"good-token": {"id": "u-good", "email": "u@example.com"},
         "other-token": {"id": "u-other", "email": "o@example.com"},
         "admin-token": {"id": "u-admin", "email": "a@example.com"}}
USER = USERS["good-token"]
SIG = base64.b64encode(b"thought-signature-bytes").decode()
FAVS = [   # 화면이 보내는 찜 목록 (최신순)
    {"key": "k-pro2", "brand": "애플", "model": "에어팟 프로 2", "query": "에어팟", "price": 299000, "rating": 4.8},
    {"key": "k-kbd", "brand": "로지텍", "model": "K295", "query": "저소음 키보드", "price": 39000},
    {"key": "k-4", "brand": "애플", "model": "에어팟 4", "query": "무선 이어폰", "price": 199000, "rating": 4.6},
]


def fake_fetch_user(token):
    if token in USERS:
        return USERS[token]
    raise auth.AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")


def user_turn(text="휴대용 마우스 찜해줘"):
    return {"role": "user", "parts": [{"text": text}]}


def call_turn(name, args=None, sig=SIG):
    part = {"functionCall": {"name": name, "args": args or {}}}
    if sig:
        part["thoughtSignature"] = sig
    return {"role": "model", "parts": [part]}


def resp_turn(name, response):
    return {"role": "user", "parts": [{"functionResponse": {"name": name, "response": response}}]}


CANDS = {"candidates": [{"number": 1, "name": "로지텍 M196"}, {"number": 2, "name": "로지텍 Pebble"}], "total": 2}


class CheckCallTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(actions.check_call("find_in_results", {"description": "  휴대용   마우스 "}, USER),
                         ({"description": "휴대용 마우스"}, None))
        self.assertEqual(actions.check_call("add_favorite", {"number": 2.0}, USER), ({"number": 2}, None))
        self.assertEqual(actions.check_call("get_favorites", {}, USER), ({}, None))
        self.assertEqual(actions.check_call("open_favorites", {}, USER), ({}, None))
        self.assertEqual(actions.check_call("remove_favorite", {"favorite_number": 1}, USER), ({"favorite_number": 1}, None))
        self.assertEqual(actions.check_call("compare_favorites", {}, USER), ({}, None))
        args, why = actions.check_call("ask_user", {"question": "어느 거요?", "options": ["1. A", " ", "2. B"] + ["x"] * 9}, USER)
        self.assertIsNone(why)
        self.assertEqual(args["options"][:2], ["1. A", "2. B"])
        self.assertEqual(len(args["options"]), 6)
        self.assertEqual(actions.check_call("remove_favorite", {"name": "K295"}, USER), ({"name": "K295"}, None))

    def test_rejects(self):
        cases = [
            ("delete_account", {}, USER, "등록되지 않은 함수"),
            ("add_favorite", {"number": 1}, None, "로그인이 필요"),           # 찜은 로그인 필요
            ("get_favorites", {}, None, "로그인이 필요"),
            ("open_favorites", {}, None, "로그인이 필요"),
            ("show_favorites", {}, USER, "등록되지 않은 함수"),                 # 예전 이름은 없음
            ("remove_favorite", {"favorite_number": 0}, USER, "1~50"),
            ("add_favorite", {}, USER, "number 값이 빠졌어요"),
            ("add_favorite", {"number": "2"}, USER, "형식이 잘못"),
            ("add_favorite", {"number": True}, USER, "형식이 잘못"),
            ("add_favorite", {"number": 0}, USER, "1~50"),
            ("add_favorite", {"number": 1, "price": 3}, USER, "모르는 입력값"),
            ("search", {"query": "  "}, USER, "query 값이 빠졌어요"),
            ("remove_favorite", {}, USER, "favorite_number·number·name 중 하나"),
            ("ask_user", {"options": ["a"]}, USER, "question 값이 빠졌어요"),
            ("ask_user", {"question": "q", "options": "a"}, USER, "형식이 잘못"),
        ]
        for name, args, user, want in cases:
            out, why = actions.check_call(name, args, user)
            self.assertIsNone(out, name)
            self.assertIn(want, why, (name, args))

    def test_find_and_ask_need_no_login(self):
        self.assertIsNone(actions.check_call("find_in_results", {"description": "x"}, None)[1])
        self.assertIsNone(actions.check_call("ask_user", {"question": "q"}, None)[1])


class CleanContentsTest(unittest.TestCase):
    def test_ok_chain(self):
        raw = [user_turn("  휴대용  마우스 찜해줘 "), call_turn("find_in_results", {"description": "휴대용 마우스"}),
               resp_turn("find_in_results", CANDS), call_turn("ask_user", {"question": "어느 거?"}, sig=None),
               resp_turn("ask_user", {"answer": "2. 로지텍 Pebble"})]
        out = actions.clean_contents(raw)
        self.assertEqual(out[0]["parts"][0]["text"], "휴대용 마우스 찜해줘")
        self.assertEqual(out[1]["parts"][0]["thoughtSignature"], SIG)
        self.assertNotIn("thoughtSignature", out[3]["parts"][0])
        self.assertEqual(out[4]["parts"][0]["functionResponse"]["response"], {"answer": "2. 로지텍 Pebble"})

    def test_bad(self):
        long = {"x": "가" * (actions.MAX_RESPONSE + 1)}
        cases = [
            None, [], "x", [{"role": "model", "parts": [{"text": "hi"}]}],
            [user_turn("")], [user_turn("가" * 201)],
            [user_turn(), call_turn("find_in_results")],                                   # 결과가 빠짐
            [user_turn(), call_turn("rm_rf"), resp_turn("rm_rf", {})],                       # 등록 안 된 함수
            [user_turn(), call_turn("search"), resp_turn("find_in_results", {})],            # 이름이 다름
            [user_turn(), call_turn("search"), resp_turn("search", "ok")],                   # 결과가 dict 아님
            [user_turn(), call_turn("search"), resp_turn("search", long)],
            [user_turn(), call_turn("search", sig="!!not base64!!"), resp_turn("search", {})],
            [user_turn(), {"role": "model", "parts": [{"functionCall": {"name": "search"}, "text": "x"}]},
             resp_turn("search", {})],
            [user_turn(), user_turn()],
            [user_turn()] + [call_turn("search"), resp_turn("search", {})] * (actions.MAX_CALLS + 1),
        ]
        for raw in cases:
            with self.assertRaises(ValueError, msg=str(raw)[:80]):
                actions.clean_contents(raw)


class CommandsTest(unittest.TestCase):
    def test_limit_and_owner(self):
        c = actions.Commands()
        cmd = c.start("u1")
        self.assertEqual(c.left(cmd), actions.MAX_CALLS - 1)
        self.assertEqual(c.use(cmd, "u2"), actions.CMD_EXPIRED)       # 다른 사람 명령
        for _ in range(actions.MAX_CALLS - 1):
            self.assertIsNone(c.use(cmd, "u1"))
        self.assertEqual(c.use(cmd, "u1"), actions.TOO_MANY)          # 5번 넘음
        self.assertEqual(c.use("nope", "u1"), actions.CMD_EXPIRED)
        self.assertEqual(c.use(None, "u1"), actions.CMD_EXPIRED)

    def test_expire_and_drop(self):
        c = actions.Commands()
        cmd = c.start("u1")
        with mock.patch.object(actions.time, "time", return_value=actions.time.time() + actions.CMD_TTL + 1):
            self.assertEqual(c.use(cmd, "u1"), actions.CMD_EXPIRED)
        cmd = c.start("u1")
        c.drop(cmd)
        self.assertEqual(c.use(cmd, "u1"), actions.CMD_EXPIRED)


class StepCallTest(unittest.TestCase):
    """gemini.step_call: 공식 Function Calling (functionDeclarations), 실행은 안 함."""

    def run_with(self, parts):
        from google.genai import types
        client = mock.MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
            usage_metadata=SimpleNamespace(total_token_count=11))
        usage = {}
        contents = actions.clean_contents([user_turn(), call_turn("find_in_results", {"description": "마우스"}),
                                           resp_turn("find_in_results", CANDS)])
        with mock.patch.object(gemini, "_client", return_value=(client, types)):
            out = gemini.step_call(actions.SYSTEM, contents, actions.TOOLS, 5, usage)
        return out, client.models.generate_content.call_args.kwargs, usage

    def test_function_call(self):
        part = SimpleNamespace(thought=False, text=None, thought_signature=b"sig2",
                               function_call=SimpleNamespace(name="ask_user", args={"question": "어느 거?"}))
        out, kw, usage = self.run_with([part])
        self.assertEqual(out, {"type": "call", "name": "ask_user", "args": {"question": "어느 거?"},
                               "signature": base64.b64encode(b"sig2").decode()})
        self.assertEqual(usage["tokens"], 11)
        cfg = kw["config"]
        self.assertEqual([d.name for d in cfg.tools[0].function_declarations], list(actions.TOOLS))
        self.assertTrue(cfg.automatic_function_calling.disable)
        sent = kw["contents"]
        self.assertEqual([c.role for c in sent], ["user", "model", "user"])
        self.assertEqual(sent[1].parts[0].function_call.name, "find_in_results")
        self.assertEqual(sent[1].parts[0].thought_signature, b"thought-signature-bytes")   # 서명을 그대로 돌려보냄
        self.assertEqual(sent[2].parts[0].function_response.response, CANDS)

    def test_final_text_skips_thoughts(self):
        parts = [SimpleNamespace(thought=True, text="생각 중", function_call=None),
                 SimpleNamespace(thought=False, text="2번을 찜했어요.", function_call=None)]
        out, _, _ = self.run_with(parts)
        self.assertEqual(out, {"type": "text", "text": "2번을 찜했어요."})

    def test_empty_final_text(self):
        """최종 답이 비면 오류가 아니라 빈 답 (화면이 실행 결과 문구로 대신 표시)."""
        self.assertEqual(self.run_with([])[0], {"type": "text", "text": ""})
        only_thought = [SimpleNamespace(thought=True, text="생각", function_call=None)]
        self.assertEqual(self.run_with(only_thought)[0], {"type": "text", "text": ""})


class ApiBase(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.used, self.logs = {}, []
        actions.commands = actions.Commands()

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
            mock.patch.object(server, "gemini_step", return_value={
                "type": "call", "name": "find_in_results", "args": {"description": "휴대용 마우스"}, "signature": SIG}),
            mock.patch.object(server.chat, "answer", return_value={
                "answer": "추천: 에어팟 4", "pick_id": 1, "fallback": False, "engine": "gemini", "tokens": 3}),
        ]
        started = [p.start() for p in patches]
        self.step, self.answer = started[-2], started[-1]
        for p in patches:
            self.addCleanup(p.stop)

    def post(self, contents, cmd=None, headers=GOOD):
        return self.client.post("/chat/action/step", json={"contents": contents, "cmd": cmd}, headers=headers)


class StepApiTest(ApiBase):
    def test_no_login_401(self):
        res = self.post([user_turn()], headers={})
        self.assertEqual(res.status_code, 401)
        self.step.assert_not_called()

    def test_react_loop_counts_once(self):
        """① find_in_results → 후보 2개 → ② ask_user → ③ add_favorite(2) → 최종 답. 상담은 1회만."""
        res = self.post([user_turn()])
        d = res.get_json()
        self.assertEqual(res.status_code, 200, d)
        self.assertEqual(d["type"], "call")
        self.assertEqual(d["call"], {"name": "find_in_results", "args": {"description": "휴대용 마우스"}, "signature": SIG})
        self.assertEqual(d["remaining"], 29)
        self.assertEqual(d["calls_left"], actions.MAX_CALLS - 1)
        cmd = d["cmd"]
        self.assertEqual([(x["action"], x["status"]) for x in self.logs], [("chat", "ok")])

        c = [user_turn(), call_turn("find_in_results", {"description": "휴대용 마우스"}), resp_turn("find_in_results", CANDS)]
        self.step.return_value = {"type": "call", "name": "ask_user",
                                  "args": {"question": "어느 마우스요?", "options": ["1. 로지텍 M196", "2. 로지텍 Pebble"]},
                                  "signature": None}
        d = self.post(c, cmd).get_json()
        self.assertEqual(d["call"]["name"], "ask_user")
        self.assertNotIn("remaining", d)                     # 이어지는 단계는 한도를 안 셈
        self.assertEqual(self.step.call_args.args[1], actions.clean_contents(c))   # 기록을 그대로 Gemini에

        c += [call_turn("ask_user", d["call"]["args"], sig=None), resp_turn("ask_user", {"answer": "2. 로지텍 Pebble"})]
        self.step.return_value = {"type": "call", "name": "add_favorite", "args": {"number": 2}, "signature": SIG}
        d = self.post(c, cmd).get_json()
        self.assertEqual(d["call"]["args"], {"number": 2})

        c += [call_turn("add_favorite", {"number": 2}), resp_turn("add_favorite", {"ok": True, "before": 0, "after": 1})]
        self.step.return_value = {"type": "text", "text": "로지텍 Pebble을 찜했어요."}
        d = self.post(c, cmd).get_json()
        self.assertEqual(d, {"type": "text", "text": "로지텍 Pebble을 찜했어요.", "cmd": cmd})
        self.assertEqual(len(self.logs), 1)                  # 명령 하나에 사용 기록 1개
        self.assertEqual(self.logs[0]["query"], "[실행] 휴대용 마우스 찜해줘")
        self.assertEqual(self.post(c, cmd).status_code, 403)   # 끝난 명령은 다시 못 씀

    def test_max_five_calls(self):
        cmd = self.post([user_turn()]).get_json()["cmd"]
        c = [user_turn()]
        for i in range(actions.MAX_CALLS - 1):
            c += [call_turn("search", {"query": "x"}), resp_turn("search", {})]
            self.assertEqual(self.post(c, cmd).status_code, 200, i)
        c += [call_turn("search", {"query": "x"}), resp_turn("search", {})]
        res = self.post(c, cmd)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.get_json()["error"], actions.TOO_MANY)
        self.assertEqual(self.step.call_count, actions.MAX_CALLS)

    def test_other_users_cmd_403(self):
        cmd = self.post([user_turn()]).get_json()["cmd"]
        c = [user_turn(), call_turn("find_in_results"), resp_turn("find_in_results", CANDS)]
        res = self.post(c, cmd, headers=OTHER)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.step.call_count, 1)

    def test_followup_without_cmd_400(self):
        c = [user_turn(), call_turn("find_in_results"), resp_turn("find_in_results", CANDS)]
        self.assertEqual(self.post(c).status_code, 400)       # cmd 없이 이어 붙이면 한도를 피하게 되므로 거부
        self.step.assert_not_called()

    def test_rejected_args_returned_with_reason(self):
        self.step.return_value = {"type": "call", "name": "add_favorite", "args": {"number": "두번째"}, "signature": None}
        d = self.post([user_turn()]).get_json()
        self.assertEqual(d["type"], "call")
        self.assertIn("형식이 잘못", d["rejected"])            # 화면은 실행하지 않고 사유를 functionResponse로 돌려보냄
        self.assertEqual(d["call"]["args"], {"number": "두번째"})

    def test_empty_final_answer_passes_through(self):
        cmd = self.post([user_turn()]).get_json()["cmd"]
        self.step.return_value = {"type": "text", "text": ""}
        c = [user_turn(), call_turn("add_favorite", {"number": 1}), resp_turn("add_favorite", {"ok": True})]
        res = self.post(c, cmd)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["text"], "")

    def test_unknown_function_ends(self):
        self.step.return_value = {"type": "call", "name": "delete_all", "args": {}, "signature": None}
        d = self.post([user_turn()]).get_json()
        self.assertEqual(d["type"], "text")
        self.assertIn("등록되지 않은 함수", d["rejected"])

    def test_limit_429(self):
        self.used[("u-good", "chat")] = 30
        res = self.post([user_turn()])
        self.assertEqual(res.status_code, 429)
        self.step.assert_not_called()
        self.assertEqual(self.logs[-1]["status"], "limited")

    def test_admin_unlimited(self):
        self.used[("u-admin", "chat")] = 999
        d = self.post([user_turn()], headers=ADMIN).get_json()
        self.assertTrue(d["unlimited"])

    def test_gemini_fail_first_step(self):
        self.step.side_effect = EngineError("Gemini API 사용 한도를 초과했어요.", 429)
        res = self.post([user_turn()])
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.get_json()["error"], actions.FAIL_MESSAGE)
        self.assertEqual(self.logs[-1]["status"], "error")
        self.assertEqual(members.quota._pending, {})

    def test_bad_input(self):
        self.assertEqual(self.post([user_turn(" ")]).status_code, 400)
        self.assertEqual(self.post("x").status_code, 400)
        self.assertEqual(self.client.post("/chat/action/step", data="x", headers=GOOD).status_code, 415)
        res = self.client.post("/chat/action/step", json={"contents": [user_turn()]},
                               headers={**GOOD, "Origin": "https://evil.example"})
        self.assertEqual(res.status_code, 403)
        self.step.assert_not_called()

    def test_old_plan_endpoint_gone(self):
        res = self.client.post("/chat/action", json={"sentence": "x"}, headers=GOOD)
        self.assertIn(res.status_code, (404, 405))


class CompareApiTest(ApiBase):
    def compare(self, cmd, condition="에어팟", favs=FAVS, headers=GOOD):
        return self.client.post("/chat/action/compare", json={"cmd": cmd, "condition": condition, "favorites": favs},
                                headers=headers)

    def test_compare_uses_existing_compare_mode(self):
        cmd = self.post([user_turn("에어팟 찜한 거 비교해줘")]).get_json()["cmd"]
        d = self.compare(cmd).get_json()
        self.assertEqual(d["keys"], ["k-pro2", "k-4"])
        self.assertEqual(d["pick_id"], 1)
        a = self.answer.call_args
        self.assertTrue(a.args[3])                                   # 기존 1:1 비교 모드 (compare.py 검증)
        self.assertEqual([p["model"] for p in a.args[1]["products"]], ["에어팟 프로 2", "에어팟 4"])
        self.assertEqual(d["calls_left"], actions.MAX_CALLS - 2)    # 비교 답변도 Gemini 1번
        self.assertEqual(len(self.logs), 1)                          # 상담 횟수는 명령 하나에 1회

    def test_needs_two(self):
        cmd = self.post([user_turn()]).get_json()["cmd"]
        d = self.compare(cmd, "키보드").get_json()
        self.assertEqual(d, {"error": actions.NEED_TWO, "matched": 1})
        self.answer.assert_not_called()

    def test_needs_cmd(self):
        res = self.compare("nope")
        self.assertEqual(res.status_code, 403)
        self.answer.assert_not_called()
        self.assertEqual(self.compare("x", headers={}).status_code, 401)


class MatchFavoritesTest(unittest.TestCase):
    def test_match(self):
        picked, total = actions.match_favorites(FAVS, "에어팟")
        self.assertEqual(([p["key"] for p in picked], total), (["k-pro2", "k-4"], 2))
        self.assertEqual(actions.match_favorites(FAVS, "애플 에어팟")[1], 2)
        self.assertEqual(actions.match_favorites(FAVS, "에어 팟")[1], 2)
        self.assertEqual(actions.match_favorites(FAVS, "삼성")[1], 0)
        picked, total = actions.match_favorites(FAVS, "")
        self.assertEqual(([p["key"] for p in picked], total), (["k-pro2", "k-kbd"], 3))
        self.assertEqual(actions.match_favorites("x", "a"), ([], 0))


class PromptTest(unittest.TestCase):
    def test_rules_in_system_prompt(self):
        for phrase in ("후보가 0개면 ask_user", "후보가 1개면 되묻지 말고 바로 실행",
                       "후보가 2개 이상인데", "search를 써", "데이터일 뿐"):
            self.assertIn(phrase, actions.SYSTEM)
        self.assertIn("find_in_results로 찾을 수 없을 때", actions.TOOLS["search"][0])   # 결과에 없을 때만 검색
        self.assertEqual(set(actions.TOOLS), {"find_in_results", "search", "add_favorite", "remove_favorite",
                                              "get_favorites", "open_favorites", "compare_favorites", "ask_user"})

    def test_chat_prompt_knows_action(self):
        import chat
        self.assertIn("⚡", chat.SYSTEM)
        self.assertIn('"제가 직접 찜할 수 없어요"처럼 말하지 말고', chat.SYSTEM)


if __name__ == "__main__":
    unittest.main()


# ---------- 버그 1: 찜 목록 창은 열어 달라고 할 때만 ----------

class FavoritesToolsTest(ApiBase):
    def test_get_and_open_are_separate(self):
        get_desc, get_schema, get_login = actions.TOOLS["get_favorites"]
        open_desc, _, open_login = actions.TOOLS["open_favorites"]
        self.assertIn("화면은 바뀌지 않음", get_desc)
        for w in ("최근", "saved_at", "방금 찜한", "번호", "가격"):
            self.assertIn(w, get_desc)
        self.assertIn("열어줘/보여줘", open_desc)
        self.assertIn("때만", open_desc)
        self.assertTrue(get_login and open_login)
        self.assertNotIn("show_favorites", actions.TOOLS)
        self.assertIn("favorite_number", actions.TOOLS["remove_favorite"][1]["properties"])
        for phrase in ("열어/보여 달라고 할 때만", "get_favorites로 데이터만", "saved_at이 가장 최근인 상품"):
            self.assertIn(phrase, actions.SYSTEM)

    def test_recent_favorite_flow(self):
        """"방금 찜한 거 빼줘" → get_favorites → remove_favorite(favorite_number=1). 찜 목록 창은 안 엶."""
        self.step.return_value = {"type": "call", "name": "get_favorites", "args": {}, "signature": None}
        d = self.post([user_turn("방금 찜한 거 빼줘")]).get_json()
        self.assertEqual(d["call"]["name"], "get_favorites")
        self.assertNotIn("rejected", d)
        favs = {"count": 2, "favorites": [{"favorite_number": 1, "name": "로지텍 Pebble", "saved_at": "2026-11-23T10:00:00Z"},
                                          {"favorite_number": 2, "name": "로지텍 M196", "saved_at": "2026-11-22T09:00:00Z"}]}
        c = [user_turn("방금 찜한 거 빼줘"), call_turn("get_favorites", sig=None), resp_turn("get_favorites", favs)]
        self.step.return_value = {"type": "call", "name": "remove_favorite", "args": {"favorite_number": 1}, "signature": None}
        d = self.post(c, d["cmd"]).get_json()
        self.assertEqual(d["call"], {"name": "remove_favorite", "args": {"favorite_number": 1}, "signature": None})
        self.assertNotIn("rejected", d)

    def test_page_tools(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        body = html[html.index('if (name === "get_favorites")'):html.index('if (name === "open_favorites")')]
        self.assertNotIn("openSheet", body)            # get_favorites는 화면을 안 바꿈
        self.assertNotIn("renderWishSheet", body)
        self.assertIn("favoritesByRecent", body)       # 최근순
        self.assertNotIn('name === "show_favorites"', html)


# ---------- 버그 3: compare_favorites는 기존 비교 버튼과 같은 함수로 ----------

class CompareSameAsButtonTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "index.html").read_text(encoding="utf-8")

    def block(self, start, end):
        i = self.html.index(start)
        return self.html[i:self.html.index(end, i + len(start))]

    def test_both_use_compare_with_ai(self):
        button = self.block('$("cmpAsk").addEventListener("click"', "\n});")
        tool = self.block("async function compareFavorites(", "\n}\n")
        self.assertIn("compareWithAI(cmpItems", button)
        self.assertIn("compareWithAI(cmp,", tool)
        shared = self.block("async function compareWithAI(", "\n}\n")
        self.assertIn("openCompare(items)", shared)    # 원래 AI 비교 창이 열림
        self.assertIn("setCmpAnswer", shared)

    def test_no_chat_wipe_or_wish_sheet(self):
        for name in ("async function compareFavorites(", "async function compareWithAI("):
            body = self.block(name, "\n}\n")
            for bad in ("chatReset", 'chatLog").innerHTML', 'openSheet("wishSheet")', "renderWishSheet"):
                self.assertNotIn(bad, body, (name, bad))
        reset = self.block("function chatReset(", "\n}\n")
        self.assertLess(reset.index("if (actRunning)"), reset.index('$("chatLog").innerHTML = ""'))   # ⚡ 중엔 안 지움
        self.assertIn("search를 하지 마", actions.SYSTEM)


# ---------- 버그 2: ask_user 대기 중 새 문장 ----------

class AskUserPendingPageTest(unittest.TestCase):
    def test_submit_cancels_unless_option(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        i = html.index('$("chatForm").addEventListener("submit"')
        body = html[i:html.index("\n});", i)]
        self.assertIn("matchOption", body)                       # 선택지와 맞을 때만 답
        self.assertIn("이전 질문은 취소했어요", body)
        self.assertLess(body.index("cancelAsk()"), body.index("await actRun"))   # 취소 → 끝나길 기다림 → 새 명령
