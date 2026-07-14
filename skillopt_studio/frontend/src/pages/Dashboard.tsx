import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, DashboardJobRow, TokenUsage, usePolling } from "../api";
import {
  Card, EmptyState, ErrorBanner, Mono, PageHeader, Spinner, StatBadge, StatusPill,
  TokenCell, formatTime, jobDuration, jobSkillLabel,
} from "../components/ui";

const STATUS_ORDER = ["running", "queued", "succeeded", "failed", "cancelled"] as const;
const STATUS_TONES: Record<string, string> = {
  running: "amber", queued: "muted", succeeded: "good", failed: "critText", cancelled: "s5",
};
const TYPE_LABEL_KEYS: Record<string, string> = {
  eval: "common:jobType.eval", train: "common:jobType.train",
  taskgen: "common:jobType.taskgen", echo: "common:jobType.echo",
};

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
        <circle cx={w / 2} cy={y(trend[0])} r={3} fill="#3987E5" />
      </svg>
    );
  }
  const step = (w - 2 * pad) / (trend.length - 1);
  const points = trend.map((v, i) => `${pad + i * step},${y(v)}`).join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden className="shrink-0">
      <polyline points={points} fill="none" stroke="#3987E5" strokeWidth={2}
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function passRateTone(rate: number): string {
  if (rate >= 0.8) return "text-good";
  if (rate >= 0.5) return "text-warn";
  return "text-critText";
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
  const { t } = useTranslation("dashboard");
  const cancel = async (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    if (!window.confirm(t("common:confirmCancelJob", { id: job.id }))) return;
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
      className="block bg-panel2 border border-amber/30 p-4 hover:border-amber transition-colors"
    >
      <div className="flex items-center justify-between">
        <Mono className="text-sm">{job.id}</Mono>
        <StatusPill status={job.status} />
      </div>
      <div className="mt-2 flex items-center justify-between text-xs text-muted">
        <span>
          {TYPE_LABEL_KEYS[job.type] ? t(TYPE_LABEL_KEYS[job.type]) : job.type} · {t("running.elapsed", { duration: jobDuration(job) })}
        </span>
        <Mono className="text-amber">{job.progress ?? "…"}</Mono>
      </div>
      <div className="mt-2 text-right">
        <button className="btn-danger !px-2 !py-1 text-xs" onClick={cancel} data-testid={`dash-cancel-${job.id}`}>
          {t("common:actions.cancel")}
        </button>
      </div>
    </Link>
  );
}

