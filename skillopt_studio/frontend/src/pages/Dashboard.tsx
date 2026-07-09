import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, DashboardJobRow, TokenUsage, usePolling } from "../api";
import {
  Card, EmptyState, ErrorBanner, Mono, PageHeader, Spinner, StatBadge, StatusPill,
  TokenCell, formatTime, jobDuration,
} from "../components/ui";

const STATUS_ORDER = ["running", "queued", "succeeded", "failed", "cancelled"] as const;
const STATUS_LABELS: Record<string, string> = {
  running: "运行中", queued: "排队中", succeeded: "成功", failed: "失败", cancelled: "已取消",
};
const STATUS_TONES: Record<string, string> = {
  running: "amber", queued: "muted", succeeded: "green", failed: "red", cancelled: "purple",
};
const TYPE_LABELS: Record<string, string> = { eval: "评估", train: "训练", taskgen: "任务生成", echo: "测试" };

/** 单序列迷你趋势线(纯 SVG;单点退化为圆点)。值域固定 [0,1]。 */
function Sparkline({ trend }: { trend: number[] }) {
  const w = 80;
  const h = 24;
  const pad = 2;
  if (trend.length === 0) return null;
  const y = (v: number) => pad + (1 - Math.min(1, Math.max(0, v))) * (h - 2 * pad);
  if (trend.length === 1) {
    return (
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden className="shrink-0">
        <circle cx={w / 2} cy={y(trend[0])} r={3} fill="#56C7D6" />
      </svg>
    );
  }
  const step = (w - 2 * pad) / (trend.length - 1);
  const points = trend.map((v, i) => `${pad + i * step},${y(v)}`).join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden className="shrink-0">
      <polyline points={points} fill="none" stroke="#56C7D6" strokeWidth={2}
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function passRateTone(rate: number): string {
  if (rate >= 0.8) return "text-green";
  if (rate >= 0.5) return "text-amber";
  return "text-red";
}

function TokenStatRow({ label, usage }: { label: string; usage: TokenUsage }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="text-xs text-muted w-10 shrink-0">{label}</span>
      <TokenCell tokens={usage.total > 0 ? usage : null} />
      <Mono className="text-xs text-text ml-auto">Σ {usage.total.toLocaleString()}</Mono>
    </div>
  );
}

function RunningCard({ job, onCancelled }: { job: DashboardJobRow; onCancelled: (err?: string) => void }) {
  const cancel = async (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    if (!window.confirm(`确定取消任务 ${job.id} 吗?`)) return;
    try {
      await api.cancelJob(job.id);
      onCancelled();
    } catch (err) {
      onCancelled(err instanceof ApiError ? err.message : String(err));
    }
  };
  return (
    <Link
      to={`/jobs/${job.id}`}
      className="block bg-panel2 border border-amber/30 rounded-md p-4 hover:border-amber transition-colors"
    >
      <div className="flex items-center justify-between">
        <Mono className="text-sm">{job.id}</Mono>
        <StatusPill status={job.status} />
      </div>
      <div className="mt-2 flex items-center justify-between text-xs text-muted">
        <span>{TYPE_LABELS[job.type] ?? job.type} · 已运行 {jobDuration(job)}</span>
        <Mono className="text-amber">{job.progress ?? "…"}</Mono>
      </div>
      <div className="mt-2 text-right">
        <button className="btn-danger !px-2 !py-1 text-xs" onClick={cancel} data-testid={`dash-cancel-${job.id}`}>
          取消
        </button>
      </div>
    </Link>
  );
}

