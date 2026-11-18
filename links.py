"""상품 링크 검증 (Claude 호출 없음).

각 상품 링크에 실제로 접속해서
  1) 정상 응답(200)인지
  2) 페이지 제목에 모델명이 들어 있는지
확인한다. 검색 결과 페이지는 상품 페이지로 치지 않는다.
검색 결과 같은 대체 링크로 바꾸지 않는다. 확인 안 되면 링크를 비운다.

결과: product["url"]           확인된 상품 상세 페이지 주소, 실패하면 None
      product["url_verified"]  True = 확인됨, False = 링크 확인 안 됨 (화면에서 버튼 숨김)
      product["url_rejected"]  검증에 실패한 주소 목록 (기록용, 실패한 경우만)

별점: 검증하면서 이미 연 상품 페이지에서 별점·리뷰 수도 읽는다 (AI 호출 없음).
      1순위 JSON-LD(schema.org aggregateRating), 2순위 다나와 페이지 안의 데이터·별점 영역.
      페이지 값이 있으면 AI 값보다 우선. 5점 만점으로 맞춤.
      product["rating_source"]  "page" = 상품 페이지에서 읽음, "ai" = AI가 준 값 (둘 다 없으면 없음)

기존 캐시 다시 검증: python links.py            (cache/와 demo/를 다시 검사해서 저장. 재검색은 하지 않음)
기존 캐시 별점만 채우기: python links.py ratings cache demo   (검증된 링크의 페이지에서 별점만 다시 읽음)
"""

import html
import ipaddress
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TIMEOUT = 8            # 초. 링크 하나당
MAX_BYTES = 400_000    # 제목만 보면 되므로 페이지 앞부분만 읽음
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
OLD_SEARCH_PREFIX = "https://search.danawa.com/dsearch.php"  # 예전 버전이 넣던 대체 링크


# ---------- 모델명 비교 ----------

def core_model(model: str) -> str:
    """'G PRO X SUPERLIGHT 2 (정품) (블랙)' → 'G PRO X SUPERLIGHT 2' (괄호 속 정품·색상 표시 제거)."""
    core = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", model)
    return " ".join(core.split()) or model.strip()


def tokens(text: str) -> list[str]:
    return re.findall(r"[0-9a-z가-힣]+", text.lower())


def title_has_model(title: str, model: str) -> bool:
    """모델명(괄호 부분 제외)의 단어가 제목에 모두 들어 있으면 True. 띄어쓰기·기호 차이는 무시."""
    want = tokens(core_model(model))
    flat = "".join(tokens(title))
    return bool(want) and all(t in flat for t in want)


def page_titles(page: str) -> str:
    """<title>과 og:title을 합친 문자열."""
    found = re.findall(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    found += re.findall(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)', page, re.I)
    found += re.findall(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:title', page, re.I)
    return html.unescape(" ".join(found))


# ---------- 안전한 접속 ----------
# 링크는 웹에서 온 값이라, 내 PC나 내부망 주소(localhost, 192.168.x.x 등)에는 접속하지 않는다.

def is_public_url(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except (ValueError, OSError):
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_url(newurl):
            raise urllib.error.URLError("내부 주소로의 리다이렉트 차단")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(SafeRedirect)


def fetch_page(url: str) -> str | None:
    """정상(200) HTML이면 내용 앞부분, 아니면 None (오류·차단·시간 초과 포함).
    네트워크 오류·시간 초과·429·5xx 같은 일시적 실패는 잠깐 쉬고 한 번 더 시도한다."""
    if not is_public_url(url):
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "ko-KR,ko;q=0.9"})
    for attempt in range(2):
        if attempt:
            time.sleep(1.5)
        try:
            with _opener.open(req, timeout=TIMEOUT) as r:
                if r.status != 200 or "html" not in (r.headers.get("Content-Type") or "html"):
                    return None
                raw = r.read(MAX_BYTES)
                charset = r.headers.get_content_charset()
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:  # 404·403 등은 다시 해도 같음
                return None
        except Exception:
            pass  # 연결 오류·시간 초과 → 한 번 더
    else:
        return None
    for enc in filter(None, [charset, "utf-8", "cp949"]):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "ignore")


