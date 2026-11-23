"""챗봇 입력 키워드 판단(intent.js의 isActionCommand) 테스트. node로 실제 JS 함수를 불러서 확인한다.
행동 명령이면 ⚡(/chat/action), 질문이면 상담(/chat). node가 없으면 건너뜀.

실행: python -m unittest discover -s tests -v
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

ACTIONS = [
    "저소음 키보드 찜해줘", "에어팟 검색해서 제일 싼 거 찜해줘", "이거 저장해 줘", "장바구니에 담아줘",
    "로지텍 K295 빼줘", "찜에서 삭제", "그거 지워", "무선 마우스 검색해줘", "블루투스 이어폰 찾아줘",
    "찜 목록 보여줘", "목록 열어줘", "첫 번째 거 찜",
]
QUESTIONS = [
    "이 키보드 어때?", "찜할 만해?", "찜할 만해", "이거 저장할까", "이 키보드 어때", "찜한 것 중 뭐가 나아",
    "가성비 하나만 골라줘", "자취생한테 뭐가 좋아?", "삼성 빼고 추천해줘", "선물용으로 추천해줘", "", "   ",
    "검색해 볼까?", "어느 게 더 조용해",
]


@unittest.skipUnless(NODE, "node가 없어요")
class IntentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = ("const {isActionCommand} = require(process.argv[1]);"
                  "const xs = JSON.parse(process.argv[2]);"
                  "console.log(JSON.stringify(xs.map(isActionCommand)));")
        texts = ACTIONS + QUESTIONS
        out = subprocess.run([NODE, "-e", script, str(ROOT / "intent.js"), json.dumps(texts)],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        cls.result = dict(zip(texts, json.loads(out)))

    def test_actions_go_to_action(self):
        for t in ACTIONS:
            self.assertTrue(self.result[t], f"행동 명령인데 상담으로 감: {t!r}")

    def test_questions_go_to_chat(self):
        for t in QUESTIONS:
            self.assertFalse(self.result[t], f"질문인데 ⚡로 감: {t!r}")


JOSA = [   # (단어, 조사 쌍, 기대값)
    ("로지텍 K295", "을/를", "로지텍 K295를"), ("로지텍 M196", "을/를", "로지텍 M196을"),
    ("에어팟 프로 2", "을/를", "에어팟 프로 2를"), ("키보드", "을/를", "키보드를"), ("애플 에어팟", "을/를", "애플 에어팟을"),
    ("“로지텍 Pebble”", "을/를", "“로지텍 Pebble”를"), ("“저소음 키보드”", "은/는", "“저소음 키보드”는"),
    ("마우스", "은/는", "마우스는"), ("이어폰", "은/는", "이어폰은"), ("갤럭시 버즈", "이/가", "갤럭시 버즈가"),
    ("Galaxy M", "을/를", "Galaxy M을"), ("Pixel", "을/를", "Pixel을"), ("서울", "으로/로", "서울로"), ("집", "으로/로", "집으로"),
    ("소니 WH-1000", "을/를", "소니 WH-1000을"), ("K7", "을/를", "K7을"),
]


@unittest.skipUnless(NODE, "node가 없어요")
class JosaTest(unittest.TestCase):
    def test_josa(self):
        script = ("const {josa} = require(process.argv[1]);"
                  "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([w, p]) => josa(w, p))));")
        out = subprocess.run([NODE, "-e", script, str(ROOT / "intent.js"), json.dumps([[w, p] for w, p, _ in JOSA])],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        for (w, p, want), got in zip(JOSA, json.loads(out)):
            self.assertEqual(got, want, (w, p))

    def test_no_placeholder_josa_in_page(self):
        """화면 문구에 "을(를)"·"은(는)" 같은 자리표시 조사를 쓰지 않음."""
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        for bad in ("을(를)", "은(는)", "이(가)", "와(과)"):
            self.assertNotIn(bad, html)


OPTS = ["1. 로지텍 M196", "2. 로지텍 Pebble", "3. 앱코 A660"]
MATCH = [   # (입력, 선택지, 기대: 고른 선택지 또는 None = 새 명령으로)
    ("2", OPTS, OPTS[1]), ("2번", OPTS, OPTS[1]), ("두 번째", OPTS, OPTS[1]), ("둘째", OPTS, OPTS[1]),
    ("2번으로 할게", OPTS, OPTS[1]), ("Pebble", OPTS, OPTS[1]), ("로지텍 pebble", OPTS, OPTS[1]),
    ("1. 로지텍 M196", OPTS, OPTS[0]), ("M196", OPTS, OPTS[0]), ("앱코", OPTS, OPTS[2]),
    ("로지텍", OPTS, None),                     # 두 개에 걸리면 애매 → 답 아님
    ("9", OPTS, None), ("에어팟 검색해줘", OPTS, None), ("무선 키보드 찜해줘", OPTS, None), ("", OPTS, None),
    ("네", ["네", "아니요"], "네"), ("응", ["네", "아니요"], None), ("2", [], None),
]


@unittest.skipUnless(NODE, "node가 없어요")
class MatchOptionTest(unittest.TestCase):
    def test_match_option(self):
        script = ("const {matchOption} = require(process.argv[1]);"
                  "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([t, o]) => matchOption(t, o))));")
        out = subprocess.run([NODE, "-e", script, str(ROOT / "intent.js"), json.dumps([[t, o] for t, o, _ in MATCH])],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        for (t, o, want), got in zip(MATCH, json.loads(out)):
            self.assertEqual(got, want, (t, o))


class IntentFileTest(unittest.TestCase):
    def test_served_and_loaded(self):
        import os, tempfile, sys  # noqa: E401
        sys.path.insert(0, str(ROOT))
        os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pickit-test-"))
        import app as server
        res = server.app.test_client().get("/intent.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn("isActionCommand", res.get_data(as_text=True))
        res.close()
        self.assertIn('<script src="intent.js"></script>', (ROOT / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
