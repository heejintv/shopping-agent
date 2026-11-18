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

차단 대응: 링크마다 결과(ok / http_403 / http_429 / captcha / timeout / title_mismatch …)를 로그와 RESULT_HOOK(모니터링)에 남긴다.
      Render(또는 LINK_BLOCK_MODE=1)에서 어떤 사이트가 차단으로 판단되면(최근 성공 없이 차단 신호 2번 이상),
      그 사이트 링크는 지우지 않고 product["url_status"] = "unverifiable"(확인 불가)로 남긴다.
      product["shop_url"](다른 쇼핑몰 상품 페이지)이 있으면 원래 링크가 안 될 때 그 주소로 확인한다.

기존 캐시 다시 검증: python links.py            (cache/와 demo/를 다시 검사해서 저장. 재검색은 하지 않음)
기존 캐시 별점만 채우기: python links.py ratings cache demo   (검증된 링크의 페이지에서 별점만 다시 읽음)
"""

import html
import ipaddress
import json
import logging
import os
import re
import threading
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger("pickit.links")
RESULT_HOOK = None        # app.py가 모니터링 기록 함수를 넣음: hook(사이트, 결과)
BLOCK_REASONS = {"http_401", "http_403", "http_429", "captcha", "timeout"}   # 차단으로 의심하는 결과
CAPTCHA_MARKS = ("captcha", "recaptcha", "hcaptcha", "cf-chl", "challenge-platform", "just a moment",
                 "access denied", "비정상적인 접근", "접근이 차단", "자동입력 방지", "보안문자")

TIMEOUT = 5            # 초. 링크 하나당 (느린·막힌 사이트에 오래 매달리지 않게)
WORKERS = 5            # 동시에 확인할 링크 수
BLOCK_MEMORY_SEC = 30 * 60   # 차단된 사이트를 기억하는 시간 (Render)
RETRY_REASONS = {"http_404", "http_410", "title_mismatch", "search_page"}   # 링크가 진짜 틀린 경우만 재검색
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


def fetch_page_info(url: str) -> tuple[str | None, str]:
    """(정상 HTML이면 내용 앞부분 아니면 None, 결과). 결과: ok / http_403 / http_429 / timeout / error / not_html / internal.
    네트워크 오류·시간 초과·429·5xx 같은 일시적 실패는 잠깐 쉬고 한 번 더 시도한다."""
    if not is_public_url(url):
        return None, "internal"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "ko-KR,ko;q=0.9"})
    reason = "error"
    for attempt in range(2):
        if attempt:
            time.sleep(1.5)
        try:
            with _opener.open(req, timeout=TIMEOUT) as r:
                if r.status != 200:
                    return None, f"http_{r.status}"
                if "html" not in (r.headers.get("Content-Type") or "html"):
                    return None, "not_html"
                raw = r.read(MAX_BYTES)
                charset = r.headers.get_content_charset()
            break
        except urllib.error.HTTPError as e:
            reason = f"http_{e.code}"
            if e.code < 500:      # 404·403·429 등은 다시 해도 같음 (429는 기다려도 차단이 풀리지 않는 경우가 많음)
                return None, reason
        except (TimeoutError, socket.timeout):
            return None, "timeout"   # 시간 초과는 다시 해도 대개 또 늦음
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                return None, "timeout"
            reason = "error"
        except Exception:
            reason = "error"   # 연결 오류 → 한 번 더
    else:
        return None, reason
    for enc in filter(None, [charset, "utf-8", "cp949"]):
        try:
            return raw.decode(enc), "ok"
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "ignore"), "ok"


def fetch_page(url: str) -> str | None:
    """정상(200) HTML이면 내용 앞부분, 아니면 None (오류·차단·시간 초과 포함)."""
    return fetch_page_info(url)[0]


def looks_like_captcha(page: str) -> bool:
    head = page[:30000].lower()
    return any(m in head for m in CAPTCHA_MARKS)


# ---------- 사이트별 차단 판단 ----------

def site_name(url_or_host: str) -> str:
    """prod.danawa.com → danawa, item.gmarket.co.kr → gmarket, www.11st.co.kr → 11st"""
    host = urllib.parse.urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host
    parts = [x for x in (host or "").lower().split(".") if x]
    while len(parts) > 1 and parts[-1] in ("kr", "com", "net", "co", "or", "org", "io", "shop", "jp"):
        parts.pop()
    return parts[-1] if parts else "unknown"


def block_mode() -> bool:
    """차단 대응은 Render(또는 LINK_BLOCK_MODE=1)에서만. 내 PC에서는 기존처럼 '검증 실패'로 처리."""
    return os.environ.get("RENDER", "").lower() == "true" or os.environ.get("LINK_BLOCK_MODE") == "1"


_recent: dict[str, deque] = {}   # 사이트 → 최근 결과 (ok / blocked / fail)
_recent_lock = threading.Lock()


def note_result(url: str, reason: str) -> None:
    site = site_name(url)
    kind = "ok" if reason == "ok" else "blocked" if reason in BLOCK_REASONS else "fail"
    with _recent_lock:
        _recent.setdefault(site, deque(maxlen=6)).append(kind)
    if kind != "ok":
        log.warning("링크 확인 %s: %s → %s", "차단 의심" if kind == "blocked" else "실패", url, reason)
    if RESULT_HOOK:
        try:
            RESULT_HOOK(site, reason)
        except Exception:   # 기록 실패가 검색에 영향을 주지 않게
            pass


_blocked_until: dict[str, float] = {}   # 사이트 → 이 시각까지 차단으로 기억 (Render)


def remember_block(site: str) -> None:
    with _recent_lock:
        _blocked_until[site] = time.time() + BLOCK_MEMORY_SEC
    log.warning("사이트 차단으로 판단: %s (%d분 동안 확인을 건너뜀)", site, BLOCK_MEMORY_SEC // 60)


def site_remembered_blocked(site: str) -> bool:
    with _recent_lock:
        until = _blocked_until.get(site, 0)
        if until and until < time.time():
            del _blocked_until[site]
            return False
    return until > 0


def site_blocked(url: str) -> bool:
    """최근 결과에 성공이 없고 차단 신호가 2번 이상이면 그 사이트는 차단된 것으로 본다."""
    with _recent_lock:
        recent = list(_recent.get(site_name(url), ()))
    return recent.count("blocked") >= 2 and "ok" not in recent


def looks_like_search_page(url: str) -> bool:
    """검색 결과·목록 페이지는 상품 상세 페이지로 치지 않는다."""
    u = urllib.parse.urlsplit(url)
    return u.hostname is not None and (u.hostname.startswith("search.") or "search" in u.path.lower())


def check_page_info(url: str, model: str) -> tuple[str | None, str]:
    """(상품 상세 페이지가 맞으면 페이지 내용 아니면 None, 결과)."""
    if not url:
        return None, "no_url"
    if looks_like_search_page(url):
        return None, "search_page"
    page, reason = fetch_page_info(url)
    if page is not None and not title_has_model(page_titles(page), model):
        page, reason = None, "captcha" if looks_like_captcha(page) else "title_mismatch"
    note_result(url, reason)
    return page, reason


def check_page(url: str, model: str) -> str | None:
    """상품 상세 페이지가 맞으면 페이지 내용, 아니면 None."""
    return check_page_info(url, model)[0]


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

def _reject(p: dict, url: str | None) -> None:
    if url:
        rejected = p.setdefault("url_rejected", [])
        if url not in rejected:
            rejected.append(url)


def _check(url: str | None, model: str, batch: dict | None) -> tuple[str | None, str]:
    """링크 하나 확인. Render에서는 같은 검색에서 이미 차단된 사이트나 30분 안에 차단된 사이트를 건너뛴다."""
    if url and block_mode():
        site = site_name(url)
        if site_remembered_blocked(site) or (batch is not None and site in batch["blocked"]):
            return None, "skipped_blocked"
    page, reason = check_page_info(url, model)
    if url and block_mode() and reason in BLOCK_REASONS:
        site = site_name(url)
        if batch is not None:
            batch["blocked"].add(site)              # 같은 검색 안에서는 이 사이트 나머지를 건너뜀
        if reason in ("http_403", "captcha") or site_blocked(url):
            if not site_remembered_blocked(site):
                remember_block(site)                # 다음 검색에서도 30분 동안 바로 '확인 불가'
    return page, reason


def verify_product(p: dict, finalize: bool = True, batch: dict | None = None) -> bool:
    """p["url"]을 검증해서 p를 고친다. 확인되면 True. 연 페이지에서 별점·리뷰 수도 읽는다
    (별점은 확인된 페이지에서만 → 차단된 사이트에서는 읽지 않음).
    원래 링크가 안 되고 p["shop_url"](다른 쇼핑몰)이 있으면 그 주소로 확인한다.
    finalize=False면 실패한 링크를 아직 지우지 않는다 (verify_links가 사이트 차단 여부를 보고 마무리)."""
    url, alt, model = p.get("url"), p.get("shop_url"), p.get("model") or ""
    p.pop("url_status", None)
    page, reason = _check(url, model, batch)
    if page is None and alt and alt != url:
        page2, _ = _check(alt, model, batch)
        if page2 is not None:
            _reject(p, url)
            p["url_original"] = url          # 원래 링크(예: 다나와)는 기록만
            url, page, reason = alt, page2, "ok"
            p["url"] = alt
    if page is not None:
        p["url_verified"] = True
        apply_rating(p, page)
        return True
    apply_rating(p, None)   # 페이지 값이 없으면 AI 값 유지 (rating_source="ai")
    p["url_verified"] = False
    p["_reason"] = reason
    if finalize:
        _finalize_failed(p)
    return False


def _finalize_failed(p: dict, batch: dict | None = None) -> None:
    """실패한 링크 마무리: Render에서 그 사이트가 차단된 것이면 링크를 남기고 '확인 불가', 아니면 지움."""
    reason = p.pop("_reason", None)
    url = p.get("url")
    blocked_site = url and (site_blocked(url) or site_remembered_blocked(site_name(url))
                            or (batch is not None and site_name(url) in batch["blocked"]))
    if url and block_mode() and (reason == "skipped_blocked" or (reason in BLOCK_REASONS and blocked_site)):
        p["url_status"] = "unverifiable"   # 화면: 회색 '상품 보기 (미확인)'
        return
    _reject(p, url)
    p["url"] = None


def verify_links(products: list[dict], on_progress=None) -> list[dict]:
    """상품들의 링크를 동시에(5개씩) 검증하고, 다시 찾을 가치가 있는 상품 목록을 돌려준다.
    재검색 대상은 링크가 진짜 틀린 경우(404, 다른 상품·검색 페이지)만. 차단·시간 초과·연결 오류는 다시 찾아도 같아서 뺀다.
    on_progress(끝난 개수, 전체 개수)를 주면 하나 끝날 때마다 알려준다 (진행 상황 표시용)."""
    if not products:
        return []
    batch = {"blocked": set()}   # 이번 검색에서 차단된 사이트
    ok = [False] * len(products)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(verify_product, p, False, batch): i for i, p in enumerate(products)}
        for done, f in enumerate(as_completed(futures), 1):
            ok[futures[f]] = f.result()
            if on_progress:
                on_progress(done, len(products))
    # 모두 확인한 뒤에 사이트별 차단 여부를 보고 실패한 링크를 마무리
    retry = []
    for p, good in zip(products, ok):
        if good:
            continue
        if p.get("_reason") in RETRY_REASONS:
            retry.append(p)
        _finalize_failed(p, batch)
    return retry


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
