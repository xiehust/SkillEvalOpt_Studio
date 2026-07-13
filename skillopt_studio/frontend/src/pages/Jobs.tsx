import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api, JobInfo, JobStatus, usePolling } from "../api";
import {
  Card, EmptyState, ErrorBanner, Mono, PageHeader, Pagination, Spinner, StatusPill,
  TokenCell, formatTime, jobDuration, usePagination,
} from "../components/ui";

const STATUS_FILTERS: (JobStatus | "all")[] = ["all", "running", "queued", "succeeded", "failed", "cancelled"];

const TYPE_FILTERS = ["all", "eval", "train", "taskgen", "echo"];

export default function Jobs() {
  const { t } = useTranslation("jobs");
  const { data: jobs, error, loading } = usePolling(() => api.jobs(), 2000);
  const [statusFilter, setStatusFilter] = useState<JobStatus | "all">("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [cancelError, setCancelError] = useState<string | null>(null);

  const onCancel = async (job: JobInfo) => {
    if (!window.confirm(t("common:confirmCancelJob", { id: job.id }))) return;
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
  const { page, setPage, pageSize, setPageSize, pageCount, pageItems, total } = usePagination(filtered);

  return (
    <div>
      <PageHeader
        title={t("title")}
        sub={t("subtitle")}
        actions={
          <>
            <Link to="/evaluate" className="btn-primary">{t("common:actions.evaluate")}</Link>
            <Link to="/train" className="btn-ghost">{t("common:actions.train")}</Link>
          </>
        }
      />

      {error && <ErrorBanner message={error.message} retryHint={t("autoRetrying")} />}
      {cancelError && <div className="mb-4"><ErrorBanner message={cancelError} /></div>}

      <div className="flex gap-3 mb-4">
        <select
          className="input max-w-[11rem]"
          value={statusFilter}
          data-testid="filter-status"
          onChange={(event) => setStatusFilter(event.target.value as JobStatus | "all")}
        >
          {STATUS_FILTERS.map((value) => (
            <option key={value} value={value}>
              {value === "all" ? t("filters.allStatus") : t(`common:status.${value}`)}
            </option>
          ))}
        </select>
        <select
          className="input max-w-[11rem]"
          value={typeFilter}
          data-testid="filter-type"
          onChange={(event) => setTypeFilter(event.target.value)}
        >
          {TYPE_FILTERS.map((value) => (
            <option key={value} value={value}>
              {value === "all" ? t("filters.allType") : t(`common:jobType.${value}`)}
            </option>
          ))}
        </select>
      </div>

      {loading && !jobs && <Spinner />}
      {jobs && filtered.length === 0 && (
        <EmptyState
          title={jobs.length === 0 ? t("empty.noJobs") : t("empty.noMatch")}
          hint={jobs.length === 0 ? t("empty.noJobsHint") : t("empty.noMatchHint")}
        />
      )}

      {filtered.length > 0 && (
        <Card>
          <div className="overflow-x-auto -m-4">
            <table className="w-full" data-testid="jobs-table">
              <thead>
                <tr>
                  <th className="th">{t("table.id")}</th>
                  <th className="th">{t("table.type")}</th>
                  <th className="th">{t("table.skillTaskset")}</th>
                  <th className="th">{t("table.status")}</th>
                  <th className="th">{t("table.duration")}</th>
                  <th className="th">{t("table.tokens")}</th>
                  <th className="th">{t("table.created")}</th>
                  <th className="th"></th>
                </tr>
              </thead>
              <tbody>
                {pageItems.map((job) => (
                  <tr key={job.id} className="hover:bg-panel2/40" data-job-row={job.id}>
                    <td className="td">
                      <Link to={`/jobs/${job.id}`} className="text-s1 hover:underline">
                        <Mono>{job.id}</Mono>
                      </Link>
                    </td>
                    <td className="td">{t(`common:jobType.${job.type}`, { defaultValue: job.type })}</td>
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
                    <td className="td"><TokenCell tokens={job.tokens} /></td>
                    <td className="td text-muted text-xs">{formatTime(job.created_at)}</td>
                    <td className="td text-right">
                      {(job.status === "running" || job.status === "queued") && (
                        <button
                          className="btn-danger !px-2 !py-1 text-xs"
                          data-testid={`cancel-${job.id}`}
                          onClick={() => onCancel(job)}
                        >
                          {t("common:actions.cancel")}
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
      {filtered.length > 0 && (
        <Pagination
          page={page} pageCount={pageCount} pageSize={pageSize} total={total}
          onPage={setPage} onPageSize={setPageSize}
        />
      )}
    </div>
  );
}
