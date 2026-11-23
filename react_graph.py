"""챗봇 ⚡ ReAct 반복을 LangGraph StateGraph로 (서버가 상태를 들고 있음, 체크포인터 MemorySaver).

    START → agent ─(functionCall)→ validate ─(통과)→ server_tool ──→ agent        (find_in_results·get_favorites·compare_favorites)
              │                      │            └(add_favorite)→ approve → wait_client → agent
              │                      ├(통과)→ wait_client → agent                  (search·remove_favorite·open_favorites·ask_user)
              │                      └(거부: 사유를 functionResponse로)→ agent
              └(최종 답 / Gemini 호출 MAX_CALLS번 넘음)→ END

- wait_client·approve는 interrupt()로 멈추고 화면 결과를 기다림. 화면은 thread_id(cmd)로 Command(resume=결과)를 보내 이어 감
- 같은 상품을 또 찜하면 server_tool이 "이미 찜한 상품이에요"로 답하고 화면은 아무것도 실행하지 않음 (찜 버튼이 토글이라)
- add_favorite 전 approve에서 승인 카드 [저장/취소]. 취소면 저장 0회로 END
"""

import json
import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import actions
import chat
from gemini import step_call

llm = step_call                 # Gemini 함수 호출 1번 (테스트에서 가짜로 바꿈)
compare_answer = chat.answer    # compare_favorites 추천 문장 (기존 1:1 비교 모드, 숫자는 compare.py 검증)

SERVER_TOOLS = {"find_in_results", "get_favorites", "compare_favorites"}
CLIENT_TOOLS = {"search", "add_favorite", "remove_favorite", "open_favorites", "ask_user"}
ALREADY = "이미 찜한 상품이에요"
SAVE_CANCELLED = "저장을 취소했어요. 찜 목록은 그대로예요."
MAX_CANDIDATES = 20
RECURSION_LIMIT = 100


class State(TypedDict, total=False):
    messages: list            # Gemini 대화 기록 (사용자 문장, functionCall, functionResponse)
    candidates: list          # 지금 화면의 검색 결과 (번호는 순서, 1부터)
    query: str                # 그 검색어
    favorites: list           # 찜 목록 (찜한 시각 최근순)
    removed: list             # 최근에 ⚡로 찜 취소한 상품 ("다시 찜해줘")
    recent_actions: list      # 직전 ⚡ 명령 요약 (최근 명령 기억)
    step_count: int           # Gemini 호출 수
    pending: dict | None      # 기다리는 일 {kind: approve|client|ask, call, product?}
    call: dict | None         # 처리 중인 함수 {name, args, product?}
    user_id: str
    search_limit: int
    final: str | None         # 최종 답 (END)
    cancelled: bool
    tokens: int
    trail: Annotated[list, operator.add]     # 지난 노드 순서
    effects: Annotated[list, operator.add]   # 화면이 할 일 (AI 비교 창 열기 등)


# ---------- 화면이 보낸 값 정리 ----------

def _s(v, n=100) -> str:
    return " ".join(str(v or "").split())[:n]


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _strs(v, k, n=120) -> list[str]:
    return [_s(x, n) for x in v if isinstance(x, str) and x.strip()][:k] if isinstance(v, list) else []


