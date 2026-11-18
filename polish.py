"""추천 이유 문장의 끝맺음을 "~해요/~예요"로 다듬는다.

AI 지시문으로도 요청하지만, 예전 캐시나 지시를 놓친 답에 "~있음", "~함", "~다", "~합니다" 같은
끝맺음이 남아 있어서 화면에 보내기 전에 서버가 한 번 더 정리한다.
모르는 형태는 바꾸지 않는다 (틀리게 고치는 것보다 그대로 두는 게 나음).

    polite("가장 싸게 살 수 있음")      → "가장 싸게 살 수 있어요"
    polite("휴대성이 우수함.")          → "휴대성이 우수해요."
    polite("가성비가 좋은 제품임")      → "가성비가 좋은 제품이에요"

기존 파일 다듬기: python polish.py demo   (demo/의 검색 결과 문구만 고쳐서 저장)
"""

import json
import re
import sys
from pathlib import Path

BASE = 0xAC00
A_VOWELS = {0, 2, 8}   # ㅏ ㅑ ㅗ → "아요", 나머지 → "어요"


def _is_hangul(c: str) -> bool:
    return len(c) == 1 and BASE <= ord(c) <= 0xD7A3


def _jong(c: str) -> int:       # 받침 번호 (0 = 받침 없음)
    return (ord(c) - BASE) % 28


def _jung(c: str) -> int:       # 모음 번호
    return (ord(c) - BASE) // 28 % 21


def _drop_jong(c: str) -> str:  # 받침을 뺀 글자
    return chr(ord(c) - _jong(c))


# 숫자·영문으로 끝날 때 읽는 소리의 받침 여부 (1 일, 3 삼, 6 육, 7 칠, 8 팔, 0 영 / L 엘, M 엠, N 엔, R 알)
JONG_DIGITS = set("013678")
JONG_LETTERS = set("LMNRlmnr")
UNIT_JONG = ("kg", "g", "KG", "G")   # 그램 → 받침 있음 (GB 등은 아래 글자 규칙)


def _copula(text: str) -> str:
    """명사 뒤 '이다' → 받침 있으면 '이에요', 없으면 '예요'. 맨 뒤 괄호 설명은 건너뛰고 판단한다."""
    t = re.sub(r"(\s*\([^()]*\))+$", "", text).rstrip()
    if not t:
        return "예요"
    if t.endswith(("kg", "Kg", "KG")) or re.search(r"\dg$", t):
        return "이에요"
    c = t[-1]
    if _is_hangul(c):
        return "이에요" if _jong(c) else "예요"
    if c in JONG_DIGITS or c in JONG_LETTERS:
        return "이에요"
    return "예요"


# 받침 없는 줄기 + 어/아 → 줄인 모음 (치→쳐, 주→줘, 보→봐, 되→돼, 크→커)
CONTRACT = {0: 0, 4: 4, 8: 9, 13: 14, 20: 6, 18: 4, 1: 1, 5: 5, 11: 10, 6: 6}


def _conj(stem: str) -> str | None:
    """받침 없는 줄기 글자에 '어요/아요'를 줄여 붙인 글자 (없으면 None)."""
    if not _is_hangul(stem) or _jong(stem):
        return None
    code = ord(stem) - BASE
    cho, jung = code // 588, code // 28 % 21
    if jung not in CONTRACT:
        return None
    return chr(BASE + (cho * 21 + CONTRACT[jung]) * 28)


def _harmony(stem_end: str) -> str:
    return "아요" if _jung(stem_end) in A_VOWELS else "어요"


