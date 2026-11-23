// 챗봇 글자 도우미 (index.html이 <script src="intent.js">로 쓰고, tests/test_intent.py가 node로 불러 테스트함)
// - isActionCommand: 입력이 "행동 명령"(⚡)인지 "상담 질문"(/chat)인지 키워드로만 판단 (Gemini 호출 없음)
// - josa: 받침에 맞게 조사 붙이기 ("로지텍 K295" + 을/를 → "로지텍 K295를")
// - matchOption: ⚡ ask_user 선택지 중 입력과 맞는 것 (없으면 null → 새 명령으로 처리)
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

  // 선택지 "2. 로지텍 Pebble" ↔ 입력 "2", "2번", "두 번째", "Pebble", "로지텍 pebble" 등
  const ORDINAL = { 첫: 1, 한: 1, 두: 2, 둘: 2, 세: 3, 셋: 3, 네: 4, 넷: 4, 다섯: 5, 여섯: 6 };
  const squash = s => String(s || "").toLowerCase().replace(/[\s"'“”‘’.,!?~·()\[\]-]+/g, "");
  function matchOption(text, options) {
    const list = Array.isArray(options) ? options.filter(o => typeof o === "string" && o.trim()) : [];
    const t = String(text || "").trim();
    if (!t || !list.length) return null;
    // 번호로 고름: "2", "2번", "2번째", "두 번째", "둘째"
    const m = t.match(/^(\d+)\s*(번째|번|째)?(?:\s*(?:거|것|요|으로|로|할게|할래|선택))*\s*[.!]?$/)
      || t.match(/^(첫|한|두|둘|세|셋|네|넷|다섯|여섯)\s*(번째|째|번)(?:\s*(?:거|것|요|으로|로|할게|할래|선택))*\s*[.!]?$/);
    if (m) {
      const n = /^\d+$/.test(m[1]) ? Number(m[1]) : ORDINAL[m[1]];
      const byPrefix = list.find(o => new RegExp(`^\\s*${n}\\s*[.)번:]`).test(o));
      return byPrefix || list[n - 1] || null;
    }
    // 이름으로 고름: 입력과 선택지 이름(앞 번호 뺀 것)이 같거나, 한쪽이 다른 쪽을 포함 (2글자 이상)
    const tt = squash(t);
    const name = o => squash(o.replace(/^\s*\d+\s*[.)번:]\s*/, ""));
    const exact = tt && list.find(o => name(o) === tt || squash(o) === tt);   // "네"처럼 짧아도 똑같으면 답
    if (exact) return exact;
    if (tt.length < 2) return null;
    const hits = list.filter(o => name(o).includes(tt) || (name(o).length >= 2 && tt.includes(name(o))));
    return hits.length === 1 ? hits[0] : null;   // 여러 개에 걸리면 애매하니 답으로 안 봄
  }

  root.isActionCommand = isActionCommand;
  root.josa = josa;
  root.matchOption = matchOption;
  if (typeof module !== "undefined" && module.exports) module.exports = { isActionCommand, josa, matchOption };
})(typeof window !== "undefined" ? window : globalThis);
