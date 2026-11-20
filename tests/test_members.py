"""members.py: Supabase REST 요청 모양과 개수 읽기 (네트워크는 가짜)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import members  # noqa: E402

ENV = {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SERVICE_KEY": "sb_secret_test"}


class FakeResp:
    def __init__(self, body=b"[]", headers=None):
        self.body, self.headers = body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class MembersTest(unittest.TestCase):
    def setUp(self):
        members._role_cache.clear()

    def test_parse_count(self):
        self.assertEqual(members.parse_count("0-0/3"), 3)
        self.assertEqual(members.parse_count("*/0"), 0)
        with self.assertRaises(members.MemberError):
            members.parse_count(None)

    def test_count_today_request(self):
        with mock.patch.dict("os.environ", ENV), \
             mock.patch("urllib.request.urlopen", return_value=FakeResp(headers={"Content-Range": "0-0/4"})) as op:
            self.assertEqual(members.count_today("u1", "chat"), 4)
        req = op.call_args[0][0]
        self.assertIn("action=in.%28chat%2Ccompare%29", req.full_url)
        self.assertIn("status=eq.ok", req.full_url)
        self.assertIn("user_id=eq.u1", req.full_url)
        self.assertEqual(req.get_header("Prefer"), "count=exact")
        self.assertEqual(req.get_header("Apikey"), "sb_secret_test")
        self.assertIsNone(req.get_header("Authorization"))   # 새 형식 키는 apikey 헤더에만

    def test_legacy_jwt_key_also_in_authorization(self):
        env = {**ENV, "SUPABASE_SERVICE_KEY": "eyJlegacy"}
        with mock.patch.dict("os.environ", env), \
             mock.patch("urllib.request.urlopen", return_value=FakeResp(b'[{"role":"admin"}]')) as op:
            self.assertEqual(members.get_role("u2"), "admin")
        self.assertEqual(op.call_args[0][0].get_header("Authorization"), "Bearer eyJlegacy")

    def test_role_defaults_to_user(self):
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", return_value=FakeResp(b"[]")):
            self.assertEqual(members.get_role("u3"), "user")

    def test_no_service_key_raises(self):
        with mock.patch.dict("os.environ", {"SUPABASE_URL": "", "SUPABASE_SERVICE_KEY": ""}):
            with self.assertRaises(members.MemberError):
                members.count_today("u1", "search")

    def test_log_usage_never_raises_and_cuts_query(self):
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", return_value=FakeResp()) as op:
            members.log_usage("u1", "a@b.c", "search", "ok", query="가" * 500, engine="claude", duration_ms=12.7)
        import json
        row = json.loads(op.call_args[0][0].data)
        self.assertEqual(len(row["query"]), 300)
        self.assertEqual(row["duration_ms"], 12)
        with mock.patch.dict("os.environ", {"SUPABASE_URL": "", "SUPABASE_SERVICE_KEY": ""}):
            members.log_usage("u1", None, "chat", "ok")   # 설정이 없어도 오류 없이 넘어감


if __name__ == "__main__":
    unittest.main()
