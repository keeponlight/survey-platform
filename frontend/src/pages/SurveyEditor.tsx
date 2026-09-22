// SPDX-License-Identifier: GPL-3.0-or-later
import { useEffect, useState } from "react";

import { ApiError, api, type QuestionOption, type Survey, type SurveyInputPayload } from "../api";
import { ErrorBanner } from "../components";
import { formatLocalTime } from "../format";
import { routes } from "../router";

/** 五档固定契约（主文档 §5.1）——前端只读展示，不可编辑。 */
const FIXED_OPTIONS: QuestionOption[] = [
  { value: "definitely_not", label: "肯定不会购买", score: 1 },
  { value: "probably_not", label: "可能不会购买", score: 2 },
  { value: "unsure", label: "不确定", score: 3 },
  { value: "probably_yes", label: "可能会购买", score: 4 },
  { value: "definitely_yes", label: "肯定会购买", score: 5 },
];

const DEFAULT_PROMPT =
  "结合你的实际需求、预算和现有替代方案，按照上述价格与购买条件，你在未来 30 天购买该产品的意向是？";

interface Draft {
  title: string;
  name: string;
  description: string;
  price: string;
  priceUnit: string;
  timeRange: string;
  conditions: string;
  prompt: string;
}

function emptyDraft(): Draft {
  return {
    title: "购买意向问卷",
    name: "",
    description: "",
    price: "",
    priceUnit: "元/件",
    timeRange: "未来 30 天",
    conditions: "",
    prompt: DEFAULT_PROMPT,
  };
}

function draftFromSurvey(survey: Survey): Draft {
  return {
    title: survey.title,
    name: survey.product.name,
    description: survey.product.description,
    price: survey.product.price,
    priceUnit: survey.product.price_unit,
    timeRange: survey.product.time_range,
    conditions: survey.product.purchase_conditions ?? "",
    prompt: survey.question.prompt,
  };
}

function toPayload(draft: Draft): SurveyInputPayload {
  return {
    title: draft.title,
    product: {
      name: draft.name,
      description: draft.description,
      price: draft.price,
      price_unit: draft.priceUnit,
      time_range: draft.timeRange,
      purchase_conditions: draft.conditions === "" ? null : draft.conditions,
    },
    question: {
      question_id: "purchase_intent",
      prompt: draft.prompt,
      type: "single_choice",
      options: FIXED_OPTIONS,
      required: true,
    },
  };
}

