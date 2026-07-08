import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, TaskItem, TaskSetDetail } from "../api";
import TaskItemsEditor, { emptyItem, validateItems } from "../components/TaskItemsEditor";
import { Card, ErrorBanner, Mono, PageHeader, SampleTag, Spinner, truncate } from "../components/ui";

const SPLIT_ORDER = ["tasks", "train", "val", "test"];

/** train/val (and single's tasks) must keep >=1 task; test may be emptied to delete it. */
const OPTIONAL_SPLITS = new Set(["test"]);

export default function TaskSetDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<TaskSetDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editSplits, setEditSplits] = useState<Record<string, TaskItem[]> | null>(null);
  const [loadingEdit, setLoadingEdit] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [copying, setCopying] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);

  const isSample = detail?.info.sample === true;

  const saveCopy = async () => {
    if (!detail) return;
    const name = window.prompt("新任务集名称:", `${detail.info.name} 副本`);
    if (!name || !name.trim()) return;
    setCopying(true);
    setCopyError(null);
    try {
      // 样例详情默认是预览(每分组截断 20 条),副本必须从全量数据创建
      const full = await api.tasksetDetail(id, true);
      const created = await api.createTasksetItems(
        name.trim(),
        full.info.mode,
        full.tasks_by_split,
      );
      navigate(`/tasksets/${encodeURIComponent(created.id)}`);
    } catch (err) {
      setCopyError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setCopying(false);
    }
  };

  const loadDetail = () => {
    api
      .tasksetDetail(id)
      .then(setDetail)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
  };

  useEffect(() => {
    setDetail(null);
    setError(null);
    setEditing(false);
    setEditSplits(null);
    setSavedAt(null);
    loadDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const enterEdit = async () => {
    // Editing must start from the FULL task list — the read view is a preview.
    setLoadingEdit(true);
    setSaveError(null);
    setSavedAt(null);
    try {
      const full = await api.tasksetDetail(id, true);
      setEditName(full.info.name);
      setEditSplits(JSON.parse(JSON.stringify(full.tasks_by_split)));
      setEditing(true);
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoadingEdit(false);
    }
  };

  const cancelEdit = () => {
    if (!window.confirm("放弃未保存的修改?")) return;
    setEditing(false);
    setEditSplits(null);
    setSaveError(null);
  };

  const save = async () => {
    if (!detail || !editSplits) return;
    setSaveError(null);
    if (!editName.trim()) {
      setSaveError("名称不能为空");
      return;
    }
    const payload: Record<string, TaskItem[]> = {};
    const problems: string[] = [];
    for (const [split, items] of Object.entries(editSplits)) {
      if (OPTIONAL_SPLITS.has(split) && items.length === 0) continue; // emptied test = delete it
      for (const message of validateItems(items)) problems.push(`${split}:${message}`);
      payload[split] = items;
    }
    if (Object.keys(payload).length === 0) problems.push("任务集不能为空");
    if (problems.length > 0) {
      setSaveError(problems.join("\n"));
      return;
    }
    setSaving(true);
    try {
      await api.updateTaskset(id, { name: editName.trim(), tasks_by_split: payload });
      setEditing(false);
      setEditSplits(null);
      setSavedAt(new Date().toLocaleTimeString());
      loadDetail();
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const editableSplits = editSplits
    ? SPLIT_ORDER.filter((split) => split in editSplits)
    : [];
  const canAddTest =
    editing && detail?.info.mode === "split" && editSplits !== null && !("test" in editSplits);

  return (
    <div>
      <PageHeader
        title={
          detail ? (
            <span className="flex items-center gap-2">
              {detail.info.name}
              {isSample && <SampleTag />}
            </span>
          ) : (
            "任务集详情"
          )
        }
        sub={
          detail
            ? `模式 ${detail.info.mode} · 共 ${detail.info.task_count} 个任务` +
              (editing ? "(编辑中)" : "(每分组最多预览 20 条)") +
              (isSample ? " · 内置样例为只读,可另存副本后编辑" : "")
            : undefined
        }
        actions={
          <div className="flex gap-2">
            {detail && !editing && !isSample && (
              <button
                className="btn-primary"
                onClick={enterEdit}
                disabled={loadingEdit}
                data-testid="taskset-edit"
              >
                {loadingEdit ? "加载全量任务…" : "编辑任务集"}
              </button>
            )}
            {detail && isSample && (
              <button
                className="btn-primary"
                onClick={saveCopy}
                disabled={copying}
                data-testid="taskset-save-copy"
              >
                {copying ? "复制中…" : "另存为我的任务集"}
              </button>
            )}
            <Link to="/tasksets" className="btn-ghost">返回任务集</Link>
          </div>
        }
      />

      {error && <ErrorBanner message={error} />}
      {copyError && (
        <div className="mb-4" data-testid="taskset-copy-error">
          <ErrorBanner message={copyError} />
        </div>
      )}
      {savedAt && !editing && (
        <div className="mb-4 rounded border border-green/50 bg-green/10 px-3 py-2 text-sm text-green" data-testid="taskset-saved">
          已保存({savedAt})— 编辑将影响后续使用该任务集的评估/训练运行
        </div>
      )}
      {!detail && !error && <Spinner />}

      {detail && editing && editSplits && (
        <Card title="编辑任务集" className="mb-6">
          <div className="space-y-4">
            <div className="max-w-md">
              <label className="label">名称</label>
              <input
                className="input"
                value={editName}
                onChange={(event) => setEditName(event.target.value)}
                data-testid="taskset-rename"
              />
            </div>

            {editableSplits.map((split) => (
              <div key={split}>
                <div className="label mb-2">
                  {split === "tasks" ? "任务列表" : `${split} 分组`}
                  {OPTIONAL_SPLITS.has(split) && (
                    <span className="text-muted">(清空全部任务并保存 = 删除该分组)</span>
                  )}
                </div>
                <TaskItemsEditor
                  items={editSplits[split]}
                  onChange={(items) => setEditSplits({ ...editSplits, [split]: items })}
                />
              </div>
            ))}

            {canAddTest && (
              <button
                type="button"
                className="btn-ghost text-sm"
                onClick={() => setEditSplits({ ...editSplits, test: [emptyItem([])] })}
                data-testid="taskset-add-test"
              >
                + 添加 test 分组
              </button>
            )}

            {saveError && (
              <div data-testid="taskset-save-error">
                <ErrorBanner message={saveError} />
              </div>
            )}

            <div className="flex gap-2">
              <button
                className="btn-primary"
                onClick={save}
                disabled={saving}
                data-testid="taskset-save"
              >
                {saving ? "校验并保存中…" : "校验并保存"}
              </button>
              <button className="btn-ghost" onClick={cancelEdit} data-testid="taskset-cancel">
                取消
              </button>
            </div>
          </div>
        </Card>
      )}

      {detail &&
        !editing &&
        Object.entries(detail.tasks_by_split).map(([split, tasks]) => (
          <Card key={split} title={`${split}(${detail.info.counts_by_split[split] ?? tasks.length} 条)`} className="mb-6">
            <div className="overflow-x-auto -m-4">
              <table className="w-full" data-testid={`tasks-table-${split}`}>
                <thead>
                  <tr>
                    <th className="th">ID</th>
                    <th className="th">类型</th>
                    <th className="th">问题</th>
                    <th className="th">评分标准(rubric)</th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((task) => (
                    <tr key={task.id} className="hover:bg-panel2/40">
                      <td className="td"><Mono className="text-cyan">{task.id}</Mono></td>
                      <td className="td"><Mono className="text-xs text-muted">{task.task_type ?? "default"}</Mono></td>
                      <td className="td text-sm max-w-md">{truncate(task.question, 120)}</td>
                      <td className="td text-sm text-muted max-w-md">{truncate(task.rubric, 120)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ))}
    </div>
  );
}