def looks_like_search_page(url: str) -> bool:
    """검색 결과·목록 페이지는 상품 상세 페이지로 치지 않는다."""
    u = urllib.parse.urlsplit(url)
    return u.hostname is not None and (u.hostname.startswith("search.") or "search" in u.path.lower())


def check_page(url: str, model: str) -> str | None:
    """상품 상세 페이지가 맞으면 페이지 내용, 아니면 None."""
    if not url or looks_like_search_page(url):
        return None
    page = fetch_page(url)
    return page if page is not None and title_has_model(page_titles(page), model) else None


def check_url(url: str, model: str) -> bool:
    return check_page(url, model) is not None


# ---------- 별점·리뷰 수 읽기 ----------

def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", v)
        return float(m.group(0).replace(",", "")) if m else None
    return None


def to_five(value, best=None) -> float | None:
    """별점을 5점 만점으로. 만점(bestRating)을 알면 그걸로, 모르면 값 크기로 10점·100점 만점을 판단."""
    v = _num(value)
    if v is None or v <= 0:
        return None
    b = _num(best)
    if b and b > 0:
        v = v / b * 5
    elif v > 10:
        v = v / 20        # 100점 만점
    elif v > 5:
        v = v / 2         # 10점 만점
    return round(v, 1) if 0 < v <= 5 else None


def _find_aggregate(node):
    """JSON-LD 안에서 aggregateRating 객체를 찾는다 (@graph, 목록 안까지)."""
    if isinstance(node, dict):
        agg = node.get("aggregateRating")
        if isinstance(agg, dict):
            return agg
        if str(node.get("@type", "")).lower() == "aggregaterating":
            return node
        for v in node.values():
            found = _find_aggregate(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_aggregate(v)
            if found:
                return found
    return None


LD_JSON = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)


def _from_json_ld(page: str):
    for raw in LD_JSON.findall(page):
        try:
            data = json.loads(html.unescape(raw.strip()))
        except ValueError:
            continue
        agg = _find_aggregate(data)
        if agg:
            rating = to_five(agg.get("ratingValue"), agg.get("bestRating"))
            count = _num(agg.get("reviewCount") if agg.get("reviewCount") is not None else agg.get("ratingCount"))
            if rating is not None:
                return rating, int(count) if count is not None else None
    return None


def _from_danawa(page: str):
    """다나와: 페이지 안 데이터(이스케이프된 JSON)의 ratingValue/reviewCount, 또는 별점 막대·상품의견 수."""
    flat = page.replace("\\", "")
    m = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)', flat)
    if m:
        rating = to_five(m.group(1))
        c = re.search(r'"(?:reviewCount|ratingCount)"\s*:\s*"?(\d+)', flat[m.start():m.start() + 300])
        if rating is not None:
            return rating, int(c.group(1)) if c else None
    m = re.search(r'star_mask[^>]*width\s*:\s*([\d.]+)%', page)          # 별 5개 막대의 채운 비율
    if m:
        rating = to_five(float(m.group(1)) / 20)
        c = re.search(r"상품의견\s*(?:<[^>]+>\s*)*\(?\s*([\d,]+)\s*\)?", page)
        if rating is not None:
            return rating, int(c.group(1).replace(",", "")) if c else None
    return None


def extract_rating(page: str, url: str = "") -> tuple[float | None, int | None]:
    """상품 페이지에서 (5점 만점 별점, 리뷰 수). 못 찾으면 (None, None)."""
    if not page:
        return None, None
    found = _from_json_ld(page)
    if not found and "danawa.com" in (urllib.parse.urlsplit(url).hostname or ""):
        found = _from_danawa(page)
    if not found:
        return None, None
    rating, count = found
    if count == 0:              # 리뷰가 하나도 없으면 별점도 의미 없음
        return None, None
    return rating, count