export default function SurveyEditor(): JSX.Element {
  const [surveys, setSurveys] = useState<Survey[]>([]);
  const [selected, setSelected] = useState<Survey | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);

  async function reload(): Promise<void> {
    try {
      const result = await api.listSurveys();
      setSurveys(result.items);
      setError(null);
    } catch (err: unknown) {
      setError(err);
    }
  }

  useEffect(() => {
    void reload();
  }, []);

  function set<K extends keyof Draft>(key: K, value: Draft[K]): void {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  async function handleCreate(): Promise<void> {
    setBusy(true);
    setInfo("");
    try {
      const created = await api.createSurvey(toPayload(draft));
      setSelected(created);
      setDraft(draftFromSurvey(created));
      setInfo(`已创建问卷（revision ${created.revision}）。`);
      await reload();
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function handleSave(): Promise<void> {
    if (selected === null) return;
    setBusy(true);
    setInfo("");
    try {
      const updated = await api.patchSurvey(selected.id, selected.revision, toPayload(draft));
      setSelected(updated);
      setInfo(`已保存，revision 更新为 ${updated.revision}（历史批次使用各自快照，不受影响）。`);
      await reload();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409) {
        setError(err);
        setInfo("revision 冲突：问卷已被修改，请重新载入最新版本后再编辑。");
        await reload();
      } else {
        setError(err);
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleClone(): Promise<void> {
    if (selected === null) return;
    setBusy(true);
    setInfo("");
    try {
      const payload = toPayload(draft);
      const cloned = await api.createSurvey({ ...payload, title: `${draft.title} (copy)` });
      setSelected(cloned);
      setDraft(draftFromSurvey(cloned));
      setInfo("已复制为新草稿（revision 1）。");
      await reload();
    } catch (err: unknown) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h2>问卷列表 / 编辑</h2>
      <p className="muted small">
        首版固定单题 <code>purchase_intent</code> 与五档选项；产品名称、说明、实际人民币价格、计价单位、购买时间范围为必填。
      </p>
      <ErrorBanner error={error} />
      {info !== "" ? <div className="ok small">{info}</div> : null}

      <section className="panel">
        <div className="row">
          <button type="button" onClick={() => {
            setSelected(null);
            setDraft(emptyDraft());
            setInfo("已切换到新建草稿。");
            setError(null);
          }}>
            新建草稿
          </button>
          <button type="button" className="primary" disabled={busy} onClick={() => void handleCreate()}>
            创建问卷
          </button>
          <button type="button" disabled={busy || selected === null} onClick={() => void handleSave()}>
            保存（PATCH，带 expected_revision）
          </button>
          <button type="button" disabled={busy || selected === null} onClick={() => void handleClone()}>
            复制为新草稿
          </button>
        </div>
      </section>

      <section className="panel">
        <h3>{selected === null ? "新建问卷" : `编辑问卷 · ${selected.id}`}</h3>
        {selected !== null ? (
          <div className="row small muted">
            <span>revision: {selected.revision}</span>
            <span>created_at: {formatLocalTime(selected.created_at)}</span>
            <span>updated_at: {formatLocalTime(selected.updated_at)}</span>
          </div>
        ) : null}

        <div className="field">
          <label htmlFor="survey-title">问卷标题</label>
          <input id="survey-title" value={draft.title} onChange={(event) => set("title", event.target.value)} />
        </div>

        <div className="grid">
          <div className="field">
            <label htmlFor="product-name">产品名称 *</label>
            <input id="product-name" value={draft.name} onChange={(event) => set("name", event.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="product-price">实际人民币价格 *</label>
            <input id="product-price" value={draft.price} onChange={(event) => set("price", event.target.value)} placeholder="例如 199.00" />
          </div>
          <div className="field">
            <label htmlFor="product-price-unit">计价单位 *</label>
            <input id="product-price-unit" value={draft.priceUnit} onChange={(event) => set("priceUnit", event.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="product-time-range">购买时间范围 *</label>
            <input id="product-time-range" value={draft.timeRange} onChange={(event) => set("timeRange", event.target.value)} />
          </div>
        </div>

        <div className="field">
          <label htmlFor="product-description">产品说明 *</label>
          <textarea id="product-description" rows={3} value={draft.description} onChange={(event) => set("description", event.target.value)} />
        </div>

        <div className="field">
          <label htmlFor="product-conditions">购买条件（可选）</label>
          <textarea id="product-conditions" rows={2} value={draft.conditions} onChange={(event) => set("conditions", event.target.value)} />
        </div>

        <div className="field">
          <label htmlFor="question-prompt">题干（题目 ID 固定为 purchase_intent）</label>
          <textarea id="question-prompt" rows={3} value={draft.prompt} onChange={(event) => set("prompt", event.target.value)} />
        </div>

        <h3>固定五档选项（只读）</h3>
        <table>
          <thead>
            <tr>
              <th>value</th>
              <th>中文展示</th>
              <th>score</th>
            </tr>
          </thead>
          <tbody>
            {FIXED_OPTIONS.map((option) => (
              <tr key={option.value}>
                <td>{option.value}</td>
                <td>{option.label}</td>
                <td>{option.score}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="row" style={{ marginTop: 12 }}>
          <button
            type="button"
            disabled={selected === null}
            onClick={() => {
              if (selected !== null) routes.runCreate(selected.id);
            }}
          >
            用此问卷配置运行 →
          </button>
        </div>
      </section>

      <section className="panel">
        <h3>已有问卷（{surveys.length}）</h3>
        <table>
          <thead>
            <tr>
              <th>title</th>
              <th>revision</th>
              <th>updated_at</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {surveys.map((survey) => (
              <tr key={survey.id}>
                <td>{survey.title}</td>
                <td>{survey.revision}</td>
                <td>{formatLocalTime(survey.updated_at)}</td>
                <td>
                  <button
                    type="button"
                    onClick={() => {
                      setSelected(survey);
                      setDraft(draftFromSurvey(survey));
                      setInfo(`已载入问卷 ${survey.id}（revision ${survey.revision}）。`);
                      setError(null);
                    }}
                  >
                    载入
                  </button>
                </td>
              </tr>
            ))}
            {surveys.length === 0 ? (
              <tr>
                <td colSpan={4} className="muted">
                  暂无问卷。
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </section>
    </div>
  );
}
