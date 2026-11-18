"""1:1 비교: 서버가 두 상품의 숫자를 직접 비교하고, AI 답변의 이유를 검사한다.

1. extract_facts(): 가격·별점·리뷰 수(상품 값)와 무게·보증·폴링·DPI·사용 시간(장단점 문장 속 숫자)을 뽑는다.
2. compare_facts(): "상품 0이 확실히 나은 점" / "상품 1이 확실히 나은 점" / "같은 점"으로 나눈다.
3. fix_reasons(): 답변의 "이유:" 줄에 추천 상품 기준 "같은 점"이나 "상대가 나은 점"이 들어 있으면
   그 줄을 숫자 없는 일반적인 장점 문장으로 바꾼다.
"""

import re

# 항목: 이름, 단위, 좋은 방향(low=낮을수록 좋음), 상품 문장에서 숫자 뽑는 식, 답변 문장에서 언급을 찾는 식
NUM = r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)"
METRICS = {
    "price": {"label": "가격", "unit": "원", "subj": "가격이", "ro": "으로", "better": "low",
              "mention": re.compile(r"저렴|싸요|싸고|싸서|싸다|비싸|가격|\d[\d,]*\s*원")},
    "rating": {"label": "별점", "unit": "점", "subj": "별점이", "ro": "으로", "better": "high",
               "mention": re.compile(r"별점|평점|\d\.\d+\s*점")},
    "review_count": {"label": "리뷰 수", "unit": "개", "subj": "리뷰 수가", "ro": "로", "better": "high",
                     "mention": re.compile(r"리뷰|후기|구매평|평가\s*수")},
    "weight": {"label": "무게", "unit": "g", "subj": "무게가", "ro": "으로", "better": "low",
               "extract": re.compile(NUM + r"\s*(kg|g|그램)(?![a-zA-Z])", re.I),
               "mention": re.compile(r"\d+(?:\.\d+)?\s*(?:kg|g|그램)(?![a-zA-Z])|가볍|가벼|무게|경량|무거", re.I)},
    "warranty": {"label": "보증 기간", "unit": "년", "subj": "보증 기간이", "ro": "으로", "better": "high",
                 "extract": re.compile(r"(?:보증|A/?S)\s*(?:기간)?\s*(\d+)\s*년|" + NUM + r"\s*년\s*(?:무상\s*)?(?:보증|A/?S)", re.I),
                 "mention": re.compile(r"보증|A/?S|\d+\s*년", re.I)},
    "polling": {"label": "폴링 속도", "unit": "Hz", "subj": "폴링 속도가", "ro": "로", "better": "high",
                "extract": re.compile(NUM + r"\s*Hz"),
                "mention": re.compile(r"\d+\s*Hz|폴링", re.I)},
    "dpi": {"label": "DPI", "unit": "DPI", "subj": "DPI가", "ro": "로", "better": "high",
            "extract": re.compile(NUM + r"\s*DPI", re.I),
            "mention": re.compile(r"DPI", re.I)},
    "battery": {"label": "사용 시간", "unit": "시간", "subj": "사용 시간이", "ro": "으로", "better": "high",
                "extract": re.compile(NUM + r"\s*시간"),
                "mention": re.compile(r"\d+\s*시간|사용\s*시간|배터리\s*(?:수명|시간)")},
}

GENERIC = [   # 숫자·비교 없는 일반적인 장점 문장 (이유 줄을 바꿀 때 사용)
    "가격 차이에 비해 기본 성능은 충분해요",   # 추천 상품이 더 저렴할 때만
    "일상적인 용도로 쓰기에 무난해요",
    "필요한 기본 기능을 고루 갖췄어요",
]
REASON_LINE = re.compile(r"^(\s*이유\s*[:：]\s*)(.*)$")


def _to_num(s: str) -> float:
    return float(s.replace(",", ""))


def _fmt(v: float) -> str:
    return f"{int(v):,}" if float(v).is_integer() else f"{v:g}"


def extract_facts(p: dict) -> dict:
    """상품 하나에서 비교 가능한 숫자를 뽑는다. {항목: 값}"""
    facts = {}
    if isinstance(p.get("price"), (int, float)) and p["price"] > 0:
        facts["price"] = float(p["price"])
    if isinstance(p.get("rating"), (int, float)):
        facts["rating"] = float(p["rating"])
    if isinstance(p.get("review_count"), (int, float)):
        facts["review_count"] = float(p["review_count"])
    text = " ".join((p.get("pros") or []) + (p.get("cons") or []))
    for key, m in METRICS.items():
        rx = m.get("extract")
        if not rx:
            continue
        found = rx.search(text)
        if not found:
            continue
        nums = [g for g in found.groups() if g and re.match(r"[\d,.]+$", g)]
        if not nums:
            continue
        value = _to_num(nums[0])
        if key == "weight" and re.search(r"kg", found.group(0), re.I):
            value *= 1000
        facts[key] = value
    return facts


