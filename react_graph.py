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
import logging
import operator
import re
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import actions
import chat
import claude_errors
from gemini import step_call

llm = step_call                 # Gemini 함수 호출 1번 (테스트에서 가짜로 바꿈)
logger = logging.getLogger("react_graph")   # app.py가 Flask 로그(Render 로그)로 바꿔 끼움
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


def to_price(v):
    """가격 → 숫자(원). 129000, 129000.0, "129,000원", "₩129,000" 모두 허용. 모르면 None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else v
    digits = re.sub(r"[^0-9.]", "", str(v or ""))
    if not digits or digits.count(".") > 1:
        return None
    n = float(digits)
    return int(n) if n.is_integer() else n


def _strs(v, k, n=120) -> list[str]:
    return [_s(x, n) for x in v if isinstance(x, str) and x.strip()][:k] if isinstance(v, list) else []


def clean_candidates(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw[:MAX_CANDIDATES]:
        if isinstance(c, dict) and _s(c.get("name")):
            out.append({"key": _s(c.get("key"), 200), "name": _s(c.get("name")), "price": to_price(c.get("price")),
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


def _wished(state, key) -> bool:
    """중복 찜 확인은 고른 상품의 ID(key)로만 (이름이 비슷한 다른 상품은 중복 아님)."""
    return bool(key) and any(w["key"] == key for w in state.get("favorites") or [])


def _for_model(state, c, i) -> dict:
    """Gemini에 보여 줄 후보 (key는 빼고 번호·찜 여부)."""
    d = {k: v for k, v in c.items() if k != "key"}
    return {"number": i + 1, **d, "wished": _wished(state, c["key"])}


SORTS = {   # 정렬 기준: (값, 큰 게 먼저?)
    "price_asc": ("price", False), "price_desc": ("price", True),
    "rating_desc": ("rating", True), "rating_asc": ("rating", False),
}


def find_in_results(state, args: dict) -> dict:
    """지금 결과에서 후보 찾기. 가격·평점 비교는 Gemini가 아니라 여기서 숫자로:
    brand·min_price·max_price로 거르고 sort로 정렬 (정렬하면 candidates[0]이 1등). description 단어로도 거름
    (검색어에 있는 단어는 모든 상품에 맞는 것으로 봄, 딱 맞는 게 없으면 거르지 않음 = exact: false)."""
    cands = state.get("candidates") or []
    if not cands:
        return {"candidates": [], "total": 0, "note": "지금 검색 결과가 없어요. 필요하면 search를 쓰세요."}
    pairs = [(_for_model(state, c, i), c) for i, c in enumerate(cands)]
    applied, notes = {}, []
    if args.get("brand"):
        b = _norm(args["brand"]).replace(" ", "")
        pairs = [(m, c) for m, c in pairs if b in _norm(c["name"]).replace(" ", "")]
        applied["brand"] = args["brand"]
    for key, ok in (("min_price", lambda p, v: p >= v), ("max_price", lambda p, v: p <= v)):
        if args.get(key) is not None:
            pairs = [(m, c) for m, c in pairs if c["price"] is not None and ok(c["price"], args[key])]
            applied[key] = args[key]
    exact = True
    q = _norm(state.get("query"))
    words = [w for w in _norm(args.get("description")).split() if len(w) >= 2 and w not in q]
    if words:
        text = lambda c: _norm(" ".join([c["name"], c.get("store") or ""] + c["features"] + c["cons"]))  # noqa: E731
        hit = [(m, c) for m, c in pairs if any(w in text(c) for w in words)]
        if hit:
            pairs = hit
        else:
            exact = False
            notes.append("설명과 딱 맞는 상품이 없어 설명으로는 거르지 않았어요.")
    if args.get("sort") in SORTS:
        field, desc = SORTS[args["sort"]]
        known = [(m, c) for m, c in pairs if c[field] is not None]
        unknown = [(m, c) for m, c in pairs if c[field] is None]
        known.sort(key=lambda mc: mc[1][field], reverse=desc)   # 같은 값이면 원래 순서 (안정 정렬)
        pairs = known + unknown
        applied["sort"] = args["sort"]
        if unknown:
            notes.append(f"{'가격' if field == 'price' else '평점'} 정보가 없는 {len(unknown)}개는 맨 뒤에 뒀어요.")
        if known:
            notes.append(f"정렬했어요. 1등은 {known[0][1]['name']} (번호 {known[0][0]['number']}).")
    out = {"candidates": [m for m, _ in pairs], "total": len(cands), "exact": exact, "applied": applied}
    if not pairs:
        out["note"] = "조건에 맞는 상품이 없어요. ask_user로 다른 조건을 물어보세요."
    elif notes:
        out["note"] = " ".join(notes)
    return out


def get_favorites(state) -> dict:
    favs = state.get("favorites") or []
    items = [{"favorite_number": i + 1, "name": _fav_name(w), "price": w["price"], "currency": w["currency"],
              "rating": w["rating"], "review_count": w["review_count"], "saved_at": w["saved_at"], "query": w["query"]}
             for i, w in enumerate(favs[:30])]
    return {"count": len(favs), "favorites": items, "note": "최근에 찜한 순서예요. 1번이 방금(가장 최근에) 찜한 상품이에요."}


MAX_CHOICES = 6   # 비교할 2개를 고르는 버튼 수


def compare_items(state, condition: str) -> list[dict]:
    """짧은 조건(예: "버즈")에 맞는 비교 후보를 코드로: 찜 목록(최근순) 먼저, 그다음 지금 검색 결과. ID(key)로 중복 제거.
    조건 단어가 모두 이름(찜은 브랜드·모델·검색어)에 들어 있어야 함 (띄어쓰기 달라도). 조건이 비면 찜 목록 전체."""
    words = _norm(condition).split()

    def ok(hay):
        t = _norm(hay)
        return all(w in t or w in t.replace(" ", "") for w in words)

    out, seen = [], set()
    for w in state.get("favorites") or []:
        if ok(f"{w['brand']} {w['model']} {w['query'] or ''}") and w["key"] not in seen:
            seen.add(w["key"])
            out.append({"key": w["key"], "brand": w["brand"], "model": w["model"], "name": _fav_name(w),
                        "price": w["price"], "currency": w["currency"], "store": w["store"], "rating": w["rating"],
                        "review_count": w["review_count"], "pros": w["pros"], "cons": w["cons"], "query": w["query"],
                        "source": "favorite"})
    if words:
        for c in state.get("candidates") or []:
            if ok(c["name"]) and c["key"] not in seen:
                seen.add(c["key"])
                out.append({"key": c["key"], "brand": "", "model": c["name"], "name": c["name"], "price": c["price"],
                            "currency": c["currency"], "store": c["store"], "rating": c["rating"],
                            "review_count": c["review_count"], "pros": c["features"], "cons": c["cons"],
                            "query": state.get("query") or None, "source": "results"})
    return out


def run_compare(items: list[dict]) -> tuple[dict, list]:
    """두 상품을 기존 1:1 비교(chat.answer 비교 모드, 숫자는 compare.py 검증)로 추천 → (Gemini에 줄 결과, 화면 effects)."""
    cmp = chat.clean_compare({"products": items})
    if cmp is None:
        return {"error": actions.NEED_TWO, "matched": len(items)}, []
    ans = compare_answer("두 상품을 비교해서 더 나은 하나를 추천해줘", cmp, [], True)
    names = [i["name"] for i in items]
    pick = ans.get("pick_id")
    resp = {"ok": True, "compared": names, "recommended": names[pick] if isinstance(pick, int) and 0 <= pick < 2 else None,
            "analysis": ans["answer"],
            "note": "화면에 AI 비교 창이 열렸어요. analysis(숫자 확인된 비교)를 보고 추천 상품과 이유를 2~3문장으로 전해 주세요."}
    shown = [{k: i[k] for k in ("key", "brand", "model", "price", "currency", "store", "rating", "review_count", "pros",
                                "cons", "query", "source")} for i in items]
    effect = {"type": "compare", "keys": [i["key"] for i in items], "items": shown, "answer": ans["answer"], "pick_id": pick}
    return resp, [effect]


def compare_favorites(state, condition: str) -> tuple[dict | None, list, dict | None, list]:
    """→ (결과, effects, 기다릴 일, 후보). 맞는 상품이 정확히 2개면 바로 비교, 3개 이상이면 2개 고르는 선택 버튼(choose),
    1개 이하면 "비교할 상품이 부족해요"."""
    items = compare_items(state, condition)
    if len(items) < 2:
        return {"error": actions.NEED_TWO, "matched": len(items)}, [], None, items
    if len(items) == 2:
        resp, effects = run_compare(items)
        return {**resp, "matched": 2}, effects, None, items
    shown = items[:MAX_CHOICES]
    pending = {"kind": "choose", "pick": 2, "matched": len(items), "multi": True, "min_select": 2, "max_select": 2,
               "submit_label": "비교하기",
               "call": {"name": "compare_favorites", "args": {"condition": condition}},
               "question": f"“{condition or '찜한 상품'}”에 맞는 상품이 {len(items)}개예요. 비교할 상품을 골라 주세요."
                           + select_hint(2, 2),
               "options": [{"key": i["key"], "name": i["name"], "price": i["price"], "currency": i["currency"],
                            "source": i["source"]} for i in shown],
               "choices": [{"id": i["key"], "label": i["name"]} for i in shown], "selected": []}
    return None, [], pending, shown


# ---------- 여러 개 선택 (ask_user min/max_select, compare 후보 2개) ----------

def select_hint(mn: int, mx: int) -> str:
    return f" ({mn}개 선택)" if mn == mx else f" ({mn}~{mx}개 선택)"


def ask_pending(call: dict) -> dict:
    """ask_user → 화면이 기다릴 일. 여러 개 선택이면 질문 끝에 "(2개 선택)", 선택지는 choices[{id, label}]."""
    args = call["args"]
    mn, mx = args.get("min_select", 1), args.get("max_select", 1)
    pending = {"kind": "ask", "call": call}
    if mx > 1:
        opts = args.get("options") or []
        pending.update(multi=True, min_select=mn, max_select=mx, submit_label="선택 완료", selected=[],
                       question=args["question"] + select_hint(mn, mx),
                       choices=[{"id": o, "label": o} for o in opts])
    return pending


def check_selection(pending: dict, response) -> tuple[list, str | None]:
    """화면이 보낸 selected(선택지 id 목록) 검사 → (고른 것, None) 또는 (맞는 것만 남긴 목록, 다시 물을 사유)."""
    ids = [c["id"] for c in pending.get("choices") or []]
    raw = response.get("selected") if isinstance(response, dict) else None
    picked = []
    for x in raw if isinstance(raw, list) else []:
        if x in ids and x not in picked:
            picked.append(x)
    mn, mx = pending["min_select"], pending["max_select"]
    if mn <= len(picked) <= mx:
        return picked, None
    want = f"{mn}개" if mn == mx else f"{mn}~{mx}개"
    return picked[:mx], f"{want}를 골라 주세요 (지금 {len(picked)}개)."


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


def _log(fmt: str, *args) -> None:
    """원인 확인용 Render 로그 (토큰·키·이메일은 가림, 길면 자름)."""
    try:
        msg = fmt % tuple(json.dumps(a, ensure_ascii=False, default=str) if isinstance(a, (dict, list)) else a for a in args)
    except Exception:
        return
    msg = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "***@***", claude_errors.mask(msg))
    logger.info("⚡ %s", msg[:1500])


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
        _log("agent #%d → 최종 답 (%d자)", upd["step_count"], len(out["text"]))
        return {**upd, "final": out["text"], "call": None}
    _log("agent #%d → functionCall %s %s", upd["step_count"], out["name"], out["args"])
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
    if call["name"] == "ask_user":
        pending = ask_pending(call)
    elif call["name"] in CLIENT_TOOLS and call["name"] != "add_favorite":
        pending = {"kind": "client", "call": call}
    return {"call": call, "pending": pending, "trail": ["validate"]}


def server_tool(state: State) -> dict:
    call = state["call"]
    name, args = call["name"], call["args"]
    effects = []
    if name == "find_in_results":
        resp = find_in_results(state, args)
        _log("find_in_results %s → %s", args, [(c["number"], c["name"], c["price"]) for c in resp["candidates"]])
    elif name == "get_favorites":
        resp = get_favorites(state)
    elif name == "compare_favorites":
        resp, effects, pending, items = compare_favorites(state, args.get("condition") or "")
        _log("compare_favorites %s → %s", args, [(i["name"], i["source"]) for i in items])
        if pending:   # 3개 이상: 화면에서 2개 고르기 (wait_client)
            return {"call": {**call, "items": items}, "pending": pending, "trail": ["server_tool"]}
    else:   # add_favorite: 대상 찾기 + 중복 찜 확인 → 승인으로
        product, err = resolve_add(state, args)
        if err:
            resp = {"error": err}
        elif _wished(state, product["key"]):   # 고른 상품 ID로만 판단
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
    pending = state["pending"]
    value = interrupt(pending)
    v = value if isinstance(value, dict) else {}
    picked = None
    while pending.get("multi") and not v.get("cancelled"):
        # 여러 개 선택: 개수가 안 맞으면 같은 선택 버튼을 다시 (고른 것은 선택된 채로). 재실행돼도 interrupt 순서대로 같은 값
        picked, why = check_selection(pending, v.get("response"))
        if why is None:
            break
        value = interrupt({**pending, "selected": picked, "error": why, "question": pending["question"]})
        v = value if isinstance(value, dict) else {}
    if v.get("cancelled"):   # ask_user 대기 중 새 명령 등: 이 명령은 끝
        return {"final": "", "cancelled": True, "call": None, "pending": None, "trail": ["wait_client"]}
    if pending.get("kind") == "choose":   # 비교할 2개를 고름 → 여기서 바로 비교
        items = {i["key"]: i for i in state["call"].get("items") or []}
        resp, effects = run_compare([items[k] for k in picked])
        return {"messages": _respond(state, "compare_favorites", resp), "effects": effects, "call": None,
                "pending": None, "trail": ["wait_client"]}
    if pending.get("multi"):   # ask_user 여러 개 선택 → answers
        return {"messages": _respond(state, "ask_user", {"answers": picked}), "call": None, "pending": None,
                "trail": ["wait_client"]}
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
    call = state.get("call")
    if not call:
        return "agent"
    return "approve" if call["name"] == "add_favorite" else "wait_client"   # 찜은 승인, 비교 후보 3개 이상은 고르기


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
    g.add_conditional_edges("server_tool", after_server_tool, ["approve", "wait_client", "agent"])
    g.add_conditional_edges("approve", after_approve, ["wait_client", END])
    g.add_conditional_edges("wait_client", after_wait, ["agent", END])
    return g.compile(checkpointer=checkpointer or MemorySaver())


saver = MemorySaver()
graph = build(saver)


def run(inp, thread_id: str) -> dict:
    """새 명령(초기 State) 또는 Command(resume=…)로 실행 → 화면에 줄 결과.
    {"type": "interrupt", "pending", ...} (화면 실행·승인 대기) 또는 {"type": "text", "text", ...} (끝)."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    if not isinstance(inp, Command):   # 새 명령: 새 thread에서 빈 상태로 시작 (이전 candidates·pending이 남지 않게)
        forget(thread_id)
        inp = {**inp, "pending": None, "call": None, "final": None, "cancelled": False}
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
