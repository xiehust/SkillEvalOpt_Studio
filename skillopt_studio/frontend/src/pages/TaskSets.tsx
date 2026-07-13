import { FormEvent, useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api, ApiError, TaskItem, TaskSetInfo } from "../api";
import GenerateTaskSetForm from "../components/GenerateTaskSetForm";
import TaskItemsEditor, { emptyItem, validateItems } from "../components/TaskItemsEditor";
import TaskSetFormatDoc from "../components/TaskSetFormatDoc";
import { Card, EmptyState, ErrorBanner, Mono, PageHeader, SampleTag, Spinner, formatTime } from "../components/ui";

interface ImportState {
  importItems?: TaskItem[];
  importName?: string;
}

export default function TaskSets() {
  const { t } = useTranslation("tasksets");
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
    if (!window.confirm(t("deleteConfirm", { id }))) return;
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
        title={t("list.title")}
        sub={t("list.subtitle")}
        actions={
          <button className="btn-primary" onClick={() => setShowForm((visible) => !visible)}>
            {showForm ? t("list.hideForm") : t("list.newTaskset")}
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
          title={t("list.empty.title")}
          hint={t("list.empty.hint")}
          action={
            <button className="btn-primary" onClick={() => setShowForm(true)}>{t("list.newTaskset")}</button>
          }
        />
      )}

      {tasksets !== null && tasksets.length > 0 && (
        <Card>
          <div className="overflow-x-auto -m-4">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">{t("list.table.name")}</th>
                  <th className="th">{t("list.table.mode")}</th>
                  <th className="th">{t("list.table.count")}</th>
                  <th className="th">{t("list.table.distribution")}</th>
                  <th className="th">{t("list.table.created")}</th>
                  <th className="th">{t("list.table.updated")}</th>
                  <th className="th"></th>
                </tr>
              </thead>
              <tbody>
                {tasksets.map((taskset) => (
                  <tr key={taskset.id} className="hover:bg-panel2/40" data-taskset-id={taskset.id}>
                    <td className="td">
                      <span className="flex items-center gap-2">
                        <Link to={`/tasksets/${encodeURIComponent(taskset.id)}`} className="text-s1 hover:underline">
                          {taskset.name}
                        </Link>
                        {taskset.sample && <SampleTag />}
                      </span>
                      <Mono className="block text-[11px] text-muted/70">{taskset.id}</Mono>
                    </td>
                    <td className="td">
                      <Mono className="text-xs">{taskset.mode === "single" ? t("list.modeCell.single") : t("list.modeCell.split")}</Mono>
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
                      {!taskset.sample && (
                        <button className="btn-danger !px-2 !py-1 text-xs" onClick={() => onDelete(taskset.id)}>
                          {t("common:actions.delete")}
                        </button>
                      )}
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

const CREATE_TABS: { key: CreateTab; labelKey: string }[] = [
  { key: "upload", labelKey: "list.createTabs.upload" },
  { key: "manual", labelKey: "list.createTabs.manual" },
  { key: "generate", labelKey: "list.createTabs.generate" },
];

function CreateTaskSetForm({
  onCreated,
  imported,
}: {
  onCreated: () => void;
  imported: { items: TaskItem[]; name: string } | null;
}) {
  const { t } = useTranslation("tasksets");
  const [tab, setTab] = useState<CreateTab>(imported ? "manual" : "upload");
  const [name, setName] = useState(imported?.name ?? "");

  useEffect(() => {
    if (imported) {
      setTab("manual");
      if (imported.name) setName(imported.name);
    }
  }, [imported]);

  return (
    <Card title={t("list.newTaskset")} className="mb-6">
      <div className="space-y-4">
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
              {t(entry.labelKey)}
            </button>
          ))}
        </div>

        {tab !== "generate" && (
          <>
            <div>
              <label className="label">{t("list.form.name")}</label>
              <input
                className="input"
                value={name}
                placeholder={t("list.form.namePlaceholder")}
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
  const { t } = useTranslation("tasksets");
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
      setFormError(t("list.form.nameRequired"));
      return;
    }
    const required = mode === "single" ? ["tasks"] : ["train", "val"];
    const missing = required.filter((key) => !files[key]);
    if (missing.length > 0) {
      setFormError(t("list.upload.missingFiles", { files: missing.join("、") }));
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
        <label className="label">{t("list.upload.mode")}</label>
        <select
          className="input"
          value={mode}
          onChange={(event) => setMode(event.target.value as "single" | "split")}
        >
          <option value="single">{t("list.upload.modeSingle")}</option>
          <option value="split">{t("list.upload.modeSplit")}</option>
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
                {split}.json{split === "test" ? t("list.upload.optionalSuffix") : ""}
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
        {submitting ? t("saveBtn.saving") : t("saveBtn.idle")}
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
  const { t } = useTranslation("tasksets");
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
      setFormError(t("list.form.nameRequired"));
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
        <div className="border border-amber/40 bg-amber/[.13] px-3 py-2 text-sm text-amber" data-testid="taskset-import-notice">
          {t("list.importNotice", { count: importedItems.length })}
        </div>
      )}
      <p className="text-xs text-muted">
        {t("list.manual.hint")}
      </p>
      <TaskItemsEditor items={items} onChange={setItems} />
      {formError && (
        <div data-testid="taskset-manual-error">
          <ErrorBanner message={formError} />
        </div>
      )}
      <button type="submit" className="btn-primary" disabled={submitting} data-testid="taskset-manual-submit">
        {submitting ? t("saveBtn.saving") : t("saveBtn.idle")}
      </button>
    </form>
  );
}
