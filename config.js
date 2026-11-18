// 검색 서버 주소. 끝에 / 없이 적으세요. 비워 두면 그 단계는 건너뜁니다.
// 연결 순서: 내 PC(localhost:5000) → RENDER_URL → SERVER_URL(ngrok) → 모두 안 되면 데모 모드
window.APP_CONFIG = {
  // Render 무료 웹 서비스 주소 (예: "https://pickit.onrender.com"). 배포 후 Render 대시보드에 나온 주소로 바꾸세요
  RENDER_URL: "https://pickit-31qt.onrender.com",
  // ngrok 고정 도메인 (내 PC 서버를 공개할 때)
  SERVER_URL: "https://eternity-generic-diligent.ngrok-free.dev",
};
