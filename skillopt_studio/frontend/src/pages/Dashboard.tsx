import { Link } from "react-router-dom";
import { api, usePolling } from "../api";
import {
  Card, EmptyState, ErrorBanner, Mono, PageHeader, Spinner, StatBadge, StatusPill,
  jobDuration, formatTime,
} from "../components/ui";

const STATUS_ORDER = ["running", "queued", "succeeded", "failed", "cancelled"] as const;
const STATUS_LABELS: Record<string, string> = {
  running: "运行中", queued: "排队中", succeeded: "成功", failed: "失败", cancelled: "已取消",
};
const STATUS_TONES: Record<string, string> = {
  running: "amber", queued: "muted", succeeded: "green", failed: "red", cancelled: "purple",
};
const TYPE_LABELS: Record<string, string> = { eval: "评估", train: "训练", taskgen: "任务生成", echo: "测试" };

export default function Dashboard() {
  const { data, error, loading } = usePolling(() => api.dashboard(), 3000);

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
      {loading && !data && <Spinner />}

      {data && (
        <div className="space-y-6">
          <div className="flex flex-wrap gap-3">
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
                  <Link
                    key={job.id}
                    to={`/jobs/${job.id}`}
                    className="block bg-panel2 border border-amber/30 rounded-md p-4 hover:border-amber transition-colors"
                  >
                    <div className="flex items-center justify-between">
                      <Mono className="text-sm">{job.id}</Mono>
                      <StatusPill status={job.status} />
                    </div>
                    <div className="mt-2 flex items-center justify-between text-xs text-muted">
                      <span>{TYPE_LABELS[job.type] ?? job.type}</span>
                      <Mono className="text-amber">{job.progress ?? "…"}</Mono>
                    </div>
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
