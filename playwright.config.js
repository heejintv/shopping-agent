// Playwright 화면 테스트: 서버 없이 정적 파일로 열어 데모 모드(demo/ 결과)로만 확인한다.
// 실행: npm ci && npx playwright install chromium && npm run test:e2e
const { defineConfig, devices } = require("@playwright/test");

const PORT = 4173;   // 5000이 아니어야 내 PC 서버 대신 데모 모드로 들어감

module.exports = defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `python3 -m http.server ${PORT} --bind 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}/index.html`,
    reuseExistingServer: !process.env.CI,
  },
});
