/**
 * T6 E2E：完整链路（主文档 T6 / task-list T6）。
 *
 * 运行前置：
 *   1. `docker compose -f deploy/compose.yaml up -d --build`（含 postgres/api/worker/web）；
 *   2. Chromium 已安装（`npx playwright install chromium`）；
 *   3. T4（HTTP 层）与 T5（报表）已落地。
 *
 * 运行：
 *   cd frontend
 *   PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 npx playwright test tests/survey-flow.spec.ts
 *
 * 环境现实（本机）：无真实第三方 key → compose 默认 mock（`MODEL_PROVIDER=mock`）。
 *
 * 设计说明（现状修复）：
 * - **单活动批次约束**（主文档 §6.2）：同一时刻只允许一个活动批次（`ready/running/
 *   pausing/paused/cancelling` 均占位）。故本文件在每个用例前后都 `cancelActiveRuns()`
 *   清理上一用例残留的活动批次，否则下一个用例 `start` 会 409 级联失败。
 * - **确定性鉴权失败**：运行时 mock 默认恒返回合法答案；`test_auth_failure_hint` 用
 *   persona_id 前缀哨兵 `__fault_auth__` 触发确定性 `ERROR_AUTH`（
 *   `app/inference/mock_provider.py` 的 `AUTH_FAULT_PERSONA_PREFIX`），从而让
 *   `pause_reason=api_auth` 链路在 mock 模式下真实可达（见 `docs/e2e-report-t6.md`）。
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

interface PersonaRow {
  user_id: string;
  age: number;
  city: string;
  gender: string;
  note: string;
}

/** 占用「活动批次」位的 run 状态（主文档 §6.2：paused 仍占位）。 */
const ACTIVE_RUN_STATUSES = ["ready", "running", "pausing", "paused", "cancelling"] as const;

/**
 * 清理所有活动批次（每个用例前后调用）。
 *
 * 单活动批次约束是**全局**的：一个用例留下的 `paused`/`running` 批次会让下一个用例的
 * `start` 返回 409（`ActiveRunConflictError`）。这里通过真实 API 取消所有活动批次并等其
 * 收敛到终态，使各用例相互隔离。取消 → `cancelling` → worker 收敛为 `cancelled`。
 */
