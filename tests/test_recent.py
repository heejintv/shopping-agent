"""최근 검색(/history) 테스트: 검색 시각은 시간대를 붙인 KST, 저장된 결과로 다시 검색해도 맨 앞으로.
검색창 검색과 ⚡ search는 화면에서 같은 경로(#f 제출 → /search/stream, 안 되면 /search)를 탄다.
Supabase·검색 엔진은 가짜로 바꾼다.

실행: python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))

import app as server  # noqa: E402
import auth  # noqa: E402
import members  # noqa: E402

server.ASYNC_LOG = False
GOOD = {"Authorization": "Bearer good-token"}
USER = {"id": "11111111-2222-3333-4444-555555555555", "email": "user@example.com"}
PRODUCTS = [{"brand": "A", "model": "M1", "price": 10000}]


class RecentBase(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="pickit-cache-"))
        self.addCleanup(shutil.rmtree, self.cache, True)
        for p in (mock.patch.object(server, "CACHE_DIR", self.cache),
                  mock.patch.object(auth, "fetch_user", side_effect=lambda t: USER if t == "good-token" else
                                    (_ for _ in ()).throw(auth.AuthError("로그인이 만료됐어요."))),
                  mock.patch.object(members, "get_role", side_effect=lambda uid, fresh=False: "user"),
                  mock.patch.object(members, "count_today", return_value=0),
                  mock.patch.object(members, "log_usage"),
                  mock.patch.object(server, "is_local_request", return_value=False),
                  mock.patch.object(server.stats, "record_request")):
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.client = server.app.test_client()

    def save(self, q, searched_at, recent_at=None, c=""):
        data = {"query": q, "condition": c or None, "products": PRODUCTS, "searched_at": searched_at}
        if recent_at:
            data["recent_at"] = recent_at
        path = server.cache_path(q, c)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def history(self):
        return self.client.get("/history").get_json()


class TimeFormatTest(RecentBase):
    def test_now_kst_has_offset(self):
        s = server.now_kst()
        self.assertTrue(s.endswith("+09:00"), s)
        t = datetime.fromisoformat(s)
        self.assertLess(abs((t - datetime.now(timezone.utc)).total_seconds()), 5)   # 서버 시간대와 상관없이 지금

    def test_new_search_saves_kst(self):
        """새 검색(perform_search)이 저장하는 searched_at은 시간대 붙은 KST."""
        path = self.cache / "x.json"
        with mock.patch.object(server, "run_search", return_value={"products": list(PRODUCTS)}), \
             mock.patch.object(server, "verify_links", return_value=[]):
            server.perform_search("무선 마우스", "", path, "gemini")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(saved["searched_at"].endswith("+09:00"), saved["searched_at"])

    def test_old_naive_values(self):
        """예전 시간대 없는 값: 서버 현지 시간으로 읽고 KST로 바꿔 보냄. demo(내 PC=KST)는 KST로."""
        self.assertEqual(server.kst_text("2026-11-23 01:00:00", timezone.utc), "2026-11-23T10:00:00+09:00")
        self.assertEqual(server.kst_text("2026-11-23 10:00:00", server.KST), "2026-11-23T10:00:00+09:00")
        self.assertEqual(server.kst_text("2026-11-23T10:00:00+09:00"), "2026-11-23T10:00:00+09:00")
        self.assertIsNone(server.kst_text("어제"))


class HistoryOrderTest(RecentBase):
    def test_order_uses_real_time(self):
        """Render(UTC)에서 예전 방식으로 저장된 값과 새 KST 값이 섞여도 실제 시각 순서."""
        now = datetime.now(server.KST)
        self.save("옛날 검색", (now - timedelta(hours=2)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        self.save("방금 검색", now.isoformat(timespec="seconds"))
        items = self.history()
        self.assertEqual(items[0]["query"], "방금 검색")
        for it in items:
            self.assertTrue(it["searched_at"].endswith("+09:00"))
            self.assertIn("recent_at", it)

    def test_recent_at_moves_to_front(self):
        self.save("A", "2026-11-20T10:00:00+09:00")
        self.save("B", "2026-11-21T10:00:00+09:00")
        self.save("C", "2026-11-19T10:00:00+09:00", recent_at="2026-11-22T10:00:00+09:00")
        items = self.history()
        self.assertEqual([i["query"] for i in items], ["C", "B", "A"])
        self.assertEqual(items[0]["searched_at"], "2026-11-19T10:00:00+09:00")   # 검색 시각은 그대로


class CacheHitUpdatesRecentTest(RecentBase):
    """저장된 결과로 다시 검색(검색창이든 ⚡ search든 같은 경로)해도 최근 검색 맨 앞으로."""

    def test_search_cached(self):
        path = self.save("옛 검색어", "2026-11-01T10:00:00+09:00")
        self.save("다른 검색어", "2026-11-20T10:00:00+09:00")
        res = self.client.get("/search", query_string={"q": "옛 검색어"}, headers=GOOD)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(res.get_json()["cached"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(saved["recent_at"].endswith("+09:00"))
        self.assertEqual(saved["searched_at"], "2026-11-01T10:00:00+09:00")
        self.assertEqual(self.history()[0]["query"], "옛 검색어")

    def test_stream_cached(self):
        path = self.save("스트림 검색어", "2026-11-01T10:00:00+09:00")
        self.save("다른 검색어", "2026-11-20T10:00:00+09:00")
        body = self.client.get("/search/stream", query_string={"q": "스트림 검색어"}, headers=GOOD).get_data(as_text=True)
        self.assertIn("event: result", body)
        self.assertIn("recent_at", json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(self.history()[0]["query"], "스트림 검색어")

    def test_new_search_on_top(self):
        self.save("예전 것", "2026-11-20T10:00:00+09:00")

        def fake_perform(q, c, path, engine, **kw):
            data = {"query": q, "condition": None, "products": PRODUCTS, "searched_at": server.now_kst()}
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        with mock.patch.object(server, "perform_search", side_effect=fake_perform):
            res = self.client.get("/search", query_string={"q": "새 검색어"}, headers=GOOD)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.history()[0]["query"], "새 검색어")

    def test_shared_link_does_not_touch(self):
        """공유 링크·최근 검색 칩으로 열어 보기만 하면 순서는 그대로 (검색한 게 아님)."""
        path = self.save("공유", "2026-11-01T10:00:00+09:00")
        self.client.get("/cached", query_string={"q": "공유"})
        self.client.get(f"/history/{path.name}")
        self.assertNotIn("recent_at", json.loads(path.read_text(encoding="utf-8")))


class SeedDemoTest(RecentBase):
    def test_demo_times_become_kst(self):
        self.assertGreater(server.seed_cache_from_demo(), 0)
        for f in self.cache.glob("*.json"):
            s = json.loads(f.read_text(encoding="utf-8")).get("searched_at")
            if s:
                self.assertTrue(s.endswith("+09:00"), (f.name, s))


class PageTest(unittest.TestCase):
    def test_both_paths_reload_recent(self):
        """검색창과 ⚡ search 모두 같은 submitSearch를 타고, 저장된 결과여도 최근 검색을 다시 불러옴."""
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        body = html[html.index("async function submitSearch("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("loadRecent();", body)
        self.assertNotIn("if (!data.cached) { loadRecent()", body)
        self.assertIn('$("f").requestSubmit()', html)                          # ⚡ search도 검색창 제출
        self.assertIn("recentTs(", html)                                       # 순서는 recent_at 우선


if __name__ == "__main__":
    unittest.main()
