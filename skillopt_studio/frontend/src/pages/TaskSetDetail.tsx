import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api, ApiError, JobInfo, TaskItem, TaskSetDetail } from "../api";
import GenerateTaskSetForm from "../components/GenerateTaskSetForm";
import TaskItemsEditor, {
  emptyItem, nextTaskId, validateItems,
} from "../components/TaskItemsEditor";
import {
  Card, ErrorBanner, Mono, PageHeader, SampleTag, Spinner, StatusPill, truncate,
} from "../components/ui";

const SPLIT_ORDER = ["tasks", "train", "val", "test"];

/** train/val (and single's tasks) must keep >=1 task; test may be emptied to delete it. */
const OPTIONAL_SPLITS = new Set(["test"]);

interface AppendGeneratedState {
  appendGeneratedTasks?: TaskItem[];
  targetSplit?: string;
  sourceJobId?: string;
}

/** Append without overwriting any ID in any split; residual collisions get nextTaskId. */
export function mergeGeneratedTasks(
  splits: Record<string, TaskItem[]>,
  targetSplit: string,
  generated: TaskItem[],
): Record<string, TaskItem[]> {
  const allItems = Object.values(splits).flat();
  const used = new Set(allItems.map((item) => item.id));
  const appended: TaskItem[] = [];
  for (const task of generated) {
    const merged = used.has(task.id)
      ? { ...task, id: nextTaskId([...allItems, ...appended]) }
      : task;
    used.add(merged.id);
    appended.push(merged);
  }
  return {
    ...splits,
    [targetSplit]: [...(splits[targetSplit] ?? []), ...appended],
  };
}

function isActiveJob(job: JobInfo | null): boolean {
  return job?.status === "queued" || job?.status === "running";
}

