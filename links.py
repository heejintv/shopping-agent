"""상품 링크 검증 (Claude 호출 없음).

각 상품 링크에 실제로 접속해서
  1) 정상 응답(200)인지
  2) 페이지 제목·내용에 모델명이 들어 있는지
확인한다. 실패하거나 링크가 없으면 다나와 검색 링크로 바꾼다.
사이트가 접속을 막아서 확인할 수 없는 경우도 검색 링크로 바꾼다.

결과: product["url"]           최종 링크
      product["url_verified"]  True = 확인된 상품 페이지, False = 다나와 검색 링크
      product["url_original"]  Claude가 준 원래 링크 (바꾼 경우만, 기록용)

기존 캐시 다시 검증: python links.py   (cache/와 demo/의 결과를 모두 다시 검사해서 저장)
"""

import ipaddress
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TIMEOUT = 8              # 초. 링크 하나당
MAX_BYTES = 1_500_000    # 페이지는 앞부분 1.5MB까지만 읽음
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
SEARCH_URL = "https://search.danawa.com/dsearch.php?query="


# ---------- 모델명 비교 ----------

def core_model(model: str) -> str:
    """'G PRO X SUPERLIGHT 2 (정품) (블랙)' → 'G PRO X SUPERLIGHT 2' (괄호 속 정품·색상 표시 제거)."""
    core = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", model)
    return " ".join(core.split()) or model.strip()


def tokens(text: str) -> list[str]:
    return re.findall(r"[0-9a-z가-힣]+", text.lower())


def page_has_model(page: str, model: str) -> bool:
    """모델명(괄호 부분 제외)의 단어가 페이지에 모두 들어 있으면 True. 띄어쓰기·기호 차이는 무시."""
    want = tokens(core_model(model))
    if not want:
        return False
    flat = "".join(tokens(page))
    return all(t in flat for t in want)


def search_link(brand: str, model: str) -> str:
    core = core_model(model)
    query = core if core.lower().startswith(brand.lower()) else f"{brand} {core}"
    return SEARCH_URL + urllib.parse.quote(query)


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
    """정상(200) HTML이면 내용, 아니면 None (오류·차단·시간 초과 포함)."""
    if not is_public_url(url):
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "ko-KR,ko;q=0.9"})
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            if r.status != 200 or "html" not in (r.headers.get("Content-Type") or "html"):
                return None
            raw = r.read(MAX_BYTES)
            charset = r.headers.get_content_charset()
    except Exception:
        return None
    for enc in filter(None, [charset, "utf-8", "cp949"]):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "ignore")


def looks_like_search_page(url: str) -> bool:
    """검색 결과 페이지는 '상품 페이지'로 치지 않는다."""
    u = urllib.parse.urlsplit(url)
    return u.hostname is not None and (u.hostname.startswith("search.") or "search" in u.path.lower())


# ---------- 검증 ----------

def verify_product(p: dict) -> dict:
    original = p.get("url_original") or p.get("url")  # 다시 검증할 때는 Claude가 준 원래 링크 기준
    page = None
    if original and not looks_like_search_page(original):
        page = fetch_page(original)
    if page and page_has_model(page, p.get("model") or ""):
        p["url"], p["url_verified"] = original, True
        p.pop("url_original", None)
    else:
        p["url"], p["url_verified"] = search_link(p.get("brand") or "", p.get("model") or ""), False
        if original:
            p["url_original"] = original
        else:
            p.pop("url_original", None)
    return p


def verify_links(data: dict) -> dict:
    """결과의 모든 상품 링크를 동시에 검증해서 data를 고친다."""
    products = data.get("products") or []
    if products:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(verify_product, products))
    return data


def reverify_dir(folder: Path) -> None:
    for f in sorted(folder.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "products" not in data:
            continue
        verify_links(data)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        cond = f" · {data['condition']}" if data.get("condition") else ""
        print(f"{folder.name}/{f.name}  {data.get('query')}{cond}")
        for p in data["products"]:
            mark = "✅ 상품 페이지" if p["url_verified"] else "🔎 다나와 검색"
            print(f"   {mark}  {p['brand']} {core_model(p['model'])}  →  {p['url']}")


if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    for name in sys.argv[1:] or ["cache", "demo"]:
        if (base / name).is_dir():
            reverify_dir(base / name)
