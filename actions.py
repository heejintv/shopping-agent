"""챗봇 "문장으로 실행" (ReAct 실습: 계획은 Gemini, 실행·관찰은 화면).

Gemini를 한 번만 불러서 문장을 최대 3단계 계획 JSON으로 바꾼다. 예: "에어팟 검색해서 제일 싼 거 찜해줘"
→ [search(에어팟), pick(cheapest), add_favorite]. 서버는 계획을 검사해서 돌려줄 뿐 실행하지 않고,
화면(index.html)이 기존 검색·찜 함수로 한 단계씩 실행한다 (pick은 Gemini 없이 결과 데이터로 계산).
"""

from gemini import EngineError, plan_json

MAX_SENTENCE = 200
MAX_NAMES = 10      # 프롬프트에 넣는 현재 검색 결과 상품명 수
MAX_ARG = 100       # 상품명·검색어 글자 수
MAX_STEPS = 3
TIMEOUT = 30        # 초
FAIL_MESSAGE = "문장으로 실행하지 못했어요. 버튼으로 해 주세요."
UNKNOWN_MESSAGE = "어떤 동작인지 알아듣지 못했어요. 버튼으로 해 주세요."

TOOLS = ("search", "pick", "add_favorite", "remove_favorite", "show_favorites")
PICK_BY = ("cheapest", "top_rated", "first", "brand", "index")

SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "maxItems": MAX_STEPS, "items": {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": list(TOOLS)},
            "query": {"type": "string", "description": "search의 검색어"},
            "by": {"type": "string", "enum": list(PICK_BY), "description": "pick 기준"},
            "value": {"type": "string", "description": "pick 기준이 brand면 브랜드 이름, index면 번호(1부터)"},
            "name": {"type": "string", "description": "add_favorite·remove_favorite의 상품명. 바로 앞 pick으로 고른 상품이면 비움"},
        },
        "required": ["tool"],
    }}},
    "required": ["steps"],
}

SYSTEM = f"""너는 쇼핑 사이트의 명령 계획기야. 사용자 문장을 최대 {MAX_STEPS}단계 계획 JSON으로 바꿔. 실행은 하지 마.
도구:
- search(query): 새 상품 검색
- pick(by, value): 지금 검색 결과에서 상품 하나 고르기. by = cheapest(최저가) | top_rated(평점 최고) | first(첫 번째)
  | brand(value=브랜드 이름) | index(value=번호, 1부터)
- add_favorite(name): 찜하기. 바로 앞 단계 pick으로 고른 상품이면 name을 비워
- remove_favorite(name): 찜 빼기. pick 뒤면 name을 비워
- show_favorites(): 찜 목록 보기
규칙:
- [현재 검색 결과]에 있는 상품을 이름으로 말하면 그 이름을 name에 그대로 써 (pick 필요 없음)
- "제일 싼/최저가/가장 저렴" = cheapest, "평점 높은/제일 좋은" = top_rated, "첫 번째" = first, "N번째" = index
- 다른 상품을 검색해야 하면 search를 먼저 넣어
- 해당하는 동작이 없으면 steps를 빈 배열로
예: "에어팟 검색해서 제일 싼 거 찜해줘" → {{"steps":[{{"tool":"search","query":"에어팟"}},{{"tool":"pick","by":"cheapest"}},{{"tool":"add_favorite"}}]}}"""


def clean_names(names) -> list[str]:
    if not isinstance(names, list):
        return []
    out = [" ".join(str(n).split())[:MAX_ARG] for n in names if isinstance(n, str) and n.strip()]
    return out[:MAX_NAMES]


def build_prompt(sentence: str, names: list[str]) -> str:
    listing = "\n".join(f"- {n}" for n in names) or "(없음)"
    return f"[현재 검색 결과]\n{listing}\n\n[사용자 문장]\n{sentence}"


def _text(v) -> str:
    return " ".join(str(v or "").split())[:MAX_ARG]


def _step(raw, picked: bool) -> dict | None:
    """단계 하나 검사 → {"tool", "args"}. 잘못됐으면 None."""
    if not isinstance(raw, dict):
        return None
    tool = raw.get("tool")
    if tool == "search":
        q = _text(raw.get("query"))
        return {"tool": tool, "args": {"query": q}} if q else None
    if tool == "pick":
        by = raw.get("by")
        if by == "brand":
            v = _text(raw.get("value"))
            return {"tool": tool, "args": {"by": by, "value": v}} if v else None
        if by == "index":
            try:
                n = int(str(raw.get("value")).strip())
            except ValueError:
                return None
            return {"tool": tool, "args": {"by": by, "value": n}} if 1 <= n <= 50 else None
        return {"tool": tool, "args": {"by": by}} if by in PICK_BY else None
    if tool in ("add_favorite", "remove_favorite"):
        name = _text(raw.get("name"))
        if name:
            return {"tool": tool, "args": {"name": name}}
        return {"tool": tool, "args": {}} if picked else None   # 이름이 없으면 앞에서 고른 상품
    if tool == "show_favorites":
        return {"tool": tool, "args": {}}
    return None


def validate(plan) -> list[dict]:
    """모델이 만든 계획 검사. 하나라도 잘못된 단계가 있으면 빈 계획(실행 안 함). 최대 MAX_STEPS단계."""
    raw = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(raw, list) or len(raw) > MAX_STEPS:
        return []
    steps, picked = [], False
    for r in raw:
        s = _step(r, picked)
        if s is None:
            return []
        if s["tool"] == "pick":
            picked = True
        elif s["tool"] == "search":
            picked = False   # 새 검색 뒤에는 다시 골라야 함
        steps.append(s)
    return steps


def choose(sentence: str, names: list[str]) -> dict:
    """문장 → {"steps", "tokens"}. Gemini는 한 번만. 실패는 FAIL_MESSAGE로 바꾼 EngineError."""
    usage = {}
    try:
        plan = plan_json(SYSTEM, build_prompt(sentence, names), SCHEMA, TIMEOUT, usage)
    except EngineError as e:
        raise EngineError(FAIL_MESSAGE, e.code) from e
    return {"steps": validate(plan), "tokens": usage.get("tokens")}
