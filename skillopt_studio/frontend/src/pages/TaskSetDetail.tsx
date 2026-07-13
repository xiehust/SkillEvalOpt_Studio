import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api, ApiError, TaskItem, TaskSetDetail } from "../api";
import TaskItemsEditor, { emptyItem, validateItems } from "../components/TaskItemsEditor";
import { Card, ErrorBanner, Mono, PageHeader, SampleTag, Spinner, truncate } from "../components/ui";

const SPLIT_ORDER = ["tasks", "train", "val", "test"];

/** train/val (and single's tasks) must keep >=1 task; test may be emptied to delete it. */
const OPTIONAL_SPLITS = new Set(["test"]);

export default function TaskSetDetailPage() {
  const { t } = useTranslation("tasksets");
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
    const name = window.prompt(t("detail.copyPrompt"), `${detail.info.name} ${t("detail.copySuffix")}`);
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
    if (!window.confirm(t("detail.discardConfirm"))) return;
    setEditing(false);
    setEditSplits(null);
    setSaveError(null);
  };

  const save = async () => {
    if (!detail || !editSplits) return;
    setSaveError(null);
    if (!editName.trim()) {
      setSaveError(t("detail.nameEmpty"));
      return;
    }
    const payload: Record<string, TaskItem[]> = {};
    const problems: string[] = [];
    for (const [split, items] of Object.entries(editSplits)) {
      if (OPTIONAL_SPLITS.has(split) && items.length === 0) continue; // emptied test = delete it
      for (const message of validateItems(items)) problems.push(t("detail.splitPrefix", { split, message }));
      payload[split] = items;
    }
    if (Object.keys(payload).length === 0) problems.push(t("detail.tasksetEmpty"));
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
            t("detail.titleFallback")
          )
        }
        sub={
          detail
            ? t("detail.subtitle", { mode: detail.info.mode, count: detail.info.task_count }) +
              (editing ? t("detail.editingSuffix") : t("detail.previewSuffix")) +
              (isSample ? t("detail.sampleReadonly") : "")
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
                {loadingEdit ? t("detail.loadingFull") : t("detail.editBtn")}
              </button>
            )}
            {detail && isSample && (
              <button
                className="btn-primary"
                onClick={saveCopy}
                disabled={copying}
                data-testid="taskset-save-copy"
              >
                {copying ? t("detail.copying") : t("detail.saveCopyBtn")}
              </button>
            )}
            <Link to="/tasksets" className="btn-ghost">{t("detail.backToTasksets")}</Link>
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
        <div className="mb-4 border border-good/50 bg-good/10 px-3 py-2 text-sm text-good" data-testid="taskset-saved">
          {t("detail.savedNotice", { time: savedAt })}
        </div>
      )}
      {!detail && !error && <Spinner />}

      {detail && editing && editSplits && (
        <Card title={t("detail.editCardTitle")} className="mb-6">
          <div className="space-y-4">
            <div className="max-w-md">
              <label className="label">{t("detail.nameLabel")}</label>
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
                  {split === "tasks" ? t("detail.tasksLabel") : t("detail.splitGroup", { split })}
                  {OPTIONAL_SPLITS.has(split) && (
                    <span className="text-muted">{t("detail.emptyToDelete")}</span>
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
                {t("detail.addTest")}
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
                {saving ? t("saveBtn.saving") : t("saveBtn.idle")}
              </button>
              <button className="btn-ghost" onClick={cancelEdit} data-testid="taskset-cancel">
                {t("common:actions.cancel")}
              </button>
            </div>
          </div>
        </Card>
      )}

      {detail &&
        !editing &&
        Object.entries(detail.tasks_by_split).map(([split, tasks]) => (
          <Card key={split} title={t("detail.splitCardTitle", { split, count: detail.info.counts_by_split[split] ?? tasks.length })} className="mb-6">
            <div className="overflow-x-auto -m-4">
              <table className="w-full" data-testid={`tasks-table-${split}`}>
                <thead>
                  <tr>
                    <th className="th">{t("detail.table.id")}</th>
                    <th className="th">{t("detail.table.type")}</th>
                    <th className="th">{t("detail.table.question")}</th>
                    <th className="th">{t("detail.table.rubric")}</th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((task) => (
                    <tr key={task.id} className="hover:bg-panel2/40">
                      <td className="td"><Mono className="text-s1">{task.id}</Mono></td>
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
