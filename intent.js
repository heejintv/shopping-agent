// 챗봇 글자 도우미 (index.html이 <script src="intent.js">로 쓰고, tests/test_intent.py가 node로 불러 테스트함)
// - isActionCommand: 입력이 "행동 명령"(⚡)인지 "상담 질문"(/chat)인지 키워드로만 판단 (Gemini 호출 없음)
// - josa: 받침에 맞게 조사 붙이기 ("로지텍 K295" + 을/를 → "로지텍 K295를")
(function (root) {
  // 질문이면 상담: 물음표 또는 '어때/할까/만해' 같은 의견을 묻는 말
  const QUESTION = /[?？]|어때|어떄|할까|만해|만한가|괜찮|나을까|좋을까|될까|뭐가|어느|어떤/;
  // 행동 명령 키워드 (찜·저장·담기·빼기·삭제·검색·목록 보기)
  const ACTION = new RegExp([
    "찜",
    "저장",
    "담아",
    "빼\\s*(줘|주세요|줄래|라|기|$)",   // "삼성 빼고 추천해줘"의 '빼고'는 행동 아님
    "삭제",
    "지워",
    "검색\\s*해",
    "찾아\\s*(줘|주세요|봐|$)",
    "목록\\s*(보여|열어|봐)",
  ].join("|"));

  function isActionCommand(text) {
    const t = String(text || "").trim();
    if (!t || QUESTION.test(t)) return false;
    return ACTION.test(t);
  }

  // 마지막 글자의 받침: 0 = 없음, 8 = ㄹ, 그 외 = 있음. 한글이 아니면 읽는 소리로 (숫자 2→이, 영어 m→엠)
  const DIGIT = { 0: 1, 1: 8, 2: 0, 3: 1, 4: 0, 5: 0, 6: 1, 7: 8, 8: 8, 9: 0 };   // 영·일·이·삼·사·오·육·칠·팔·구
  const LETTER = { l: 8, m: 1, n: 1, r: 8 };   // 엘·엠·엔·알 (나머지 영어 이름은 받침 없음으로)
  function finalConsonant(word) {
    const t = String(word || "").replace(/[\s"'“”‘’)\]}>.,!?…]+$/u, "");
    const ch = t.slice(-1);
    if (!ch) return 0;
    const code = ch.charCodeAt(0);
    if (code >= 0xac00 && code <= 0xd7a3) return (code - 0xac00) % 28;
    if (/\d/.test(ch)) return DIGIT[ch];
    return LETTER[ch.toLowerCase()] || 0;
  }
  const PAIRS = { "을/를": ["을", "를"], "은/는": ["은", "는"], "이/가": ["이", "가"], "과/와": ["과", "와"], "으로/로": ["으로", "로"] };
  function josa(word, pair) {
    const [withB, without] = PAIRS[pair] || PAIRS["을/를"];
    const f = finalConsonant(word);
    const useWith = pair === "으로/로" ? f !== 0 && f !== 8 : f !== 0;   // ㄹ 받침은 "로"
    return String(word) + (useWith ? withB : without);
  }

  root.isActionCommand = isActionCommand;
  root.josa = josa;
  if (typeof module !== "undefined" && module.exports) module.exports = { isActionCommand, josa };
})(typeof window !== "undefined" ? window : globalThis);