export default function Dashboard() {
  const navigate = useNavigate();
  const { data, error, loading } = usePolling(() => api.dashboard(), 3000);
  const [cancelError, setCancelError] = useState<string | null>(null);

  return (
    <div>
      <PageHeader
        title="总览"
        sub="SkillEval&Opt Studio — 技能评估与训练操作台"
        actions={
          <>
            <Link to="/evaluate" className="btn-primary">发起评估</Link>
            <Link to="/train" className="btn-ghost">发起训练</Link>
          </>
        }
      />

      {error && <ErrorBanner message={error.message} retryHint="每 3 秒自动重试" />}
      {cancelError && <div className="mb-4"><ErrorBanner message={cancelError} /></div>}
      {loading && !data && <Spinner />}

      {data && (
        <div className="space-y-6">
          {/* 资源总览 + 状态徽章 */}
          <div className="flex flex-wrap gap-3">
            <Link to="/skills"><StatBadge label="技能" value={data.resources.skills} tone="cyan" /></Link>
            <Link to="/tasksets"><StatBadge label="任务集" value={data.resources.tasksets} tone="cyan" /></Link>
            <Link to="/jobs"><StatBadge label="累计任务" value={data.resources.jobs} tone="text" /></Link>
            {STATUS_ORDER.map((status) => (
              <StatBadge
                key={status}
                label={STATUS_LABELS[status]}
                value={data.totals.by_status[status] ?? 0}
                tone={STATUS_TONES[status]}
              />
            ))}
          </div>

          {data.running.length > 0 && (
            <Card title="运行中">
              <div className="grid gap-3 md:grid-cols-2">
                {data.running.map((job) => (
                  <RunningCard
                    key={job.id}
                    job={job}
                    onCancelled={(err) => setCancelError(err ?? null)}
                  />
                ))}
              </div>
            </Card>
          )}

          <div className="grid gap-6 xl:grid-cols-2">
            {/* 技能健康 */}
            <Card title="技能健康">
              {data.skill_health.length === 0 ? (
                <EmptyState
                  title="还没有评估记录"
                  hint="发起一次评估后,每个技能的通过率和趋势会显示在这里。"
                  action={<Link to="/evaluate" className="btn-primary">发起第一次评估</Link>}
                />
              ) : (
                <div className="space-y-2">
                  {data.skill_health.map((entry) => (
                    <button
                      key={entry.skill_id}
                      type="button"
                      className="w-full flex items-center gap-3 bg-panel2 border border-line rounded-md px-4 py-3 hover:border-cyan transition-colors text-left"
                      onClick={() => navigate(`/evaluate?skill=${encodeURIComponent(entry.skill_id)}`)}
                      data-skill-health={entry.skill_id}
                    >
                      <span className="min-w-0 flex-1">
                        <Mono className="block text-sm truncate">{entry.skill_id}</Mono>
                        <span className="block text-[11px] text-muted mt-0.5">
                          {entry.runs} 次评估 · 最近 {formatTime(entry.last_run_at)}
                        </span>
                      </span>
                      <Sparkline trend={entry.trend} />
                      <span className={`text-xl font-mono font-semibold w-16 text-right ${passRateTone(entry.last_pass_rate)}`}>
                        {(entry.last_pass_rate * 100).toFixed(0)}%
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </Card>

            <div className="space-y-6">
              {/* 训练收益 */}
              <Card title="训练收益">
                {data.train_gains.length === 0 ? (
                  <EmptyState
                    title="还没有完成的训练"
                    hint="训练完成后,baseline → best 的分数提升会显示在这里。"
                    action={<Link to="/train" className="btn-ghost">发起训练</Link>}
                  />
                ) : (
                  <div className="space-y-2">
                    {data.train_gains.map((gain) => {
                      const delta = gain.baseline != null && gain.best != null
                        ? gain.best - gain.baseline : null;
                      return (
                        <Link
                          key={gain.job_id}
                          to={`/jobs/${gain.job_id}`}
                          className="flex items-center gap-3 bg-panel2 border border-line rounded-md px-4 py-3 hover:border-cyan transition-colors"
                        >
                          <span className="min-w-0 flex-1">
                            <Mono className="block text-sm truncate">{gain.skill_id ?? gain.job_id}</Mono>
                            <span className="block text-[11px] text-muted mt-0.5">
                              接受 {gain.accepts ?? "—"} · 拒绝 {gain.rejects ?? "—"} · {formatTime(gain.finished_at)}
                            </span>
                          </span>
                          <Mono className="text-sm whitespace-nowrap">
                            {gain.baseline != null ? (gain.baseline * 100).toFixed(0) + "%" : "—"}
                            <span className="text-muted mx-1">→</span>
                            {gain.best != null ? (gain.best * 100).toFixed(0) + "%" : "—"}
                          </Mono>
                          {delta != null && (
                            <Mono className={`text-xs w-14 text-right ${delta >= 0 ? "text-green" : "text-red"}`}>
                              {delta >= 0 ? "+" : ""}{(delta * 100).toFixed(0)}pp
                            </Mono>
                          )}
                        </Link>
                      );
                    })}
                  </div>
                )}
              </Card>

              {/* Token 消耗 */}
              <Card title="Token 消耗(已完成任务)">
                <TokenStatRow label="今日" usage={data.token_stats.today} />
                <TokenStatRow label="累计" usage={data.token_stats.total} />
                <p className="text-[11px] text-muted mt-2">
                  口径:评估 = 执行 agent + LLM 判分;训练 = 优化器 + rollout 执行 agent;旧任务未记录 token 的不计入。
                </p>
              </Card>
            </div>
          </div>

          {/* 最近失败 */}
          {data.failures.length > 0 && (
            <Card title="最近失败">
              <div className="grid gap-3 md:grid-cols-2">
                {data.failures.map((failure) => (
                  <Link
                    key={failure.job_id}
                    to={`/jobs/${failure.job_id}`}
                    className="block bg-panel2 border border-red/30 rounded-md p-4 hover:border-red transition-colors"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <Mono className="text-sm truncate">{failure.job_id}</Mono>
                      <span className="text-xs text-muted shrink-0">
                        {TYPE_LABELS[failure.type] ?? failure.type} · {formatTime(failure.finished_at)}
                      </span>
                    </div>
                    {failure.log_tail && (
                      <pre className="mt-2 text-[11px] text-muted bg-bg/60 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-24">
                        {failure.log_tail}
                      </pre>
                    )}
                  </Link>
                ))}
              </div>
            </Card>
          )}

          <Card title="近期任务">
            {data.recent.length === 0 ? (
              <EmptyState
                title="还没有任务"
                hint="从技能库选择一个技能,再发起评估或训练,任务会出现在这里。"
                action={<Link to="/evaluate" className="btn-primary">发起第一次评估</Link>}
              />
            ) : (
              <div className="overflow-x-auto -m-4">
                <table className="w-full">
                  <thead>
                    <tr>
                      <th className="th">任务 ID</th>
                      <th className="th">类型</th>
                      <th className="th">技能</th>
                      <th className="th">状态</th>
                      <th className="th">通过率</th>
                      <th className="th">耗时</th>
                      <th className="th">Token 消耗</th>
                      <th className="th">创建时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent.map((job) => (
                      <tr key={job.id} className="hover:bg-panel2/40">
                        <td className="td">
                          <Link to={`/jobs/${job.id}`} className="text-cyan hover:underline">
                            <Mono>{job.id}</Mono>
                          </Link>
                        </td>
                        <td className="td">{TYPE_LABELS[job.type] ?? job.type}</td>
                        <td className="td">
                          <Mono className="text-muted">{String(job.params?.skill_id ?? "—")}</Mono>
                        </td>
                        <td className="td"><StatusPill status={job.status} /></td>
                        <td className="td">
                          <Mono className={job.pass_rate != null ? "text-green" : "text-muted"}>
                            {job.pass_rate != null ? `${(job.pass_rate * 100).toFixed(0)}%` : "—"}
                          </Mono>
                        </td>
                        <td className="td"><Mono>{jobDuration(job)}</Mono></td>
                        <td className="td"><TokenCell tokens={job.tokens} /></td>
                        <td className="td text-muted text-xs">{formatTime(job.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
