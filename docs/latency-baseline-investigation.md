# Baseline Latency Investigation（真实 API 延迟基线取证报告）

> 生成时间：2026-09-21（北京时间下午）；执行者：主 agent（按《AI执行步骤-真实API延迟补充取证测试.md》执行）。
> 原则：只测试、只读取、只分析；**未修改任何业务代码、未调整 RPM/concurrency/timeout/retry 参数**。

## 1. Executive Summary

- **同一冻结配置、同一 50 人快照，5 个时间窗口的结果差异显著**：HTTP p50 在 12.5s–31.0s 之间波动（2.5×），429 次数 0–15 次/轮，SERVER_ERROR 仅在 Run-2 出现 5 次，Run-2 触发 worker `api_unavailable` 自动熔断（44/50 提前终止）。
- **TIMEOUT 是当前最昂贵的失败模式**：可测 wasted slot 全部来自 TIMEOUT（18 次 × 120s ≈ 2160 槽秒）；429/5xx 为毫秒级秒败，浪费可忽略。
- **TIMEOUT 后重试成功率极高（15/18）**；**429 后重试成功率为 0/24**——429 以连续风暴出现，`attempt_limit=3` + 总 backoff ≈ 11s 的序列在 429 风暴窗口内必然耗尽。
- 本轮**未观察到** Retry-After 覆盖 backoff 的直接证据（429 重试间隔精确等于 2s/8s+jitter）；日志未记录 response header，Retry-After 分析标记"现有数据不足"。
- 5 轮中 3 轮达到 50/50 或 49/50；整体成功率 = (49+44+50+50+48)/250 = **96.4%**（Run-2 含熔断截断）。

**结论归类（对应执行文档 §16）**：命中**情况 A**（时间窗口波动大）+ **情况 C**（TIMEOUT 是主要 wasted slot 来源）。下一阶段优先做时间交错的中转站健康度观测；TIMEOUT 值得作为单变量实验对象；**不建议**基于单轮结果直接调 RPM/concurrency。

## 2. Frozen Configuration

记录于 `investigation/FROZEN-CONFIG.txt`（2026-09-21T04:56:53Z 固化）。要点：

| 项 | 值 |
|---|---|
| 代码版本 | 项目未 git init；以 6 个关键文件 sha256 固化（execute/main/limits/provider/config/source） |
| Provider / 模型 | `openai_compatible` / `gpt-5.6-terra` @ `https://ooioo.work/v1`（newapi 中转） |
| MODEL_RPM / TPM | 60 / 400,000 |
| AGENT_CONCURRENCY | 20 |
| MODEL_TIMEOUT_SECONDS / MAX_OUTPUT_TOKENS | 120 / 2000 |
| Retry | attempt_limit=3（成员级）；backoff=(2.0s, 8.0s)+jitter；Retry-After 可覆盖（本轮未触发） |
| prompt_version | `purchase-intent-zh-v1` |
| 数据集 | import_id=`68d1ac54-3821-4640-9a2f-581abc6d930f`，row_count=50，CSV sha256=`a1c8a233…c13e`（User_360_Analysis_sample50.csv） |
| 每轮产品/问题 | 与 Run `5e2ea0a3` 完全同一 survey 快照（survey_id=`a516e450-…9612`，白茶赤芝精华霜 ¥218/瓶） |

## 3. Test Method

- 每轮新建 run（同一 import + survey + model_config），新 Idempotency-Key；start 后轮询至终态；**不人工 retry-failed**、不改变任何系统行为。
- 每轮结束立即落盘：`run.json`、`summary.json`、`attempts.csv`（全表 join）、`worker/api_logs.txt`（该轮时间窗）。
- 轮间间隔 ≥2.5 分钟。
- **已记录的实验偏差**：
  1. Run-2 被 worker 熔断为 `paused(api_unavailable)`；paused 属于活动态，阻塞了 Run-3 的 start（409 `ACTIVE_RUN_EXISTS`，`uq_runs_single_active` precheck）。处置：cancel Run-2（数据已存档）后重新 start Run-3。Run-2 保留为"熔断样本轮"。
  2. Run-2 的 1 个 retry_wait 成员随 cancel 终止；其 5 个 failed 中 1 个是 TIMEOUT 后未获重试（非 3 次用尽）。

## 4. Five-Run Results

