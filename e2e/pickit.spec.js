// 외부 요청(내 PC·Render·ngrok 서버, Supabase, CDN)은 모두 가로채서 가짜로 응답한다.
// → Claude·Gemini·Supabase를 실제로 부르지 않고, 연결 실패로 데모 모드에 들어간다.
const { test, expect } = require("@playwright/test");

const EMPTY = { script: "application/javascript", stylesheet: "text/css" };

test.beforeEach(async ({ page, baseURL }) => {
  await page.route("**/*", (route) => {
    const req = route.request();
    if (req.url().startsWith(baseURL)) return route.continue();
    if (new URL(req.url()).pathname.endsWith("/health")) {
      return route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":false}' });
    }
    return route.fulfill({ status: 200, contentType: EMPTY[req.resourceType()] || "text/plain", body: "" });
  });
});

async function openHome(page, path = "/") {
  await page.goto(path);
  await expect(page.locator("#demo")).toBeVisible();   // 데모 모드 진입
}

async function openRecent(page, query) {
  const chip = page.locator(".chip", { hasText: query });
  const more = page.getByRole("button", { name: /더보기/ });
  if (!(await chip.isVisible()) && (await more.isVisible())) await more.click();
  await chip.click();
}

test("1. 첫 화면이 열리고 콘솔 에러가 0건", async ({ page }) => {
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(String(e)));
  await openHome(page);
  await expect(page.getByRole("heading", { name: "최근 검색" })).toBeVisible();
  await expect(page.locator(".chip").first()).toBeVisible();
  expect(errors).toEqual([]);
});

test("2. 최근 검색 '무선 마우스' → 추천 카드 3장과 비교표", async ({ page }) => {
  await openHome(page);
  await openRecent(page, "무선 마우스");
  await expect(page.locator("#picks > *")).toHaveCount(3);
  await expect(page.locator("#table")).toBeVisible();
  expect(await page.locator("#table tbody tr").count()).toBeGreaterThan(0);
});

test("3. 비로그인으로 💬 → 로그인 안내, 입력칸 없음", async ({ page }) => {
  await openHome(page);
  await page.getByRole("button", { name: "상담 챗봇 열기" }).click();
  await expect(page.locator(".chat-gate")).toContainText("로그인하고 상담받기");
  await expect(page.locator("#chatIn")).toBeHidden();
});

test("4. 공유 링크(?q=무선 마우스)로 들어오면 결과가 바로 나옴", async ({ page }) => {
  await openHome(page, "/?q=" + encodeURIComponent("무선 마우스"));
  await expect(page.locator("#picks > *")).toHaveCount(3);
  await expect(page.locator("#table")).toBeVisible();
});

test("5. 휴대폰 크기(390px)에서 가로 스크롤 없음", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openHome(page, "/?q=" + encodeURIComponent("무선 마우스"));
  await expect(page.locator("#picks > *")).toHaveCount(3);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