def _short(p: dict) -> str:
    name = re.sub(r"\([^)]*\)", " ", p.get("model") or "")
    return " ".join(name.split()) or "상품"


def compare_facts(products: list[dict]) -> dict:
    """두 상품을 비교한다. better[i] = 상품 i가 확실히 나은 점, same = 같은 점. 각 항목은 {"key", "text"}."""
    a, b = products
    fa, fb = extract_facts(a), extract_facts(b)
    same_currency = (a.get("currency") or "KRW") == (b.get("currency") or "KRW")
    better, same = [[], []], []
    for key, m in METRICS.items():
        if key not in fa or key not in fb or (key == "price" and not same_currency):
            continue
        va, vb = fa[key], fb[key]
        unit = m["unit"]
        if abs(va - vb) < 1e-9:
            same.append({"key": key, "text": f"{m['subj']} {_fmt(va)}{unit}{m['ro']} 같음"})
            continue
        win = 0 if (va < vb) == (m["better"] == "low") else 1
        w, l = (va, vb) if win == 0 else (vb, va)
        diff = abs(va - vb)
        if key == "price":
            text = f"가격이 {_fmt(diff)}원 저렴함 ({_fmt(w)}원 vs {_fmt(l)}원)"
        elif key == "weight":
            text = f"무게가 {_fmt(diff)}g 가벼움 ({_fmt(w)}g vs {_fmt(l)}g)"
        elif key == "warranty":
            text = f"보증 기간이 {_fmt(diff)}년 더 김 ({_fmt(w)}년 vs {_fmt(l)}년)"
        else:
            text = f"{m['subj']} 더 높음 ({_fmt(w)}{unit} vs {_fmt(l)}{unit})"
        better[win].append({"key": key, "text": text})
    return {"names": [_short(a), _short(b)], "better": better, "same": same}


def facts_prompt(cmp: dict) -> str:
    """프롬프트에 넣을 '서버가 직접 비교한 숫자' 부분."""
    def block(items):
        return "\n".join(f"- {x['text']}" for x in items) or "- (없음)"
    n = cmp["names"]
    return (
        "[서버가 직접 비교한 숫자] (이 목록이 정답이야. 여기 없는 숫자 비교는 하지 마)\n"
        f"상품 id 0 「{n[0]}」의 확실히 나은 점:\n{block(cmp['better'][0])}\n"
        f"상품 id 1 「{n[1]}」의 확실히 나은 점:\n{block(cmp['better'][1])}\n"
        f"같은 점 (이유에 쓰지 마):\n{block(cmp['same'])}\n"
    )


def fix_reasons(text: str, pick: int | None, cmp: dict) -> tuple[str, int]:
    """추천 상품 기준으로 '같은 점'이나 '상대가 나은 점'을 말하는 이유 줄을 일반 장점 문장으로 바꾼다.
    (고친 텍스트, 바꾼 줄 수)를 돌려준다."""
    if pick not in (0, 1):
        return text, 0
    banned = {x["key"] for x in cmp["same"]} | {x["key"] for x in cmp["better"][1 - pick]}
    if not banned:
        return text, 0
    pick_better = {x["key"] for x in cmp["better"][pick]}
    pool = [g for g in GENERIC if g != GENERIC[0] or "price" in pick_better]
    lines = text.split("\n")
    reasons = [i for i, line in enumerate(lines) if REASON_LINE.match(line)]
    fixed = 0
    for i in reasons:
        head, body = REASON_LINE.match(lines[i]).groups()
        mentioned = {k for k, m in METRICS.items() if m["mention"].search(body)}
        if not mentioned & banned:
            continue
        others = {REASON_LINE.match(lines[j]).group(2).strip() for j in reasons if j != i}
        sentence = next((g for g in pool if g not in others), pool[-1])
        lines[i] = head + sentence + ("." if body.rstrip().endswith(".") else "")   # 원래 줄의 마침표 유지
        fixed += 1
    return "\n".join(lines), fixed
