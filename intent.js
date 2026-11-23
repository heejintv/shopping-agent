// 챗봇 입력이 "행동 명령"(⚡ /chat/action)인지 "상담 질문"(/chat)인지 키워드로만 판단 (Gemini 호출 없음)
// index.html이 <script src="intent.js">로 쓰고, tests/test_intent.py가 node로 불러 테스트함
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

  root.isActionCommand = isActionCommand;
  if (typeof module !== "undefined" && module.exports) module.exports = { isActionCommand };
})(typeof window !== "undefined" ? window : globalThis);
