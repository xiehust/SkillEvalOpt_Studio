import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import ReactMarkdown, { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import {
  api, ApiError, EvalResults, JobInfo, JobResults, TaskgenResults, TrainResults, usePolling,
  ArtifactDir, ArtifactFile,
} from "../api";
import {
  CodeHighlight, isExternalHref, languageForFile, markdownPre, resolveRelative,
} from "../components/highlight";
import {
  Card, ErrorBanner, Mono, PageHeader, Spinner, StatBadge, StatusPill,
  formatDuration, formatSize, formatTime, jobDuration, jobSkillLabel, truncate,
} from "../components/ui";

const TABS = ["overview", "log", "results", "artifacts"] as const;

type TabKey = (typeof TABS)[number];

function isActive(job: JobInfo | null): boolean {
  return job?.status === "running" || job?.status === "queued";
}

export default function JobDetailPage() {
  const { t } = useTranslation("jobs");
  const { id = "" } = useParams();
  const [tab, setTab] = useState<TabKey>("overview");
  const { data: job, error } = usePolling(() => api.job(id), 2500, [id]);

  return (
    <div>
      <PageHeader
        title={
          <span className="flex items-center gap-3">
            <Mono className="text-lg">{id}</Mono>
            {job && <StatusPill status={job.status} />}
          </span>
        }
        sub={job ? t("detail.subtitle", {
          type: t(`common:jobType.${job.type}`, { defaultValue: job.type }),
          time: formatTime(job.created_at),
        }) : undefined}
        actions={<Link to="/jobs" className="btn-ghost">{t("detail.backToJobs")}</Link>}
      />

      {error && !job && <ErrorBanner message={error.message} />}
      {!job && !error && <Spinner />}

      {job && (
        <>
          <div className="grid grid-cols-2 gap-1 border-b border-line mb-6 sm:flex">
            {TABS.map((key) => (
              <button
                key={key}
                data-testid={`tab-${key}`}
                className={`min-w-0 px-2 py-2 text-sm border-b-2 -mb-px transition-colors sm:px-4 ${
                  tab === key
                    ? "border-amber text-amber font-medium"
                    : "border-transparent text-muted hover:text-text"
                }`}
                onClick={() => setTab(key)}
              >
                {t(`tabs.${key}`)}
              </button>
            ))}
          </div>
          {tab === "overview" && <OverviewTab job={job} />}
          {tab === "log" && <LogTab job={job} />}
          {tab === "results" && <ResultsTab job={job} />}
          {tab === "artifacts" && <ArtifactsTab job={job} />}
        </>
      )}
    </div>
  );
}

// ── Overview ─────────────────────────────────────────────────────────────

function OverviewTab({ job }: { job: JobInfo }) {
  const { t } = useTranslation("jobs");
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [logTail, setLogTail] = useState("");

  useEffect(() => {
    if (job.status !== "failed") return;
    api
      .jobLog(job.id, 0)
      .then((chunk) => setLogTail(chunk.content.slice(-2000)))
      .catch(() => setLogTail(""));
  }, [job.id, job.status]);

  const onCancel = async () => {
    if (!window.confirm(t("common:confirmCancelJob", { id: job.id }))) return;
    setCancelError(null);
    try {
      await api.cancelJob(job.id);
    } catch (err) {
      setCancelError(err instanceof ApiError ? err.message : String(err));
    }
  };

  return (
    <div className="space-y-6 max-w-4xl">
      {job.status === "failed" && (
        <div className="card border-crit/50 border-l-[3px] border-l-crit p-4" data-testid="failed-card">
          <div className="text-critText font-semibold text-sm mb-1">{t("overview.failed")}</div>
          <Mono className="text-sm text-critText/90 block">{job.error ?? t("overview.unknownError")}</Mono>
          {logTail && (
            <pre className="mt-3 bg-codebg border border-grid p-3 text-xs font-mono text-muted overflow-x-auto max-h-48 overflow-y-auto">
              {logTail}
            </pre>
          )}
        </div>
      )}
      {cancelError && <ErrorBanner message={cancelError} />}

      <div className="flex flex-wrap gap-3">
        <StatBadge label={t("overview.status")} value={<StatusPill status={job.status} />} />
        <StatBadge label={t("overview.duration")} value={jobDuration(job)} tone="s2" />
        {Array.isArray(job.params?.skill_ids) && (
          <StatBadge label={t("overview.target")} value={jobSkillLabel(job)} tone="s5" />
        )}
        {job.exit_code !== null && (
          <StatBadge label={t("overview.exitCode")} value={job.exit_code} tone={job.exit_code === 0 ? "good" : "critText"} />
        )}
      </div>

      <Card title={t("overview.params")}>
        <table className="w-full max-w-2xl">
          <tbody>
            {Object.entries(job.params ?? {}).map(([key, value]) => (
              <tr key={key}>
                <td className="td w-48 text-muted"><Mono className="text-xs">{key}</Mono></td>
                <td className="td"><Mono className="text-xs">{JSON.stringify(value)}</Mono></td>
              </tr>
            ))}
            <tr>
              <td className="td w-48 text-muted"><Mono className="text-xs">{t("overview.startEnd")}</Mono></td>
              <td className="td text-xs text-muted">
                {formatTime(job.started_at)} → {formatTime(job.finished_at)}
              </td>
            </tr>
          </tbody>
        </table>
      </Card>

      {isActive(job) && (
        <button className="btn-danger" data-testid="cancel-job" onClick={onCancel}>
          ✕ {t("overview.cancelJob")}
        </button>
      )}
    </div>
  );
}

// ── Log ──────────────────────────────────────────────────────────────────

function LogTab({ job }: { job: JobInfo }) {
  const { t } = useTranslation("jobs");
  const [content, setContent] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const offsetRef = useRef(0);
  const preRef = useRef<HTMLPreElement>(null);
  const active = isActive(job);

  const pull = useCallback(async () => {
    try {
      const chunk = await api.jobLog(job.id, offsetRef.current);
      if (chunk.content) {
        offsetRef.current = chunk.next_offset;
        setContent((current) => current + chunk.content);
      }
    } catch {
      /* transient poll failure — next tick retries */
    }
  }, [job.id]);

  useEffect(() => {
    offsetRef.current = 0;
    setContent("");
    pull();
  }, [pull]);

  useEffect(() => {
    if (!active) {
      pull(); // final catch-up after the run ends
      return;
    }
    const timer = window.setInterval(pull, 2000);
    return () => window.clearInterval(timer);
  }, [active, pull]);

  useEffect(() => {
    if (autoScroll && preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [content, autoScroll]);

  return (
    <Card
      title={active ? t("log.titleLive") : t("log.title")}
      actions={
        <button
          className="btn-ghost !px-2 !py-1 text-xs"
          data-testid="autoscroll-toggle"
          onClick={() => setAutoScroll((value) => !value)}
        >
          {t("log.autoScroll", { state: autoScroll ? t("log.on") : t("log.off") })}
        </button>
      }
    >
      <pre
        ref={preRef}
        data-testid="log-view"
        className="bg-codebg border border-grid p-4 text-xs font-mono leading-relaxed
          text-text/85 overflow-auto h-[32rem] whitespace-pre-wrap"
      >
        {content || (active ? t("log.waiting") : t("log.empty"))}
      </pre>
    </Card>
  );
}

// ── Results ──────────────────────────────────────────────────────────────

function ResultsTab({ job }: { job: JobInfo }) {
  const { t } = useTranslation("jobs");
  const [results, setResults] = useState<JobResults | null>(null);
  const [notReady, setNotReady] = useState(false);
  const active = isActive(job);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await api.jobResults(job.id);
        if (alive) {
          setResults(data);
          setNotReady(false);
        }
      } catch (err) {
        if (alive && err instanceof ApiError && err.status === 404) setNotReady(true);
      }
    };
    load();
    if (!active) return () => { alive = false; };
    const timer = window.setInterval(load, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [job.id, active]);

  if (job.type === "echo") {
    return <Card><p className="text-sm text-muted">{t("results.noEchoView")}</p></Card>;
  }
  if (!results) {
    return (
      <Card>
        <p className="text-sm text-muted" data-testid="results-pending">
          {notReady && active
            ? t("results.pendingActive")
            : notReady
              ? t("results.noResultFile")
              : t("common:loading")}
        </p>
      </Card>
    );
  }
  if (results.type === "eval") return <EvalResultsView results={results} />;
  if (results.type === "taskgen") return <TaskgenResultsView results={results} job={job} />;
  return <TrainResultsView results={results} />;
}

function TaskgenResultsView({ results, job }: { results: TaskgenResults; job: JobInfo }) {
  const { t } = useTranslation("jobs");
  const navigate = useNavigate();
  const { tasks, summary } = results;

  const suggestedName = () => {
    const plugin = String(job.params.plugin ?? "");
    const skillIds = Array.isArray(job.params.skill_ids)
      ? job.params.skill_ids.map(String)
      : [];
    if (skillIds.length > 1) {
      return `${plugin || t("taskgen.multiSkillBase")}-${t("taskgen.autoGenSuffix")}`;
    }
    const skillId = String(job.params.skill_id ?? "");
    const base = skillId.includes("--") ? skillId.split("--").slice(1).join("--") : skillId;
    return `${base || t("taskgen.defaultBase")}-${t("taskgen.autoGenSuffix")}`;
  };

  const showTargets = tasks.some((task) => task.target_skills?.length);

  const importTasks = () => {
    navigate("/tasksets", {
      state: { importItems: tasks, importName: suggestedName() },
    });
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3">
        <StatBadge label={t("taskgen.generatedCount")} value={tasks.length} tone="good" />
        {summary.requested_count != null && <StatBadge label={t("taskgen.requestedCount")} value={summary.requested_count} />}
        {summary.skill_count != null && summary.skill_count > 1 && (
          <StatBadge label={t("taskgen.skillCount")} value={summary.skill_count} tone="s2" />
        )}
        {summary.backend && <StatBadge label={t("taskgen.backend")} value={summary.backend} tone="s2" />}
        {summary.model != null && <StatBadge label={t("taskgen.model")} value={summary.model || t("taskgen.cliDefault")} tone="muted" />}
        {summary.attempts != null && <StatBadge label={t("taskgen.attempts")} value={summary.attempts} tone="muted" />}
        {summary.duration_s != null && (
          <StatBadge label={t("taskgen.duration")} value={formatDuration(summary.duration_s)} tone="muted" />
        )}
      </div>

      <Card
        title={t("taskgen.reviewTitle")}
        actions={
          <button className="btn-primary" onClick={importTasks} data-testid="taskgen-import">
            {t("taskgen.importAsNew")}
          </button>
        }
      >
        <div className="overflow-x-auto -m-4">
          <table className="w-full" data-testid="taskgen-results-table">
            <thead>
              <tr>
                <th className="th">ID</th>
                <th className="th">{t("taskgen.table.type")}</th>
                {showTargets && <th className="th">{t("taskgen.table.targets")}</th>}
                <th className="th">{t("taskgen.table.question")}</th>
                <th className="th">{t("taskgen.table.rubric")}</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr key={task.id} className="hover:bg-panel2/40">
                  <td className="td"><Mono className="text-s1">{task.id}</Mono></td>
                  <td className="td"><Mono className="text-xs text-muted">{task.task_type ?? "default"}</Mono></td>
                  {showTargets && (
                    <td className="td">
                      <Mono className="text-xs text-s2">
                        {task.target_skills?.join(", ") || "—"}
                      </Mono>
                    </td>
                  )}
                  <td className="td text-sm max-w-md">{truncate(task.question, 120)}</td>
                  <td className="td text-sm text-muted max-w-md">{truncate(task.rubric, 120)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function EvalResultsView({ results }: { results: EvalResults }) {
  const { t } = useTranslation("jobs");
  const [expanded, setExpanded] = useState<string | null>(null);
  const { summary, aggregates, rows } = results;
  const pluginAggregates = aggregates?.mode === "plugin" ? aggregates : null;
  const showTargets = rows.some((row) => row.target_skills?.length);
  const metricValue = (metric: { hard: number; soft: number }) =>
    `${(metric.hard * 100).toFixed(0)}% / ${metric.soft.toFixed(3)}`;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3">
        <StatBadge label={t("eval.tasks")} value={summary.tasks} />
        <StatBadge label={t("eval.passRate")} value={`${(summary.pass_rate * 100).toFixed(0)}%`} tone="good" />
        <StatBadge label={t("eval.softMean")} value={summary.soft_mean.toFixed(3)} tone="s2" />
        {pluginAggregates && (
          <StatBadge label={t("eval.skills")} value={pluginAggregates.skill_count} tone="s5" />
        )}
        <StatBadge label={t("eval.totalDuration")} value={formatDuration(summary.duration_s)} tone="muted" />
      </div>
      {pluginAggregates && (
        <>
          <div className="flex flex-wrap gap-3" data-testid="eval-plugin-metrics">
            {pluginAggregates.routing && (
              <StatBadge
                label={t("eval.routing")}
                value={metricValue(pluginAggregates.routing)}
                tone="s2"
              />
            )}
            {pluginAggregates.integration && (
              <StatBadge
                label={t("eval.integration")}
                value={metricValue(pluginAggregates.integration)}
                tone="s5"
              />
            )}
            {pluginAggregates.weakest_skill && (
              <StatBadge
                label={t("eval.weakestSkill")}
                value={`${pluginAggregates.weakest_skill.name} · ${metricValue(pluginAggregates.weakest_skill)}`}
                tone="critText"
              />
            )}
          </div>
          <Card title={t("eval.pluginBreakdownTitle")}>
            <div className="grid gap-6 xl:grid-cols-2">
              <div className="overflow-x-auto">
                <table className="w-full" data-testid="eval-by-skill">
                  <thead>
                    <tr>
                      <th className="th">{t("eval.skill")}</th>
                      <th className="th">{t("eval.count")}</th>
                      <th className="th">{t("eval.hard")}</th>
                      <th className="th">{t("eval.soft")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(pluginAggregates.by_skill).map(([name, metric]) => (
                      <tr key={name}>
                        <td className="td"><Mono className="text-s1">{name}</Mono></td>
                        <td className="td"><Mono>{metric.count}</Mono></td>
                        <td className="td"><Mono>{(metric.hard * 100).toFixed(0)}%</Mono></td>
                        <td className="td"><Mono>{metric.soft.toFixed(3)}</Mono></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full" data-testid="eval-by-task-type">
                  <thead>
                    <tr>
                      <th className="th">{t("eval.table.type")}</th>
                      <th className="th">{t("eval.count")}</th>
                      <th className="th">{t("eval.hard")}</th>
                      <th className="th">{t("eval.soft")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(pluginAggregates.by_task_type).map(([name, metric]) => (
                      <tr key={name}>
                        <td className="td"><Mono className="text-s2">{name}</Mono></td>
                        <td className="td"><Mono>{metric.count}</Mono></td>
                        <td className="td"><Mono>{(metric.hard * 100).toFixed(0)}%</Mono></td>
                        <td className="td"><Mono>{metric.soft.toFixed(3)}</Mono></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </Card>
        </>
      )}
      <Card title={t("eval.perTaskTitle")}>
        <div className="overflow-x-auto -m-4">
          <table className="w-full" data-testid="eval-results-table">
            <thead>
              <tr>
                <th className="th">ID</th>
                <th className="th">{t("eval.table.type")}</th>
                {showTargets && <th className="th">{t("eval.table.targets")}</th>}
                <th className="th">{t("eval.table.pass")}</th>
                <th className="th">{t("eval.table.soft")}</th>
                <th className="th">{t("eval.table.judgeReason")}</th>
                <th className="th">{t("eval.table.duration")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="hover:bg-panel2/40">
                  <td className="td"><Mono className="text-s1">{row.id}</Mono></td>
                  <td className="td"><Mono className="text-xs text-muted">{row.task_type ?? "default"}</Mono></td>
                  {showTargets && (
                    <td className="td">
                      <Mono className="text-xs text-s2">{row.target_skills?.join(", ") || "—"}</Mono>
                    </td>
                  )}
                  <td className="td">
                    <span className={row.hard ? "text-good font-semibold" : "text-critText font-semibold"}>
                      {row.hard ? "✓" : "✗"}
                    </span>
                  </td>
                  <td className="td"><Mono>{(row.soft ?? 0).toFixed(2)}</Mono></td>
                  <td
                    className="td max-w-lg cursor-pointer"
                    title={t("eval.toggleHint")}
                    onClick={() => setExpanded(expanded === row.id ? null : row.id)}
                  >
                    <span className={`text-xs text-text/80 ${expanded === row.id ? "" : "line-clamp-2"}`}>
                      {row.error ? t("eval.runError", { error: row.error }) : row.judge_reason ?? "—"}
                    </span>
                  </td>
                  <td className="td"><Mono className="text-xs">{formatDuration(row.duration_s)}</Mono></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

const ACTION_STYLES: Record<string, string> = {
  accept: "border-good/50 text-good",
  accept_new_best: "border-good/50 text-good",
  reject: "border-crit/50 text-critText",
};

function TrainResultsView({ results }: { results: TrainResults }) {
  const { t } = useTranslation("jobs");
  const { summary, skill_diff } = results;
  const chartData = summary.steps.map((step) => ({
    step: step.step,
    sel_soft: step.selection_soft,
    best: step.best_score,
  }));
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3">
        <StatBadge label={t("train.steps")} value={summary.totals.steps} />
        <StatBadge label={t("train.accepts")} value={summary.totals.accepts ?? "—"} tone="good" />
        <StatBadge label={t("train.rejects")} value={summary.totals.rejects ?? "—"} tone="critText" />
        <StatBadge
          label={t("train.bestStepScore")}
          value={`#${summary.best_step ?? "—"} / ${summary.best_score?.toFixed(3) ?? "—"}`}
          tone="s2"
        />
        {summary.test_scores.best != null && (
          <StatBadge
            label={t("train.testBaselineBest")}
            value={`${summary.test_scores.baseline?.toFixed(2) ?? "—"} → ${summary.test_scores.best.toFixed(2)}`}
            tone="s5"
          />
        )}
      </div>

      {!summary.finished && (
        <p className="text-xs text-amber">{t("train.inProgress")}</p>
      )}

      <Card title={t("train.valCurveTitle")}>
        <div className="h-64" data-testid="val-chart">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="#212823" strokeDasharray="3 3" />
              <XAxis dataKey="step" stroke="#69736C" fontSize={11} />
              <YAxis stroke="#69736C" fontSize={11} domain={[0, 1]} />
              <Tooltip
                contentStyle={{ background: "#0E1211", border: "1px solid #2E3833", borderRadius: 0, fontFamily: '"IBM Plex Mono", monospace', fontSize: 11 }}
                labelStyle={{ color: "#E9EDEA" }}
              />
              <Legend />
              <Line type="monotone" dataKey="sel_soft" name={t("train.legendValSoft")} stroke="#3987E5" dot strokeWidth={2} />
              <Line type="monotone" dataKey="best" name={t("train.legendBest")} stroke="#199E70" dot strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Card>

      <Card title={t("train.timelineTitle")}>
        <div className="space-y-2" data-testid="train-timeline">
          {summary.steps.map((step) => (
            <div
              key={step.step}
              data-step-action={step.action}
              className={`flex flex-wrap items-center gap-x-5 gap-y-1 border-l-2 bg-panel2 px-4 py-2.5 ${
                ACTION_STYLES[step.action] ?? "border-line text-muted"
              }`}
            >
              <Mono className="text-sm font-semibold w-14">#{step.step}</Mono>
              <span className="text-xs font-semibold uppercase tracking-wider min-w-[7rem]">{step.action}</span>
              <Mono className="text-xs text-text/80">
                sel {step.selection_hard?.toFixed(2) ?? "—"} / {step.selection_soft?.toFixed(2) ?? "—"}
              </Mono>
              <Mono className="text-xs text-text/80">best {step.best_score?.toFixed(2) ?? "—"}</Mono>
              <Mono className="text-xs text-muted">len {step.skill_len ?? "—"}</Mono>
              <Mono className="text-xs text-muted">{formatDuration(step.wall_time_s)}</Mono>
            </div>
          ))}
          {summary.steps.length === 0 && <p className="text-sm text-muted">{t("train.noSteps")}</p>}
        </div>
      </Card>

      <Card title={t("train.skillDiffTitle")}>
        {skill_diff ? (
          <pre
            className="bg-codebg border border-grid p-4 text-xs font-mono leading-relaxed overflow-auto max-h-[28rem]"
            data-testid="skill-diff"
          >
            {skill_diff.split("\n").map((line, index) => (
              <div
                key={index}
                className={
                  line.startsWith("+++") || line.startsWith("---")
                    ? "text-muted"
                    : line.startsWith("+")
                      ? "text-good bg-good/5"
                      : line.startsWith("-")
                        ? "text-critText bg-crit/5"
                        : line.startsWith("@@")
                          ? "text-s1"
                          : "text-text/70"
                }
              >
                {line || " "}
              </div>
            ))}
          </pre>
        ) : (
          <p className="text-sm text-muted">{t("train.noDiff")}</p>
        )}
      </Card>
    </div>
  );
}

// ── Artifacts ────────────────────────────────────────────────────────────

function ArtifactsTab({ job }: { job: JobInfo }) {
  const { t } = useTranslation("jobs");
  const [path, setPath] = useState("");
  const [listing, setListing] = useState<ArtifactDir | null>(null);
  const [file, setFile] = useState<ArtifactFile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    setFile(null);
    api
      .jobArtifacts(job.id, path)
      .then((entry) => {
        if (entry.kind === "dir") setListing(entry);
        else setFile(entry);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
  }, [job.id, path]);

  const crumbs = path ? path.split("/") : [];

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-1 text-sm flex-wrap" data-testid="artifact-breadcrumbs">
        <button className="text-s1 hover:underline" onClick={() => setPath("")}>out</button>
        {crumbs.map((part, index) => (
          <span key={index} className="flex items-center gap-1">
            <span className="text-muted">/</span>
            <button
              className="text-s1 hover:underline"
              onClick={() => setPath(crumbs.slice(0, index + 1).join("/"))}
            >
              {part}
            </button>
          </span>
        ))}
      </div>

      {error && <ErrorBanner message={error} />}

      {file ? (
        <ArtifactFileView jobId={job.id} file={file} onOpen={setPath} />
      ) : listing ? (
        <Card title={t("artifacts.directory")}>
          <ul className="divide-y divide-line/60" data-testid="artifact-listing">
            {listing.dirs.map((dir) => (
              <li key={dir}>
                <button
                  className="w-full text-left px-2 py-2 hover:bg-panel2/60 flex items-center gap-2"
                  onClick={() => setPath(path ? `${path}/${dir}` : dir)}
                >
                  <span className="text-amber">▸</span>
                  <Mono className="text-sm">{dir}/</Mono>
                </button>
              </li>
            ))}
            {listing.files.map((entry) => (
              <li key={entry.name}>
                <button
                  className="w-full text-left px-2 py-2 hover:bg-panel2/60 flex items-center justify-between gap-2"
                  onClick={() => setPath(path ? `${path}/${entry.name}` : entry.name)}
                >
                  <Mono className="text-sm text-s1">{entry.name}</Mono>
                  <Mono className="text-xs text-muted">{entry.size} B</Mono>
                </button>
              </li>
            ))}
            {listing.dirs.length === 0 && listing.files.length === 0 && (
              <li className="text-sm text-muted py-3 px-2">{t("artifacts.empty")}</li>
            )}
          </ul>
        </Card>
      ) : (
        !error && <Spinner />
      )}
    </div>
  );
}

/** File preview: markdown rendered, code highlighted, binary → download hint.
 * Mirrors the skill-library file viewer (SkillDetail.tsx). */
function ArtifactFileView({
  jobId, file, onOpen,
}: { jobId: string; file: ArtifactFile; onOpen: (path: string) => void }) {
  const { t } = useTranslation("jobs");
  const isMarkdown = /\.(md|markdown)$/i.test(file.path);

  // Relative links in the markdown open the referenced artifact in this
  // viewer instead of navigating the SPA (where they would 404).
  const markdownComponents: Components = {
    pre: markdownPre,
    a: ({ node: _node, href, children, ...rest }) => {
      if (!href || isExternalHref(href)) {
        const external = !!href && /^https?:/i.test(href);
        return (
          <a href={href} target={external ? "_blank" : undefined} rel={external ? "noreferrer" : undefined} {...rest}>
            {children}
          </a>
        );
      }
      const target = resolveRelative(file.path, href);
      return (
        <a
          href={api.jobArtifactRawUrl(jobId, target)}
          onClick={(event) => {
            event.preventDefault();
            onOpen(target);
          }}
          {...rest}
        >
          {children}
        </a>
      );
    },
  };

  return (
    <Card
      title={<Mono>{file.path}</Mono>}
      actions={
        <a
          className="btn-ghost"
          href={api.jobArtifactRawUrl(jobId, file.path)}
          download
          data-testid="artifact-download"
        >
          {t("common:actions.download")}
        </a>
      }
    >
      {file.kind === "binary" ? (
        <p className="text-sm text-muted py-6 text-center">
          {t("artifacts.binaryNotice", { size: formatSize(file.size) })}
        </p>
      ) : (
        <div data-testid="artifact-content">
          {isMarkdown ? (
            <div className="prose-dark max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {file.content ?? ""}
              </ReactMarkdown>
            </div>
          ) : (
            <CodeHighlight
              language={languageForFile(file.path)}
              code={file.content ?? ""}
              maxHeight="32rem"
            />
          )}
          {file.truncated && (
            <p className="text-xs text-amber mt-3">
              {t("artifacts.truncatedNotice", { size: formatSize(file.size) })}
            </p>
          )}
        </div>
      )}
    </Card>
  );
}
