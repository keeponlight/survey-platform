// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * 统一 API 封装（`/api/v1`，主文档 §8.2）。
 *
 * 约定：
 * - 时间一律 ISO 8601 UTC（后端产出），前端按本地时区显示。
 * - 错误体形如 `{"code","message","details"}`，封装为 {@link ApiError}。
 * - 金额为字符串（后端 Decimal），`null` 表示**未知**（不等于 0）。
 * - 本文件是前端唯一直接接触 HTTP 的地方；页面只依赖这里导出的类型与函数。
 */

export const API_BASE = "/api/v1";

// ---------------------------------------------------------------------------
// 错误
// ---------------------------------------------------------------------------

export interface ErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, body: ErrorBody) {
    super(body.message || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code || "UNKNOWN";
    this.details = body.details ?? {};
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function asNumberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: ErrorBody = { code: `HTTP_${response.status}`, message: `HTTP ${response.status}` };
  try {
    const parsed: unknown = await response.json();
    const record = asRecord(parsed);
    body = {
      code: asString(record.code, `HTTP_${response.status}`),
      message: asString(record.message, `HTTP ${response.status}`),
      details: asRecord(record.details),
    };
  } catch {
    // 非 JSON 错误体：保留状态码语义
  }
  return new ApiError(response.status, body);
}

async function parseJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") return {};
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError(response.status, {
      code: "INVALID_JSON",
      message: "服务端返回了非 JSON 响应",
    });
  }
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
    ...init,
  });
  if (!response.ok) {
    throw await toApiError(response);
  }
  return response;
}

async function getJson(path: string): Promise<unknown> {
  return parseJson(await request(path));
}

async function sendJson(method: "POST" | "PATCH", path: string, body: unknown, headers?: Record<string, string>): Promise<unknown> {
  return parseJson(
    await request(path, {
      method,
      headers: { "Content-Type": "application/json", ...(headers ?? {}) },
      body: JSON.stringify(body),
    }),
  );
}

// ---------------------------------------------------------------------------
// 领域类型
// ---------------------------------------------------------------------------

export const QUESTION_ID = "purchase_intent";
export const PROMPT_VERSION = "purchase-intent-zh-v1";
/** 固定目标并发（100 = 槽位数，不是样本数）。 */
export const TARGET_CONCURRENCY = 100;

export interface QuestionOption {
  value: string;
  label: string;
  score: number;
}

export interface Question {
  question_id: string;
  prompt: string;
  type: string;
  options: QuestionOption[];
  required: boolean;
}

export interface Product {
  name: string;
  description: string;
  price: string;
  price_unit: string;
  time_range: string;
  purchase_conditions: string | null;
}