def apply_rating(p: dict, page: str | None) -> bool:
    """페이지에서 읽은 별점이 있으면 AI 값 대신 쓰고 rating_source="page". 없으면 AI 값을 그대로 두고 "ai"."""
    rating, count = extract_rating(page or "", p.get("url") or "")
    if rating is not None:
        p["rating"] = rating
        if count is not None:
            p["review_count"] = count
        p["rating_source"] = "page"
        return True
    if isinstance(p.get("rating"), (int, float)) and p.get("rating_source") != "page":
        p["rating_source"] = "ai"
    return False


# ---------- 검증 ----------

def verify_product(p: dict) -> bool:
    """p["url"]을 검증해서 p를 고친다. 확인되면 True. 연 페이지에서 별점·리뷰 수도 읽는다."""
    url = p.get("url")
    page = check_page(url, p.get("model") or "")
    if page is not None:
        p["url_verified"] = True
        apply_rating(p, page)
        return True
    apply_rating(p, None)   # 페이지 값이 없으면 AI 값 유지 (rating_source="ai")
    if url:
        rejected = p.setdefault("url_rejected", [])
        if url not in rejected:
            rejected.append(url)
    p["url"], p["url_verified"] = None, False
    return False


def verify_links(products: list[dict], on_progress=None) -> list[dict]:
    """상품들의 링크를 동시에 검증하고, 실패한 상품 목록을 돌려준다.
    on_progress(끝난 개수, 전체 개수)를 주면 하나 끝날 때마다 알려준다 (진행 상황 표시용)."""
    if not products:
        return []
    ok = [False] * len(products)
    with ThreadPoolExecutor(max_workers=4) as pool:  # 한 사이트에 너무 몰아서 접속하지 않도록
        futures = {pool.submit(verify_product, p): i for i, p in enumerate(products)}
        for done, f in enumerate(as_completed(futures), 1):
            ok[futures[f]] = f.result()
            if on_progress:
                on_progress(done, len(products))
    return [p for p, good in zip(products, ok) if not good]


def migrate_old(p: dict) -> None:
    """예전 버전(다나와 검색 링크로 대체)의 저장 형식을 원래 링크 기준으로 되돌린다."""
    if "url_original" in p:
        p["url"] = p.pop("url_original")
    elif (p.get("url") or "").startswith(OLD_SEARCH_PREFIX) and p.get("url_verified") is False:
        p["url"] = None


def reverify_dir(folder: Path) -> None:
    for f in sorted(folder.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "products" not in data:
            continue
        for p in data["products"]:
            migrate_old(p)
        verify_links(data["products"])
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        cond = f" · {data['condition']}" if data.get("condition") else ""
        print(f"{folder.name}/{f.name}  {data.get('query')}{cond}")
        for p in data["products"]:
            mark = "✅" if p["url_verified"] else "❌ 링크 확인 안 됨"
            print(f"   {mark}  {p['brand']} {core_model(p['model'])}  {p['url'] or ''}")


def _refill_product(p: dict) -> bool:
    page = fetch_page(p["url"]) if p.get("url_verified") and p.get("url") else None
    return apply_rating(p, page)


def refill_ratings_dir(folder: Path) -> tuple[int, int, int]:
    """검증된 링크의 페이지에서 별점만 다시 읽어 채운다 (링크·검증 결과는 그대로). (상품 수, 전 별점 수, 후 별점 수)"""
    total = before = after = 0
    for f in sorted(folder.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            continue
        products = data["products"]
        has = lambda p: isinstance(p.get("rating"), (int, float))  # noqa: E731
        b = sum(map(has, products))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(_refill_product, products))
        a = sum(map(has, products))
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        total, before, after = total + len(products), before + b, after + a
        cond = f" · {data['condition']}" if data.get("condition") else ""
        print(f"{folder.name}/{f.name}  {data.get('query')}{cond}: 별점 {b} → {a} / {len(products)}")
    return total, before, after


if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    args = sys.argv[1:]
    if args[:1] == ["ratings"]:
        for name in args[1:] or ["cache", "demo"]:
            if (base / name).is_dir():
                t, b, a = refill_ratings_dir(base / name)
                print(f"== {name}: 상품 {t}개 중 별점 {b}개 → {a}개")
    else:
        for name in args or ["cache", "demo"]:
            if (base / name).is_dir():
                reverify_dir(base / name)