export default function TaskSetDetailPage() {
  const { t } = useTranslation("tasksets");
  const { id = "" } = useParams();
  const location = useLocation();
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
  const [expansionOpen, setExpansionOpen] = useState(false);
  const [targetSplit, setTargetSplit] = useState("tasks");
  const [expansionJob, setExpansionJob] = useState<JobInfo | null>(null);
  const [expansionError, setExpansionError] = useState<string | null>(null);
  const [expansionNotice, setExpansionNotice] = useState<string | null>(null);
  const appliedJobIds = useRef(new Set<string>());
  const recoveringJobId = useRef<string | null>(null);
  const editLoadToken = useRef(0);
  const detailLoadToken = useRef(0);
  const editLoadInFlight = useRef(false);
  const currentIdRef = useRef(id);
  const editSplitsRef = useRef(editSplits);
  currentIdRef.current = id;
  editSplitsRef.current = editSplits;

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
    const requestedId = id;
    const loadToken = ++detailLoadToken.current;
    api
      .tasksetDetail(requestedId)
      .then((loaded) => {
        if (loadToken === detailLoadToken.current && requestedId === currentIdRef.current) {
          setDetail(loaded);
        }
      })
      .catch((err) => {
        if (loadToken === detailLoadToken.current && requestedId === currentIdRef.current) {
          setError(err instanceof ApiError ? err.message : String(err));
        }
      });
  };

  useEffect(() => {
    editLoadToken.current += 1;
    editLoadInFlight.current = false;
    setDetail(null);
    setError(null);
    setEditing(false);
    setEditSplits(null);
    setLoadingEdit(false);
    setSaveError(null);
    setSavedAt(null);
    setExpansionOpen(false);
    setExpansionJob(null);
    setExpansionError(null);
    setExpansionNotice(null);
    appliedJobIds.current.clear();
    recoveringJobId.current = null;
    loadDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const enterEdit = async ({ openExpansion = false }: { openExpansion?: boolean } = {}) => {
    if (editLoadInFlight.current) return;
    // Editing must start from the FULL task list — the read view is a preview.
    const requestedId = id;
    const loadToken = ++editLoadToken.current;
    editLoadInFlight.current = true;
    setLoadingEdit(true);
    setSaveError(null);
    setSavedAt(null);
    try {
      const full = await api.tasksetDetail(requestedId, true);
      if (loadToken !== editLoadToken.current || requestedId !== currentIdRef.current) return;
      setEditName(full.info.name);
      setEditSplits(JSON.parse(JSON.stringify(full.tasks_by_split)));
      setTargetSplit(full.info.mode === "single" ? "tasks" : Object.keys(full.tasks_by_split)[0] ?? "train");
      setExpansionOpen(openExpansion);
      setEditing(true);
    } catch (err) {
      if (loadToken === editLoadToken.current && requestedId === currentIdRef.current) {
        setSaveError(err instanceof ApiError ? err.message : String(err));
      }
    } finally {
      if (loadToken === editLoadToken.current && requestedId === currentIdRef.current) {
        editLoadInFlight.current = false;
        setLoadingEdit(false);
      }
    }
  };

  useEffect(() => {
    const state = location.state as AppendGeneratedState | null;
    const tasks = state?.appendGeneratedTasks;
    const restoreSplit = state?.targetSplit;
    const sourceJobId = state?.sourceJobId;
    if (!tasks?.length || !restoreSplit || !sourceJobId) return;
    if (recoveringJobId.current === sourceJobId || appliedJobIds.current.has(sourceJobId)) return;
    const requestedId = id;
    const loadToken = ++editLoadToken.current;
    recoveringJobId.current = sourceJobId;
    editLoadInFlight.current = true;
    setLoadingEdit(true);
    setSaveError(null);
    api.tasksetDetail(requestedId, true)
      .then((full) => {
        if (loadToken !== editLoadToken.current || requestedId !== currentIdRef.current) return;
        const allowed = full.info.mode === "single"
          ? restoreSplit === "tasks"
          : restoreSplit === "test" || restoreSplit in full.tasks_by_split;
        if (!allowed || full.info.sample) {
          throw new Error(t("detail.expand.invalidRestoreTarget"));
        }
        const latest = JSON.parse(JSON.stringify(full.tasks_by_split)) as Record<string, TaskItem[]>;
        setDetail(full);
        setEditName(full.info.name);
        setEditSplits(mergeGeneratedTasks(latest, restoreSplit, tasks));
        setTargetSplit(restoreSplit);
        setEditing(true);
        setExpansionNotice(t("detail.expand.appended", { count: tasks.length, split: restoreSplit }));
        appliedJobIds.current.add(sourceJobId);
        recoveringJobId.current = null;
        navigate(location.pathname, { replace: true, state: null });
      })
      .catch((err) => {
        if (loadToken !== editLoadToken.current || requestedId !== currentIdRef.current) return;
        recoveringJobId.current = null;
        setSaveError(err instanceof ApiError ? err.message : String(err));
      })
      .finally(() => {
        if (loadToken === editLoadToken.current && requestedId === currentIdRef.current) {
          editLoadInFlight.current = false;
          setLoadingEdit(false);
        }
      });
    // Consume router state only after the latest full task set has loaded and merged.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, location.state]);

  useEffect(() => {
    const jobId = expansionJob?.id;
    if (!jobId || !isActiveJob(expansionJob)) return;
    let active = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const current = await api.job(jobId);
        if (active) {
          setExpansionJob(current);
          setExpansionError(null);
        }
      } catch (err) {
        if (active) setExpansionError(err instanceof ApiError ? err.message : String(err));
      } finally {
        if (active) timer = window.setTimeout(poll, 2500);
      }
    };
    timer = window.setTimeout(poll, 2500);
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [expansionJob?.id, expansionJob?.status]);

  useEffect(() => {
    if (expansionJob?.status !== "succeeded") return;
    if (appliedJobIds.current.has(expansionJob.id) || !editSplits) return;
    appliedJobIds.current.add(expansionJob.id);
    const jobTasksetId = expansionJob.params.taskset_id;
    const jobTarget = expansionJob.params.target_split;
    const validTarget = typeof jobTarget === "string" && (
      detail?.info.mode === "single"
        ? jobTarget === "tasks"
        : jobTarget === "test" || jobTarget in editSplits
    );
    if (jobTasksetId !== id || !validTarget) {
      setExpansionError(t("detail.expand.invalidRestoreTarget"));
      return;
    }
    api.jobResults(expansionJob.id)
      .then((results) => {
        if (currentIdRef.current !== jobTasksetId) return;
        if (results.type !== "taskgen" || !Array.isArray(results.tasks) || results.tasks.length === 0) {
          throw new Error(t("detail.expand.emptyResult"));
        }
        if (!editSplitsRef.current) {
          throw new Error(t("detail.expand.noDraft"));
        }
        setEditSplits((current) => current
          ? mergeGeneratedTasks(current, jobTarget, results.tasks)
          : current);
        setExpansionNotice(t("detail.expand.appended", {
          count: results.tasks.length,
          split: jobTarget,
        }));
        setExpansionError(null);
      })
      .catch((err) => {
        if (currentIdRef.current === id) {
          setExpansionError(err instanceof ApiError ? err.message : String(err));
        }
      });
  }, [editSplits, expansionJob, t]);

  const onExpansionJobCreated = (job: JobInfo) => {
    setExpansionJob(job);
    setExpansionError(null);
    setExpansionNotice(null);
    setExpansionOpen(false);
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
  const expansionTargetOptions = detail?.info.mode === "single"
    ? ["tasks"]
    : SPLIT_ORDER.filter((split) => split !== "tasks" && (split in (editSplits ?? {}) || split === "test"));

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
              <>
                <button
                  className="btn-ghost"
                  onClick={() => void enterEdit({ openExpansion: true })}
                  disabled={loadingEdit}
                  data-testid="taskset-ai-expand-shortcut"
                >
                  {loadingEdit ? t("detail.loadingFull") : t("detail.expand.title")}
                </button>
                <button
                  className="btn-primary"
                  onClick={() => void enterEdit()}
                  disabled={loadingEdit}
                  data-testid="taskset-edit"
                >
                  {loadingEdit ? t("detail.loadingFull") : t("detail.editBtn")}
                </button>
              </>
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
      {saveError && !editing && (
        <div className="mb-4" data-testid="taskset-edit-error">
          <ErrorBanner message={saveError} />
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

            <div className="border border-line bg-panel2/30 p-3 space-y-3" data-testid="taskset-ai-expand">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="text-sm font-semibold">{t("detail.expand.title")}</div>
                  <div className="text-xs text-muted">{t("detail.expand.hint")}</div>
                </div>
                {!isActiveJob(expansionJob) && (
                  <button
                    type="button"
                    className="btn-ghost text-sm"
                    onClick={() => setExpansionOpen((open) => !open)}
                    data-testid="taskset-ai-expand-toggle"
                  >
                    {expansionOpen ? t("detail.expand.hide") : t("detail.expand.open")}
                  </button>
                )}
              </div>

              {expansionJob && (
                <div className="flex flex-wrap items-center gap-3 text-sm" data-testid="taskset-ai-expand-status">
                  <StatusPill status={expansionJob.status} />
                  <Mono className="text-xs">{expansionJob.id}</Mono>
                  <Link className="text-s1 hover:underline" to={`/jobs/${encodeURIComponent(expansionJob.id)}`}>
                    {t("detail.expand.jobDetail")}
                  </Link>
                  {expansionJob.error && <span className="text-critText">{expansionJob.error}</span>}
                </div>
              )}
              {expansionNotice && (
                <div className="text-sm text-good" data-testid="taskset-ai-expand-appended">
                  {expansionNotice}
                </div>
              )}
              {expansionError && (
                <div data-testid="taskset-ai-expand-error"><ErrorBanner message={expansionError} /></div>
              )}

              {expansionOpen && !isActiveJob(expansionJob) && (
                <div className="space-y-3" data-testid="taskset-ai-expand-form">
                  <div className="max-w-xs">
                    <label className="label">{t("detail.expand.targetSplit")}</label>
                    <select
                      className="input"
                      value={targetSplit}
                      disabled={detail.info.mode === "single"}
                      onChange={(event) => setTargetSplit(event.target.value)}
                      data-testid="taskset-ai-expand-target"
                    >
                      {expansionTargetOptions.map((split) => (
                        <option key={split} value={split}>{split}</option>
                      ))}
                    </select>
                  </div>
                  <GenerateTaskSetForm
                    extraJobParams={{ taskset_id: id, target_split: targetSplit }}
                    onJobCreated={onExpansionJobCreated}
                  />
                </div>
              )}
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