export default function Dashboard() {
  const navigate = useNavigate();
  const { t } = useTranslation("dashboard");
  const { data, error, loading } = usePolling(() => api.dashboard(), 3000);
  const [cancelError, setCancelError] = useState<string | null>(null);

  return (
    <div>
      <PageHeader
        title={t("title")}
        sub={t("sub")}
        actions={
          <>
            <Link to="/evaluate" className="btn-primary">{t("common:actions.evaluate")}</Link>
            <Link to="/train" className="btn-ghost">{t("common:actions.train")}</Link>
          </>
        }
      />

      {error && <ErrorBanner message={error.message} retryHint={t("autoRetry")} />}
      {cancelError && <div className="mb-4"><ErrorBanner message={cancelError} /></div>}
      {loading && !data && <Spinner />}

      {data && (
        <div className="space-y-6">
          {/* 资源总览 + 状态徽章 */}
          <div className="flex flex-wrap gap-3">
            <Link to="/skills"><StatBadge label={t("resources.skills")} value={data.resources.skills} tone="s2" /></Link>
            <Link to="/tasksets"><StatBadge label={t("resources.tasksets")} value={data.resources.tasksets} tone="s2" /></Link>
            <Link to="/jobs"><StatBadge label={t("resources.jobs")} value={data.resources.jobs} tone="text" /></Link>
            {STATUS_ORDER.map((status) => (
              <StatBadge
                key={status}
                label={t(`common:status.${status}`)}
                value={data.totals.by_status[status] ?? 0}
                tone={STATUS_TONES[status]}
              />
            ))}
          </div>

          {data.running.length > 0 && (
            <Card title={t("common:status.running")}>
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
            <Card title={t("skillHealth.title")}>
              {data.skill_health.length === 0 ? (
                <EmptyState
                  title={t("skillHealth.empty.title")}
                  hint={t("skillHealth.empty.hint")}
                  action={<Link to="/evaluate" className="btn-primary">{t("firstEval")}</Link>}
                />
              ) : (
                <div className="space-y-2">
                  {data.skill_health.map((entry) => (
                    <button
                      key={entry.skill_id}
                      type="button"
                      className="w-full flex items-center gap-3 bg-panel2 border border-line px-4 py-3 hover:border-faint transition-colors text-left"
                      onClick={() => navigate(`/evaluate?skill=${encodeURIComponent(entry.skill_id)}`)}
                      data-skill-health={entry.skill_id}
                    >
                      <span className="min-w-0 flex-1">
                        <Mono className="block text-sm truncate">{entry.skill_id}</Mono>
                        <span className="block text-[11px] text-muted mt-0.5">
                          {t("skillHealth.runsSummary", { runs: entry.runs, time: formatTime(entry.last_run_at) })}
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
              <Card title={t("trainGains.title")}>
                {data.train_gains.length === 0 ? (
                  <EmptyState
                    title={t("trainGains.empty.title")}
                    hint={t("trainGains.empty.hint")}
                    action={<Link to="/train" className="btn-ghost">{t("common:actions.train")}</Link>}
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
                          className="flex items-center gap-3 bg-panel2 border border-line px-4 py-3 hover:border-faint transition-colors"
                        >
                          <span className="min-w-0 flex-1">
                            <Mono className="block text-sm truncate">{gain.skill_id ?? gain.job_id}</Mono>
                            <span className="block text-[11px] text-muted mt-0.5">
                              {t("trainGains.summary", {
                                accepts: gain.accepts ?? "—",
                                rejects: gain.rejects ?? "—",
                                time: formatTime(gain.finished_at),
                              })}
                            </span>
                          </span>
                          <Mono className="text-sm whitespace-nowrap">
                            {gain.baseline != null ? (gain.baseline * 100).toFixed(0) + "%" : "—"}
                            <span className="text-muted mx-1">→</span>
                            {gain.best != null ? (gain.best * 100).toFixed(0) + "%" : "—"}
                          </Mono>
                          {delta != null && (
                            <Mono className={`text-xs w-14 text-right ${delta >= 0 ? "text-good" : "text-critText"}`}>
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
              <Card title={t("tokens.title")}>
                <TokenStatRow label={t("tokens.today")} usage={data.token_stats.today} />
                <TokenStatRow label={t("tokens.total")} usage={data.token_stats.total} />
                <p className="text-[11px] text-muted mt-2">
                  {t("tokens.note")}
                </p>
              </Card>
            </div>
          </div>

          {/* 最近失败 */}
          {data.failures.length > 0 && (
            <Card title={t("failures.title")}>
              <div className="grid gap-3 md:grid-cols-2">
                {data.failures.map((failure) => (
                  <Link
                    key={failure.job_id}
                    to={`/jobs/${failure.job_id}`}
                    className="block bg-panel2 border border-crit/30 p-4 hover:border-crit transition-colors"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <Mono className="text-sm truncate">{failure.job_id}</Mono>
                      <span className="text-xs text-muted shrink-0">
                        {TYPE_LABEL_KEYS[failure.type] ? t(TYPE_LABEL_KEYS[failure.type]) : failure.type} · {formatTime(failure.finished_at)}
                      </span>
                    </div>
                    {failure.log_tail && (
                      <pre className="mt-2 text-[11px] text-muted bg-codebg border border-grid p-2 overflow-x-auto whitespace-pre-wrap max-h-24">
                        {failure.log_tail}
                      </pre>
                    )}
                  </Link>
                ))}
              </div>
            </Card>
          )}

          <Card title={t("recent.title")}>
            {data.recent.length === 0 ? (
              <EmptyState
                title={t("recent.empty.title")}
                hint={t("recent.empty.hint")}
                action={<Link to="/evaluate" className="btn-primary">{t("firstEval")}</Link>}
              />
            ) : (
              <div className="overflow-x-auto -m-4">
                <table className="w-full">
                  <thead>
                    <tr>
                      <th className="th">{t("recent.cols.id")}</th>
                      <th className="th">{t("recent.cols.type")}</th>
                      <th className="th">{t("recent.cols.skill")}</th>
                      <th className="th">{t("recent.cols.status")}</th>
                      <th className="th">{t("recent.cols.passRate")}</th>
                      <th className="th">{t("recent.cols.duration")}</th>
                      <th className="th">{t("recent.cols.tokens")}</th>
                      <th className="th">{t("recent.cols.createdAt")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent.map((job) => (
                      <tr key={job.id} className="hover:bg-panel2/40">
                        <td className="td">
                          <Link to={`/jobs/${job.id}`} className="text-s1 hover:underline">
                            <Mono>{job.id}</Mono>
                          </Link>
                        </td>
                        <td className="td">{TYPE_LABEL_KEYS[job.type] ? t(TYPE_LABEL_KEYS[job.type]) : job.type}</td>
                        <td className="td">
                          <Mono className="text-muted">{jobSkillLabel(job)}</Mono>
                        </td>
                        <td className="td"><StatusPill status={job.status} /></td>
                        <td className="td">
                          <Mono className={job.pass_rate != null ? "text-good" : "text-muted"}>
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