def clean_candidates(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw[:MAX_CANDIDATES]:
        if isinstance(c, dict) and _s(c.get("name")):
            out.append({"key": _s(c.get("key"), 200), "name": _s(c.get("name")), "price": _num(c.get("price")),
                        "currency": _s(c.get("currency"), 10) or None, "rating": _num(c.get("rating")),
                        "review_count": _num(c.get("review_count")), "store": _s(c.get("store"), 60) or None,
                        "features": _strs(c.get("features"), 2), "cons": _strs(c.get("cons"), 1)})
    return out


def clean_favorites(raw, limit=actions.MAX_FAVORITES) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for w in raw[:limit]:
        if not isinstance(w, dict) or not isinstance(w.get("model"), str) or not w["model"].strip():
            continue
        out.append({"key": _s(w.get("key"), 200), "brand": _s(w.get("brand"), 60), "model": _s(w.get("model")),
                    "price": _num(w.get("price")), "currency": _s(w.get("currency"), 10) or None,
                    "rating": _num(w.get("rating")), "review_count": _num(w.get("review_count")),
                    "store": _s(w.get("store"), 60) or None, "saved_at": _s(w.get("saved_at"), 40) or None,
                    "query": _s(w.get("query")) or None, "pros": _strs(w.get("pros"), 3), "cons": _strs(w.get("cons"), 3)})
    return out


def clean_resume(raw) -> dict:
    """화면이 보낸 resume 값: {response, candidates?, query?, favorites?} | {approved} | {cancelled}. 아니면 ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("요청 형식이 잘못됐어요.")
    if raw.get("cancelled") is True:
        return {"cancelled": True}
    if "approved" in raw:
        return {"approved": raw["approved"] is True}
    resp = raw.get("response")
    if not isinstance(resp, dict) or len(json.dumps(resp, ensure_ascii=False)) > actions.MAX_RESPONSE:
        raise ValueError("도구 결과 형식이 잘못됐어요.")
    out = {"response": resp}
    if "candidates" in raw:
        out["candidates"], out["query"] = clean_candidates(raw["candidates"]), _s(raw.get("query"))
    if "favorites" in raw:
        out["favorites"] = clean_favorites(raw["favorites"])
    return out


def initial_state(sentence, user_id, recent, candidates, query, favorites, removed, search_limit) -> State:
    return {"messages": [{"role": "user", "parts": [{"text": sentence}]}],
            "candidates": clean_candidates(candidates), "query": _s(query), "favorites": clean_favorites(favorites),
            "removed": clean_favorites(removed, 10), "recent_actions": actions.clean_recent(recent),
            "step_count": 0, "pending": None, "call": None, "user_id": user_id, "search_limit": search_limit,
            "final": None, "cancelled": False, "tokens": 0, "trail": [], "effects": []}


# ---------- 서버 도구 ----------

def _norm(s) -> str:
    return " ".join(str(s or "").split()).lower()


def _fav_name(w) -> str:
    return f"{w.get('brand') or ''} {w.get('model') or ''}".strip()


def _wished(state, key, name="") -> bool:
    favs = state.get("favorites") or []
    if key:
        return any(w["key"] == key for w in favs)
    return bool(name) and any(_norm(_fav_name(w)) == _norm(name) for w in favs)


def _for_model(state, c, i) -> dict:
    """Gemini에 보여 줄 후보 (key는 빼고 번호·찜 여부)."""
    d = {k: v for k, v in c.items() if k != "key"}
    return {"number": i + 1, **d, "wished": _wished(state, c["key"], c["name"])}


def find_in_results(state, description: str) -> dict:
    """설명 단어로 후보를 거름. 검색어에 있는 단어(예: "마우스")는 모든 상품에 맞는 것으로 봄. 없으면 전체 (exact: false)."""
    cands = state.get("candidates") or []
    if not cands:
        return {"candidates": [], "total": 0, "note": "지금 검색 결과가 없어요. 필요하면 search를 쓰세요."}
    q = _norm(state.get("query"))
    words = [w for w in _norm(description).split() if len(w) >= 2 and w not in q]
    text = lambda c: _norm(" ".join([c["name"], c.get("store") or ""] + c["features"] + c["cons"]))  # noqa: E731
    all_ = [_for_model(state, c, i) for i, c in enumerate(cands)]
    hit = [m for m, c in zip(all_, cands) if any(w in text(c) for w in words)] if words else all_
    if not hit:
        return {"candidates": all_, "total": len(all_), "exact": False,
                "note": "설명과 딱 맞는 상품이 없어 전체 결과를 보냈어요. 맞는 게 없으면 ask_user로 물어보세요."}
    return {"candidates": hit, "total": len(all_), "exact": True}


def get_favorites(state) -> dict:
    favs = state.get("favorites") or []
    items = [{"favorite_number": i + 1, "name": _fav_name(w), "price": w["price"], "currency": w["currency"],
              "rating": w["rating"], "review_count": w["review_count"], "saved_at": w["saved_at"], "query": w["query"]}
             for i, w in enumerate(favs[:30])]
    return {"count": len(favs), "favorites": items, "note": "최근에 찜한 순서예요. 1번이 방금(가장 최근에) 찜한 상품이에요."}


def compare_favorites(state, condition: str) -> tuple[dict, list]:
    picked, total = actions.match_favorites(state.get("favorites") or [], condition)
    cmp = chat.clean_compare({"products": picked}) if len(picked) == 2 else None
    if cmp is None:
        return {"error": actions.NEED_TWO, "matched": total}, []
    ans = compare_answer("두 상품을 비교해서 더 나은 하나를 추천해줘", cmp, [], True)
    names = [_fav_name(p) for p in picked]
    pick = ans.get("pick_id")
    resp = {"ok": True, "compared": names, "recommended": names[pick] if isinstance(pick, int) and 0 <= pick < 2 else None,
            "matched": total, "analysis": ans["answer"],
            "note": "화면에 AI 비교 창이 열렸어요. analysis(숫자 확인된 비교)를 보고 추천 상품과 이유를 2~3문장으로 전해 주세요."}
    effect = {"type": "compare", "keys": [p["key"] for p in picked], "answer": ans["answer"], "pick_id": pick, "matched": total}
    return resp, [effect]


def resolve_add(state, args) -> tuple[dict | None, str | None]:
    """add_favorite 대상 → (상품, None) 또는 (None, 오류). 상품: {key, name, price, currency, source, number?}"""
    cands = state.get("candidates") or []
    if args.get("number"):
        n = args["number"]
        if n > len(cands):
            return None, f"{n}번 상품이 지금 결과에 없어요 (결과 {len(cands)}개)."
        c = cands[n - 1]
        return {"key": c["key"], "name": c["name"], "price": c["price"], "currency": c["currency"], "source": "results",
                "number": n}, None
    want = _norm(args.get("name"))
    for i, c in enumerate(cands):
        if want and (want in _norm(c["name"]) or _norm(c["name"]) in want):
            return {"key": c["key"], "name": c["name"], "price": c["price"], "currency": c["currency"],
                    "source": "results", "number": i + 1}, None
    for w in state.get("removed") or []:   # "다시 찜해줘": 최근에 찜 취소한 상품
        if want and (want in _norm(_fav_name(w)) or _norm(_fav_name(w)) in want):
            return {"key": w["key"], "name": _fav_name(w), "price": w["price"], "currency": w["currency"],
                    "source": "removed"}, None
    return None, f"“{args.get('name')}” 상품을 지금 결과에서 찾지 못했어요."


def _respond(state, name, response) -> list:
    return state["messages"] + [{"role": "user", "parts": [{"functionResponse": {"name": name, "response": response}}]}]


# ---------- 노드 ----------

def agent(state: State) -> dict:
    if state["step_count"] >= actions.MAX_CALLS:
        return {"final": actions.TOO_MANY, "call": None, "trail": ["agent(한도)"]}
    usage = {}
    out = llm(actions.system_prompt(state.get("recent_actions") or []), state["messages"], actions.TOOLS,
              actions.TIMEOUT, usage)
    upd = {"step_count": state["step_count"] + 1, "trail": ["agent"],
           "tokens": (state.get("tokens") or 0) + (usage.get("tokens") or 0)}
    if out["type"] == "text":
        return {**upd, "final": out["text"], "call": None}
    part = {"functionCall": {"name": out["name"], "args": out["args"]}}
    if out.get("signature"):
        part["thoughtSignature"] = out["signature"]
    return {**upd, "call": {"name": out["name"], "args": out["args"]},
            "messages": state["messages"] + [{"role": "model", "parts": [part]}]}


def validate(state: State) -> dict:
    call = state["call"]
    args, why = actions.check_call(call["name"], call["args"], state.get("user_id"))
    if why is None and call["name"] == "search" and not actions.search_confirmed(state["messages"]):
        why = actions.search_confirm_reason(args["query"], state.get("search_limit") or 0)   # 검색은 확인부터
    if why:   # 사유를 담아 agent로
        return {"messages": _respond(state, call["name"], {"error": why}), "call": None, "trail": ["validate"]}
    call = {"name": call["name"], "args": args}
    pending = None
    if call["name"] in CLIENT_TOOLS and call["name"] != "add_favorite":
        pending = {"kind": "ask" if call["name"] == "ask_user" else "client", "call": call}
    return {"call": call, "pending": pending, "trail": ["validate"]}


def server_tool(state: State) -> dict:
    call = state["call"]
    name, args = call["name"], call["args"]
    effects = []
    if name == "find_in_results":
        resp = find_in_results(state, args["description"])
    elif name == "get_favorites":
        resp = get_favorites(state)
    elif name == "compare_favorites":
        resp, effects = compare_favorites(state, args.get("condition") or "")
    else:   # add_favorite: 대상 찾기 + 중복 찜 확인 → 승인으로
        product, err = resolve_add(state, args)
        if err:
            resp = {"error": err}
        elif _wished(state, product["key"], product["name"]):
            resp = {"ok": True, "already": True, "name": product["name"], "message": ALREADY}   # 실행 0회
        else:
            return {"call": {**call, "product": product}, "trail": ["server_tool"],
                    "pending": {"kind": "approve", "call": {"name": name, "args": args}, "product": product}}
    return {"messages": _respond(state, name, resp), "call": None, "pending": None, "effects": effects,
            "trail": ["server_tool"]}


def approve(state: State) -> dict:
    """add_favorite 전 승인 카드 (상품명·가격·"내 찜 목록"에 저장 [저장/취소])."""
    answer = interrupt(state["pending"])
    if not (isinstance(answer, dict) and answer.get("approved") is True):
        cancelled = isinstance(answer, dict) and answer.get("cancelled") is True
        return {"final": "" if cancelled else SAVE_CANCELLED, "cancelled": cancelled, "call": None, "pending": None,
                "trail": ["approve"]}
    call = state["call"]
    return {"pending": {"kind": "client", "call": {"name": call["name"], "args": call["args"]}, "product": call["product"]},
            "trail": ["approve"]}


def wait_client(state: State) -> dict:
    """화면에서 실행할 도구: 멈추고 결과를 기다림 → functionResponse로 붙여 agent로."""
    value = interrupt(state["pending"])
    v = value if isinstance(value, dict) else {}
    if v.get("cancelled"):   # ask_user 대기 중 새 명령 등: 이 명령은 끝
        return {"final": "", "cancelled": True, "call": None, "pending": None, "trail": ["wait_client"]}
    upd = {"messages": _respond(state, state["call"]["name"], v.get("response") or {}), "call": None, "pending": None,
           "trail": ["wait_client"]}
    if "candidates" in v:
        upd["candidates"], upd["query"] = v["candidates"], v.get("query") or ""
    if "favorites" in v:
        upd["favorites"] = v["favorites"]
    return upd


# ---------- 엣지 ----------

def after_agent(state: State) -> str:
    return "validate" if state.get("call") else END        # 최종 답 / 한도 → END


def after_validate(state: State) -> str:
    call = state.get("call")
    if not call:
        return "agent"                                      # 검증 실패 → 사유를 담아 agent로
    if call["name"] in SERVER_TOOLS or call["name"] == "add_favorite":
        return "server_tool"
    return "wait_client"


def after_server_tool(state: State) -> str:
    return "approve" if state.get("call") else "agent"


def after_approve(state: State) -> str:
    return END if state.get("final") is not None else "wait_client"


def after_wait(state: State) -> str:
    return END if state.get("cancelled") else "agent"


def build(checkpointer=None):
    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_node("validate", validate)
    g.add_node("server_tool", server_tool)
    g.add_node("approve", approve)
    g.add_node("wait_client", wait_client)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent, ["validate", END])
    g.add_conditional_edges("validate", after_validate, ["agent", "server_tool", "wait_client"])
    g.add_conditional_edges("server_tool", after_server_tool, ["approve", "agent"])
    g.add_conditional_edges("approve", after_approve, ["wait_client", END])
    g.add_conditional_edges("wait_client", after_wait, ["agent", END])
    return g.compile(checkpointer=checkpointer or MemorySaver())


saver = MemorySaver()
graph = build(saver)


def run(inp, thread_id: str) -> dict:
    """새 명령(초기 State) 또는 Command(resume=…)로 실행 → 화면에 줄 결과.
    {"type": "interrupt", "pending", ...} (화면 실행·승인 대기) 또는 {"type": "text", "text", ...} (끝)."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    graph.invoke(inp, config)
    snap = graph.get_state(config)
    v = snap.values
    waits = [i for t in snap.tasks for i in t.interrupts]
    base = {"trail": list(v.get("trail") or []), "effects": list(v.get("effects") or []), "tokens": v.get("tokens") or 0}
    if waits:
        return {"type": "interrupt", "pending": waits[0].value, "done": False, **base}
    base["trail"].append("END")
    return {"type": "text", "text": v.get("final") or "", "cancelled": bool(v.get("cancelled")), "done": True, **base}


def resume(value) -> Command:
    return Command(resume=value)


def forget(thread_id: str) -> None:
    """끝났거나 만료된 명령의 체크포인트 지우기."""
    try:
        saver.delete_thread(thread_id)
    except Exception:
        pass
