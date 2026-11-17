"""상품 링크 검증 (Claude 호출 없음).

각 상품 링크에 실제로 접속해서
  1) 정상 응답(200)인지
  2) 페이지 제목에 모델명이 들어 있는지
확인한다. 검색 결과 페이지는 상품 페이지로 치지 않는다.
검색 결과 같은 대체 링크로 바꾸지 않는다. 확인 안 되면 링크를 비운다.

결과: product["url"]           확인된 상품 상세 페이지 주소, 실패하면 None
      product["url_verified"]  True = 확인됨, False = 링크 확인 안 됨 (화면에서 버튼 숨김)
      product["url_rejected"]  검증에 실패한 주소 목록 (기록용, 실패한 경우만)

기존 캐시 다시 검증: python links.py   (cache/와 demo/를 다시 검사해서 저장. 재검색은 하지 않음)
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
from concurrent.futures import ThreadPoolExecutor
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


def check_url(url: str, model: str) -> bool:
    if not url or looks_like_search_page(url):
        return False
    page = fetch_page(url)
    return page is not None and title_has_model(page_titles(page), model)


# ---------- 검증 ----------

def verify_product(p: dict) -> bool:
    """p["url"]을 검증해서 p를 고친다. 확인되면 True."""
    url = p.get("url")
    if check_url(url, p.get("model") or ""):
        p["url_verified"] = True
        return True
    if url:
        rejected = p.setdefault("url_rejected", [])
        if url not in rejected:
            rejected.append(url)
    p["url"], p["url_verified"] = None, False
    return False


def verify_links(products: list[dict]) -> list[dict]:
    """상품들의 링크를 동시에 검증하고, 실패한 상품 목록을 돌려준다."""
    if not products:
        return []
    with ThreadPoolExecutor(max_workers=4) as pool:  # 한 사이트에 너무 몰아서 접속하지 않도록
        ok = list(pool.map(verify_product, products))
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


if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    for name in sys.argv[1:] or ["cache", "demo"]:
        if (base / name).is_dir():
            reverify_dir(base / name)