# 불규칙이라 따로 적어 둔 것들 (끝 글자 기준)
IRREGULAR = {
    "큼": "커요", "빠름": "빨라요", "다름": "달라요", "느림": "느려요", "예쁨": "예뻐요", "바쁨": "바빠요",
    "가벼움": "가벼워요", "무거움": "무거워요", "쉬움": "쉬워요", "어려움": "어려워요", "부드러움": "부드러워요",
    "귀여움": "귀여워요", "아쉬움": "아쉬워요", "즐거움": "즐거워요", "차가움": "차가워요", "뜨거움": "뜨거워요",
    "크다": "커요", "빠르다": "빨라요", "다르다": "달라요", "느리다": "느려요", "예쁘다": "예뻐요",
    "가볍다": "가벼워요", "무겁다": "무거워요", "쉽다": "쉬워요", "어렵다": "어려워요", "귀엽다": "귀여워요",
}


NOUN_END = ("게임", "프레임", "타임", "결함", "소음", "마음", "처음", "느낌", "포함")   # 명사 (음·함·임 규칙에서 제외)
HADA_NOUN = ("지원", "제공", "탑재", "추천", "가능", "우수", "저렴", "편리", "적합", "충분", "호환", "내장", "확인",
             "사용", "조절", "연결", "작동", "측정", "세척", "보관", "차단", "유지", "절약")
# 이 글자로 끝나면 문장이 덜 끝난 것(연결형·조사)으로 보고 그대로 둠
OPEN_END = set("고며는은을를에의와과게니랑든여만")   # (서·면·도로 끝나는 명사가 많아서 제외: 센서, 대화면, 별도)


def _polite_clause(s: str) -> str:
    """문장 하나(끝 문장부호 제외)의 끝맺음을 바꾼다."""
    t = s.rstrip()
    if not t or t.endswith(("요", "죠")):
        return s
    if t.endswith("포함") and not t.endswith("미포함"):   # "케이블 포함" → "케이블 포함돼요"
        return t + "돼요"
    if t.endswith("아님"):
        return t[:-2] + "아니에요"
    if re.search(r"미(지원|포함|탑재|제공|사용)$", t) or t.endswith("불가"):   # "블루투스 미지원" → "미지원이에요"
        return t + _copula(t)
    if t.endswith(("어감", "아감", "라감", "러감", "나감")):              # "케이스에 들어감" → "들어가요" (그립감·착용감은 명사)
        return t[:-1] + "가요"
    if t.endswith(HADA_NOUN):                             # "AI 2.0 지원" → "AI 2.0 지원해요"
        return t + "해요"
    if t.endswith(NOUN_END):
        return t + _copula(t)
    for k, v in IRREGULAR.items():
        if t.endswith(k):
            return t[: -len(k)] + v
    # ~니다
    if t.endswith("합니다"):
        return t[:-3] + "해요"
    if t.endswith("됩니다"):
        return t[:-3] + "돼요"
    if t.endswith("입니다") and len(t) > 3:
        return t[:-3] + _copula(t[:-3])
    if t.endswith("습니다") and len(t) > 3 and _is_hangul(t[-4]):
        return t[:-3] + _harmony(t[-4])
    if t.endswith("니다") and len(t) > 2 and _is_hangul(t[-3]) and _jong(t[-3]) == 17:   # 그칩니다 → 그쳐요
        c = _conj(_drop_jong(t[-3]))
        return t[:-3] + c + "요" if c else s
    # ~다
    if t.endswith(("한다", "하다")):
        return t[:-2] + "해요"
    if t.endswith(("된다", "되다")):
        return t[:-2] + "돼요"
    if t.endswith("이다") and len(t) > 2 and t[-3] not in "있없" and (not _is_hangul(t[-3]) or _jong(t[-3]) or t[-3] == ")"):
        return t[:-2] + _copula(t[:-2])                   # "평점 4.7(리뷰 21개)이다" → "…이에요"
    if t.endswith("다") and len(t) > 1 and not _is_hangul(t[-2]) and t[-2].isalnum():
        return t[:-1] + _copula(t[:-1])                   # "VRAM이 16GB다" → "16GB예요"
    if t.endswith("다") and len(t) > 1 and _is_hangul(t[-2]):
        stem = t[-2]
        if _jong(stem) == 4:                              # 판다·쓴다 같은 '-ㄴ다' 동사는 틀리기 쉬워 그대로
            return s
        if _jong(stem):                                   # 좋다 → 좋아요, 있다 → 있어요
            return t[:-1] + _harmony(stem)
        if _jung(stem) in (0, 4):                         # 싸다 → 싸요, 서다 → 서요
            return t[:-1] + "요"
        return s
    # ~ㅁ (명사형)
    last = t[-1]
    if not _is_hangul(last):
        return t + _copula(t) if last.isalnum() else s   # "가장 가벼운 1.5kg" → "1.5kg이에요"
    if t.endswith("함"):
        return t[:-1] + "해요"
    if t.endswith("됨"):
        return t[:-1] + "돼요"
    if t.endswith("임") and len(t) > 1:
        return t[:-1] + _copula(t[:-1])
    if t.endswith("음") and len(t) > 1 and _is_hangul(t[-2]) and _jong(t[-2]):   # 좋음 → 좋아요, 있음 → 있어요
        return t[:-1] + _harmony(t[-2])
    if last in OPEN_END:                                  # 연결형·조사로 끝난 덜 끝난 문장
        return s
    return t + _copula(t)                                 # 명사로 끝남: "초경량 핸디형" → "초경량 핸디형이에요"


