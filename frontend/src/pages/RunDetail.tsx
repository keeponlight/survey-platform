// SPDX-License-Identifier: GPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type RunView } from "../api";
import { ErrorBanner, Metric, Notice, StatusPill, usePolling } from "../components";
import { errorRateText, formatCount, formatDuration, formatKnownCost, formatLocalTime } from "../format";
import { routes } from "../router";

const NON_TERMINAL = new Set(["ready", "running", "pausing", "cancelling"]);

const PAUSE_REASON_TEXT: Record<string, string> = {
  user: "用户暂停",
  api_auth: "API 认证失败，请修复服务端模型配置后恢复",
  api_unavailable: "模型服务不可用或持续限流，请稍后恢复",
  budget: "预算或请求额度不足，请提高预算后恢复",
};

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `retry-${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`;
}

export default function RunDetail({ runId }: { runId: string }): JSX.Element {
  const [run, setRun] = useState<RunView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [retryKey] = useState<string>(() => newIdempotencyKey());

  const load = useCallback(async (): Promise<void> => {
    try {
      const value = await api.getRun(runId);
      setRun(value);
      setError(null);
    } catch (err: unknown) {
      setError(err);
    }
  }, [runId]);

  // 仅本页轮询；切页/卸载时 usePolling 会清理计时器。
  usePolling(load, 3000, run === null || NON_TERMINAL.has(run.status));

  // 进入本页 / runId 变化时立即拉一次（轮询首帧不等待 3 秒）。
  useEffect(() => {
    void load();
  }, [load]);

  async function control(action: "start" | "pause" | "resume" | "cancel"): Promise<void> {
    setBusy(true);
    setInfo("");
    try {
      const updated = await api.controlRun(runId, action);
      setRun(updated);
      setError(null);
      setInfo(`已提交控制动作：${action}`);
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function retryFailed(): Promise<void> {
    setBusy(true);
    setInfo("");
    try {
      const updated = await api.retryFailed(runId, retryKey);
      setRun(updated);
      setError(null);
      setInfo("已重新安排失败成员（幂等键只生效一次）。请再显式「开始」。");
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function increaseBudget(): Promise<void> {
    const next = window.prompt("新的预算上限（只允许提高，不得更改币种）");
    if (next === null || next.trim() === "") return;
    setBusy(true);
    setInfo("");
    try {
      const updated = await api.increaseBudget(runId, next.trim());
      setRun(updated);
      setError(null);
      setInfo(`预算已更新为 ${next.trim()}`);
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  if (run === null && error === null) {
    return <div className="muted">加载运行详情…</div>;
  }

  const allowed = new Set(run?.allowedActions ?? []);
  const isPartial = run !== null && NON_TERMINAL.has(run.status);

  return (
    <div>
      <h2>
        运行详情 <span className="muted small">{runId}</span>
      </h2>
      <ErrorBanner error={error} />
      {info !== "" ? <div className="ok small">{info}</div> : null}
      {isPartial ? <Notice>运行中：以下为**部分结果**，尚未收敛。</Notice> : null}

      {run !== null ? (
        <>
          <section className="panel">
            <h3>状态</h3>
            <div className="row">
              <StatusPill status={run.status} />
              <span className="small muted">prompt_version: {run.promptVersion}</span>
              <button
                type="button"
                className="small"
                onClick={() => routes.runResults(run.id)}
              >
                查看结果页 →
              </button>
            </div>
            <div className="grid" style={{ marginTop: 12 }}>
              <Metric label="状态" value={run.status} hint={run.status === "completed_with_errors" ? "部分成功、部分失败（不隐去）" : undefined} />
              <Metric
                label="pause_reason"
                value={run.pauseReason ?? "—"}
                hint={
                  run.pauseHint ??
                  (run.pauseReason !== null ? PAUSE_REASON_TEXT[run.pauseReason] : undefined)
                }
              />
              <Metric label="started_at" value={formatLocalTime(run.startedAt)} />
              <Metric label="finished_at" value={formatLocalTime(run.finishedAt)} />
              <Metric label="累计时长（含暂停/重试等待）" value={formatDuration(run.startedAt, run.finishedAt)} />
            </div>
            {run.errorSummary !== null ? (
              <Notice>
                错误摘要：{run.errorSummary}
                <br />
                可操作说明：
                {run.pauseHint ??
                  (run.pauseReason !== null
                    ? PAUSE_REASON_TEXT[run.pauseReason]
                    : "请检查批次错误码后重试或提高预算。")}
              </Notice>
            ) : null}
          </section>

          <section className="panel">
            <h3>并发</h3>
            <div className="grid">
              <Metric label="目标并发" value={String(run.targetConcurrency)} hint="固定 100（槽位数）" />
              <Metric label="实际在途数" value={formatCount(run.inFlight)} />
              <Metric label="限流等待数" value={formatCount(run.throttled)} />
            </div>
          </section>

          <section className="panel">
            <h3>计数</h3>
            <div className="grid">
              <Metric label="样本数（planned）" value={String(run.sampleSize)} />
              <Metric label="有效数（valid）" value={String(run.counts.valid)} />
              <Metric label="失败率（failed / planned）" value={errorRateText(run.counts.failed, run.sampleSize)} />
              <Metric label="成功 (succeeded)" value={String(run.counts.succeeded)} />
              <Metric label="失败 (failed)" value={String(run.counts.failed)} />
              <Metric label="待执行 (pending)" value={String(run.counts.pending)} />
              <Metric label="重试等待 (retry_wait)" value={String(run.counts.retry_wait)} />
              <Metric label="执行中 (running)" value={String(run.counts.running)} />
              <Metric label="已取消 (cancelled)" value={String(run.counts.cancelled)} />
            </div>
          </section>

          <section className="panel">
            <h3>费用（已知 / 未知**分列**，不合并）</h3>
            <div className="grid">
              <Metric label="已知费用" value={formatKnownCost(run.costs.known, run.costs.currency)} />
              <Metric label="未知费用（请求数）" value={String(run.costs.unknownCount)} hint="未知计费保留预留，不按 0 释放" />
              <Metric label="预算上限" value={run.budgetLimit === null ? "未设" : `${run.budgetLimit} ${run.budgetCurrency}`} />
              <Metric label="request_limit" value={run.requestLimit === null ? "—" : String(run.requestLimit)} />
              <Metric label="requests_reserved" value={run.requestsReserved === null ? "—" : String(run.requestsReserved)} />
            </div>
          </section>

          <section className="panel">
            <h3>控制</h3>
            <div className="row">
              <button type="button" className="primary" disabled={busy || !allowed.has("start")} onClick={() => void control("start")}>
                开始 / 恢复运行
              </button>
              <button type="button" disabled={busy || !allowed.has("pause")} onClick={() => void control("pause")}>
                暂停
              </button>
              <button type="button" disabled={busy || !allowed.has("resume")} onClick={() => void control("resume")}>
                恢复
              </button>
              <button type="button" className="danger" disabled={busy || !allowed.has("cancel")} onClick={() => void control("cancel")}>
                取消
              </button>
              <button type="button" disabled={busy || !allowed.has("retry-failed")} onClick={() => void retryFailed()}>
                失败重试（幂等）
              </button>
              <button type="button" disabled={busy || !allowed.has("increase-budget")} onClick={() => void increaseBudget()}>
                提高预算
              </button>
            </div>
            <p className="muted small">
              按钮按服务端下发的 allowed_actions 显示/启用；非法转移由服务端返回 409。
            </p>
            {error instanceof ApiError && error.status === 409 ? (
              <div className="warn small">当前状态不允许该操作（409）。</div>
            ) : null}
          </section>
        </>
      ) : null}
    </div>
  );
}