| Metric | Run-1 | Run-2⚠️ | Run-3 | Run-4 | Run-5 |
|---|---:|---:|---:|---:|---:|
| 终态 | completed_with_errors | paused→cancelled | completed | completed | completed_with_errors |
| Wall clock | 2m53s | 2m37s(熔断) | 5m36s | 3m31s | 4m47s |
| succeeded / 50 | 49 | 44 | 50 | 50 | 48 |
| failed | 1 | 5 | 0 | 0 | 2 |
| Total attempts | 53 | 65 | 57 | 57 | 56 |
| TIMEOUT | 1 | 1 | 7 | 7 | 2 |
| RATE_LIMITED(429) | 3 | 15 | 0 | 0 | 6 |
| SERVER_ERROR | 0 | 5 | 0 | 0 | 0 |
| HTTP p50 (s) | 23.8 | 12.5 | 31.0 | 24.7 | 18.1 |
| HTTP p95 (s) | 115.1 | 57.8 | 108.3 | 95.5 | 64.5 |
| HTTP max (s) | 119.7 | 80.3 | 116.7 | 108.9 | 95.0 |
| 一次成功 / 重试成功 | 48 / 1 | 40 / 4 | 44 / 6 | 43 / 7 | 47 / 1 |

全局成功 HTTP 耗时（241 次）：min 3.5s / p50 22.0s / p90 79.6s / p95 95.0s / p99 116.7s / max 119.7s / mean 33.8s。

## 5. Time-Window Variability

- **HTTP p50 波动 2.5×**（12.5s ↔ 31.0s），p95 波动近 2×（57.8s ↔ 115.1s）。
- **429 从 0 → 15 次/轮剧烈波动**：Run-2 的 429 集中在 05:05:17–05:05:26 的 10 秒内（≥8 次密集命中），Run-3/4 完全无 429。
- **SERVER_ERROR 只出现在 Run-2**（5 次，毫秒级秒败）。
- 结论：仅时间窗口变化时，p50/p95、429、5xx 均出现剧烈变化 → **中转链路健康度存在分钟级时间波动**。

## 6. Failure Distribution

5 轮合计 47 次失败 attempt：

| failure_type | count | 占比 | next_retry_success_rate |
|---|---:|---:|---|
| ~120s TIMEOUT | 18 | 38% | **15/18 (83%)** |
| 429 (RATE_LIMITED) | 24 | 51% | **0/24 (0%)** |
| SERVER_ERROR（<1s 秒败，估） | 5 | 11% | 5/5 (100%) |

- **最常见失败**：429（51%）。
- **占用 HTTP 时间最多的失败**：TIMEOUT（每次 120s 全额；429/5xx 为毫秒级）。
- 429 呈**连续风暴**模式：同一成员的 3 次 attempt 依次全撞 429（间隔 2.1s / 8.8s ≈ backoff 序列），~11 秒内耗尽 attempt_limit。R1/R5 各 1 个成员、R2 至少 4 个成员因此阵亡。

## 7. Retry Reconstruction

重建公式（执行文档 §5/§9）：`attempt_end = started_at + duration_ms`（失败 attempt 的 duration_ms 缺失：TIMEOUT 用 120s 配置估计；429/5xx 无法估计，未计入 waits）。

- 17 次 TIMEOUT 后重试等待全部落在 backoff 邻域（extra_wait ≤ 30s；多数 ≈ 0，因 worker 在超时判定后立即重新 claim）。
- **没有发现** extra_wait > 30s 的等待 → 本轮 RPM=60 limiter 没有造成可测的额外重试延迟。
- 429/5xx 的 observed_wait 无法从现有数据重建（duration 缺失）；但重试**间隔**（next.started − cur.started）实测为 2.1s/8.8s，与 backoff(2s/8s)+jitter 精确吻合 → 未见 Retry-After 覆盖迹象。

## 8. Retry-After Evidence

- **现有数据不足**：worker 日志（httpx INFO）只记录响应行与时间戳，不记录 response header；`attempts` 表不存 header。无法直接读取 Retry-After 值。
- 间接证据（弱）：本轮 24 次 429 的重试间隔全部 = backoff+jitter（无一次 >10s），说明这些 429 响应**未携带**生效的 Retry-After，或 worker 未收到。
- 与此前 Run `5e2ea0a3`（04:03 时段）观察到的 ~129s 重试间隔（当时推断为 SERVER_ERROR + Retry-After≈120s）相比，本轮无同类现象。按执行文档要求，只能表述为：**"中转链路返回行为存在不一致，具体发生在哪一层尚未确认"**——不能声称"不同上游节点"（无 node-id/upstream-id/request-id 证据）。

## 9. Wasted Slot Analysis

| 类别 | 次数 | wasted slot（估） |
|---|---:|---:|
| TIMEOUT | 18 | **2160s**（18×120s，配置上限估计） |
| SERVER_ERROR | 5 | ≈0（毫秒级秒败） |
| RATE_LIMITED | 24 | ≈0（毫秒级秒败） |

