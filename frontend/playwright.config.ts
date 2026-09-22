import { defineConfig } from "@playwright/test";

// E2E 目标：compose 起的 web（nginx，默认 8080），经其反代到 api。
// 可用 PLAYWRIGHT_BASE_URL 覆盖（例如本地 `npm run dev` + api 直连时）。
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:8080";

export default defineConfig({
  testDir: "./tests",
  timeout: 120_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL,
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  // 不自动拉起 webServer：E2E 依赖 compose 已启动（含 postgres/api/worker）。
  // 运行前请执行：docker compose -f deploy/compose.yaml up -d --build
});
