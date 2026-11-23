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
        return out(self.seen[-1]) if callable(out) else out   # 함수면 받은 기록을 보고 고름 (진짜 Gemini처럼)

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


class CheckCallLoginTest(unittest.TestCase):
    def test_find_and_ask_need_no_login(self):
        self.assertIsNone(actions.check_call("find_in_results", {"description": "x"}, None)[1])
        self.assertIsNone(actions.check_call("ask_user", {"question": "q"}, None)[1])


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
    def test_compare_opens_window_via_wait_client(self):
        """2개면 서버가 비교 → 화면(wait_client)에 비교 창 열기 → opened: true를 받은 뒤에야 Gemini가 답함."""
        out = self.start(FakeLLM(call("compare_favorites", {"condition": "에어팟"}), text("에어팟 4가 더 나아요.")),
                         sentence="에어팟 찜한 거 비교해줘", favorites=FAVS)
        self.assertEqual(out["type"], "interrupt")
        p = out["pending"]
        self.assertEqual((p["kind"], p["call"]["name"]), ("client", "compare_favorites"))
        c = p["compare"]
        self.assertEqual([i["key"] for i in c["items"]], ["k-pro2", "k-4"])
        self.assertEqual(c["items"][0]["model"], "에어팟 프로 2")                     # 상품 데이터를 직접 (찜에만 있어도)
        self.assertEqual(c["items"][0]["price"], 299000)
        self.assertEqual((c["answer"], c["pick_id"]), (self.answer.return_value["answer"], 1))
        self.assertEqual(len(self.llm.seen), 1)                                       # 열기 전엔 Gemini 답 없음
        a = self.answer.call_args
        self.assertTrue(a.args[3])                                                    # 기존 1:1 비교 모드
        out = self.resume({"response": {"opened": True}})
        self.assertEqual(out["type"], "text")
        resp = self.llm.last_response()["response"]
        self.assertTrue(resp["opened"])
        self.assertEqual(resp["recommended"], "애플 에어팟 4")
        self.assertIn("실제로 열었어요", resp["note"])
        self.assertEqual(out["trail"], ["agent", "validate", "server_tool", "wait_client", "agent", "END"])

    def test_open_failed_says_reason_without_gemini(self):
        self.start(FakeLLM(call("compare_favorites", {"condition": "에어팟"}), text("AI 비교 창을 열었어요.")), favorites=FAVS)
        out = self.resume({"response": {"opened": False, "reason": "상품 이름이 없어요"}})
        self.assertEqual(out["type"], "text")
        self.assertEqual(out["text"], "비교 창을 열지 못했어요: 상품 이름이 없어요")   # "열었어요"라고 말하지 않음
        self.assertEqual(len(self.llm.seen), 1)
        self.start(FakeLLM(call("compare_favorites", {"condition": "에어팟"}), text("x")), favorites=FAVS)
        out = self.resume({"response": {}})                                           # opened가 없으면 실패
        self.assertEqual(out["text"], "비교 창을 열지 못했어요: 화면에서 결과를 받지 못했어요")

    def test_prompt_says_only_when_opened(self):
        self.assertIn("opened: true가 있을 때만", actions.SYSTEM)
        self.assertIn("opened(true/false)", actions.TOOLS["compare_favorites"][0])

    def test_needs_two(self):
        out = self.start(FakeLLM(call("compare_favorites", {"condition": "키보드"}), text("2개 이상 필요해요")),
                         favorites=FAVS)
        self.assertEqual(self.llm.last_response()["response"], {"error": actions.NEED_TWO, "matched": 1})
        self.assertEqual(out["type"], "text")
        self.answer.assert_not_called()

    def test_page_uses_same_compare_function(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        def block(start):
            i = html.index(start)
            return html[i:html.index("\n}\n", i)]
        self.assertIn("compareWithAI(cmpItems", html[html.index('$("cmpAsk").addEventListener("click"'):])
        tool = block("async function openCompareTool(")
        self.assertIn("compareWithAI(cmp,", tool)                                    # 비교 버튼과 같은 함수
        self.assertIn("opened: true", tool)
        self.assertIn("opened: false, reason", tool)
        self.assertIn("console.warn", tool)                                           # 실패 사유를 콘솔에
        self.assertIn('$("cmpSheet").classList.contains("open")', tool)              # 실제로 열렸는지 확인
        self.assertIn("wishList.find(w => w.key === it.key)", tool)
        self.assertIn('p.call?.name === "compare_favorites" && p.compare', html)
        for name in ("async function openCompareTool(", "async function compareWithAI("):
            for bad in ("chatReset", 'chatLog").innerHTML', 'openSheet("wishSheet")', "renderWishSheet"):
                self.assertNotIn(bad, block(name), (name, bad))


# ---------- 최근 명령 기억 ----------

RECENT = [{"input": "로지텍 K295 찜 취소해줘", "calls": ["remove_favorite(\"로지텍 K295\")"], "targets": ["로지텍 K295"]},
          {"input": "무선 마우스 검색해줘", "calls": ['search("무선 마우스")'], "targets": []}]


class RecentCommandsTest(GraphBase):
    def test_clean_recent_bad_input(self):
        self.assertEqual(actions.clean_recent("x"), [])
        self.assertEqual(actions.clean_recent([{"input": " "}, 3]), [])
        out = actions.clean_recent([{"input": "a", "calls": ["x"] * 9, "targets": [1, "t"]}])
        self.assertEqual(len(out[0]["calls"]), 5)
        self.assertEqual(out[0]["targets"], ["t"])                              # 숫자는 버림

    def test_page_keeps_five_and_sends_on_start(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("recent: actHistory", html[html.index("async function runAct("):])
        self.assertIn("actHistory = [{ input: sentence, calls, targets: [...new Set(targets)] }, ...actHistory].slice(0, 5)", html)
        self.assertIn("actHistory = []; recentlyRemoved = [];", html)           # 계정이 바뀌면 지움

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
        self.assertEqual(actions.eul_reul("K295"), "K295를")
        self.assertIn("ask_user로 확인을 받아야", actions.TOOLS["search"][0])
        self.assertIn('새로 검색할까요? (하루 검색 N회 중 1회 사용)", options=["검색", "취소"]', actions.SYSTEM)

    def test_search_confirmed_rules(self):
        u = lambda t: {"role": "user", "parts": [{"text": t}]}   # noqa: E731
        ask = lambda q: {"role": "model", "parts": [{"functionCall": {"name": "ask_user", "args": {"question": q}}}]}  # noqa: E731
        ans = lambda a: {"role": "user", "parts": [{"functionResponse": {"name": "ask_user", "response": {"answer": a}}}]}  # noqa: E731
        self.assertTrue(actions.search_confirmed([u("무선 마우스 검색")]))
        self.assertTrue(actions.search_confirmed([u("무선 마우스 찾아줘")]))
        self.assertFalse(actions.search_confirmed([u("무선 마우스 찜해줘")]))
        self.assertTrue(actions.search_confirmed([u("x 찜"), ask("마우스를 새로 검색할까요?"), ans("검색")]))
        self.assertFalse(actions.search_confirmed([u("x 찜"), ask("어느 거요?"), ans("검색")]))     # 검색 확인 질문이 아님
        self.assertFalse(actions.search_confirmed([u("x 찜"), ask("새로 검색할까요?"), ans("취소")]))

    def test_allowed_with_find_word(self):
        out = self.start(FakeLLM(call("search", {"query": "에어팟"})), sentence="에어팟 찾아줘")
        self.assertEqual(out["pending"]["call"]["name"], "search")


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
        self.assertLess(body.index("cancelAsk()"), body.index("await actRun"))   # 취소 → 끝나길 기다림 → 새 명령

    def test_page_ends_server_graph_and_keeps_chat(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        run = html[html.index("async function runAct("):]
        self.assertIn("if (r.resume.cancelled)", run)                              # 서버 그래프도 cancelled로 END
        i = html.index("function chatReset(")
        reset = html[i:html.index("\n}\n", i)]
        self.assertLess(reset.index("if (actRunning)"), reset.index('$("chatLog").innerHTML = ""'))   # ⚡ 중엔 안 지움

    def test_approve_card_cancel_by_new_input(self):
        """승인 카드 대기 중 새 명령 → cancelled → 저장 없이 END (최종 답 없음)."""
        out = self.start(FakeLLM(call("add_favorite", {"number": 1}), text("안 쓰임")))
        self.assertEqual(out["pending"]["kind"], "approve")
        out = self.resume({"cancelled": True})
        self.assertEqual((out["type"], out["text"], out["cancelled"]), ("text", "", True))
        self.assertNotIn("wait_client", out["trail"])


# ---------- 범위 기본값 ----------

class ScopeDefaultTest(unittest.TestCase):
    def test_scope_phrases(self):
        self.assertIn("최저가/제일 싼/가장 비싼/평점 높은", actions.TOOLS["find_in_results"][0])
        fav = actions.TOOLS["get_favorites"][0]
        self.assertIn('"찜한 것 중"', fav)
        self.assertIn("평점", fav)
        self.assertIn("범위 기본값", actions.SYSTEM)

    def test_page_sends_favorite_rating(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        i = html.index("const favForServer")
        self.assertIn("rating: w.rating", html[i:i + 300])

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


class CompareItemsTest(unittest.TestCase):
    def items(self, cond, favorites=FAVS, results=()):
        st = react_graph.initial_state("x", "u", [], list(results), "이어폰", list(favorites), [], 5)
        return react_graph.compare_items(st, cond)

    def test_match(self):
        self.assertEqual([i["key"] for i in self.items("에어팟")], ["k-pro2", "k-4"])
        self.assertEqual(len(self.items("에어 팟")), 2)
        self.assertEqual(self.items("삼성"), [])
        self.assertEqual(len(self.items("")), 3)                                  # 조건 없으면 찜 목록 전체


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


# ---------- 버그: "제일 비싼 거 찜해줘"가 (이미 찜한) 최저가를 고름 ----------

PRICED = [   # 최저가(샤크닌자)는 이미 찜한 상태, 가격은 글자로 온 것도 섞임
    {"key": "k-shark", "name": "샤크닌자 WV200", "price": 89000, "rating": 4.2},
    {"key": "k-dyson", "name": "다이슨 V12", "price": "899,000원", "rating": 4.8},
    {"key": "k-lg", "name": "LG 코드제로 A9", "price": 549000.0, "rating": 4.6},
    {"key": "k-none", "name": "가격 미정 청소기", "price": None, "rating": None},
]


def pick_first_candidate(seen):
    """진짜 Gemini처럼 find_in_results가 돌려준 첫 번째 후보(정렬 1등)의 번호로 찜."""
    top = seen["messages"][-1]["functionResponse"]["response"]["candidates"][0]
    return call("add_favorite", {"number": top["number"]})


class MostExpensiveBugTest(GraphBase):
    def test_most_expensive_when_cheapest_already_wished(self):
        favs = [{"key": "k-shark", "brand": "샤크닌자", "model": "WV200", "price": 89000}]
        llm = FakeLLM(call("find_in_results", {"sort": "price_desc"}), pick_first_candidate, text("다이슨 V12를 찜했어요."))
        with self.assertLogs(react_graph.logger, "INFO") as logs:
            out = self.start(llm, sentence="제일 비싼 거 찜해줘", results=PRICED, favorites=favs, query="무선 청소기")
        self.assertEqual(out["type"], "interrupt")
        self.assertEqual(out["pending"]["kind"], "approve")                      # "이미 찜" 아니고 승인 카드
        self.assertEqual(out["pending"]["product"]["name"], "다이슨 V12")        # 최고가
        self.assertEqual(out["pending"]["product"]["price"], 899000)
        resp = llm.seen[1]["messages"][-1]["functionResponse"]["response"]
        self.assertEqual([c["price"] for c in resp["candidates"]], [899000, 549000, 89000, None])   # 숫자로 정렬, 모르는 값은 맨 뒤
        self.assertTrue(resp["candidates"][2]["wished"])
        log = "\n".join(logs.output)
        self.assertIn('functionCall find_in_results {"sort": "price_desc"}', log)   # Gemini가 보낸 이름·입력값
        self.assertIn('[2, "다이슨 V12", 899000]', log)                              # 돌려준 후보(가격 포함)

    def test_cheapest_is_already_wished(self):
        favs = [{"key": "k-shark", "brand": "샤크닌자", "model": "WV200"}]
        llm = FakeLLM(call("find_in_results", {"sort": "price_asc"}), pick_first_candidate, text("이미 찜했어요."))
        out = self.start(llm, sentence="제일 싼 거 찜해줘", results=PRICED, favorites=favs)
        self.assertEqual(out["type"], "text")
        self.assertTrue(llm.last_response()["response"]["already"])


class FindSortFilterTest(unittest.TestCase):
    def find(self, args, results=PRICED, favorites=(), query="무선 청소기"):
        st = react_graph.initial_state("x", "u", [], results, query, list(favorites), [], 5)
        return react_graph.find_in_results(st, args)

    def names(self, out):
        return [c["name"] for c in out["candidates"]]

    def test_sorts(self):
        self.assertEqual(self.names(self.find({"sort": "price_asc"}))[:3], ["샤크닌자 WV200", "LG 코드제로 A9", "다이슨 V12"])
        self.assertEqual(self.names(self.find({"sort": "price_desc"}))[0], "다이슨 V12")
        self.assertEqual(self.names(self.find({"sort": "rating_desc"}))[0], "다이슨 V12")
        self.assertEqual(self.names(self.find({"sort": "rating_asc"}))[0], "샤크닌자 WV200")
        self.assertEqual(self.names(self.find({"sort": "price_asc"}))[-1], "가격 미정 청소기")   # 가격 없는 건 맨 뒤
        self.assertEqual([c["number"] for c in self.find({"sort": "price_desc"})["candidates"]], [2, 3, 1, 4])  # 번호는 원래 순서

    def test_filters(self):
        self.assertEqual(self.names(self.find({"max_price": 100000})), ["샤크닌자 WV200"])
        self.assertEqual(self.names(self.find({"min_price": 500000, "sort": "price_asc"})), ["LG 코드제로 A9", "다이슨 V12"])
        self.assertEqual(self.names(self.find({"brand": "다이슨"})), ["다이슨 V12"])
        self.assertEqual(self.names(self.find({"brand": "lg"})), ["LG 코드제로 A9"])
        out = self.find({"brand": "삼성"})
        self.assertEqual(out["candidates"], [])
        self.assertIn("ask_user", out["note"])
        self.assertEqual(self.find({"max_price": 600000, "sort": "price_desc"})["applied"],
                         {"max_price": 600000, "sort": "price_desc"})

    def test_price_to_number(self):
        for raw, want in ((129000, 129000), (129000.0, 129000), ("129,000원", 129000), ("₩1,299,000", 1299000),
                          ("가격 문의", None), (None, None), (True, None)):
            self.assertEqual(react_graph.to_price(raw), want, raw)

    def test_check_call_new_args(self):
        self.assertEqual(actions.check_call("find_in_results", {"sort": "price_desc"}, None), ({"sort": "price_desc"}, None))
        self.assertEqual(actions.check_call("find_in_results", {"max_price": 100000.0, "brand": "삼성"}, None),
                         ({"max_price": 100000, "brand": "삼성"}, None))
        self.assertIn("중 하나여야", actions.check_call("find_in_results", {"sort": "expensive"}, None)[1])
        self.assertIn("형식이 잘못", actions.check_call("find_in_results", {"max_price": "10만원"}, None)[1])
        self.assertIn("0 이상 금액", actions.check_call("find_in_results", {"min_price": -1}, None)[1])

    def test_description_says_use_args(self):
        d = actions.TOOLS["find_in_results"][0]
        for w in ('제일 싼/최저가 → sort="price_asc"', '제일 비싼/최고가 → sort="price_desc"', '평점 높은 → sort="rating_desc"',
                  "10만원 이하 → max_price=100000", "직접 하지 말고 반드시 sort·max_price·min_price·brand"):
            self.assertIn(w, d)
        self.assertIn('find_in_results(sort="price_desc")', actions.SYSTEM)


class DuplicateByIdTest(GraphBase):
    def test_same_name_other_id_is_not_duplicate(self):
        """찜한 상품과 이름이 같아도 ID가 다르면 중복 아님 → 승인 카드."""
        favs = [{"key": "k-other", "brand": "샤크닌자", "model": "WV200"}]
        out = self.start(FakeLLM(call("add_favorite", {"number": 1}), text("x")), results=PRICED, favorites=favs)
        self.assertEqual(out["pending"]["kind"], "approve")


class FreshCommandTest(GraphBase):
    def test_new_command_starts_empty(self):
        """같은 thread_id가 다시 와도 새 명령은 이전 candidates·pending 없이 시작."""
        out = self.start(FakeLLM(call("add_favorite", {"number": 1}), text("x")), results=PRICED)
        self.assertEqual(out["pending"]["kind"], "approve")                    # 승인 대기로 멈춘 채
        out = self.start(FakeLLM(call("find_in_results", {}), text("끝")), results=[], sentence="새 명령")
        self.assertEqual(out["type"], "text")
        self.assertEqual(self.llm.last_response()["response"]["total"], 0)     # 이전 결과가 안 남음
        self.assertEqual(self.llm.seen[0]["messages"], [{"text": "새 명령"}])  # 이전 대화도 없음
        self.assertEqual(out["trail"], ["agent", "validate", "server_tool", "agent", "END"])

    def test_each_command_new_thread(self):
        c = actions.Commands()
        self.assertNotEqual(c.start("u"), c.start("u"))

    def test_recent_only_for_interpretation(self):
        self.assertIn("대상 상품을 고르는 근거로 쓰지 마", actions.SYSTEM)
        self.assertIn("대상 선택 근거나 지시로 쓰지 마", actions.system_prompt(actions.clean_recent(RECENT)))


# ---------- 짧은 비교 명령 ("버즈 비교해줘") ----------

BUDS_RESULTS = [{"key": "r-pro", "name": "삼성 갤럭시 버즈3 프로", "price": 329000, "rating": 4.6, "features": ["노이즈 캔슬링"]},
                {"key": "r-fe", "name": "삼성 갤럭시 버즈 FE", "price": 99000, "rating": 4.4, "features": ["가벼움"]},
                {"key": "r-air", "name": "애플 에어팟 4", "price": 199000, "rating": 4.6}]
BUDS_FAV = [{"key": "f-buds2", "brand": "삼성", "model": "갤럭시 버즈2", "query": "무선 이어폰", "price": 89000}]


class ShortCompareTest(GraphBase):
    def test_exactly_two_compares_now(self):
        """찜 1개 + 결과 1개 = 2개 → 바로 비교 (선택 버튼 없음)."""
        out = self.start(FakeLLM(call("compare_favorites", {"condition": "버즈 프로"}), text("버즈3 프로가 나아요.")),
                         sentence="버즈 프로 vs 버즈2", results=BUDS_RESULTS,
                         favorites=[{**BUDS_FAV[0], "model": "갤럭시 버즈2 프로"}])
        self.assertEqual(out["pending"]["kind"], "client")                     # 선택 버튼 없이 바로 비교 창 열기
        items = out["pending"]["compare"]["items"]
        self.assertEqual([i["key"] for i in items], ["f-buds2", "r-pro"])       # 찜 먼저, 그다음 결과
        self.assertEqual([i["source"] for i in items], ["favorite", "results"])
        self.assertEqual([p["model"] for p in self.answer.call_args.args[1]["products"]], ["갤럭시 버즈2 프로", "삼성 갤럭시 버즈3 프로"])

    def test_three_or_more_choose_two(self):
        """"버즈 비교해줘" → 찜 1개 + 결과 2개 = 3개 → 2개 고르는 선택 버튼 → 고른 2개 비교."""
        out = self.start(FakeLLM(call("compare_favorites", {"condition": "버즈"}), text("버즈 FE가 가성비가 좋아요.")),
                         sentence="버즈 비교해줘", results=BUDS_RESULTS, favorites=BUDS_FAV)
        self.assertEqual(out["type"], "interrupt")
        p = out["pending"]
        self.assertEqual((p["kind"], p["pick"], p["matched"]), ("choose", 2, 3))
        self.assertEqual([o["key"] for o in p["options"]], ["f-buds2", "r-pro", "r-fe"])
        self.assertTrue(p["question"].endswith("(2개 선택)"))
        self.assertEqual((p["multi"], p["min_select"], p["max_select"], p["submit_label"]), (True, 2, 2, "비교하기"))
        self.assertEqual([c["id"] for c in p["choices"]], ["f-buds2", "r-pro", "r-fe"])
        self.answer.assert_not_called()                                         # 고르기 전엔 비교 안 함
        out = self.resume({"response": {"selected": ["r-fe", "f-buds2"]}})
        self.assertEqual(out["pending"]["kind"], "client")                     # 고른 뒤 비교 → 비교 창 열기도 화면에서
        self.assertEqual([i["key"] for i in out["pending"]["compare"]["items"]], ["r-fe", "f-buds2"])
        out = self.resume({"response": {"opened": True}})
        self.assertEqual(out["type"], "text")
        self.assertEqual(out["trail"], ["agent", "validate", "server_tool", "wait_client", "wait_client", "agent", "END"])
        self.assertEqual(self.llm.last_response()["name"], "compare_favorites")
        self.assertTrue(self.llm.last_response()["response"]["opened"])

    def test_bad_choice_asks_again(self):
        """후보에 없는 ID는 버리고 1개만 남음 → 같은 선택 버튼을 다시 (남은 1개는 선택된 채), Gemini는 안 부름."""
        self.start(FakeLLM(call("compare_favorites", {"condition": "버즈"}), text("버즈 FE가 나아요")),
                   results=BUDS_RESULTS, favorites=BUDS_FAV)
        out = self.resume({"response": {"selected": ["r-fe", "nope"]}})
        self.assertEqual(out["type"], "interrupt")
        self.assertEqual(out["pending"]["selected"], ["r-fe"])
        self.assertIn("2개를 골라 주세요 (지금 1개)", out["pending"]["error"])
        self.assertEqual(len(self.llm.seen), 1)
        self.answer.assert_not_called()
        out = self.resume({"response": {"selected": ["r-fe", "r-pro"]}})
        self.assertEqual([i["key"] for i in out["pending"]["compare"]["items"]], ["r-fe", "r-pro"])

    def test_one_or_less_not_enough(self):
        out = self.start(FakeLLM(call("compare_favorites", {"condition": "에어팟"}), text("부족해요")),
                         results=BUDS_RESULTS, favorites=BUDS_FAV)
        self.assertEqual(self.llm.last_response()["response"], {"error": actions.NEED_TWO, "matched": 1})
        self.assertIn("비교할 상품이 부족해요", actions.NEED_TWO)
        self.assertEqual(out["type"], "text")

    def test_prompts(self):
        import chat
        d = actions.TOOLS["compare_favorites"][0]
        for w in ("짧은 이름이면 충분", "3개 이상이면 화면에 2개 고르는 선택 버튼", "긴 상품명을 다시 입력하라고 하지 말고"):
            self.assertIn(w, d)
        self.assertIn('compare_favorites(condition="짧은 이름")', actions.SYSTEM)
        self.assertIn("긴 상품명을 정확히 다시 입력하라고 안내하지 마", chat.SYSTEM)
        self.assertIn("선택 버튼", chat.SYSTEM)

    def test_page_choose_ui(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("} else if (p.multi) {", html)
        body = html[html.index("function askMulti("):]
        self.assertIn("pendingAsk.multi = choices", body[:4000])                # 입력으로도 고름
        tool = html[html.index("async function openCompareTool("):html.index("async function openCompareTool(") + 1500]
        self.assertIn("shownResult?.products", tool)                            # 결과에만 있는 상품도 비교 창에


# ---------- ask_user 여러 개 선택 ----------

OPTS3 = ["1. 테팔 무선 청소기", "2. 리빙센스 청소기", "3. 필립스 청소기"]


class MultiSelectTest(GraphBase):
    def ask(self, **kw):
        return call("ask_user", {"question": "어느 두 개를 비교할까요?", "options": OPTS3, **kw})

    def test_check_call(self):
        self.assertEqual(actions.check_call("ask_user", {"question": "q", "options": OPTS3, "min_select": 2, "max_select": 2}, USER)[0],
                         {"question": "q", "options": OPTS3, "min_select": 2, "max_select": 2})
        self.assertEqual(actions.check_call("ask_user", {"question": "q", "options": OPTS3, "min_select": 2}, USER)[0]["max_select"], 2)
        self.assertNotIn("min_select", actions.check_call("ask_user", {"question": "q", "options": OPTS3}, USER)[0])   # 기본 1개
        self.assertIn("클 수 없어요", actions.check_call("ask_user", {"question": "q", "options": OPTS3, "min_select": 3, "max_select": 2}, USER)[1])
        self.assertIn("options가 4개 이상", actions.check_call("ask_user", {"question": "q", "options": OPTS3, "max_select": 4}, USER)[1])
        self.assertIn("min_select", actions.TOOLS["ask_user"][1]["properties"])
        self.assertIn("answers", actions.TOOLS["ask_user"][0])

    def test_multi_pending_and_answers(self):
        out = self.start(FakeLLM(self.ask(min_select=2, max_select=2), text("테팔과 필립스를 비교할게요.")))
        p = out["pending"]
        self.assertEqual((p["kind"], p["multi"], p["min_select"], p["max_select"]), ("ask", True, 2, 2))
        self.assertEqual(p["question"], "어느 두 개를 비교할까요? (2개 선택)")
        self.assertEqual([c["id"] for c in p["choices"]], OPTS3)
        out = self.resume({"response": {"selected": [OPTS3[0], OPTS3[2]]}})
        self.assertEqual(out["type"], "text")
        self.assertEqual(self.llm.last_response()["response"], {"answers": [OPTS3[0], OPTS3[2]]})

    def test_one_only_asks_again_keeping_selection(self):
        out = self.start(FakeLLM(self.ask(min_select=2, max_select=2), text("끝")))
        out = self.resume({"response": {"selected": [OPTS3[1]]}})
        self.assertEqual(out["type"], "interrupt")                              # 같은 질문 다시
        self.assertEqual(out["pending"]["selected"], [OPTS3[1]])                # 고른 것은 선택된 채
        self.assertEqual(out["pending"]["question"], "어느 두 개를 비교할까요? (2개 선택)")
        self.assertEqual(len(self.llm.seen), 1)                                 # 다시 물을 땐 Gemini 안 부름
        out = self.resume({"response": {"selected": [OPTS3[1], OPTS3[1], "없는 것"]}})   # 중복·없는 것 → 여전히 1개
        self.assertEqual(out["pending"]["selected"], [OPTS3[1]])
        out = self.resume({"response": {"selected": [OPTS3[1], OPTS3[0]]}})
        self.assertEqual(self.llm.last_response()["response"], {"answers": [OPTS3[1], OPTS3[0]]})

    def test_too_many_trimmed(self):
        self.start(FakeLLM(self.ask(min_select=1, max_select=2), text("끝")))
        out = self.resume({"response": {"selected": OPTS3}})
        self.assertEqual(out["pending"]["selected"], OPTS3[:2])
        self.assertEqual(out["pending"]["question"], "어느 두 개를 비교할까요? (1~2개 선택)")

    def test_single_select_unchanged(self):
        out = self.start(FakeLLM(self.ask(), text("끝")))
        self.assertNotIn("multi", out["pending"])
        self.resume({"response": {"answer": OPTS3[0]}})
        self.assertEqual(self.llm.last_response()["response"], {"answer": OPTS3[0]})

    def test_cancel_while_multi(self):
        self.start(FakeLLM(self.ask(min_select=2, max_select=2), text("끝")))
        out = self.resume({"cancelled": True})
        self.assertTrue(out["cancelled"])

    def test_page(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        body = html[html.index("function askMulti("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("picked.size < mn || picked.size > mx", body)            # 개수 맞을 때만 보내기 버튼
        self.assertIn("p.selected", body)                                       # 다시 물을 때 선택 유지
        self.assertIn("b.disabled = true", body)                                # 답한 질문의 버튼은 비활성화
        sub = html[html.index('$("chatForm").addEventListener("submit"'):]
        self.assertIn("matchOptions", sub[:2000])