- `timeout_wasted / total_wasted = 100%`（可测口径）。
- **TIMEOUT 是当前最昂贵的失败模式**：单次即占用一个并发槽 120 秒（≈ 两个完整 p50 请求的时间），且 R3/R4 各 7 次。
- 注意：TIMEOUT 后重试成功率 83%，即大部分 120s 最终仍产出有效答案——"浪费"指槽位时间而非必然无产出。

## 10. Representative Lifecycles（自动选取）

- **Case A 最快一次成功**：Run-1 `ILB_708fd1ede3d47f87` 04:58:41 发出，**3.49s** 完成。
- **Case B 最慢一次成功**：Run-1 `ILB_ebf96a7939057b82` 04:58:41 发出，**119.73s**（几乎贴满 120s 超时线）——同一秒发出的两个请求，耗时差 34×。
- **Case C 快速失败后重试成功**（5 例）：Run-2 `ILB_99e0969c5f36af22` a1 SERVER_ERROR 秒败 → 2s backoff → a2 12.85s 成功。
- **Case D TIMEOUT 后最终成功**（15 例）：Run-3 `ILB_8da72f3ed7cebe81` a1 TIMEOUT(120s) → a2 立即重试 → 成功。
- **Case E 最终失败**（9 例，全部 429 三连败模式）：Run-1 `ILB_9511a1e833c59023`：04:58:45.4 a1 429 →(2.1s) 04:58:47.5 a2 429 →(8.8s) 04:58:56.3 a3 429 → failed。11 秒内 3 次尝试全部命中同一 429 风暴窗口。该成员在 Run-2、Run-5 再次以相同模式失败（**同一 persona 三轮均死于 429 风暴**）。

## 11. What Is Proven

1. 中转链路存在分钟级健康度波动（p50 2.5×、429 0↔15、5xx 出现/消失）。
2. TIMEOUT 是 wasted slot 的绝对主要来源（100% 可测口径）。
3. TIMEOUT 后重试成功率 83%，attempt_limit=3 对 TIMEOUT 足够。
4. 429 风暴下 attempt_limit=3 + backoff(2s/8s) 必然耗尽（总退避 ~11s ≪ 429 窗口），429 后重试成功率 0%。
5. worker 熔断（api_unavailable）能自动止损，但 paused run 会阻塞后续 run 启动（活动态唯一性约束）。
6. RPM=60 限流器在本轮 5 个窗口中未产生可测的额外等待。

## 12. What Is Not Proven

- 429/5xx 的单次真实耗时（duration_ms 缺失，仅有毫秒级间接证据）。
- Retry-After 是否存在及其数值分布（日志无 header 记录）。
- RPM/concurrency/timeout 的更优取值（本轮未做单变量实验）。
- 429 风暴的成因（网关限流策略 / 上游配额 / 时间段负载）。
- 上一轮观察到的 ~129s 重试等待与 Retry-After 的因果关系。

## 13. Recommendation for Next Experiment

1. **优先**：时间交错的健康度观测（如每 30 分钟一轮轻量探针 run），确认 429/5xx 风暴的时间分布规律——按执行文档 §16 情况 A，先查健康度，再谈参数。
2. **TIMEOUT 单变量实验**（情况 C）：p95 已达 95–115s 且贴近 120s 上限，timeout 取 60/90/120/150s 的对比具备条件；注意缩短 timeout 会增加 TIMEOUT 次数、拉长会占用槽位，需权衡。
3. **429 韧性问题**独立于 RPM 实验：若 429 风暴复现，attempt_limit=3 必然不够——可考虑（a）对 429 使用更长专用退避/尊重 Retry-After（需日志先支持记录 header）；（b）429 不计入 attempt_limit 或单独限额。属于代码改动，需另行立项，本轮未动。
4. 若进入 RPM 单变量实验：建议先以 60 → 120 一档为准（本数据不支撑直接跳 300）。
5. 观测改进建议（不改行为，仅加日志）：在 worker/provider 日志中记录 response header 的 Retry-After 与 request-id——本轮多项"数据不足"均源于此。

---

### 附：机器可读数据

- `investigation/run_01..05/`：run.json / summary.json / attempts.csv / worker_logs.txt / api_logs.txt / round_meta.json
- `investigation/analysis.json`：全部分析结果（分布、分桶、waits、案例）
- `investigation/FROZEN-CONFIG.txt`：冻结配置与文件 hash
- `/tmp/latency_baseline/`：以上数据的副本 + summary.json
