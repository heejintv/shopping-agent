"""관리자 대시보드 테스트: /admin/stats 권한(401/403/200), 집계(admin_stats.build), 토큰 수 기록.
Supabase·Gemini는 가짜로 바꾼다.

실행: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pickit-test-")

import admin_stats  # noqa: E402
import app as server  # noqa: E402
import auth  # noqa: E402
import gemini  # noqa: E402
import members  # noqa: E402

server.ASYNC_LOG = False
KST = admin_stats.KST
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=KST)

USERS = {
    "user-token": {"id": "u-user", "email": "user@example.com"},
    "admin-token": {"id": "u-admin", "email": "admin@example.com"},
}
ROLES = {"u-admin": "admin"}


def row(when: str, email="a@x.com", uid="u-a", action="search", status="ok", **kw):
    """when: '11-20 10:00' 같은 KST 시각"""
    t = datetime.strptime(f"2026-{when}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    return {"created_at": t.isoformat(), "email": email, "user_id": uid, "action": action, "status": status,
            "query": kw.pop("query", "q"), "engine": kw.pop("engine", "claude"), "duration_ms": 100,
            "tokens": kw.pop("tokens", None), "exempt": kw.pop("exempt", False)}


class AdminApiTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

        def fake_fetch_user(token):
            if token in USERS:
                return USERS[token]
            raise auth.AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")

        patches = [
            mock.patch.object(auth, "fetch_user", side_effect=fake_fetch_user),
            mock.patch.object(members, "get_role", side_effect=lambda uid, fresh=False: ROLES.get(uid, "user")),
            mock.patch.object(members, "usage_since", return_value=([row("11-20 10:00")], False)),
            mock.patch.object(members, "count_rows", return_value=3),
            mock.patch.object(server, "is_local_request", return_value=False),
        ]
        started = [p.start() for p in patches]
        self.role, self.usage = started[1], started[2]
        for p in patches:
            self.addCleanup(p.stop)

    def get(self, token=None, **qs):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.get("/admin/stats", query_string=qs, headers=headers)

    def test_no_token_401(self):
        self.assertEqual(self.get().status_code, 401)
        self.usage.assert_not_called()

    def test_wrong_token_401(self):
        self.assertEqual(self.get("nope").status_code, 401)
        self.usage.assert_not_called()

    def test_not_admin_403(self):
        res = self.get("user-token")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json(), {"error": "관리자만 볼 수 있어요."})   # 숫자는 하나도 안 줌
        self.usage.assert_not_called()                                     # 기록을 읽지도 않음

    def test_not_admin_even_from_my_pc_403(self):
        """내 PC(무제한)라도 admin이 아니면 대시보드는 못 봄."""
        with mock.patch.object(server, "is_local_request", return_value=True):
            self.assertEqual(self.get("user-token").status_code, 403)

    def test_admin_200(self):
        res = self.get("admin-token", period="7d")
        self.assertEqual(res.status_code, 200, res.get_json())
        d = res.get_json()
        self.assertEqual(d["cards"]["members"], 3)
        self.assertEqual(d["period"], "7d")
        for k in ("gemini", "global_search", "daily", "ranking", "recent"):
            self.assertIn(k, d)
        self.assertEqual(res.headers["Cache-Control"], "no-store")
        self.role.assert_called_with("u-admin", fresh=True)   # 권한은 캐시 없이 새로 확인

    def test_bad_period_400(self):
        self.assertEqual(self.get("admin-token", period="1y").status_code, 400)

    def test_member_db_down_503(self):
        self.usage.side_effect = members.MemberError("down")
        self.assertEqual(self.get("admin-token").status_code, 503)

    def test_admin_page_has_no_data(self):
        res = self.client.get("/admin.html")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("/admin/stats", html)          # 데이터는 API에서만 받음
        self.assertNotIn("SUPABASE_SERVICE_KEY", html)
        res.close()


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row("11-20 14:00", "a@x.com", "u-a", "search", "ok", query="목도리"),
            row("11-20 13:00", "a@x.com", "u-a", "chat", "ok", engine="gemini", tokens=120, query="뭐가 좋아?"),
            row("11-20 12:00", "b@x.com", "u-b", "search", "limited"),
            row("11-20 11:00", "b@x.com", "u-b", "compare", "error", engine="gemini", tokens=None),
            row("11-20 10:00", "admin@x.com", "u-ad", "search", "ok", exempt=True),
            row("11-20 09:00", "b@x.com", "u-b", "search", "cached", engine="gemini"),
            row("11-19 23:59", "b@x.com", "u-b", "search", "ok"),          # 어제 (KST)
            row("11-14 10:00", "c@x.com", "u-c", "chat", "ok"),            # 7일 그래프 첫날
            row("11-01 10:00", "c@x.com", "u-c", "search", "ok"),          # 30일 안, 7일 밖
        ]

    def build(self, **kw):
        return admin_stats.build(self.rows, 10, NOW, gemini_limit=1000, global_limit=20, **kw)

    def test_cards_today(self):
        c = self.build()["cards"]
        self.assertEqual(c, {"members": 10, "today_users": 3, "today_searches": 2, "today_chats": 1})

    def test_gemini_counts_real_calls_only(self):
        g = self.build()["gemini"]
        self.assertEqual((g["calls"], g["tokens"], g["remaining"]), (2, 120, 998))   # cached는 호출 아님

    def test_global_excludes_exempt(self):
        self.assertEqual(self.build()["global_search"], {"used": 1, "limit": 20, "remaining": 19})

    def test_daily_seven_days(self):
        daily = self.build()["daily"]
        self.assertEqual([d["date"] for d in daily][0], "11-14")
        self.assertEqual(len(daily), 7)
        self.assertEqual(daily[0]["chats"], 1)
        self.assertEqual(daily[5], {"date": "11-19", "searches": 1, "chats": 0})
        self.assertEqual(daily[6], {"date": "11-20", "searches": 2, "chats": 1})

    def test_period_and_email_filter(self):
        d = self.build(period="today", q="B@X")
        self.assertEqual({r["email"] for r in d["recent"]}, {"b@x.com"})
        self.assertEqual(len(d["recent"]), 3)          # 오늘 b의 기록 3개 (어제 것은 빠짐)
        self.assertEqual(d["ranking"][0]["searches"], 0)   # 성공한 검색 없음
        d30 = self.build(period="30d", q="c@")
        self.assertEqual(d30["ranking"][0], {"email": "c@x.com", "searches": 1, "chats": 1,
                                             "last_used": "2026-11-14T10:00:00+09:00"})

    def test_ranking_order_and_recent_newest_first(self):
        d = self.build(period="7d")
        self.assertEqual(d["ranking"][0]["email"], "a@x.com")   # 성공 2회로 1등
        self.assertEqual(d["recent"][0]["query"], "목도리")

    def test_no_gemini_limit(self):
        g = admin_stats.build(self.rows, 0, NOW)["gemini"]
        self.assertIsNone(g["limit"])
        self.assertIsNone(g["remaining"])


class TokenLoggingTest(unittest.TestCase):
    def test_chat_gemini_reports_tokens(self):
        resp = SimpleNamespace(text="답변", usage_metadata=SimpleNamespace(total_token_count=321))
        client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: resp))
        types = SimpleNamespace(GenerateContentConfig=lambda **kw: None)
        usage = {}
        with mock.patch.object(gemini, "_client", return_value=(client, types)):
            self.assertEqual(gemini.chat_gemini("sys", "p", 5, usage), "답변")
        self.assertEqual(usage["tokens"], 321)

    def test_log_usage_tokens_column(self):
        sent = []
        with mock.patch.object(members, "_request", side_effect=lambda *a, **kw: sent.append(kw["body"])):
            members.log_usage("u", "e", "chat", "ok", tokens=50)
            members.log_usage("u", "e", "chat", "ok")
        self.assertEqual(sent[0]["tokens"], 50)
        self.assertNotIn("tokens", sent[1])   # 모르면 열을 비워 둠

    def test_chat_route_logs_tokens(self):
        logs = []
        client = server.app.test_client()
        body = {"question": "골라줘", "results": {"query": "q", "products": [{"brand": "A", "model": "M"}]}}
        with mock.patch.object(auth, "fetch_user", return_value=USERS["admin-token"]), \
             mock.patch.object(members, "get_role", return_value="admin"), \
             mock.patch.object(members, "log_usage", side_effect=lambda *a, **kw: logs.append(kw)), \
             mock.patch.object(server, "is_local_request", return_value=False), \
             mock.patch.object(server.stats, "record_request"), \
             mock.patch.object(server.chat, "answer", return_value={
                 "answer": "추천", "pick_id": None, "engine": "gemini", "fallback": False, "tokens": 77}):
            res = client.post("/chat", json=body, headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual((logs[-1]["tokens"], logs[-1]["engine"]), (77, "gemini"))


if __name__ == "__main__":
    unittest.main()