SENTENCE = re.compile(r"\s*\S.*?(?:[.!?]+(?=\s|$)|$)", re.S)
TAIL_PAREN = re.compile(r"^(.*?)(\s*\([^()]*\))$")


def polite(text: str | None) -> str | None:
    """여러 문장이어도 문장마다 끝맺음을 다듬는다. 끝 문장부호와 맨 뒤 괄호 설명은 그대로 둔다."""
    if not isinstance(text, str) or not text.strip():
        return text
    out = []
    for m in SENTENCE.finditer(text):
        part = m.group(0)
        body = part.rstrip(".!? ")
        punct = part[len(body):]
        tail = ""
        pm = TAIL_PAREN.match(body)
        if pm and pm.group(1).strip():                     # "저렴함(최저가 기준)" → "저렴해요(최저가 기준)"
            body, tail = pm.group(1), pm.group(2)
        lead = body[: len(body) - len(body.lstrip())]
        out.append(lead + _polite_clause(body.strip()) + tail + punct)
    return "".join(out)


# 사실상 정보가 없는 장단점 항목 ("확인 안 됨", "리뷰 기반 단점은 확인하지 못함" 등) → 목록에서 빼고 화면엔 "–"
PLACEHOLDER = re.compile(r"(리뷰\s*기반\s*)?(장점|단점)?\s*[은는이가]?\s*(정보\s*)?"
                         r"(확인\s*안\s*됨|확인하지\s*못함|확인하지\s*못했어요|확인\s*못\s*함|확인되지\s*않음|없음|정보\s*없음|없어요)\.?")


def is_placeholder(text) -> bool:
    return isinstance(text, str) and bool(PLACEHOLDER.fullmatch(text.strip()))


def polish_result(data: dict) -> dict:
    """검색 결과의 추천 이유(가성비·품질), 조건 적합 이유, 장점·단점을 다듬는다. data를 고쳐서 돌려줌."""
    if not isinstance(data, dict):
        return data
    for k in ("value_pick", "quality_pick"):
        pick = data.get(k)
        if isinstance(pick, dict) and pick.get("reason"):
            pick["reason"] = polite(pick["reason"])
    for p in data.get("products") or []:
        if not isinstance(p, dict):
            continue
        if p.get("fit_reason"):
            p["fit_reason"] = polite(p["fit_reason"])
        for k in ("pros", "cons"):
            if isinstance(p.get(k), list):
                p[k] = [polite(t) for t in p[k] if isinstance(t, str) and t.strip() and not is_placeholder(t)]
    return data


if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    for name in sys.argv[1:]:
        for f in sorted((base / name).glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "products" not in data:
                continue
            before = json.dumps(data, ensure_ascii=False)
            polish_result(data)
            if json.dumps(data, ensure_ascii=False) != before:
                f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"다듬음: {name}/{f.name}  {data.get('query')}")
