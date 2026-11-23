"""상품 링크가 거의 없을 때 대비: 검색 프롬프트(여러 쇼핑몰 상세 페이지), 절반 미만이면 같은 검색 안에서 1번만 다시 찾기,
요약 줄의 개발 용어 정리, 화면의 '쇼핑몰에서 찾기' 버튼. 검색 엔진·링크 확인은 가짜로 바꾼다.

실행: python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))

import app as server  # noqa: E402

server.ASYNC_LOG = False


def fake_verify(products, on_progress=None):
    """주소가 있으면 확인됨, 없으면 확인 안 됨. 진짜 틀린 링크는 없다고 봄(재검색 대상 없음)."""
    for p in products:
        p["url_verified"] = bool(p.get("url"))
    return []


def product(i, url=None):
    return {"brand": "B", "model": f"M{i}", "price": 1000 * i, "url": url}


class FallbackBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pickit-link-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.object(server, "verify_links", side_effect=fake_verify),
                  mock.patch.object(server.stats, "bump")):
            p.start()
        self.addCleanup(mock.patch.stopall)

    def search(self, products, found=None, notes=None):
        """가짜 검색 → perform_search. found: 다시 찾기에서 돌려줄 {번호: 주소}. (결과, 엔진 호출 목록)"""
        calls = []

        def engine(prompt, schema, timeout, eng, job=None):
            calls.append(prompt)
            if prompt.startswith("아래 상품들의"):
                return {"links": [{"no": n, "url": u} for n, u in (found or {}).items()]}
            return {"products": json.loads(json.dumps(products)), "value_pick": None, "quality_pick": None, "notes": notes}
        with mock.patch.object(server, "call_engine", side_effect=engine):
            data = server.perform_search("마우스", "", self.dir / "x.json", "gemini")
        return data, calls


class RetryTest(FallbackBase):
    def test_few_links_retry_once(self):
        """5개 중 링크 1개(절반 미만) → 링크 없는 4개만 한 번 다시 찾음, 찾은 건 채워짐."""
        data, calls = self.search([product(1, "https://a.com/1")] + [product(i) for i in range(2, 6)],
                                  found={1: "https://www.coupang.com/vp/products/2"})
        self.assertEqual(len(calls), 2)                       # 검색 1번 + 링크 다시 찾기 1번
        self.assertIn("1. B M2", calls[1])
        self.assertNotIn("B M1", calls[1])                    # 이미 링크 있는 상품은 안 넣음
        self.assertEqual(data["products"][1]["url"], "https://www.coupang.com/vp/products/2")
        self.assertEqual(data["link_retry"], 4)

    def test_still_none_keeps_result(self):
        """다시 해도 못 찾으면 결과는 그대로 (또 다시 찾지 않음)."""
        data, calls = self.search([product(i) for i in range(1, 6)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(data["products"]), 5)
        self.assertTrue(all(not p.get("url") for p in data["products"]))

    def test_half_or_more_no_retry(self):
        data, calls = self.search([product(1, "https://a.com/1"), product(2, "https://a.com/2"), product(3), product(4)])
        self.assertEqual(len(calls), 1)

    def test_retry_failure_keeps_result(self):
        def engine(prompt, *a, **k):
            if prompt.startswith("아래 상품들의"):
                raise RuntimeError("시간 초과")
            return {"products": [product(1), product(2)], "value_pick": None, "quality_pick": None, "notes": None}
        with mock.patch.object(server, "call_engine", side_effect=engine):
            data = server.perform_search("마우스", "", self.dir / "x.json", "gemini")
        self.assertEqual(len(data["products"]), 2)

    def test_unverifiable_counts_as_link(self):
        self.assertTrue(server.has_link({"url": "https://x.com", "url_verified": False, "url_status": "unverifiable"}))
        self.assertFalse(server.has_link({"url": None, "url_verified": False}))
        self.assertFalse(server.few_links([]))

    def test_quota_once_per_search(self):
        """다시 찾기는 perform_search 안에서 끝남 → /search 한 번 = 검색 횟수 1회 (finish 한 번)."""
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        body = src[src.index("def perform_search("):src.index("def touch_recent(")]
        self.assertNotIn("reserve", body)
        self.assertNotIn("quota", body)


class PromptTest(unittest.TestCase):
    def test_prompt_lists_shops(self):
        for shop in ("쿠팡", "11번가", "G마켓", "네이버쇼핑", "다나와"):
            self.assertIn(shop, server.PROMPT)
            self.assertIn(shop, server.RETRY_PROMPT)
        self.assertIn("기획전", server.PROMPT)
        self.assertIn("상세 페이지", server.PROMPT)


class NotesTest(FallbackBase):
    def test_dev_words_become_plain(self):
        data, _ = self.search([product(1, "https://a.com/1"), product(2, "https://a.com/2"), product(3)],
                              notes="일부 상품의 url은 null이에요")
        self.assertEqual(data["notes"], "상품 링크를 찾지 못했어요")

    def test_dev_words_dropped_when_all_linked(self):
        d = {"notes": "값이 None인 항목 있음", "products": [{"url": "https://a.com", "url_verified": True}]}
        server.clean_notes(d)
        self.assertIsNone(d["notes"])

    def test_normal_notes_kept(self):
        d = {"notes": "가격은 오늘 기준이에요", "products": [{"url": None}]}
        server.clean_notes(d)
        self.assertEqual(d["notes"], "가격은 오늘 기준이에요")


class PageTest(unittest.TestCase):
    def test_find_button_replaces_nolink(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("링크 확인 안 됨</span>", html)
        self.assertIn("search.shopping.naver.com/search/all?query=", html)
        self.assertIn("쇼핑몰에서 찾기", html)
        self.assertIn("a.find-link", html)                     # 회색 버튼 (진짜 링크와 구분)
        self.assertIn("상품 링크를 찾지 못했어요", html)


if __name__ == "__main__":
    unittest.main()
