"""⚡ 문장으로 실행 테스트 (LangGraph StateGraph, react_graph.py). Gemini·Supabase는 가짜로 바꾼다.

- actions.check_call: 등록된 함수·입력값·권한 검사
- actions.Commands: 명령(thread_id)의 주인·만료, 끝나면 체크포인트 지움
- gemini.step_call: 함수 선언 등록, functionCall·최종 답·thoughtSignature 읽기
- react_graph: agent → validate → server_tool / approve / wait_client → agent … → END, interrupt/resume
- /chat/action/step: 로그인·한도(명령당 상담 1회)·resume
- 최근 명령 기억, search 확인, 범위 기본값, 찜 목록 창은 open_favorites만, ask_user 취소, 중복 찜, 저장 승인

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
import react_graph  # noqa: E402
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
RESULTS = [   # 화면이 보내는 지금 검색 결과
    {"key": "k-k295", "name": "로지텍 K295", "price": 39000, "rating": 4.5, "features": ["저소음", "생활 방수"]},
    {"key": "k-k660", "name": "앱코 K660", "price": 59000, "rating": 4.7, "features": ["기계식"]},
    {"key": "k-mx", "name": "로지텍 MX Keys", "price": 139000, "rating": 4.8, "features": ["저소음", "백라이트"]},
]
FAVS = [   # 화면이 보내는 찜 목록 (최신순)
    {"key": "k-pro2", "brand": "애플", "model": "에어팟 프로 2", "query": "에어팟", "price": 299000, "rating": 4.8,
     "saved_at": "2026-11-23T10:00:00Z"},
    {"key": "k-kbd", "brand": "로지텍", "model": "K295", "query": "저소음 키보드", "price": 39000,
     "saved_at": "2026-11-22T10:00:00Z"},
    {"key": "k-4", "brand": "애플", "model": "에어팟 4", "query": "무선 이어폰", "price": 199000, "rating": 4.6,
     "saved_at": "2026-11-21T10:00:00Z"},
]


def fake_fetch_user(token):
    if token in USERS:
        return USERS[token]
    raise auth.AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")


def call(name, args=None, sig=None):
    return {"type": "call", "name": name, "args": args or {}, "signature": sig}


def text(t):
    return {"type": "text", "text": t}


class FakeLLM:
    """Gemini 가짜: 정해 둔 응답을 차례로. 받은 (system, messages)를 기록."""

    def __init__(self, *outs):
        self.outs = list(outs)
        self.seen = []

    def __call__(self, system, messages, tools, timeout, usage):
        self.seen.append({"system": system, "messages": [m["parts"][0] for m in messages]})
        usage["tokens"] = 7
        out = self.outs.pop(0) if len(self.outs) > 1 else self.outs[0]
        if isinstance(out, Exception):
            raise out
        return out

    def last_response(self, i=-1):
        """i번째 호출 때 받은 마지막 functionResponse."""
        return self.seen[i]["messages"][-1]["functionResponse"]


# ---------- 검사 규칙 ----------

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

    def test_rejects(self):
        cases = [
            ("delete_account", {}, USER, "등록되지 않은 함수"),
            ("show_favorites", {}, USER, "등록되지 않은 함수"),
            ("add_favorite", {"number": 1}, None, "로그인이 필요"),
            ("get_favorites", {}, None, "로그인이 필요"),
            ("open_favorites", {}, None, "로그인이 필요"),
            ("add_favorite", {}, USER, "number나 name 중 하나"),
            ("add_favorite", {"number": "2"}, USER, "형식이 잘못"),
            ("add_favorite", {"number": True}, USER, "형식이 잘못"),
            ("add_favorite", {"number": 0}, USER, "1~50"),
            ("add_favorite", {"number": 1, "price": 3}, USER, "모르는 입력값"),
            ("search", {"query": "  "}, USER, "query 값이 빠졌어요"),
            ("remove_favorite", {}, USER, "favorite_number·number·name 중 하나"),
            ("ask_user", {"options": ["a"]}, USER, "question 값이 빠졌어요"),
        ]
        for name, args, user, want in cases:
            out, why = actions.check_call(name, args, user)
            self.assertIsNone(out, name)
            self.assertIn(want, why, (name, args))


class CommandsTest(unittest.TestCase):
    def test_owner_expire_drop(self):
        dropped = []
        c = actions.Commands(on_drop=dropped.append)
        cmd = c.start("u1")
        self.assertIsNone(c.check(cmd, "u1"))
        self.assertEqual(c.check(cmd, "u2"), actions.CMD_EXPIRED)      # 다른 사람 명령
        self.assertEqual(c.check("nope", "u1"), actions.CMD_EXPIRED)
        with mock.patch.object(actions.time, "time", return_value=actions.time.time() + actions.CMD_TTL + 1):
            self.assertEqual(c.check(cmd, "u1"), actions.CMD_EXPIRED)
            c.start("u1")                                                # 만료된 명령 정리 → 체크포인트 지움
        self.assertIn(cmd, dropped)
        cmd2 = c.start("u1")
        c.drop(cmd2)
        self.assertEqual(dropped[-1], cmd2)
        self.assertEqual(c.check(cmd2, "u1"), actions.CMD_EXPIRED)


class StepCallTest(unittest.TestCase):
    """gemini.step_call: 공식 Function Calling (functionDeclarations), 실행은 안 함."""

    MESSAGES = [{"role": "user", "parts": [{"text": "마우스 찜해줘"}]},
                {"role": "model", "parts": [{"functionCall": {"name": "find_in_results", "args": {"description": "마우스"}},
                                             "thoughtSignature": SIG}]},
                {"role": "user", "parts": [{"functionResponse": {"name": "find_in_results", "response": {"total": 0}}}]}]

    def run_with(self, parts):
        from google.genai import types
        client = mock.MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
            usage_metadata=SimpleNamespace(total_token_count=11))
        usage = {}
        with mock.patch.object(gemini, "_client", return_value=(client, types)):
            out = gemini.step_call(actions.SYSTEM, self.MESSAGES, actions.TOOLS, 5, usage)
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
        self.assertEqual(sent[1].parts[0].thought_signature, b"thought-signature-bytes")

    def test_final_and_empty(self):
        parts = [SimpleNamespace(thought=True, text="생각", function_call=None),
                 SimpleNamespace(thought=False, text="2번을 찜했어요.", function_call=None)]
        self.assertEqual(self.run_with(parts)[0], {"type": "text", "text": "2번을 찜했어요."})
        self.assertEqual(self.run_with([])[0], {"type": "text", "text": ""})


# ---------- 그래프 (react_graph) ----------

class GraphBase(unittest.TestCase):
    def setUp(self):
        self.tid = f"t-{id(self)}-{self._testMethodName}"
        self.answer = mock.patch.object(react_graph, "compare_answer", return_value={
            "answer": "추천: 에어팟 4\n이유: 10만원 저렴해요", "pick_id": 1, "fallback": False, "engine": "gemini"}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(react_graph.forget, self.tid)

    def start(self, llm, sentence="저소음 키보드 찜해줘", results=RESULTS, favorites=(), recent=(), removed=(), query="키보드"):
        self.llm = llm
        mock.patch.object(react_graph, "llm", llm).start()
        st = react_graph.initial_state(sentence, "u-good", list(recent), list(results), query, list(favorites),
                                       list(removed), 5)
        return react_graph.run(st, self.tid)

    def resume(self, value):
        return react_graph.run(react_graph.resume(react_graph.clean_resume(value)), self.tid)


class GraphFlowTest(GraphBase):
    def test_find_approve_wait_end(self):
        """find_in_results(server_tool) → add_favorite → approve → wait_client → agent → END."""
        out = self.start(FakeLLM(call("find_in_results", {"description": "저소음 키보드"}),
                                 call("add_favorite", {"number": 1}, SIG), text("로지텍 K295를 찜했어요.")))
        self.assertEqual(out["type"], "interrupt")
        self.assertEqual(out["pending"]["kind"], "approve")                     # 저장 승인 카드
        self.assertEqual(out["pending"]["product"], {"key": "k-k295", "name": "로지텍 K295", "price": 39000,
                                                     "currency": None, "source": "results", "number": 1})
        cands = self.llm.last_response(1)["response"]["candidates"]
        self.assertEqual([c["number"] for c in cands], [1, 3])                  # "저소음"이 들어 있는 것만 (키보드는 검색어)
        self.assertNotIn("key", cands[0])
        out = self.resume({"approved": True})
        self.assertEqual(out["pending"]["kind"], "client")                     # 승인 → 화면에서 찜 실행
        self.assertEqual(out["pending"]["call"], {"name": "add_favorite", "args": {"number": 1}})
        out = self.resume({"response": {"ok": True, "before": 0, "after": 1},
                           "favorites": [{"key": "k-k295", "brand": "로지텍", "model": "K295"}]})
        self.assertEqual(out["type"], "text")
        self.assertEqual(out["text"], "로지텍 K295를 찜했어요.")
        self.assertEqual(out["trail"], ["agent", "validate", "server_tool", "agent", "validate", "server_tool",
                                        "approve", "wait_client", "agent", "END"])
        self.assertEqual(self.llm.last_response()["response"], {"ok": True, "before": 0, "after": 1})
        model_turn = self.llm.seen[2]["messages"][3]
        self.assertEqual(model_turn["thoughtSignature"], SIG)                  # 서명을 그대로 Gemini에

    def test_duplicate_favorite_zero_runs(self):
        """이미 찜한 상품 → 승인·화면 실행 없이 "이미 찜한 상품이에요"."""
        favs = [{"key": "k-k295", "brand": "로지텍", "model": "K295"}]
        out = self.start(FakeLLM(call("add_favorite", {"number": 1}), text("이미 찜한 상품이에요.")), favorites=favs)
        self.assertEqual(out["type"], "text")
        self.assertEqual(self.llm.last_response()["response"],
                         {"ok": True, "already": True, "name": "로지텍 K295", "message": "이미 찜한 상품이에요"})
        self.assertNotIn("approve", out["trail"])
        self.assertNotIn("wait_client", out["trail"])

    def test_approve_cancel_saves_nothing(self):
        out = self.start(FakeLLM(call("add_favorite", {"number": 2}), text("안 쓰임")))
        self.assertEqual(out["pending"]["kind"], "approve")
        out = self.resume({"approved": False})
        self.assertEqual(out["type"], "text")
        self.assertEqual(out["text"], react_graph.SAVE_CANCELLED)
        self.assertEqual(out["trail"][-2:], ["approve", "END"])
        self.assertNotIn("wait_client", out["trail"])                          # 저장 0회
        self.assertEqual(len(self.llm.seen), 1)

    def test_validation_fail_goes_back_to_agent(self):
        out = self.start(FakeLLM(call("add_favorite", {"number": "두번째"}), text("번호를 다시 알려 주세요.")))
        self.assertEqual(out["type"], "text")
        self.assertIn("형식이 잘못", self.llm.last_response()["response"]["error"])
        self.assertEqual(out["trail"], ["agent", "validate", "agent", "END"])

    def test_unknown_function_reason_to_agent(self):
        out = self.start(FakeLLM(call("delete_all"), text("그건 할 수 없어요.")))
        self.assertIn("등록되지 않은 함수", self.llm.last_response()["response"]["error"])
        self.assertEqual(out["text"], "그건 할 수 없어요.")

    def test_step_count_limit(self):
        """Gemini가 계속 함수만 고르면 5번 부른 뒤 END."""
        out = self.start(FakeLLM(call("get_favorites")))
        self.assertEqual(out["type"], "text")
        self.assertEqual(out["text"], actions.TOO_MANY)
        self.assertEqual(len(self.llm.seen), actions.MAX_CALLS)
        self.assertEqual(out["trail"][-2:], ["agent(한도)", "END"])

    def test_wait_client_updates_state(self):
        """search(화면 실행) 결과의 새 후보로 다음 find_in_results를 함."""
        out = self.start(FakeLLM(call("search", {"query": "무선 마우스"}), call("find_in_results", {"description": "무선"}),
                                 text("끝")), sentence="무선 마우스 검색해줘", results=[])
        self.assertEqual(out["pending"], {"kind": "client", "call": {"name": "search", "args": {"query": "무선 마우스"}}})
        out = self.resume({"response": {"ok": True, "total": 1}, "query": "무선 마우스",
                           "candidates": [{"key": "m1", "name": "로지텍 M196", "price": 15000}]})
        self.assertEqual(out["type"], "text")
        self.assertEqual(self.llm.last_response()["response"]["candidates"][0]["name"], "로지텍 M196")

    def test_resume_values_checked(self):
        for bad in (None, "x", {"response": "ok"}, {"response": {"x": "가" * (actions.MAX_RESPONSE + 1)}}):
            with self.assertRaises(ValueError):
                react_graph.clean_resume(bad)
        self.assertEqual(react_graph.clean_resume({"approved": "yes"}), {"approved": False})   # true만 승인


# ---------- 찜 목록 창은 open_favorites만 ----------

class FavoritesToolsTest(GraphBase):
    def test_get_favorites_server_side_no_window(self):
        out = self.start(FakeLLM(call("get_favorites"), call("remove_favorite", {"favorite_number": 1}), text("뺐어요")),
                         sentence="방금 찜한 거 빼줘", favorites=FAVS)
        favs = self.llm.last_response()["response"]["favorites"]
        self.assertEqual([f["favorite_number"] for f in favs], [1, 2, 3])
        self.assertEqual(favs[0]["name"], "애플 에어팟 프로 2")                 # 최근순 1번 = 방금 찜한 것
        self.assertEqual(favs[0]["rating"], 4.8)
        self.assertEqual(out["pending"]["call"], {"name": "remove_favorite", "args": {"favorite_number": 1}})
        self.assertEqual(out["trail"], ["agent", "validate", "server_tool", "agent", "validate"])   # 창 여는 일 없음

    def test_open_favorites_is_client(self):
        out = self.start(FakeLLM(call("open_favorites"), text("열었어요")), sentence="찜 목록 열어줘")
        self.assertEqual(out["pending"], {"kind": "client", "call": {"name": "open_favorites", "args": {}}})

    def test_descriptions(self):
        self.assertIn("화면은 바뀌지 않음", actions.TOOLS["get_favorites"][0])
        self.assertIn("열어줘/보여줘", actions.TOOLS["open_favorites"][0])
        self.assertIn("때만", actions.TOOLS["open_favorites"][0])
        self.assertEqual(react_graph.SERVER_TOOLS, {"find_in_results", "get_favorites", "compare_favorites"})
        self.assertIn("open_favorites", react_graph.CLIENT_TOOLS)


# ---------- compare_favorites: 서버가 비교, 화면은 기존 AI 비교 창 ----------

class CompareTest(GraphBase):
    def test_compare_effect(self):
        out = self.start(FakeLLM(call("compare_favorites", {"condition": "에어팟"}), text("에어팟 4가 더 나아요.")),
                         sentence="에어팟 찜한 거 비교해줘", favorites=FAVS)
        self.assertEqual(out["type"], "text")
        self.assertEqual(out["effects"], [{"type": "compare", "keys": ["k-pro2", "k-4"], "answer": self.answer.return_value["answer"],
                                           "pick_id": 1, "matched": 2}])
        a = self.answer.call_args
        self.assertTrue(a.args[3])                                               # 기존 1:1 비교 모드
        self.assertEqual([p["model"] for p in a.args[1]["products"]], ["에어팟 프로 2", "에어팟 4"])
        self.assertEqual(self.llm.last_response()["response"]["recommended"], "애플 에어팟 4")

    def test_needs_two(self):
        out = self.start(FakeLLM(call("compare_favorites", {"condition": "키보드"}), text("2개 이상 필요해요")),
                         favorites=FAVS)
        self.assertEqual(self.llm.last_response()["response"], {"error": actions.NEED_TWO, "matched": 1})
        self.assertEqual(out["effects"], [])
        self.answer.assert_not_called()

    def test_page_uses_same_compare_function(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        def block(start):
            i = html.index(start)
            return html[i:html.index("\n}\n", i)]
        self.assertIn("compareWithAI(cmpItems", html[html.index('$("cmpAsk").addEventListener("click"'):])
        effect = block("function applyEffect(")
        self.assertIn("compareWithAI(cmp,", effect)
        for name in ("function applyEffect(", "async function compareWithAI("):
            for bad in ("chatReset", 'chatLog").innerHTML', 'openSheet("wishSheet")', "renderWishSheet"):
                self.assertNotIn(bad, block(name), (name, bad))


# ---------- 최근 명령 기억 ----------

RECENT = [{"input": "로지텍 K295 찜 취소해줘", "calls": ["remove_favorite(\"로지텍 K295\")"], "targets": ["로지텍 K295"]},
          {"input": "무선 마우스 검색해줘", "calls": ['search("무선 마우스")'], "targets": []}]


class RecentCommandsTest(GraphBase):
    def test_clean_recent_and_prompt(self):
        raw = RECENT + [{"input": f"명령 {i}", "calls": ["x"] * 9, "targets": [1, "가" * 200]} for i in range(9)]
        out = actions.clean_recent(raw)
        self.assertEqual(len(out), actions.MAX_RECENT)
        self.assertEqual(out[2]["targets"], ["가" * 80])
        self.assertEqual(actions.system_prompt([]), actions.SYSTEM)
        p = actions.system_prompt(actions.clean_recent(RECENT))
        self.assertIn('1. 입력: 로지텍 K295 찜 취소해줘 | 실행: remove_favorite("로지텍 K295") | 대상: 로지텍 K295', p)
        self.assertIn("다시/방금/그거/아까", actions.SYSTEM)

    def test_recent_in_every_agent_call_and_rewish(self):
        """"다시 찜해줘" → 최근에 찜 취소한 상품(removed)을 이름으로 다시 찜 (결과에 없어도)."""
        removed = [{"key": "k-old", "brand": "로지텍", "model": "Pebble", "price": 29000}]
        out = self.start(FakeLLM(call("add_favorite", {"name": "로지텍 Pebble"}), text("다시 찜했어요")),
                         sentence="다시 찜해줘", recent=RECENT, removed=removed, results=[])
        self.assertEqual(out["pending"]["product"]["source"], "removed")
        self.assertEqual(out["pending"]["product"]["key"], "k-old")
        self.resume({"approved": True})
        self.resume({"response": {"ok": True, "before": 0, "after": 1}})
        self.assertEqual(len(self.llm.seen), 2)
        for seen in self.llm.seen:
            self.assertIn("대상: 로지텍 K295", seen["system"])                    # 매 agent 호출에


# ---------- search 확인 ----------

class SearchConfirmTest(GraphBase):
    ASK = {"question": "에어팟을 새로 검색할까요? (하루 검색 5회 중 1회 사용)", "options": ["검색", "취소"]}

    def test_rejected_then_ask_then_search(self):
        out = self.start(FakeLLM(call("search", {"query": "에어팟"}), call("ask_user", self.ASK),
                                 call("search", {"query": "에어팟"}), text("검색했어요")),
                         sentence="에어팟 제일 싼 거 찜해줘")
        why = self.llm.last_response()["response"]["error"]
        self.assertIn("에어팟을 새로 검색할까요? (하루 검색 5회 중 1회 사용)", why)
        self.assertIn('options=["검색", "취소"]', why)
        self.assertEqual(out["pending"], {"kind": "ask", "call": {"name": "ask_user", "args": self.ASK}})
        out = self.resume({"response": {"answer": "검색"}})
        self.assertEqual(out["pending"]["call"], {"name": "search", "args": {"query": "에어팟"}})   # 확인 뒤 통과

    def test_cancel_answer_still_rejected(self):
        out = self.start(FakeLLM(call("ask_user", self.ASK), call("search", {"query": "에어팟"}), text("안 할게요")),
                         sentence="에어팟 제일 싼 거 찜해줘")
        out = self.resume({"response": {"answer": "취소"}})
        self.assertEqual(out["type"], "text")
        self.assertIn("검색을 직접 말하지 않았어요", self.llm.last_response()["response"]["error"])

    def test_allowed_when_user_said_search(self):
        out = self.start(FakeLLM(call("search", {"query": "에어팟"})), sentence="에어팟 검색해줘")
        self.assertEqual(out["pending"]["call"]["name"], "search")

    def test_unit(self):
        self.assertEqual(actions.eul_reul("에어팟"), "에어팟을")
        self.assertEqual(actions.eul_reul("무선 마우스"), "무선 마우스를")
        self.assertIn("ask_user로 확인을 받아야", actions.TOOLS["search"][0])


# ---------- ask_user 대기 중 취소 ----------

class AskCancelTest(GraphBase):
    def test_cancel_ends_without_gemini(self):
        out = self.start(FakeLLM(call("ask_user", {"question": "어느 거요?", "options": ["1. 로지텍 K295", "2. 앱코 K660"]}),
                                 text("안 쓰임")))
        self.assertEqual(out["pending"]["kind"], "ask")
        out = self.resume({"cancelled": True})
        self.assertEqual(out["type"], "text")
        self.assertTrue(out["cancelled"])
        self.assertEqual(len(self.llm.seen), 1)
        self.assertEqual(out["trail"][-2:], ["wait_client", "END"])

    def test_page_submit_rule(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        i = html.index('$("chatForm").addEventListener("submit"')
        body = html[i:html.index("\n});", i)]
        self.assertIn("matchOption", body)
        self.assertIn("이전 질문은 취소했어요", body)


# ---------- 범위 기본값 ----------

class ScopeDefaultTest(unittest.TestCase):
    def test_scope(self):
        self.assertIn("기본적으로 이 현재 검색 결과 안에서", actions.TOOLS["find_in_results"][0])
        self.assertIn("때만 이 목록을 기준으로", actions.TOOLS["get_favorites"][0])
        self.assertIn('"찜한 것 중/찜 목록에서"라고 말할 때만 찜 목록(get_favorites) 기준', actions.SYSTEM)


# ---------- /chat/action/step ----------

class StepApiTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.used, self.logs = {}, []
        actions.commands = actions.Commands(on_drop=react_graph.forget)

        def fake_log(user_id, email, action, status, **kw):
            self.logs.append({"action": action, "status": status, **kw})

        self.llm = FakeLLM(call("add_favorite", {"number": 2}), text("앱코 K660을 찜했어요."))
        for p in (mock.patch.object(auth, "fetch_user", side_effect=fake_fetch_user),
                  mock.patch.object(members, "get_role",
                                    side_effect=lambda uid, fresh=False: "admin" if uid == "u-admin" else "user"),
                  mock.patch.object(members, "count_today", side_effect=lambda uid, kind: self.used.get((uid, kind), 0)),
                  mock.patch.object(members, "log_usage", side_effect=fake_log),
                  mock.patch.object(server, "is_local_request", return_value=False),
                  mock.patch.object(server.stats, "record_request"),
                  mock.patch.dict(server.LIMITS, {"search": 5, "chat": 30}),
                  mock.patch.object(react_graph, "llm", self.llm)):
            p.start()
        self.addCleanup(mock.patch.stopall)

    def start(self, headers=GOOD, **kw):
        body = {"sentence": "앱코 키보드 찜해줘", "results": RESULTS, "favorites": [], **kw}
        return self.client.post("/chat/action/step", json=body, headers=headers)

    def resume(self, cmd, value, headers=GOOD):
        return self.client.post("/chat/action/step", json={"cmd": cmd, "resume": value}, headers=headers)

    def test_full_command_counts_once(self):
        d = self.start().get_json()
        self.assertEqual(d["type"], "interrupt")
        self.assertEqual(d["pending"]["kind"], "approve")
        self.assertEqual(d["remaining"], 29)
        cmd = d["cmd"]
        d = self.resume(cmd, {"approved": True}).get_json()
        self.assertEqual(d["pending"]["kind"], "client")
        self.assertNotIn("remaining", d)                                       # 이어 가기는 한도를 안 셈
        d = self.resume(cmd, {"response": {"ok": True, "before": 0, "after": 1}}).get_json()
        self.assertEqual(d["type"], "text")
        self.assertEqual(d["text"], "앱코 K660을 찜했어요.")
        self.assertEqual(d["trail"][-1], "END")
        self.assertEqual([(x["action"], x["status"]) for x in self.logs], [("chat", "ok")])   # 명령당 상담 1회
        self.assertEqual(self.logs[0]["query"], "[실행] 앱코 키보드 찜해줘")
        self.assertEqual(self.resume(cmd, {"approved": True}).status_code, 403)   # 끝난 명령은 다시 못 씀
        self.assertFalse(react_graph.graph.get_state({"configurable": {"thread_id": cmd}}).values)   # 체크포인트 지움

    def test_other_user_and_bad_resume(self):
        cmd = self.start().get_json()["cmd"]
        self.assertEqual(self.resume(cmd, {"approved": True}, headers=OTHER).status_code, 403)
        self.assertEqual(self.resume(cmd, "x").status_code, 400)
        self.assertEqual(self.resume(cmd, {"response": "ok"}).status_code, 400)

    def test_login_and_limits(self):
        self.assertEqual(self.start(headers={}).status_code, 401)
        self.used[("u-good", "chat")] = 30
        res = self.start()
        self.assertEqual(res.status_code, 429)
        self.assertEqual(self.logs[-1]["status"], "limited")
        self.used[("u-admin", "chat")] = 999
        self.assertTrue(self.start(headers=ADMIN).get_json()["unlimited"])
        self.assertEqual(len(self.llm.seen), 1)

    def test_bad_input(self):
        self.assertEqual(self.start(sentence=" ").status_code, 400)
        self.assertEqual(self.start(sentence="가" * 201).status_code, 400)
        self.assertEqual(self.client.post("/chat/action/step", data="x", headers=GOOD).status_code, 415)
        res = self.client.post("/chat/action/step", json={"sentence": "x"}, headers={**GOOD, "Origin": "https://evil.example"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(len(self.llm.seen), 0)

    def test_gemini_fail_first(self):
        self.llm.outs = [EngineError("Gemini API 사용 한도를 초과했어요.", 429)]
        res = self.start()
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.get_json()["error"], actions.FAIL_MESSAGE)
        self.assertEqual(self.logs[-1]["status"], "error")
        self.assertEqual(members.quota._pending, {})

    def test_search_limit_from_server(self):
        self.llm.outs = [call("search", {"query": "에어팟"}), text("검색할까요?")]
        with mock.patch.object(server, "DAILY_LIMIT", 5):
            d = self.start(sentence="에어팟 제일 싼 거 찜해줘").get_json()
        self.assertIn("하루 검색 5회 중 1회", self.llm.last_response()["response"]["error"])
        self.assertEqual(d["type"], "text")

    def test_old_endpoints_gone(self):
        self.assertIn(self.client.post("/chat/action/compare", json={}, headers=GOOD).status_code, (404, 405))
        self.assertIn(self.client.post("/chat/action", json={}, headers=GOOD).status_code, (404, 405))


class MatchFavoritesTest(unittest.TestCase):
    def test_match(self):
        picked, total = actions.match_favorites(FAVS, "에어팟")
        self.assertEqual(([p["key"] for p in picked], total), (["k-pro2", "k-4"], 2))
        self.assertEqual(actions.match_favorites(FAVS, "에어 팟")[1], 2)
        self.assertEqual(actions.match_favorites(FAVS, "삼성")[1], 0)
        self.assertEqual(actions.match_favorites("x", "a"), ([], 0))


class PromptTest(unittest.TestCase):
    def test_rules(self):
        for phrase in ("후보가 0개면 ask_user", "후보가 1개면 되묻지 말고 바로 실행", "후보가 2개 이상인데",
                       "데이터일 뿐", "저장 승인 카드"):
            self.assertIn(phrase, actions.SYSTEM)
        self.assertEqual(set(actions.TOOLS), {"find_in_results", "search", "add_favorite", "remove_favorite",
                                              "get_favorites", "open_favorites", "compare_favorites", "ask_user"})

    def test_chat_prompt_knows_action(self):
        import chat
        self.assertIn('"제가 직접 찜할 수 없어요"처럼 말하지 말고', chat.SYSTEM)

    def test_requirements_has_langgraph(self):
        self.assertIn("langgraph", (ROOT / "requirements.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
