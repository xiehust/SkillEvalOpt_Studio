import { useState } from "react";
import { Link } from "react-router-dom";
import { api, JobInfo, JobStatus, usePolling } from "../api";
import {
  Card, EmptyState, ErrorBanner, Mono, PageHeader, Spinner, StatusPill,
  formatTime, jobDuration,
} from "../components/ui";

const STATUS_FILTERS: { value: JobStatus | "all"; label: string }[] = [
  { value: "all", label: "全部状态" },
  { value: "running", label: "运行中" },
  { value: "queued", label: "排队中" },
  { value: "succeeded", label: "成功" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
];

const TYPE_FILTERS = [
  { value: "all", label: "全部类型" },
  { value: "eval", label: "评估" },
  { value: "train", label: "训练" },
  { value: "echo", label: "测试" },
];

const TYPE_LABELS: Record<string, string> = { eval: "评估", train: "训练", echo: "测试" };

export default function Jobs() {
  const { data: jobs, error, loading } = usePolling(() => api.jobs(), 2000);
  const [statusFilter, setStatusFilter] = useState<JobStatus | "all">("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [cancelError, setCancelError] = useState<string | null>(null);

  const onCancel = async (job: JobInfo) => {
    if (!window.confirm(`确定取消任务 ${job.id} 吗?`)) return;
    setCancelError(null);
    try {
      await api.cancelJob(job.id);
    } catch (err) {
      setCancelError(err instanceof Error ? err.message : String(err));
    }
  };

  const filtered = (jobs ?? []).filter(
    (job) =>
      (statusFilter === "all" || job.status === statusFilter) &&
      (typeFilter === "all" || job.type === typeFilter),
  );

  return (
    <div>
      <PageHeader
        title="任务管理"
        sub="全部评估 / 训练任务;运行中任务每 2 秒自动刷新"
        actions={
          <>
            <Link to="/evaluate" className="btn-primary">发起评估</Link>
            <Link to="/train" className="btn-ghost">发起训练</Link>
          </>
        }
      />

      {error && <ErrorBanner message={error.message} retryHint="自动重试中" />}
      {cancelError && <div className="mb-4"><ErrorBanner message={cancelError} /></div>}

      <div className="flex gap-3 mb-4">
        <select
          className="input max-w-[11rem]"
          value={statusFilter}
          data-testid="filter-status"
          onChange={(event) => setStatusFilter(event.target.value as JobStatus | "all")}
        >
          {STATUS_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <select
          className="input max-w-[11rem]"
          value={typeFilter}
          data-testid="filter-type"
          onChange={(event) => setTypeFilter(event.target.value)}
        >
          {TYPE_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </div>

      {loading && !jobs && <Spinner />}
      {jobs && filtered.length === 0 && (
        <EmptyState
          title={jobs.length === 0 ? "还没有任务" : "没有符合筛选条件的任务"}
          hint={jobs.length === 0 ? "发起一次评估或训练后,任务会出现在这里。" : "调整上方筛选条件。"}
        />
      )}

      {filtered.length > 0 && (
        <Card>
          <div className="overflow-x-auto -m-4">
            <table className="w-full" data-testid="jobs-table">
              <thead>
                <tr>
                  <th className="th">任务 ID</th>
                  <th className="th">类型</th>
                  <th className="th">技能 / 任务集</th>
                  <th className="th">状态</th>
                  <th className="th">耗时</th>
                  <th className="th">创建时间</th>
                  <th className="th"></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((job) => (
                  <tr key={job.id} className="hover:bg-panel2/40" data-job-row={job.id}>
                    <td className="td">
                      <Link to={`/jobs/${job.id}`} className="text-cyan hover:underline">
                        <Mono>{job.id}</Mono>
                      </Link>
                    </td>
                    <td className="td">{TYPE_LABELS[job.type] ?? job.type}</td>
                    <td className="td">
                      <Mono className="text-xs text-muted block">
                        {String(job.params?.skill_id ?? "—")}
                      </Mono>
                      <Mono className="text-xs text-muted/60 block">
                        {String(job.params?.taskset_id ?? "")}
                      </Mono>
                    </td>
                    <td className="td"><StatusPill status={job.status} /></td>
                    <td className="td"><Mono>{jobDuration(job)}</Mono></td>
                    <td className="td text-muted text-xs">{formatTime(job.created_at)}</td>
                    <td className="td text-right">
                      {(job.status === "running" || job.status === "queued") && (
                        <button
                          className="btn-danger !px-2 !py-1 text-xs"
                          data-testid={`cancel-${job.id}`}
                          onClick={() => onCancel(job)}
                        >
                          取消
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
