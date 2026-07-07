import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, TaskSetInfo } from "../api";
import { Card, EmptyState, ErrorBanner, Mono, PageHeader, Spinner, formatTime } from "../components/ui";

export default function TaskSets() {
  const [tasksets, setTasksets] = useState<TaskSetInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

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
      {showForm && <CreateTaskSetForm onCreated={() => { setShowForm(false); reload(); }} />}

      {tasksets === null && !error && <Spinner />}
      {tasksets !== null && tasksets.length === 0 && (
        <EmptyState
          title="还没有任务集"
          hint='上传一个 tasks.json(JSON 数组,每项含 id / question / rubric)即可开始。'
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

function CreateTaskSetForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
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
    <Card title="新建任务集" className="mb-6">
      <form onSubmit={onSubmit} className="space-y-4 max-w-2xl" data-testid="taskset-form">
        <div className="grid gap-4 md:grid-cols-2">
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
    </Card>
  );
}
