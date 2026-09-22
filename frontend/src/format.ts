// SPDX-License-Identifier: GPL-3.0-or-later
/** 展示层格式化工具（四舍五入只在展示层做；UTC → 本地时区）。 */

export function formatLocalTime(iso: string | null): string {
  if (iso === null || iso === "") return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

/** 累计时长（含暂停/重试等待）——不是纯执行时长（见 docs/architecture.md §2.3）。 */
export function formatDuration(startIso: string | null, endIso: string | null, now: Date = new Date()): string {
  if (startIso === null || startIso === "") return "—";
  const start = new Date(startIso);
  if (Number.isNaN(start.getTime())) return "—";
  const end = endIso !== null && endIso !== "" ? new Date(endIso) : now;
  const seconds = Math.max(0, Math.floor((end.getTime() - start.getTime()) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  if (hours > 0) return `${hours} 小时 ${minutes} 分 ${secs} 秒`;
  if (minutes > 0) return `${minutes} 分 ${secs} 秒`;
  return `${secs} 秒`;
}

/** 比例（0..1 的小数）→ 百分比字符串；null = 暂无有效回答（**不显示 0%**）。 */
export function formatRate(rate: number | null): string {
  if (rate === null || !Number.isFinite(rate)) return "暂无有效回答";
  return `${(rate * 100).toFixed(1)}%`;
}

/** 均分；null = 暂无有效回答。 */
export function formatMean(mean: number | null): string {
  if (mean === null || !Number.isFinite(mean)) return "暂无有效回答";
  return mean.toFixed(2);
}

/** 已知费用；null = 未知（**不等于 0**）。 */
export function formatKnownCost(known: string | null, currency: string): string {
  if (known === null || known === "") return "未知";
  return `${known} ${currency}`;
}

export function formatCount(value: number | null): string {
  return value === null ? "—" : String(value);
}

export function errorRateText(failed: number, planned: number): string {
  if (planned <= 0) return "—";
  return `${((failed / planned) * 100).toFixed(1)}%`;
}