export interface Survey {
  id: string;
  title: string;
  product: Product;
  question: Question;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface SurveyInputPayload {
  title: string;
  product: Product;
  question: Question;
}

export interface PersonaSnapshot {
  row_no: number;
  persona_id: string;
  profile_text: string;
  age: number | null;
  city_tier: string | null;
  source_version: string | null;
}

export interface RowError {
  message: string;
  sheet?: string;
  row_no?: number;
  column?: string;
}

export interface ImportPreview {
  import_id: string;
  row_count: number;
  sheet_name: string | null;
  source_version: string;
  preview: PersonaSnapshot[];
  errors: RowError[];
}

export interface ColumnMapping {
  /** 唯一用户 ID 列。 */
  persona_id: string;
  /** 拼接画像文本的列（按顺序确定性拼接）。 */
  profile_text_columns: string[];
  /** 可选：年龄列（用于分组统计）。 */
  age?: string;
  /** 可选：城市/城市等级列。 */
  city_tier?: string;
}

export type RunStatus =
  | "ready"
  | "running"
  | "pausing"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "completed_with_errors"
  | "failed";

export type PauseReason = "user" | "api_auth" | "api_unavailable" | "budget";

export type RunAction =
  | "start"
  | "pause"
  | "resume"
  | "cancel"
  | "retry-failed"
  | "increase-budget";

export interface RunCounts {
  succeeded: number;
  failed: number;
  pending: number;
  running: number;
  retry_wait: number;
  cancelled: number;
  /** 有效数 = succeeded（主文档 §8.3）。 */
  valid: number;
}

export interface RunCosts {
  /** 已知费用（字符串 Decimal）；null = 未知。 */
  known: string | null;
  /** 未知计费的请求数（unknown 不写成 0）。 */
  unknownCount: number;
  currency: string;
}

export interface RunView {
  id: string;
  surveyId: string;
  importId: string;
  status: RunStatus;
  pauseReason: PauseReason | null;
  pauseHint: string | null;
  errorSummary: string | null;
  /** 冻结的提示词版本。 */
  promptVersion: string;
  /** 目标并发（固定 100）。 */
  targetConcurrency: number;
  /** 实际在途请求数。 */
  inFlight: number;
  /** 限流等待数。 */
  throttled: number;
  sampleSize: number;
  counts: RunCounts;
  costs: RunCosts;
  budgetLimit: string | null;
  budgetCurrency: string;
  requestLimit: number | null;
  requestsReserved: number | null;
  allowedActions: RunAction[];
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface RunPreview {
  sampleSize: number;
  targetConcurrency: number;
  estimatedCost: string | null;
  costIsUnknown: boolean;
  estimatedDurationSeconds: number | null;
  promptPreviews: string[];
}

export interface ResultsQuery {
  page?: number;
  pageSize?: number;
  memberStatus?: string;
}

export interface MemberRow {
  id: string;
  /** 数据行号（1 起，不含表头）。 */
  rowNo: number;
  personaId: string;
  age: number | null;
  cityTier: string | null;
  memberStatus: string;
  value: string | null;
  score: number | null;
  reason: string | null;
  attemptCount: number;
  errorCode: string | null;
}

export interface ResultsPage {
  items: MemberRow[];
  total: number;
  page: number;
  pageSize: number;
}

export interface BucketCount {
  value: string;
  label: string;
  score: number;
  count: number;
  rate: number | null;
}

export interface GroupStat {
  label: string;
  planned: number;
  valid: number;
  coverage: number | null;
  distribution: Record<string, number>;
}

export interface Summary {
  sampleSize: number;
  validCount: number;
  failedCount: number;
  coverage: number | null;
  top2box: number | null;
  meanScore: number | null;
  buckets: BucketCount[];
  ageGroups: GroupStat[] | null;
  cityGroups: GroupStat[] | null;
  reportRevision: string;
  asOf: string;
  isPartial: boolean;
}

export interface RunCreatePayload {
  survey_id: string;
  survey_revision: number;
  import_id: string;
  model_config_id: string;
  budget_limit?: string;
  budget_currency?: string;
  request_limit?: number;
}

// ---------------------------------------------------------------------------
// 归一化（后端 T4 尚未落地时按主文档 §8.2/§8.3 字段名解析，兼容扁平/嵌套）
// ---------------------------------------------------------------------------

const PAUSE_HINTS: Record<PauseReason, string> = {
  user: "已按操作暂停。点击「恢复」继续剩余任务。",
  api_auth: "API 认证失败，请修复服务端模型配置后恢复。",
  api_unavailable: "模型服务暂不可用或持续限流，请稍后恢复。",
  budget: "预算或请求额度不足，请提高预算后恢复。",
};

function normalizeRun(raw: unknown): RunView {
  const record = asRecord(raw);
  const nestedCounts = asRecord(record.counts);
  const nestedCosts = asRecord(record.costs);
  const pick = (key: string, fallbackRecord: Record<string, unknown>): unknown =>
    record[key] !== undefined ? record[key] : fallbackRecord[key];

  const succeeded = asNumber(pick("succeeded", nestedCounts));
  const counts: RunCounts = {
    succeeded,
    failed: asNumber(pick("failed", nestedCounts)),
    pending: asNumber(pick("pending", nestedCounts)),
    running: asNumber(pick("running", nestedCounts)),
    retry_wait: asNumber(pick("retry_wait", nestedCounts)),
    cancelled: asNumber(pick("cancelled", nestedCounts)),
    valid: asNumber(pick("valid_count", nestedCounts), succeeded),
  };

  const currency = asString(pick("budget_currency", record), "CNY");
  const costs: RunCosts = {
    known: asStringOrNull(pick("actual_cost", nestedCosts)),
    unknownCount: asNumber(pick("unknown_cost_count", nestedCosts)),
    currency,
  };

  const pauseReason = asStringOrNull(record.pause_reason) as PauseReason | null;
  const inFlightRaw = pick("in_flight", record);
  const throttledRaw = pick("throttled", record);

  return {
    id: asString(record.id),
    surveyId: asString(record.survey_id),
    importId: asString(record.import_id),
    status: asString(record.status, "ready") as RunStatus,
    pauseReason,
    pauseHint:
      asStringOrNull(record.pause_hint) ?? (pauseReason !== null ? PAUSE_HINTS[pauseReason] : null),
    errorSummary: asStringOrNull(record.error_summary),
    promptVersion: asString(record.prompt_version, PROMPT_VERSION),
    targetConcurrency: asNumber(pick("target_concurrency", record), TARGET_CONCURRENCY),
    inFlight: asNumberOrNull(inFlightRaw) ?? counts.running,
    throttled: asNumberOrNull(throttledRaw) ?? counts.retry_wait,
    sampleSize: asNumber(record.sample_size),
    counts,
    costs,
    budgetLimit: asStringOrNull(record.budget_limit),
    budgetCurrency: currency,
    requestLimit: asNumberOrNull(record.request_limit),
    requestsReserved: asNumberOrNull(record.requests_reserved),
    allowedActions: asArray<RunAction>(record.allowed_actions),
    createdAt: asString(record.created_at),
    startedAt: asStringOrNull(record.started_at),
    finishedAt: asStringOrNull(record.finished_at),
  };
}

function normalizeImportPreview(raw: unknown): ImportPreview {
  const record = asRecord(raw);
  return {
    import_id: asString(record.import_id),
    row_count: asNumber(record.row_count),
    sheet_name: asStringOrNull(record.sheet_name),
    source_version: asString(record.source_version),
    preview: asArray<unknown>(record.preview).map((item) => {
      const snap = asRecord(item);
      return {
        row_no: asNumber(snap.row_no),
        persona_id: asString(snap.persona_id),
        profile_text: asString(snap.profile_text),
        age: asNumberOrNull(snap.age),
        city_tier: asStringOrNull(snap.city_tier),
        source_version: asStringOrNull(snap.source_version),
      };
    }),
    errors: asArray<unknown>(record.errors).map((item) => {
      const err = asRecord(item);
      return {
        message: asString(err.message),
        sheet: asStringOrNull(err.sheet) ?? undefined,
        row_no: asNumberOrNull(err.row_no) ?? undefined,
        column: asStringOrNull(err.column) ?? undefined,
      };
    }),
  };
}

function normalizeSurvey(raw: unknown): Survey {
  const record = asRecord(raw);
  const product = asRecord(record.product);
  const question = asRecord(record.question);
  return {
    id: asString(record.id),
    title: asString(record.title),
    product: {
      name: asString(product.name),
      description: asString(product.description),
      price: asString(product.price),
      price_unit: asString(product.price_unit),
      time_range: asString(product.time_range),
      purchase_conditions: asStringOrNull(product.purchase_conditions),
    },
    question: {
      question_id: asString(question.question_id, QUESTION_ID),
      prompt: asString(question.prompt),
      type: asString(question.type, "single_choice"),
      options: asArray<unknown>(question.options).map((item) => {
        const option = asRecord(item);
        return {
          value: asString(option.value),
          label: asString(option.label),
          score: asNumber(option.score),
        };
      }),
      required: question.required === undefined ? true : Boolean(question.required),
    },
    revision: asNumber(record.revision, 1),
    created_at: asString(record.created_at),
    updated_at: asString(record.updated_at),
  };
}

function normalizeRunPreview(raw: unknown): RunPreview {
  const record = asRecord(raw);
  const estimatedCost = asStringOrNull(record.estimated_cost);
  return {
    sampleSize: asNumber(record.sample_size),
    targetConcurrency: asNumber(record.target_concurrency, TARGET_CONCURRENCY),
    estimatedCost,
    costIsUnknown: record.cost_is_unknown === true || estimatedCost === null,
    estimatedDurationSeconds: asNumberOrNull(record.estimated_duration_seconds),
    promptPreviews: asArray<unknown>(record.prompt_previews).map((item) =>
      typeof item === "string" ? item : JSON.stringify(item),
    ),
  };
}

function normalizeMemberRow(raw: unknown): MemberRow {
  const record = asRecord(raw);
  const answer = asRecord(record.answer_json);
  const value = record.value !== undefined ? record.value : answer.value;
  return {
    id: asString(record.id),
    rowNo: asNumber(record.row_no),
    personaId: asString(record.persona_id),
    age: asNumberOrNull(record.age),
    cityTier: asStringOrNull(record.city_tier),
    memberStatus: asString(record.member_status ?? record.status),
    value: typeof value === "string" ? value : null,
    score: asNumberOrNull(record.score ?? answer.score),
    reason: asStringOrNull(record.reason ?? answer.reason),
    attemptCount: asNumber(record.attempt_count),
    errorCode: asStringOrNull(record.error_code),
  };
}

function normalizeResults(raw: unknown): ResultsPage {
  const record = asRecord(raw);
  return {
    items: asArray<unknown>(record.items).map(normalizeMemberRow),
    total: asNumber(record.total),
    page: asNumber(record.page, 1),
    pageSize: asNumber(record.page_size, 50),
  };
}

function normalizeSummary(raw: unknown): Summary {
  const record = asRecord(raw);
  const groups = asRecord(record.groups);
  const toGroups = (value: unknown): GroupStat[] | null => {
    if (!Array.isArray(value)) return null;
    return value.map((item) => {
      const group = asRecord(item);
      return {
        label: asString(group.label),
        planned: asNumber(group.planned),
        valid: asNumber(group.valid),
        coverage: asNumberOrNull(group.coverage),
        distribution: asRecord(group.distribution) as unknown as Record<string, number>,
      };
    });
  };
  return {
    sampleSize: asNumber(record.sample_size),
    validCount: asNumber(record.valid_count),
    failedCount: asNumber(record.failed_count),
    coverage: asNumberOrNull(record.coverage),
    top2box: asNumberOrNull(record.top2box),
    meanScore: asNumberOrNull(record.mean_score),
    buckets: asArray<unknown>(record.buckets).map((item) => {
      const bucket = asRecord(item);
      return {
        value: asString(bucket.value),
        label: asString(bucket.label),
        score: asNumber(bucket.score),
        count: asNumber(bucket.count),
        rate: asNumberOrNull(bucket.rate),
      };
    }),
    ageGroups: toGroups(groups.age ?? record.age_groups),
    cityGroups: toGroups(groups.city_tier ?? record.city_groups),
    reportRevision: asString(record.report_revision),
    asOf: asString(record.as_of),
    isPartial: record.is_partial === true,
  };
}

// ---------------------------------------------------------------------------
// 端点
// ---------------------------------------------------------------------------

export interface QuotaEstimate {
  rpm: number;
  tpm: number;
  agentConcurrency: number;
}

export const api = {
  // 健康检查
  async healthLive(): Promise<unknown> {
    return getJson("/health/live");
  },
  async healthReady(): Promise<unknown> {
    return getJson("/health/ready");
  },

  // 问卷
  async listSurveys(page = 1, pageSize = 20): Promise<{ items: Survey[]; total: number }> {
    const raw = asRecord(
      await getJson(`/surveys?page=${page}&page_size=${pageSize}`),
    );
    const items = Array.isArray(raw.items)
      ? raw.items
      : Array.isArray(raw)
        ? (raw as unknown[])
        : [];
    return { items: items.map(normalizeSurvey), total: asNumber(raw.total, items.length) };
  },
  async getSurvey(id: string): Promise<Survey> {
    return normalizeSurvey(await getJson(`/surveys/${id}`));
  },
  async createSurvey(payload: SurveyInputPayload): Promise<Survey> {
    return normalizeSurvey(await sendJson("POST", "/surveys", payload));
  },
  async patchSurvey(id: string, expectedRevision: number, payload: SurveyInputPayload): Promise<Survey> {
    return normalizeSurvey(
      await sendJson("PATCH", `/surveys/${id}`, { ...payload, expected_revision: expectedRevision }),
    );
  },

  // 导入
  async createImport(file: File, sheet: string | null, mapping: ColumnMapping): Promise<ImportPreview> {
    const form = new FormData();
    form.append("file", file);
    if (sheet !== null && sheet !== "") form.append("sheet", sheet);
    form.append("column_mapping", JSON.stringify(mapping));
    return normalizeImportPreview(
      await parseJson(await request("/imports", { method: "POST", body: form })),
    );
  },
  async getImport(id: string): Promise<ImportPreview> {
    return normalizeImportPreview(await getJson(`/imports/${id}`));
  },

  // 运行
  async previewRun(surveyId: string, surveyRevision: number, importId: string, modelConfigId: string): Promise<RunPreview> {
    return normalizeRunPreview(
      await sendJson("POST", "/runs/preview", {
        survey_id: surveyId,
        survey_revision: surveyRevision,
        import_id: importId,
        model_config_id: modelConfigId,
      }),
    );
  },
  async createRun(payload: RunCreatePayload, idempotencyKey: string): Promise<RunView> {
    return normalizeRun(
      await sendJson("POST", "/runs", payload, { "Idempotency-Key": idempotencyKey }),
    );
  },
  async listRuns(): Promise<RunView[]> {
    const raw = await getJson("/runs");
    const record = asRecord(raw);
    const items = Array.isArray(raw) ? (raw as unknown[]) : asArray<unknown>(record.items);
    return items.map(normalizeRun);
  },
  async getRun(id: string): Promise<RunView> {
    return normalizeRun(await getJson(`/runs/${id}`));
  },
  async controlRun(id: string, action: "start" | "pause" | "resume" | "cancel"): Promise<RunView> {
    return normalizeRun(await sendJson("POST", `/runs/${id}/${action}`, {}));
  },
  async retryFailed(id: string, idempotencyKey: string): Promise<RunView> {
    return normalizeRun(
      await sendJson("POST", `/runs/${id}/retry-failed`, {}, { "Idempotency-Key": idempotencyKey }),
    );
  },
  async increaseBudget(id: string, budgetLimit: string): Promise<RunView> {
    return normalizeRun(await sendJson("PATCH", `/runs/${id}/budget`, { budget_limit: budgetLimit }));
  },

  // 结果 / 报表
  async getResults(id: string, query: ResultsQuery = {}): Promise<ResultsPage> {
    const params = new URLSearchParams();
    params.set("page", String(query.page ?? 1));
    params.set("page_size", String(query.pageSize ?? 50));
    if (query.memberStatus) params.set("status", query.memberStatus);
    return normalizeResults(await getJson(`/runs/${id}/results?${params.toString()}`));
  },
  async getSummary(id: string): Promise<Summary> {
    return normalizeSummary(await getJson(`/runs/${id}/summary`));
  },
  /** CSV 导出直链（浏览器下载，带 UTF-8 BOM；失败行 value 为空）。 */
  exportCsvUrl(id: string): string {
    return `${API_BASE}/runs/${id}/export.csv`;
  },
};
