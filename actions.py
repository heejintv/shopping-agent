"""챗봇 "문장으로 실행" (함수 호출 실습).

Gemini 함수 호출로 문장에 맞는 도구 하나와 입력값만 고른다. 실제 실행(찜·검색)은 하지 않고,
고른 결과를 {"tool", "args"} JSON으로 돌려주면 화면(index.html)이 기존 찜 버튼·검색 함수를 그대로 부른다.
"""

from gemini import EngineError, pick_tool

MAX_SENTENCE = 200
MAX_NAMES = 10      # 프롬프트에 넣는 현재 검색 결과 상품명 수
MAX_ARG = 100       # 상품명·검색어 글자 수
TIMEOUT = 30        # 초
FAIL_MESSAGE = "문장으로 실행하지 못했어요. 버튼으로 해 주세요."
UNKNOWN_MESSAGE = "어떤 동작인지 알아듣지 못했어요. 버튼으로 해 주세요."

_NAME = {"type": "object", "properties": {"name": {"type": "string", "description": "상품명 (브랜드 + 모델)"}},
         "required": ["name"]}
TOOLS = {
    "add_favorite": ("현재 검색 결과의 상품 하나를 찜 목록에 추가한다.", _NAME),
    "remove_favorite": ("찜 목록에서 상품 하나를 뺀다.", _NAME),
    "show_favorites": ("찜 목록을 보여 준다.", None),
    "search": ("새 상품 검색을 한다.",
               {"type": "object", "properties": {"query": {"type": "string", "description": "검색어 (상품 종류)"}},
                "required": ["query"]}),
}
ARG_NAMES = {"add_favorite": "name", "remove_favorite": "name", "search": "query"}

SYSTEM = """너는 쇼핑 사이트의 명령 해석기야. 사용자 문장을 보고 알맞은 함수 하나만 골라 호출해.
- 상품명은 [현재 검색 결과]에 있는 이름 중 가장 가까운 것을 그대로 써. 목록에 없으면 사용자가 말한 이름을 그대로 써.
- 찜/관심/저장 = add_favorite, 찜 취소/빼기/삭제 = remove_favorite, 찜 목록 보기 = show_favorites, 찾아줘/검색 = search
- 해당하는 함수가 없으면 함수를 호출하지 마."""


def clean_names(names) -> list[str]:
    if not isinstance(names, list):
        return []
    out = [" ".join(str(n).split())[:MAX_ARG] for n in names if isinstance(n, str) and n.strip()]
    return out[:MAX_NAMES]


def build_prompt(sentence: str, names: list[str]) -> str:
    listing = "\n".join(f"- {n}" for n in names) or "(없음)"
    return f"[현재 검색 결과]\n{listing}\n\n[사용자 문장]\n{sentence}"


def validate(name: str | None, args) -> dict:
    """모델이 고른 함수를 검사해서 {"tool", "args"}로. 모르는 함수·빈 입력값이면 tool=None."""
    if name not in TOOLS:
        return {"tool": None, "args": {}}
    key = ARG_NAMES.get(name)
    if not key:
        return {"tool": name, "args": {}}
    value = " ".join(str((args or {}).get(key) or "").split())[:MAX_ARG]
    if not value:
        return {"tool": None, "args": {}}
    return {"tool": name, "args": {key: value}}


def choose(sentence: str, names: list[str]) -> dict:
    """문장 → {"tool", "args", "tokens"}. Gemini 실패는 FAIL_MESSAGE로 바꾼 EngineError."""
    usage = {}
    try:
        name, args = pick_tool(SYSTEM, build_prompt(sentence, names), TOOLS, TIMEOUT, usage)
    except EngineError as e:
        raise EngineError(FAIL_MESSAGE, e.code) from e
    out = validate(name, args)
    out["tokens"] = usage.get("tokens")
    return out
