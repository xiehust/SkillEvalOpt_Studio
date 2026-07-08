import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
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
  formatDuration, formatSize, formatTime, jobDuration, truncate,
} from "../components/ui";

const TABS = [
  { key: "overview", label: "概览" },
  { key: "log", label: "日志" },
  { key: "results", label: "结果" },
  { key: "artifacts", label: "产物" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

const TYPE_LABELS: Record<string, string> = { eval: "评估", train: "训练", taskgen: "任务生成", echo: "测试" };

function isActive(job: JobInfo | null): boolean {
  return job?.status === "running" || job?.status === "queued";
}

export default function JobDetailPage() {
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
        sub={job ? `${TYPE_LABELS[job.type] ?? job.type} · 创建于 ${formatTime(job.created_at)}` : undefined}
        actions={<Link to="/jobs" className="btn-ghost">返回任务列表</Link>}
      />

      {error && !job && <ErrorBanner message={error.message} />}
      {!job && !error && <Spinner />}

      {job && (
        <>
          <div className="flex gap-1 border-b border-line mb-6">
            {TABS.map((item) => (
              <button
                key={item.key}
                data-testid={`tab-${item.key}`}
                className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
                  tab === item.key
                    ? "border-green text-text font-medium"
                    : "border-transparent text-muted hover:text-text"
                }`}
                onClick={() => setTab(item.key)}
              >
                {item.label}
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
    if (!window.confirm(`确定取消任务 ${job.id} 吗?`)) return;
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
        <div className="card border-red/50 bg-red/5 p-4" data-testid="failed-card">
          <div className="text-red font-semibold text-sm mb-1">任务失败</div>
          <Mono className="text-sm text-red/90 block">{job.error ?? "未知错误"}</Mono>
          {logTail && (
            <pre className="mt-3 bg-bg border border-line rounded p-3 text-xs font-mono text-muted overflow-x-auto max-h-48 overflow-y-auto">
              {logTail}
            </pre>
          )}
        </div>
      )}
      {cancelError && <ErrorBanner message={cancelError} />}

      <div className="flex flex-wrap gap-3">
        <StatBadge label="状态" value={<StatusPill status={job.status} />} />
        <StatBadge label="耗时" value={jobDuration(job)} tone="cyan" />
        {job.exit_code !== null && (
          <StatBadge label="退出码" value={job.exit_code} tone={job.exit_code === 0 ? "green" : "red"} />
        )}
      </div>

      <Card title="参数">
        <table className="w-full max-w-2xl">
          <tbody>
            {Object.entries(job.params ?? {}).map(([key, value]) => (
              <tr key={key}>
                <td className="td w-48 text-muted"><Mono className="text-xs">{key}</Mono></td>
                <td className="td"><Mono className="text-xs">{JSON.stringify(value)}</Mono></td>
              </tr>
            ))}
            <tr>
              <td className="td w-48 text-muted"><Mono className="text-xs">开始 / 结束</Mono></td>
              <td className="td text-xs text-muted">
                {formatTime(job.started_at)} → {formatTime(job.finished_at)}
              </td>
            </tr>
          </tbody>
        </table>
      </Card>

      {isActive(job) && (
        <button className="btn-danger" data-testid="cancel-job" onClick={onCancel}>
          ✕ 取消任务
        </button>
      )}
    </div>
  );
}

// ── Log ──────────────────────────────────────────────────────────────────

function LogTab({ job }: { job: JobInfo }) {
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
      title={active ? "日志(实时轮询中)" : "日志"}
      actions={
        <button
          className="btn-ghost !px-2 !py-1 text-xs"
          data-testid="autoscroll-toggle"
          onClick={() => setAutoScroll((value) => !value)}
        >
          自动滚动:{autoScroll ? "开" : "关"}
        </button>
      }
    >
      <pre
        ref={preRef}
        data-testid="log-view"
        className="bg-bg border border-line rounded-md p-4 text-xs font-mono leading-relaxed
          text-text/85 overflow-auto h-[32rem] whitespace-pre-wrap"
      >
        {content || (active ? "等待日志输出…" : "(无日志)")}
      </pre>
    </Card>
  );
}

// ── Results ──────────────────────────────────────────────────────────────

function ResultsTab({ job }: { job: JobInfo }) {
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
    return <Card><p className="text-sm text-muted">测试类任务没有结果视图。</p></Card>;
  }
  if (!results) {
    return (
      <Card>
        <p className="text-sm text-muted" data-testid="results-pending">
          {notReady && active
            ? "任务运行中,结果将在产出后出现(每 5 秒自动检查)。"
            : notReady
              ? "该任务没有产出结果文件。"
              : "加载中…"}
        </p>
      </Card>
    );
  }
  if (results.type === "eval") return <EvalResultsView results={results} />;
  if (results.type === "taskgen") return <TaskgenResultsView results={results} job={job} />;
  return <TrainResultsView results={results} />;
}

function TaskgenResultsView({ results, job }: { results: TaskgenResults; job: JobInfo }) {
  const navigate = useNavigate();
  const { tasks, summary } = results;

  const suggestedName = () => {
    const skillId = String(job.params.skill_id ?? "");
    const base = skillId.includes("--") ? skillId.split("--").slice(1).join("--") : skillId;
    return `${base || "任务集"}-自动生成`;
  };

  const importTasks = () => {
    navigate("/tasksets", {
      state: { importItems: tasks, importName: suggestedName() },
    });
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3">
        <StatBadge label="生成任务数" value={tasks.length} tone="green" />
        {summary.requested_count != null && <StatBadge label="请求数量" value={summary.requested_count} />}
        {summary.backend && <StatBadge label="后端" value={summary.backend} tone="cyan" />}
        {summary.model != null && <StatBadge label="模型" value={summary.model || "CLI 默认"} tone="muted" />}
        {summary.attempts != null && <StatBadge label="尝试次数" value={summary.attempts} tone="muted" />}
        {summary.duration_s != null && (
          <StatBadge label="耗时" value={formatDuration(summary.duration_s)} tone="muted" />
        )}
      </div>

      <Card
        title="生成的任务(审阅后导入)"
        actions={
          <button className="btn-primary" onClick={importTasks} data-testid="taskgen-import">
            导入为新任务集
          </button>
        }
      >
        <div className="overflow-x-auto -m-4">
          <table className="w-full" data-testid="taskgen-results-table">
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
    </div>
  );
}

function EvalResultsView({ results }: { results: EvalResults }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const { summary, rows } = results;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3">
        <StatBadge label="任务数" value={summary.tasks} />
        <StatBadge label="通过率" value={`${(summary.pass_rate * 100).toFixed(0)}%`} tone="green" />
        <StatBadge label="软分均值" value={summary.soft_mean.toFixed(3)} tone="cyan" />
        <StatBadge label="总耗时" value={formatDuration(summary.duration_s)} tone="muted" />
      </div>
      <Card title="逐任务结果">
        <div className="overflow-x-auto -m-4">
          <table className="w-full" data-testid="eval-results-table">
            <thead>
              <tr>
                <th className="th">ID</th>
                <th className="th">类型</th>
                <th className="th">通过</th>
                <th className="th">软分</th>
                <th className="th">判分理由</th>
                <th className="th">耗时</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="hover:bg-panel2/40">
                  <td className="td"><Mono className="text-cyan">{row.id}</Mono></td>
                  <td className="td"><Mono className="text-xs text-muted">{row.task_type ?? "default"}</Mono></td>
                  <td className="td">
                    <span className={row.hard ? "text-green font-semibold" : "text-red font-semibold"}>
                      {row.hard ? "✓" : "✗"}
                    </span>
                  </td>
                  <td className="td"><Mono>{(row.soft ?? 0).toFixed(2)}</Mono></td>
                  <td
                    className="td max-w-lg cursor-pointer"
                    title="点击展开 / 收起"
                    onClick={() => setExpanded(expanded === row.id ? null : row.id)}
                  >
                    <span className={`text-xs text-text/80 ${expanded === row.id ? "" : "line-clamp-2"}`}>
                      {row.error ? `[运行错误] ${row.error}` : row.judge_reason ?? "—"}
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
  accept: "border-green/50 text-green",
  accept_new_best: "border-green/50 text-green",
  reject: "border-red/50 text-red",
};

function TrainResultsView({ results }: { results: TrainResults }) {
  const { summary, skill_diff } = results;
  const chartData = summary.steps.map((step) => ({
    step: step.step,
    sel_soft: step.selection_soft,
    best: step.best_score,
  }));
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3">
        <StatBadge label="步数" value={summary.totals.steps} />
        <StatBadge label="接受" value={summary.totals.accepts ?? "—"} tone="green" />
        <StatBadge label="拒绝" value={summary.totals.rejects ?? "—"} tone="red" />
        <StatBadge
          label="最优步 / 分数"
          value={`#${summary.best_step ?? "—"} / ${summary.best_score?.toFixed(3) ?? "—"}`}
          tone="cyan"
        />
        {summary.test_scores.best != null && (
          <StatBadge
            label="test 基线→最优"
            value={`${summary.test_scores.baseline?.toFixed(2) ?? "—"} → ${summary.test_scores.best.toFixed(2)}`}
            tone="purple"
          />
        )}
      </div>

      {!summary.finished && (
        <p className="text-xs text-amber">训练仍在进行,以下为已完成步骤的实时视图。</p>
      )}

      <Card title="验证分数曲线">
        <div className="h-64" data-testid="val-chart">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="#2A3647" strokeDasharray="3 3" />
              <XAxis dataKey="step" stroke="#94A3B7" fontSize={12} />
              <YAxis stroke="#94A3B7" fontSize={12} domain={[0, 1]} />
              <Tooltip
                contentStyle={{ background: "#18212F", border: "1px solid #2A3647", borderRadius: 6 }}
                labelStyle={{ color: "#EAF0F7" }}
              />
              <Legend />
              <Line type="monotone" dataKey="sel_soft" name="验证软分" stroke="#56C7D6" dot strokeWidth={2} />
              <Line type="monotone" dataKey="best" name="历史最优" stroke="#A6DB4C" dot strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Card>

      <Card title="步骤时间线">
        <div className="space-y-2" data-testid="train-timeline">
          {summary.steps.map((step) => (
            <div
              key={step.step}
              data-step-action={step.action}
              className={`flex flex-wrap items-center gap-x-5 gap-y-1 border-l-2 rounded-r bg-panel2 px-4 py-2.5 ${
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
          {summary.steps.length === 0 && <p className="text-sm text-muted">还没有完成的步骤。</p>}
        </div>
      </Card>

      <Card title="技能变化(seed → best)">
        {skill_diff ? (
          <pre
            className="bg-bg border border-line rounded-md p-4 text-xs font-mono leading-relaxed overflow-auto max-h-[28rem]"
            data-testid="skill-diff"
          >
            {skill_diff.split("\n").map((line, index) => (
              <div
                key={index}
                className={
                  line.startsWith("+++") || line.startsWith("---")
                    ? "text-muted"
                    : line.startsWith("+")
                      ? "text-green bg-green/5"
                      : line.startsWith("-")
                        ? "text-red bg-red/5"
                        : line.startsWith("@@")
                          ? "text-cyan"
                          : "text-text/70"
                }
              >
                {line || " "}
              </div>
            ))}
          </pre>
        ) : (
          <p className="text-sm text-muted">暂无差异(best_skill.md 尚未产出或与种子一致)。</p>
        )}
      </Card>
    </div>
  );
}

// ── Artifacts ────────────────────────────────────────────────────────────

function ArtifactsTab({ job }: { job: JobInfo }) {
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
        <button className="text-cyan hover:underline" onClick={() => setPath("")}>out</button>
        {crumbs.map((part, index) => (
          <span key={index} className="flex items-center gap-1">
            <span className="text-muted">/</span>
            <button
              className="text-cyan hover:underline"
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
        <Card title="目录">
          <ul className="divide-y divide-line/60" data-testid="artifact-listing">
            {listing.dirs.map((dir) => (
              <li key={dir}>
                <button
                  className="w-full text-left px-2 py-2 hover:bg-panel2/60 rounded flex items-center gap-2"
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
                  className="w-full text-left px-2 py-2 hover:bg-panel2/60 rounded flex items-center justify-between gap-2"
                  onClick={() => setPath(path ? `${path}/${entry.name}` : entry.name)}
                >
                  <Mono className="text-sm text-cyan">{entry.name}</Mono>
                  <Mono className="text-xs text-muted">{entry.size} B</Mono>
                </button>
              </li>
            ))}
            {listing.dirs.length === 0 && listing.files.length === 0 && (
              <li className="text-sm text-muted py-3 px-2">目录为空(任务可能尚未产出文件)。</li>
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
          下载
        </a>
      }
    >
      {file.kind === "binary" ? (
        <p className="text-sm text-muted py-6 text-center">
          二进制文件({formatSize(file.size)}),无法预览 —— 请使用右上角「下载」。
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
              文件过大,预览已截断({formatSize(file.size)})—— 完整内容请下载。
            </p>
          )}
        </div>
      )}
    </Card>
  );
}
