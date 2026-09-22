// SPDX-License-Identifier: GPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";

import { api, type ResultsPage, type RunView, type Summary } from "../api";
import { ErrorBanner, Metric, Notice, StatusPill } from "../components";
import { errorRateText, formatLocalTime, formatMean, formatRate } from "../format";

const PAGE_SIZE = 50;

const NON_TERMINAL = new Set(["ready", "running", "pausing", "cancelling"]);

export default function RunResults({ runId }: { runId: string }): JSX.Element {
  const [run, setRun] = useState<RunView | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [results, setResults] = useState<ResultsPage | null>(null);
  const [page, setPage] = useState<number>(1);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState<boolean>(false);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const [runValue, summaryValue, resultsValue] = await Promise.all([
        api.getRun(runId),
        api.getSummary(runId),
        api.getResults(runId, {
          page,
          pageSize: PAGE_SIZE,
          ...(statusFilter === "" ? {} : { memberStatus: statusFilter }),
        }),
      ]);
      setRun(runValue);
      setSummary(summaryValue);
      setResults(resultsValue);
      setError(null);
    } catch (err: unknown) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [runId, page, statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const totalPages = results === null ? 1 : Math.max(1, Math.ceil(results.total / results.pageSize));
  const isPartial = run !== null && NON_TERMINAL.has(run.status);
  const validZero = summary !== null && summary.validCount === 0;

  return (
    <div>
      <h2>
        结果页 · 模拟购买意向 <span className="muted small">{runId}</span>
      </h2>
      <ErrorBanner error={error} />
      {isPartial ? <Notice>运行中：以下为**部分结果**，尚未收敛。</Notice> : null}
      {validZero ? <Notice>暂无有效回答（valid = 0；比率与均分不显示为 0%）。</Notice> : null}

      <section className="panel">
        <div className="row">
          {run !== null ? <StatusPill status={run.status} /> : null}
          {run !== null ? <span className="muted small">report as_of: {formatLocalTime(summary?.asOf ?? null)}</span> : null}
          {summary !== null ? <span className="muted small">report_revision: {summary.reportRevision}</span> : null}
          <a href={api.exportCsvUrl(runId)} download>
            导出 CSV（UTF-8 BOM，含全部成员与状态，失败行 value 为空）
          </a>
          <button type="button" disabled={loading} onClick={() => void load()}>
            刷新
          </button>
        </div>
      </section>

      {summary !== null ? (
        <section className="panel">
          <h3>汇总</h3>
          <div className="grid">
            <Metric label="样本数（planned）" value={String(summary.sampleSize)} />
            <Metric label="总有效数（valid）" value={String(summary.validCount)} />
            <Metric label="覆盖率（coverage）" value={formatRate(summary.coverage)} />
            <Metric label="失败率（failed / planned）" value={errorRateText(summary.failedCount, summary.sampleSize)} />
            <Metric label="Top-2-Box" value={formatRate(summary.top2box)} hint="选 4/5 档比例；**不是**真实购买转化率" />
            <Metric label="均分（mean_score）" value={formatMean(summary.meanScore)} />
          </div>

          <h3>五档分布</h3>
          <table>
            <thead>
              <tr>
                <th>档位</th>
                <th>中文展示</th>
                <th>score</th>
                <th>count</th>
                <th>rate</th>
              </tr>
            </thead>
            <tbody>
              {summary.buckets.map((bucket) => (
                <tr key={bucket.value}>
                  <td>{bucket.value}</td>
                  <td>{bucket.label}</td>
                  <td>{bucket.score}</td>
                  <td>{bucket.count}</td>
                  <td>{formatRate(bucket.rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">五档计数之和必须等于 valid（{summary.validCount}）。</p>

          {summary.ageGroups !== null ? (
            <>
              <h3>年龄分组</h3>
              <table>
                <thead>
                  <tr>
                    <th>分组</th>
                    <th>planned</th>
                    <th>valid</th>
                    <th>coverage</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.ageGroups.map((group) => (
                    <tr key={group.label}>
                      <td>{group.label}</td>
                      <td>{group.planned}</td>
                      <td>{group.valid}</td>
                      <td>{formatRate(group.coverage)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : null}

          {summary.cityGroups !== null ? (
            <>
              <h3>城市分组</h3>
              <table>
                <thead>
                  <tr>
                    <th>分组</th>
                    <th>planned</th>
                    <th>valid</th>
                    <th>coverage</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.cityGroups.map((group) => (
                    <tr key={group.label}>
                      <td>{group.label}</td>
                      <td>{group.planned}</td>
                      <td>{group.valid}</td>
                      <td>{formatRate(group.coverage)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : null}
        </section>
      ) : null}

      <section className="panel">
        <h3>逐人结果（按输入顺序 / row_no 升序）</h3>
        <div className="row">
          <label htmlFor="status-filter">按成员状态过滤</label>
          <select id="status-filter" value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); setPage(1); }}>
            <option value="">全部</option>
            <option value="pending">pending</option>
            <option value="running">running</option>
            <option value="succeeded">succeeded</option>
            <option value="retry_wait">retry_wait</option>
            <option value="failed">failed</option>
            <option value="cancelled">cancelled</option>
          </select>
          <span className="muted small">
            共 {results?.total ?? 0} 行 · 第 {page} / {totalPages} 页
          </span>
          <button type="button" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>
            上一页
          </button>
          <button type="button" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>
            下一页
          </button>
        </div>

        <table>
          <thead>
            <tr>
              <th>数据行号</th>
              <th>表格行号（含表头）</th>
              <th>用户 ID</th>
              <th>意向档位</th>
              <th>1–5 分</th>
              <th>状态</th>
              <th>错误</th>
            </tr>
          </thead>
          <tbody>
            {(results?.items ?? []).map((member) => (
              <tr key={member.id}>
                <td>{member.rowNo}</td>
                <td>{member.rowNo + 1}</td>
                <td>{member.personaId}</td>
                <td>{member.value ?? "—"}</td>
                <td>{member.score ?? "—"}</td>
                <td>
                  <StatusPill status={member.memberStatus} />
                </td>
                <td>{member.errorCode ?? "—"}</td>
              </tr>
            ))}
            {(results?.items.length ?? 0) === 0 ? (
              <tr>
                <td colSpan={7} className="muted">
                  暂无数据。
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
        <p className="muted small">
          失败/待完成行答案为空，**不用空值冒充中立答案**。失败行不改变分母（valid）。
        </p>
      </section>
    </div>
  );
}