async function cancelActiveRuns(request: APIRequestContext): Promise<void> {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const response = await request.get("/api/v1/runs?page=1&page_size=100");
    if (!response.ok()) return;
    const body = (await response.json()) as unknown;
    const record =
      typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
    const rawItems: unknown[] = Array.isArray(body)
      ? (body as unknown[])
      : Array.isArray(record.items)
        ? (record.items as unknown[])
        : [];
    const active = rawItems.flatMap((item) => {
      if (typeof item !== "object" || item === null) return [];
      const run = item as Record<string, unknown>;
      const id = typeof run.id === "string" ? run.id : "";
      const status = typeof run.status === "string" ? run.status : "";
      if (id === "" || !(ACTIVE_RUN_STATUSES as readonly string[]).includes(status)) return [];
      return [{ id, status }];
    });
    if (active.length === 0) return;
    for (const run of active) {
      await request.post(`/api/v1/runs/${run.id}/cancel`);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

test.beforeEach(async ({ request }) => {
  await cancelActiveRuns(request);
});

test.afterEach(async ({ request }) => {
  await cancelActiveRuns(request);
});

function personaRows(count: number, idPrefix = "p"): PersonaRow[] {
  const cities = ["北京", "上海", "广州", "深圳", "成都"];
  const rows: PersonaRow[] = [];
  for (let index = 1; index <= count; index += 1) {
    rows.push({
      user_id: `${idPrefix}_${String(index).padStart(6, "0")}`,
      age: 18 + (index % 50),
      city: cities[index % cities.length],
      gender: index % 2 === 0 ? "女" : "男",
      note: `备注 ${index}`,
    });
  }
  return rows;
}

function writeCsv(rows: PersonaRow[], options: { empty?: boolean } = {}): string {
  // 沙箱可能禁止写系统临时目录，可用 E2E_TMP_DIR 指向工作区内目录。
  const baseTmp = process.env.E2E_TMP_DIR ?? os.tmpdir();
  const dir = fs.mkdtempSync(path.join(baseTmp, "survey-e2e-"));
  const file = path.join(dir, "personas.csv");
  const header = "user_id,age,city,gender,note";
  const body = options.empty
    ? ""
    : rows.map((row) => `${row.user_id},${row.age},${row.city},${row.gender},${row.note}`).join("\n");
  fs.writeFileSync(file, `${header}\n${body}\n`, "utf8");
  return file;
}

/** 走完「建问卷 → 上传 → 预览 → 创建批次」，返回批次 id。 */
async function prepareRun(
  page: Page,
  csvPath: string,
  rowCount: number,
  options: { requestLimit?: string } = {},
): Promise<string> {
  // 1) 问卷：新建 + 创建
  await page.goto("/#/surveys");
  await page.getByRole("button", { name: "新建草稿" }).click();
  await page.locator("#survey-title").fill("E2E 购买意向问卷");
  await page.locator("#product-name").fill("E2E 演示产品");
  await page.locator("#product-price").fill("199.00");
  await page.locator("#product-price-unit").fill("元/件");
  await page.locator("#product-description").fill("用于 E2E 的产品说明");
  await page.getByRole("button", { name: "创建问卷" }).click();
  await expect(page.locator(".ok")).toContainText("已创建问卷");

  // 2) 上传与配置
  await page.goto("/#/runs/new");
  await page.locator("#file-input").setInputFiles(csvPath);
  await page.getByRole("button", { name: "上传并解析（不调用模型）" }).click();
  if (rowCount > 0) {
    await expect(page.locator(".ok")).toContainText(`导入成功：${rowCount} 行`);
    await expect(page.getByText("目标并发（固定，不可编辑）")).toBeVisible();
  }

  // 2b) 可选：设置 request_limit（用于制造 request_limit 耗尽的 budget 暂停）。
  if (options.requestLimit !== undefined) {
    await page.locator("#request-limit").fill(options.requestLimit);
  }

  // 3) 预览（不调用模型）
  await page.getByRole("button", { name: "预览成本与时长（不调用模型）" }).click();

  // 4) 创建批次
  await page.getByRole("button", { name: /创建批次/ }).click();
  await expect(page).toHaveURL(/#\/runs\/[0-9a-f-]{36}/);
  const runId = page.url().split("/runs/")[1].split(/[/?#]/)[0];
  return runId;
}

// ---------------------------------------------------------------------------
// §5.4 字段保真 / allowed_actions 核对辅助
// ---------------------------------------------------------------------------

/** 读取运行详情页某个 ``Metric`` 的展示值（按 label 文本定位）。 */
async function metricValue(page: Page, label: string): Promise<string> {
  return (
    await page
      .locator(".metric")
      .filter({ has: page.locator(".metric-label", { hasText: label }) })
      .locator(".metric-value")
      .first()
      .innerText()
  ).trim();
}

/** 一次性读取一组 ``Metric`` 的展示值。 */
async function readRunMetrics(
  page: Page,
  labels: readonly string[],
): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  for (const label of labels) {
    out[label] = await metricValue(page, label);
  }
  return out;
}

/** 直接取后端 ``GET /runs/{id}`` 的**原始 JSON**（前端 normalize 之外的真值源）。 */
async function fetchRawRun(
  request: APIRequestContext,
  runId: string,
): Promise<Record<string, unknown>> {
  const response = await request.get(`/api/v1/runs/${runId}`);
  expect(response.ok(), `GET /runs/${runId} -> ${response.status()}`).toBeTruthy();
  return (await response.json()) as Record<string, unknown>;
}

const asNum = (value: unknown): number => (typeof value === "number" ? value : Number(value));
const nestedCounts = (raw: Record<string, unknown>): Record<string, unknown> =>
  (raw.counts ?? {}) as Record<string, unknown>;

interface FieldSpec {
  label: string;
  expected: (raw: Record<string, unknown>) => string;
}

/**
 * 受检字段表：DOM label → 由**原始服务端 JSON** 按页面同款格式化规则推出的期望文本。
 *
 * 覆盖状态 / 并发 / 计数 / 费用 共 18 个字段；若前端对任一字段回落到兜底默认值，
 * 在「服务端原始值 ≠ 兜底默认值」时即会被逐字段比对捕获。
 */
const FIDELITY_FIELDS: readonly FieldSpec[] = [
  { label: "状态", expected: (r) => String(r.status) },
  {
    label: "pause_reason",
    expected: (r) => (r.pause_reason == null ? "—" : String(r.pause_reason)),
  },
  { label: "目标并发", expected: (r) => String(asNum(r.target_concurrency)) },
  { label: "实际在途数", expected: (r) => String(asNum(r.in_flight)) },
  { label: "限流等待数", expected: (r) => String(asNum(r.throttled)) },
  { label: "样本数（planned）", expected: (r) => String(asNum(r.sample_size)) },
  { label: "有效数（valid）", expected: (r) => String(asNum(nestedCounts(r).valid_count)) },
  { label: "成功 (succeeded)", expected: (r) => String(asNum(nestedCounts(r).succeeded)) },
  { label: "失败 (failed)", expected: (r) => String(asNum(nestedCounts(r).failed)) },
  { label: "待执行 (pending)", expected: (r) => String(asNum(nestedCounts(r).pending)) },
  { label: "重试等待 (retry_wait)", expected: (r) => String(asNum(nestedCounts(r).retry_wait)) },
  { label: "执行中 (running)", expected: (r) => String(asNum(nestedCounts(r).running)) },
  { label: "已取消 (cancelled)", expected: (r) => String(asNum(nestedCounts(r).cancelled)) },
  {
    label: "已知费用",
    expected: (r) =>
      r.actual_cost == null || r.actual_cost === ""
        ? "未知"
        : `${String(r.actual_cost)} ${String(r.budget_currency)}`,
  },
  { label: "未知费用（请求数）", expected: (r) => String(asNum(r.unknown_cost_count)) },
  {
    label: "预算上限",
    expected: (r) =>
      r.budget_limit == null ? "未设" : `${String(r.budget_limit)} ${String(r.budget_currency)}`,
  },
  {
    label: "request_limit",
    expected: (r) => (r.request_limit == null ? "—" : String(asNum(r.request_limit))),
  },
  {
    label: "requests_reserved",
    expected: (r) => (r.requests_reserved == null ? "—" : String(asNum(r.requests_reserved))),
  },
];

/** 控制按钮 → 服务端 ``allowed_actions`` 动作名（与 RunDetail 的 disabled 绑定一致）。 */
const CONTROL_BUTTONS: readonly { action: string; name: string; exact: boolean }[] = [
  { action: "start", name: "开始 / 恢复运行", exact: true },
  { action: "pause", name: "暂停", exact: true },
  { action: "resume", name: "恢复", exact: true },
  { action: "cancel", name: "取消", exact: true },
  { action: "retry-failed", name: "失败重试（幂等）", exact: true },
  { action: "increase-budget", name: "提高预算", exact: true },
];

/** 读取 6 个控制按钮的 enabled 状态（key = allowed_actions 动作名）。 */
async function controlButtonEnabled(page: Page): Promise<Record<string, boolean>> {
  const out: Record<string, boolean> = {};
  for (const button of CONTROL_BUTTONS) {
    out[button.action] = await page
      .getByRole("button", { name: button.name, exact: button.exact })
      .isEnabled();
  }
  return out;
}

test("test_full_flow", async ({ page }) => {
  const csvPath = writeCsv(personaRows(100));
  const runId = await prepareRun(page, csvPath, 100);
  expect(runId).not.toBe("");

  // 开始（此时才发起 mock 调用）
  await page.getByRole("button", { name: "开始 / 恢复运行" }).click();

  // 运行详情必须显示：目标并发 100、实际在途、限流等待、planned、valid、失败率、已知/未知费用
  await expect(page.getByText("目标并发", { exact: false })).toBeVisible();
  await expect(page.getByText("实际在途数")).toBeVisible();
  await expect(page.getByText("限流等待数")).toBeVisible();
  await expect(page.getByText("样本数（planned）")).toBeVisible();
  await expect(page.getByText("有效数（valid）")).toBeVisible();
  await expect(page.getByText("失败率（failed / planned）")).toBeVisible();
  await expect(page.getByText("已知费用")).toBeVisible();
  await expect(page.getByText("未知费用（请求数）")).toBeVisible();
  await expect(page.getByText("模拟购买意向", { exact: false })).toBeVisible();

  // 暂停 → 恢复（注意：「恢复」是「开始 / 恢复运行」的子串，必须 exact）
  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.locator(".status-pill")).toContainText(/pausing|paused/, { timeout: 60_000 });
  // 「恢复」按钮仅在该 run 为 paused 时由服务端 allowed_actions 启用；
  // toBeEnabled 会自动等待其变为可用（pausing → paused 后即启用）。
  const resumeButton = page.getByRole("button", { name: "恢复", exact: true });
  await expect(resumeButton).toBeEnabled({ timeout: 60_000 });
  await resumeButton.click();

  // 跑到完成（mock 100 人）
  await expect(page.locator(".status-pill").first()).toContainText(/completed/, { timeout: 180_000 });

  // 结果页 + 导出
  await page.getByRole("button", { name: "查看结果页 →" }).click();
  await expect(page.getByText("汇总")).toBeVisible();
  await expect(page.getByText("五档分布")).toBeVisible();
  await expect(page.getByText("Top-2-Box")).toBeVisible();

  const exportLink = page.getByRole("link", { name: /导出 CSV/ });
  await expect(exportLink).toBeVisible();
  const download = await Promise.all([page.waitForEvent("download"), exportLink.click()]);
  // 服务端导出文件名为 `run-{run_id}.csv`（见 backend/app/api/routes.py:696）。
  expect(download[0].suggestedFilename()).toMatch(/^run-.*\.csv$/);
});

test("test_empty_table", async ({ page }) => {
  const csvPath = writeCsv([], { empty: true });
  await page.goto("/#/surveys");
  await page.getByRole("button", { name: "新建草稿" }).click();
  await page.locator("#survey-title").fill("空表用例");
  await page.locator("#product-name").fill("空表产品");
  await page.locator("#product-price").fill("1.00");
  await page.locator("#product-price-unit").fill("元");
  await page.locator("#product-description").fill("空表说明");
  await page.getByRole("button", { name: "创建问卷" }).click();

  await page.goto("/#/runs/new");
  await page.locator("#file-input").setInputFiles(csvPath);
  await page.getByRole("button", { name: "上传并解析（不调用模型）" }).click();

  // 空表必须 422 INVALID_USER_TABLE，页面展示错误且不进入预览
  await expect(page.locator(".error")).toContainText("INVALID_USER_TABLE");
  await expect(page.getByRole("button", { name: /创建批次/ })).toBeDisabled();
});

test("test_budget_pause", async ({ page }) => {
  const csvPath = writeCsv(personaRows(100));
  // 把 request_limit 设到远低于样本数（100）→ 额度耗尽 → pause_reason=budget。
  // （无单价配置时金额预留为 0，故用 request_limit 而非 budget_limit 触发暂停。）
  await prepareRun(page, csvPath, 100, { requestLimit: "10" });
  await page.getByRole("button", { name: "开始 / 恢复运行" }).click();

  // 预算/额度不足 → pause_reason=budget，页面显示可操作说明
  await expect(page.locator(".status-pill").first()).toContainText(/paused|pausing/, { timeout: 120_000 });
  await expect(page.getByText("预算或请求额度不足，请提高预算后恢复")).toBeVisible();
  // 「提高预算」仅在该 run 为 paused 时启用；toBeEnabled 会自动等到 paused。
  await expect(page.getByRole("button", { name: "提高预算" })).toBeEnabled({ timeout: 60_000 });
});

test("test_auth_failure_hint", async ({ page }) => {
  // persona_id 前缀哨兵 → 运行时 mock 确定性返回 ERROR_AUTH → pause_reason=api_auth。
  const csvPath = writeCsv(personaRows(100, "__fault_auth__"));
  await prepareRun(page, csvPath, 100);
  await page.getByRole("button", { name: "开始 / 恢复运行" }).click();

  // 认证失败场景下，页面必须显示可操作说明（来自服务端 pause_hint / pause_reason）。
  await expect(page.getByText("API 认证失败，请修复服务端模型配置后恢复")).toBeVisible({
    timeout: 120_000,
  });
});

test("test_page_close_task_continues", async ({ page, context }) => {
  const csvPath = writeCsv(personaRows(100));
  const runId = await prepareRun(page, csvPath, 100);
  await page.getByRole("button", { name: "开始 / 恢复运行" }).click();
  await page.waitForTimeout(2000);

  // 关闭页面后任务必须继续（worker 独立进程，不依赖浏览器）
  await page.close();

  const reopened = await context.newPage();
  await reopened.goto(`/#/runs/${runId}`);
  await expect(reopened.locator(".status-pill").first()).toContainText(
    /running|completed|completed_with_errors|paused/,
  );
});

// ---------------------------------------------------------------------------
// §5.4 第 3 项：失败不可隐藏（completed_with_errors 原样返回）
// ---------------------------------------------------------------------------

test("test_failures_not_hidden", async ({ page }) => {
  // 60 正常 + 40 确定性非法输出（persona_id 前缀哨兵）→ succeeded>0 且 failed>0。
  const rows = [...personaRows(60, "ok"), ...personaRows(40, "__fault_invalid__")];
  const csvPath = writeCsv(rows);
  await prepareRun(page, csvPath, rows.length);
  await page.getByRole("button", { name: "开始 / 恢复运行" }).click();

  // 终态必须是 completed_with_errors（**原样返回**，不得被强制成 completed）。
  await expect(page.locator(".status-pill").first()).toContainText(/completed_with_errors/, {
    timeout: 180_000,
  });
  // 状态字段逐字为 completed_with_errors（非 completed）。
  expect(await metricValue(page, "状态")).toBe("completed_with_errors");

  // 失败数必须可见且 > 0（不隐去）。
  expect(Number(await metricValue(page, "失败 (failed)"))).toBeGreaterThan(0);
  // 有效数 > 0。
  expect(Number(await metricValue(page, "有效数（valid）"))).toBeGreaterThan(0);

  // 错误摘要可见，且为真实错误码 INVALID_OUTPUT（非空、非占位）。
  await expect(page.getByText(/错误摘要/)).toBeVisible();
  await expect(page.getByText(/INVALID_OUTPUT/)).toBeVisible();
});

// ---------------------------------------------------------------------------
// §5.4 第 4 项：前端 normalize 兜底值逐字段核对（后端原始 JSON vs 渲染值）
// ---------------------------------------------------------------------------

test("test_field_fidelity_vs_raw", async ({ page, request }) => {
  // 460 正常 + 40 非法：制造「在途 > 0」与「限流等待 > 0」并存的运行期，
  // 以及失败成员（终态 failed>0）。
  const rows = [...personaRows(460, "ok"), ...personaRows(40, "__fault_invalid__")];
  const csvPath = writeCsv(rows);
  const runId = await prepareRun(page, csvPath, rows.length);
  await page.getByRole("button", { name: "开始 / 恢复运行" }).click();
  await expect(page.locator(".status-pill").first()).toContainText(/running/, { timeout: 60_000 });

  const labels = FIDELITY_FIELDS.map((field) => field.label);
  const matchedEver = new Map<string, number>();
  let inFlightNonZeroMatched = false;
  let throttledNonZeroMatched = false;
  let inFlightSnapshot: Record<string, { dom: string; raw: string }> | null = null;
  let throttledSnapshot: Record<string, { dom: string; raw: string }> | null = null;

  for (let iteration = 0; iteration < 80; iteration += 1) {
    // DOM 读取被 rawBefore/rawAfter 夹住，容忍 3s 轮询的一拍错位。
    const rawBefore = await fetchRawRun(request, runId);
    const dom = await readRunMetrics(page, labels);
    const rawAfter = await fetchRawRun(request, runId);

    for (const field of FIDELITY_FIELDS) {
      const candidates = [field.expected(rawBefore), field.expected(rawAfter)];
      if (candidates.includes(dom[field.label])) {
        matchedEver.set(field.label, (matchedEver.get(field.label) ?? 0) + 1);
      }
    }

    // 证伪「in_flight/throttled 恒为 0 的假兜底」：服务端 >0 时前端须如实渲染同一值。
    const domInFlight = Number(dom["实际在途数"]);
    if (
      domInFlight > 0 &&
      (asNum(rawBefore.in_flight) === domInFlight || asNum(rawAfter.in_flight) === domInFlight)
    ) {
      inFlightNonZeroMatched = true;
      if (inFlightSnapshot === null) {
        inFlightSnapshot = Object.fromEntries(
          FIDELITY_FIELDS.map((field) => [
            field.label,
            { dom: dom[field.label], raw: field.expected(rawBefore) },
          ]),
        );
      }
    }
    const domThrottled = Number(dom["限流等待数"]);
    if (
      domThrottled > 0 &&
      (asNum(rawBefore.throttled) === domThrottled || asNum(rawAfter.throttled) === domThrottled)
    ) {
      throttledNonZeroMatched = true;
      if (throttledSnapshot === null) {
        throttledSnapshot = Object.fromEntries(
          FIDELITY_FIELDS.map((field) => [
            field.label,
            { dom: dom[field.label], raw: field.expected(rawBefore) },
          ]),
        );
      }
    }

    if (!["running", "pausing"].includes(String(rawAfter.status))) break;
    await page.waitForTimeout(400);
  }

  // 每个受检字段都至少一次等于某真实服务端快照值 → 无捏造兜底。
  for (const field of FIDELITY_FIELDS) {
    expect(
      matchedEver.get(field.label) ?? 0,
      `字段「${field.label}」从未匹配过服务端原始值`,
    ).toBeGreaterThan(0);
  }
  expect(inFlightNonZeroMatched, "服务端在途数>0 时前端未如实渲染").toBe(true);
  expect(throttledNonZeroMatched, "服务端限流等待>0 时前端未如实渲染").toBe(true);

  // 运行期落一份**同刻**完整快照（可入报告）。
  if (inFlightSnapshot !== null) {
    console.log(`FIELD_FIDELITY_INFLIGHT=${JSON.stringify(inFlightSnapshot)}`);
  }
  if (throttledSnapshot !== null) {
    console.log(`FIELD_FIDELITY_THROTTLED=${JSON.stringify(throttledSnapshot)}`);
  }

  // 终态（稳定）再做一次逐字段**全量精确**比对：若任一字段被兜底默认值掩盖，此处即失败。
  await expect(page.locator(".status-pill").first()).toContainText(/completed/, {
    timeout: 180_000,
  });
  const rawFinal = await fetchRawRun(request, runId);
  const domFinal = await readRunMetrics(page, labels);
  const finalTable: Record<string, { dom: string; raw: string }> = {};
  for (const field of FIDELITY_FIELDS) {
    const expected = field.expected(rawFinal);
    finalTable[field.label] = { dom: domFinal[field.label], raw: expected };
    expect(domFinal[field.label], `终态字段「${field.label}」`).toBe(expected);
  }
  console.log(`FIELD_FIDELITY_FINAL=${JSON.stringify(finalTable)}`);
});

// ---------------------------------------------------------------------------
// §5.4 第 5 项：allowed_actions 由服务端下发，按钮启用态与 API 一致
// ---------------------------------------------------------------------------

test("test_allowed_actions_drive_buttons", async ({ page, request }) => {
  const csvPath = writeCsv(personaRows(100));
  const runId = await prepareRun(page, csvPath, 100);

  async function assertButtonsMatchApi(): Promise<void> {
    const raw = await fetchRawRun(request, runId);
    const allowed = new Set((raw.allowed_actions as unknown[]).map(String));
    const enabled = await controlButtonEnabled(page);
    for (const button of CONTROL_BUTTONS) {
      expect(
        enabled[button.action],
        `状态 ${String(raw.status)}：按钮「${button.name}」启用态与 allowed_actions 不符`,
      ).toBe(allowed.has(button.action));
    }
  }

  // ready：start/cancel/increase-budget 可用；pause/resume/retry-failed 不可用。
  await assertButtonsMatchApi();

  // running：pause/cancel/increase-budget 可用；start/resume/retry-failed 不可用。
  await page.getByRole("button", { name: "开始 / 恢复运行" }).click();
  await expect(page.locator(".status-pill").first()).toContainText(/running/, { timeout: 60_000 });
  await assertButtonsMatchApi();

  // paused：resume/cancel/increase-budget 可用；start/pause/retry-failed 不可用。
  await page.getByRole("button", { name: "暂停", exact: true }).click();
  await expect(page.locator(".status-pill").first()).toContainText(/paused/, { timeout: 60_000 });
  await assertButtonsMatchApi();
});
