// SPDX-License-Identifier: GPL-3.0-or-later
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { ApiError } from "./api";

/** 「模拟购买意向」标识（PRD §4 / P0-08：UI 必须明确模拟性质）。 */
export function SimBadge(): JSX.Element {
  return <span className="sim-badge">模拟购买意向（非真实购买转化率）</span>;
}

export function StatusPill({ status }: { status: string }): JSX.Element {
  return <span className={`status-pill status-${status}`}>{status}</span>;
}

export function Metric({ label, value, hint }: { label: string; value: string; hint?: string }): JSX.Element {
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
      {hint !== undefined ? <div className="metric-label">{hint}</div> : null}
    </div>
  );
}

export function Notice({ children }: { children: ReactNode }): JSX.Element {
  return <div className="notice">{children}</div>;
}

export function ErrorBanner({ error }: { error: unknown }): JSX.Element | null {
  if (error === null || error === undefined) return null;
  if (error instanceof ApiError) {
    return (
      <div className="error">
        [{error.code}] {error.message}
        {Object.keys(error.details).length > 0 ? ` ${JSON.stringify(error.details)}` : ""}
      </div>
    );
  }
  return <div className="error">{String(error)}</div>;
}

/**
 * 轮询 hook：**仅运行详情页使用**；切页/卸载时清理计时器（task-list T6）。
 * `enabled=false` 时不建计时器。
 */
export function usePolling(callback: () => void | Promise<void>, intervalMs: number, enabled: boolean): void {
  const saved = useRef(callback);
  saved.current = callback;

  useEffect(() => {
    if (!enabled) return undefined;
    const timer = window.setInterval(() => {
      void saved.current();
    }, intervalMs);
    return () => {
      window.clearInterval(timer);
    };
  }, [intervalMs, enabled]);
}

/** 异步加载状态容器。 */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[]): {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [tick, setTick] = useState<number>(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loader()
      .then((value) => {
        if (!cancelled) {
          setData(value);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { data, error, loading, reload: () => setTick((value) => value + 1) };
}
