import { FormEvent, useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError, TaskItem, TaskSetInfo } from "../api";
import GenerateTaskSetForm from "../components/GenerateTaskSetForm";
import TaskItemsEditor, { emptyItem, validateItems } from "../components/TaskItemsEditor";
import TaskSetFormatDoc from "../components/TaskSetFormatDoc";
import { Card, EmptyState, ErrorBanner, Mono, PageHeader, Spinner, formatTime } from "../components/ui";

interface ImportState {
  importItems?: TaskItem[];
  importName?: string;
}

export default function TaskSets() {
  const location = useLocation();
  const navigate = useNavigate();
  const [tasksets, setTasksets] = useState<TaskSetInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [imported, setImported] = useState<{ items: TaskItem[]; name: string } | null>(null);

  const reload = async () => {
    try {
      setTasksets(await api.tasksets());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  };

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 作业详情页“导入为新任务集”通过 router state 传入生成条目;消费后立即清掉,
  // 刷新/返回不会重复导入。
  useEffect(() => {
    const state = location.state as ImportState | null;
    if (state?.importItems?.length) {
      setImported({ items: state.importItems, name: state.importName ?? "" });
      setShowForm(true);
      navigate(location.pathname, { replace: true, state: null });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.state]);

  const onDelete = async (id: string) => {
    if (!window.confirm(`确定删除任务集 ${id} 吗?`)) return;
    try {
      await api.deleteTaskset(id);
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  };

  return (
    <div>
      <PageHeader
        title="任务集"
        sub="skilleval 任务文件(每个任务自带 rubric 判分标准),保存前会做严格校验"
        actions={
          <button className="btn-primary" onClick={() => setShowForm((visible) => !visible)}>
            {showForm ? "收起表单" : "新建任务集"}
          </button>
        }
      />

      {error && <ErrorBanner message={error} />}
      {showForm && (
        <CreateTaskSetForm
          onCreated={() => { setShowForm(false); setImported(null); reload(); }}
          imported={imported}
        />
      )}

      {tasksets === null && !error && <Spinner />}
      {tasksets !== null && tasksets.length === 0 && (
        <EmptyState
          title="还没有任务集"
          hint='上传一个 tasks.json,或在“新建任务集”里手动逐条输入(每项含 id / question / rubric)。'
          action={
            <button className="btn-primary" onClick={() => setShowForm(true)}>新建任务集</button>
          }
        />
      )}

      {tasksets !== null && tasksets.length > 0 && (
        <Card>
          <div className="overflow-x-auto -m-4">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">名称</th>
                  <th className="th">模式</th>
                  <th className="th">任务数</th>
                  <th className="th">分布</th>
                  <th className="th">创建时间</th>
                  <th className="th">更新时间</th>
                  <th className="th"></th>
                </tr>
              </thead>
              <tbody>
                {tasksets.map((taskset) => (
                  <tr key={taskset.id} className="hover:bg-panel2/40" data-taskset-id={taskset.id}>
                    <td className="td">
                      <Link to={`/tasksets/${encodeURIComponent(taskset.id)}`} className="text-cyan hover:underline">
                        {taskset.name}
                      </Link>
                      <Mono className="block text-[11px] text-muted/70">{taskset.id}</Mono>
                    </td>
                    <td className="td">
                      <Mono className="text-xs">{taskset.mode === "single" ? "single(单文件)" : "split(预分割)"}</Mono>
                    </td>
                    <td className="td"><Mono>{taskset.task_count}</Mono></td>
                    <td className="td">
                      <Mono className="text-xs text-muted">
                        {Object.entries(taskset.counts_by_split)
                          .map(([split, count]) => `${split}:${count}`)
                          .join(" · ")}
                      </Mono>
                    </td>
                    <td className="td text-muted text-xs">{formatTime(taskset.created_at)}</td>
                    <td className="td text-muted text-xs">
                      {taskset.updated_at ? formatTime(taskset.updated_at) : "—"}
                    </td>
                    <td className="td text-right">
                      <button className="btn-danger !px-2 !py-1 text-xs" onClick={() => onDelete(taskset.id)}>
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

type CreateTab = "upload" | "manual" | "generate";

const CREATE_TABS: { key: CreateTab; label: string }[] = [
  { key: "upload", label: "上传 JSON 文件" },
  { key: "manual", label: "手动逐条输入" },
  { key: "generate", label: "AI 自动生成" },
];

function CreateTaskSetForm({
  onCreated,
  imported,
}: {
  onCreated: () => void;
  imported: { items: TaskItem[]; name: string } | null;
}) {
  const [tab, setTab] = useState<CreateTab>(imported ? "manual" : "upload");
  const [name, setName] = useState(imported?.name ?? "");

  useEffect(() => {
    if (imported) {
      setTab("manual");
      if (imported.name) setName(imported.name);
    }
  }, [imported]);

  return (
    <Card title="新建任务集" className="mb-6">
      <div className="space-y-4 max-w-3xl">
        <div className="flex gap-2" data-testid="taskset-tabs">
          {CREATE_TABS.map((entry) => (
            <button
              key={entry.key}
              type="button"
              className={
                tab === entry.key
                  ? "btn-primary !px-3 !py-1.5 text-sm"
                  : "btn-ghost !px-3 !py-1.5 text-sm"
              }
              onClick={() => setTab(entry.key)}
              data-testid={`taskset-tab-${entry.key}`}
            >
              {entry.label}
            </button>
          ))}
        </div>

        {tab !== "generate" && (
          <>
            <div>
              <label className="label">名称</label>
              <input
                className="input"
                value={name}
                placeholder="例如:报表生成回归集"
                onChange={(event) => setName(event.target.value)}
                data-testid="taskset-name"
              />
            </div>
            <TaskSetFormatDoc />
          </>
        )}

        {tab === "upload" && <UploadTaskSetForm name={name} onCreated={onCreated} />}
        {tab === "manual" && (
          <ManualTaskSetForm name={name} onCreated={onCreated} importedItems={imported?.items ?? null} />
        )}
        {tab === "generate" && <GenerateTaskSetForm />}
      </div>
    </Card>
  );
}

function UploadTaskSetForm({ name, onCreated }: { name: string; onCreated: () => void }) {
  const [mode, setMode] = useState<"single" | "split">("single");
  const [files, setFiles] = useState<Record<string, File>>({});
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const setFile = (key: string, file: File | undefined) => {
    setFiles((current) => {
      const next = { ...current };
      if (file) next[key] = file;
      else delete next[key];
      return next;
    });
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setFormError(null);
    if (!name.trim()) {
      setFormError("请填写任务集名称");
      return;
    }
    const required = mode === "single" ? ["tasks"] : ["train", "val"];
    const missing = required.filter((key) => !files[key]);
    if (missing.length > 0) {
      setFormError(`缺少文件:${missing.join("、")}`);
      return;
    }
    setSubmitting(true);
    try {
      const selected: Record<string, File> = {};
      const keys = mode === "single" ? ["tasks"] : ["train", "val", "test"];
      for (const key of keys) if (files[key]) selected[key] = files[key];
      await api.createTaskset(name.trim(), mode, selected);
      onCreated();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-4" data-testid="taskset-form">
      <div>
        <label className="label">模式</label>
        <select
          className="input"
          value={mode}
          onChange={(event) => setMode(event.target.value as "single" | "split")}
        >
          <option value="single">single — 单个 tasks.json(训练时按比例自动分割)</option>
          <option value="split">split — 预分割 train / val / test</option>
        </select>
      </div>

      {mode === "single" ? (
        <div>
          <label className="label">tasks.json</label>
          <input
            type="file"
            accept=".json,.jsonl"
            className="input"
            data-testid="taskset-file-tasks"
            onChange={(event) => setFile("tasks", event.target.files?.[0])}
          />
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-3">
          {(["train", "val", "test"] as const).map((split) => (
            <div key={split}>
              <label className="label">
                {split}.json{split === "test" ? "(可选)" : ""}
              </label>
              <input
                type="file"
                accept=".json,.jsonl"
                className="input"
                data-testid={`taskset-file-${split}`}
                onChange={(event) => setFile(split, event.target.files?.[0])}
              />
            </div>
          ))}
        </div>
      )}

      {formError && (
        <div data-testid="taskset-error">
          <ErrorBanner message={formError} />
        </div>
      )}

      <button type="submit" className="btn-primary" disabled={submitting} data-testid="taskset-submit">
        {submitting ? "校验并保存中…" : "校验并保存"}
      </button>
    </form>
  );
}

function ManualTaskSetForm({
  name,
  onCreated,
  importedItems,
}: {
  name: string;
  onCreated: () => void;
  importedItems: TaskItem[] | null;
}) {
  const [items, setItems] = useState<TaskItem[]>(() =>
    importedItems?.length ? importedItems : [emptyItem([])],
  );
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    if (importedItems?.length) setItems(importedItems);
  }, [importedItems]);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setFormError(null);
    if (!name.trim()) {
      setFormError("请填写任务集名称");
      return;
    }
    const problems = validateItems(items);
    if (problems.length > 0) {
      setFormError(problems.join("\n"));
      return;
    }
    setSubmitting(true);
    try {
      await api.createTasksetItems(name.trim(), "single", { tasks: items });
      onCreated();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-4" data-testid="taskset-manual-form">
      {importedItems && importedItems.length > 0 && (
        <div className="rounded border border-cyan/50 bg-cyan/10 px-3 py-2 text-sm text-cyan" data-testid="taskset-import-notice">
          已导入 {importedItems.length} 条 AI 生成任务,请审阅后保存
        </div>
      )}
      <p className="text-xs text-muted">
        手动录入生成 single 模式任务集(训练时按比例自动分割);需要预分割 train / val / test 请改用文件上传。
      </p>
      <TaskItemsEditor items={items} onChange={setItems} />
      {formError && (
        <div data-testid="taskset-manual-error">
          <ErrorBanner message={formError} />
        </div>
      )}
      <button type="submit" className="btn-primary" disabled={submitting} data-testid="taskset-manual-submit">
        {submitting ? "校验并保存中…" : "校验并保存"}
      </button>
    </form>
  );
}
