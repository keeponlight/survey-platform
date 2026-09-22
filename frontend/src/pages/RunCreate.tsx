// SPDX-License-Identifier: GPL-3.0-or-later
import { useEffect, useMemo, useState } from "react";

import {
  TARGET_CONCURRENCY,
  PROMPT_VERSION,
  api,
  type ColumnMapping,
  type ImportPreview,
  type RunPreview,
  type Survey,
} from "../api";
import { ErrorBanner, Metric, Notice } from "../components";
import { routes } from "../router";

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `run-${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`;
}

function parseColumns(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

export default function RunCreate({ initialSurveyId }: { initialSurveyId: string | null }): JSX.Element {
  const [surveys, setSurveys] = useState<Survey[]>([]);
  const [surveyId, setSurveyId] = useState<string>(initialSurveyId ?? "");
  const [modelConfigId, setModelConfigId] = useState<string>("default");
  const [budgetLimit, setBudgetLimit] = useState<string>("");
  const [requestLimit, setRequestLimit] = useState<string>("");

  const [file, setFile] = useState<File | null>(null);
  const [sheet, setSheet] = useState<string>("");
  const [idColumn, setIdColumn] = useState<string>("user_id");
  const [profileColumns, setProfileColumns] = useState<string>("age, city, gender, note");
  const [ageColumn, setAgeColumn] = useState<string>("age");
  const [cityColumn, setCityColumn] = useState<string>("city");

  const [importPreview, setImportPreview] = useState<ImportPreview | null>(null);
  const [runPreview, setRunPreview] = useState<RunPreview | null>(null);

  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [idempotencyKey] = useState<string>(() => newIdempotencyKey());

  const selectedSurvey = useMemo(
    () => surveys.find((survey) => survey.id === surveyId) ?? null,
    [surveys, surveyId],
  );

  useEffect(() => {
    api
      .listSurveys()
      .then((result) => {
        setSurveys(result.items);
        if (initialSurveyId === null && result.items.length > 0) {
          setSurveyId(result.items[0].id);
        }
      })
      .catch((err: unknown) => setError(err));
  }, [initialSurveyId]);

  function mapping(): ColumnMapping {
    const result: ColumnMapping = {
      persona_id: idColumn.trim(),
      profile_text_columns: parseColumns(profileColumns),
    };
    if (ageColumn.trim() !== "") result.age = ageColumn.trim();
    if (cityColumn.trim() !== "") result.city_tier = cityColumn.trim();
    return result;
  }

  async function handleUpload(): Promise<void> {
    if (file === null) {
      setError(new Error("请先选择 CSV 或 XLSX 文件"));
      return;
    }
    setBusy(true);
    setRunPreview(null);
    setInfo("");
    try {
      const preview = await api.createImport(file, sheet.trim() === "" ? null : sheet.trim(), mapping());
      setImportPreview(preview);
      setError(null);
      setInfo(`导入成功：${preview.row_count} 行。`);
    } catch (err: unknown) {
      setImportPreview(null);
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function handlePreview(): Promise<void> {
    if (selectedSurvey === null || importPreview === null) {
      setError(new Error("请先选择问卷并完成导入"));
      return;
    }
    setBusy(true);
    setInfo("");
    try {
      const preview = await api.previewRun(
        selectedSurvey.id,
        selectedSurvey.revision,
        importPreview.import_id,
        modelConfigId.trim(),
      );
      setRunPreview(preview);
      setError(null);
      setInfo("预览完成（未调用模型）。");
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateRun(): Promise<void> {
    if (selectedSurvey === null || importPreview === null) {
      setError(new Error("请先选择问卷并完成导入"));
      return;
    }
    setBusy(true);
    setInfo("");
    try {
      const payload = {
        survey_id: selectedSurvey.id,
        survey_revision: selectedSurvey.revision,
        import_id: importPreview.import_id,
        model_config_id: modelConfigId.trim(),
        budget_currency: "CNY",
        ...(budgetLimit.trim() !== "" ? { budget_limit: budgetLimit.trim() } : {}),
        ...(requestLimit.trim() !== "" ? { request_limit: Number(requestLimit.trim()) } : {}),
      };
      // 同一幂等键 → 重复点击只产生一个批次（真正防重在服务端）。
      const run = await api.createRun(payload, idempotencyKey);
      setInfo(`已创建批次 ${run.id}（ready）。前往运行详情点击「开始」才会发起付费调用。`);
      routes.runDetail(run.id);
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h2>上传与运行配置</h2>
      <p className="muted small">
        创建与预览**不调用模型**；点击「创建批次」后仍需在运行详情页显式「开始」才会触发付费调用。
      </p>
      <ErrorBanner error={error} />
      {info !== "" ? <div className="ok small">{info}</div> : null}

      <section className="panel">
        <h3>1. 选择问卷与模型配置</h3>
        <div className="grid">
          <div className="field">
            <label htmlFor="survey-select">问卷（含 revision）</label>
            <select id="survey-select" value={surveyId} onChange={(event) => setSurveyId(event.target.value)}>
              <option value="">— 请选择 —</option>
              {surveys.map((survey) => (
                <option key={survey.id} value={survey.id}>
                  {survey.title} (rev {survey.revision})
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="model-config-id">模型标识（不含密钥）</label>
            <input id="model-config-id" value={modelConfigId} onChange={(event) => setModelConfigId(event.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="prompt-version">prompt_version</label>
            <input id="prompt-version" value={PROMPT_VERSION} readOnly />
          </div>
        </div>
        <div className="grid">
          <div className="field">
            <label htmlFor="timeout">超时（秒）</label>
            <input id="timeout" value="60（服务端配置）" readOnly />
          </div>
          <div className="field">
            <label htmlFor="max-output">输出上限（tokens）</label>
            <input id="max-output" value="256（服务端配置）" readOnly />
          </div>
        </div>
      </section>

      <section className="panel">
        <h3>2. 上传用户表与列映射</h3>
        <div className="grid">
          <div className="field">
            <label htmlFor="file-input">用户表（CSV UTF-8 可带 BOM / XLSX，≤20,000 行）</label>
            <input
              id="file-input"
              type="file"
              accept=".csv,.xlsx,.xlsm"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
          </div>
          <div className="field">
            <label htmlFor="sheet-input">工作表名（XLSX；留空取第一张）</label>
            <input id="sheet-input" value={sheet} onChange={(event) => setSheet(event.target.value)} />
          </div>
        </div>
        <div className="grid">
          <div className="field">
            <label htmlFor="id-column">唯一用户 ID 列 *</label>
            <input id="id-column" value={idColumn} onChange={(event) => setIdColumn(event.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="profile-columns">画像文本列（逗号分隔，按序拼接）*</label>
            <input id="profile-columns" value={profileColumns} onChange={(event) => setProfileColumns(event.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="age-column">年龄列（可选，用于分组）</label>
            <input id="age-column" value={ageColumn} onChange={(event) => setAgeColumn(event.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="city-column">城市列（可选，用于分组）</label>
            <input id="city-column" value={cityColumn} onChange={(event) => setCityColumn(event.target.value)} />
          </div>
        </div>
        <div className="row">
          <button type="button" className="primary" disabled={busy} onClick={() => void handleUpload()}>
            上传并解析（不调用模型）
          </button>
        </div>

        {importPreview !== null ? (
          <div style={{ marginTop: 12 }}>
            <h3>导入结果</h3>
            <div className="grid">
              <Metric label="总行数（有效 personas）" value={String(importPreview.row_count)} />
              <Metric label="import_id" value={importPreview.import_id} />
              <Metric label="工作表" value={importPreview.sheet_name ?? "（CSV）"} />
            </div>
            {importPreview.errors.length > 0 ? (
              <Notice>
                行级错误（工作表 / 行号 / 列名）：
                <ul>
                  {importPreview.errors.map((rowError, index) => (
                    <li key={index}>
                      {rowError.sheet !== undefined ? `工作表=${rowError.sheet} ` : ""}
                      {rowError.row_no !== undefined ? `行号=${rowError.row_no} ` : ""}
                      {rowError.column !== undefined ? `列名=${rowError.column} ` : ""}
                      {rowError.message}
                    </li>
                  ))}
                </ul>
              </Notice>
            ) : null}
            <h3>3 行预览</h3>
            <table>
              <thead>
                <tr>
                  <th>数据行号</th>
                  <th>用户 ID</th>
                  <th>年龄</th>
                  <th>城市</th>
                  <th>画像文本</th>
                </tr>
              </thead>
              <tbody>
                {importPreview.preview.map((snapshot) => (
                  <tr key={snapshot.persona_id}>
                    <td>{snapshot.row_no}</td>
                    <td>{snapshot.persona_id}</td>
                    <td>{snapshot.age ?? "—"}</td>
                    <td>{snapshot.city_tier ?? "—"}</td>
                    <td>{snapshot.profile_text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <section className="panel">
        <h3>3. 并发与规模、成本与时长</h3>
        <div className="grid">
          <Metric label="目标并发（固定，不可编辑）" value={String(TARGET_CONCURRENCY)} hint="100 = 并行槽位数，不是样本数" />
          <Metric label="有效 personas 数" value={importPreview !== null ? String(importPreview.row_count) : "—"} />
        </div>
        <div className="grid">
          <div className="field">
            <label htmlFor="budget-limit">预算上限（{""}CNY，可留空=未设）</label>
            <input id="budget-limit" value={budgetLimit} onChange={(event) => setBudgetLimit(event.target.value)} placeholder="例如 50.00" />
          </div>
          <div className="field">
            <label htmlFor="request-limit">request_limit（可留空=默认 3 × 样本数）</label>
            <input id="request-limit" value={requestLimit} onChange={(event) => setRequestLimit(event.target.value)} />
          </div>
        </div>
        <div className="row">
          <button type="button" disabled={busy} onClick={() => void handlePreview()}>
            预览成本与时长（不调用模型）
          </button>
        </div>

        {runPreview !== null ? (
          <div style={{ marginTop: 12 }}>
            <h3>预览</h3>
            <div className="grid">
              <Metric label="预估成本" value={runPreview.costIsUnknown ? "未知" : (runPreview.estimatedCost ?? "未知")} hint="无价格配置时显示「未知」，不展示虚假金额" />
              <Metric
                label="预估时长"
                value={
                  runPreview.estimatedDurationSeconds === null
                    ? "未知"
                    : `${Math.round(runPreview.estimatedDurationSeconds)} 秒`
                }
              />
              <Metric label="样本数" value={String(runPreview.sampleSize)} />
              <Metric label="目标并发" value={String(runPreview.targetConcurrency)} />
            </div>
            <h3>题目文本</h3>
            <div>{selectedSurvey !== null ? selectedSurvey.question.prompt : "—"}</div>
            <h3>3 条画像提示词预览</h3>
            {runPreview.promptPreviews.map((preview, index) => (
              <pre className="json" key={index}>
                {preview}
              </pre>
            ))}
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="row">
          <button
            type="button"
            className="primary"
            disabled={busy || selectedSurvey === null || importPreview === null}
            onClick={() => void handleCreateRun()}
          >
            创建批次（ready；点击「开始」才发起付费调用）
          </button>
          <span className="muted small">幂等键：{idempotencyKey}</span>
        </div>
        <p className="muted small">启动期间按钮禁用；真正的防重在服务端（同 Idempotency-Key 只产生一个批次）。</p>
      </section>
    </div>
  );
}
