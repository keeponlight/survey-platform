#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""QA 独立复核：解析已落盘的 50 人真实运行 JSON（只读，零网络调用）。

本脚本**不发起任何网络请求**，**不访问真实 provider**。它只做：
1. 从 ``results/*.json`` 重新统计 finish_reason / tokens / 重试 / 并发峰值；
2. 用**两种独立定义**重算并发峰值（100ms 采样 vs 请求区间重叠重建）；
3. 逐请求校验 ``completion_tokens == max_tokens``（length 截断的判据）。

用途：复核 ``docs/real-run-50.md`` 的结论是否被原始数据支撑（尝试证伪）。
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter

HERE = pathlib.Path(__file__).resolve()
RESULTS = HERE.parent / "results"

FILES = {
    "R1a(2048)": "real_50_maxtok2048.json",
    "R1b(4096)": "real_50_maxtok4096.json",
    "R2(cap8)": "real_50_cap8_maxtok4096.json",
}


def load(name: str) -> dict:
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def rebuild_peak(records: list[dict]) -> int:
    """由每个请求的 [t_start, t_end] 区间做重叠统计（+1 开始 / -1 结束）。"""
    events: list[tuple[float, int]] = []
    for r in records:
        events.append((float(r["t_start_offset_s"]), 1))
        events.append((float(r["t_end_offset_s"]), -1))
    # 同一时刻先处理 -1（结束）再处理 +1（开始），得到保守（偏低）的重叠峰值。
    events.sort(key=lambda it: (it[0], it[1]))
    cur = 0
    peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def rebuild_peak_inclusive(records: list[dict]) -> int:
    """同上，但同一时刻先 +1 再 -1（区间端点视为闭区间），得到偏高上界。"""
    events: list[tuple[float, int]] = []
    for r in records:
        events.append((float(r["t_start_offset_s"]), 1))
        events.append((float(r["t_end_offset_s"]), -1))
    events.sort(key=lambda it: (it[0], -it[1]))
    cur = 0
    peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def analyze(label: str, d: dict) -> None:
    recs = d["network"]["requests"]
    net = d["network"]["summary"]
    con = d["concurrency"]
    cap = con["cap"]
    series = con["series"]
    cols = con["series_columns"]

    print("=" * 78)
    print(f"[{label}] run={d['final']['run_id']} status={d['final']['final_status']} "
          f"cap={cap} sample={d['sample']['row_count']}")
    print(f"  records={len(recs)}  ok={net['ok_count']} err={net['error_count']}")

    # ---- finish_reason ----
    fr = Counter(r["finish_reason"] for r in recs)
    print(f"  finish_reason distribution      : {dict(fr)}")

    # ---- max_tokens on wire + length requests ----
    mt = sorted({r.get("req_max_tokens") for r in recs if r.get("req_max_tokens")})
    print(f"  req_max_tokens observed values  : {mt}")
    length_recs = [r for r in recs if r["finish_reason"] == "length"]
    print(f"  #length requests                : {len(length_recs)}")
    for r in length_recs:
        ok = r.get("completion_tokens") == r.get("req_max_tokens")
        print(f"    - persona={r['persona_id']} attempt={r['attempt_no']} "
              f"completion={r['completion_tokens']} req_max_tokens={r.get('req_max_tokens')} "
              f"==max? {ok} error_code={r.get('error_code')}")

    # ---- tokens ----
    in_tok = sum(int(r["prompt_tokens"]) for r in recs if r["prompt_tokens"] is not None)
    out_tok = sum(int(r["completion_tokens"]) for r in recs if r["completion_tokens"] is not None)
    reas = sum(int(r["reasoning_tokens"]) for r in recs if r.get("reasoning_tokens") is not None)
    n_reas = sum(1 for r in recs if r.get("reasoning_tokens") is not None)
    print(f"  tokens in/out                   : {in_tok} / {out_tok}")
    print(f"  reasoning_tokens sum            : {reas}  (#records with value={n_reas}/{len(recs)})")
    if n_reas:
        content = out_tok - reas
        print(f"  content(=out-reasoning) sum     : {content}")
        print(f"  mean reasoning / mean content   : {reas / n_reas:.1f} / {content / n_reas:.1f}")
        print(f"  reasoning share of output       : {reas / out_tok * 100:.2f}%")
    # normal calls (exclude length-truncated) average output
    normal = [r for r in recs if r["finish_reason"] != "length"]
    if normal:
        avg = sum(int(r["completion_tokens"]) for r in normal) / len(normal)
        print(f"  mean output on non-length calls : {avg:.1f}  (n={len(normal)})")

    # ---- concurrency: independent recomputation ----
    idx = {c: i for i, c in enumerate(cols)}
    imax = idx.get("in_flight_limiter", 1)
    dmmax = idx.get("db_running_members", 2)
    damax = idx.get("db_running_attempts", 3)
    ser_peak_inflight = max((row[imax] for row in series), default=0)
    ser_peak_members = max((row[dmmax] for row in series), default=0)
    ser_peak_attempts = max((row[damax] for row in series), default=0)
    violations = [row for row in series if any(row[j] > cap for j in (imax, dmmax, damax))]
    rebuilt = rebuild_peak(recs)
    rebuilt_incl = rebuild_peak_inclusive(recs)
    print(f"  samples                         : {len(series)}")
    print(f"  series peak  in_flight/members/attempts : "
          f"{ser_peak_inflight} / {ser_peak_members} / {ser_peak_attempts}")
    print(f"  series rows with ANY col > cap({cap})    : {len(violations)}")
    print(f"  rebuilt peak (half-open)        : {rebuilt}")
    print(f"  rebuilt peak (closed intervals) : {rebuilt_incl}")
    print(f"  report claims peak_in_flight    : {con['peak_in_flight_limiter']} "
          f"(match series? {con['peak_in_flight_limiter'] == ser_peak_inflight})")
    print(f"  report claims rebuilt_peak      : "
          f"{con['rebuilt_peak_in_flight_from_requests']} (match? "
          f"{con['rebuilt_peak_in_flight_from_requests'] == rebuilt})")
    print(f"  report always_le_cap            : {con['always_le_cap']} "
          f"(raw-series derived: {len(violations) == 0})")

    # ---- per-persona attempts ----
    by_persona: dict[str, list[dict]] = {}
    for r in recs:
        by_persona.setdefault(r["persona_id"], []).append(r)
    multi = {p: len(v) for p, v in by_persona.items() if len(v) > 1}
    print(f"  distinct personas               : {len(by_persona)}")
    print(f"  personas with >1 attempt        : {len(multi)} -> {sorted(multi)}")
    # attempt_no gaps (e.g. missing attempt 1)
    gaps = []
    for p, v in by_persona.items():
        nos = sorted(int(x["attempt_no"]) for x in v)
        if nos != list(range(1, len(nos) + 1)):
            gaps.append((p, nos))
    print(f"  personas with attempt_no gaps   : {gaps}")

    # ---- retries & connections ----
    print(f"  retry requests / attempts_total : {net['retry_requests']} / "
          f"{net['retry_attempts_total']}")
    print(f"  distinct connections / on-wire  : {net['distinct_http_connections']} / "
          f"{net['http_requests_on_wire']}")
    print(f"  measured rpm/tpm                : {net['measured_rpm']} / {net['measured_tpm']}")
    print(f"  http 429/5xx/4xx-other          : {net['http_429_count']} / "
          f"{net['http_5xx_count']} / {net['http_4xx_other_count']}")

    # ---- budget / invariants ----
    print(f"  invariants.all_pass             : {d['invariants']['all_pass']}")
    print(f"  requests_reserved/limit         : {d['budget'].get('requests_reserved')} / "
          f"{d['budget'].get('request_limit')}")
    print(f"  resume_events                   : {d.get('resume_events')}")


def main() -> int:
    for label, name in FILES.items():
        analyze(label, load(name))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
