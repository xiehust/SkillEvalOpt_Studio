import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, TaskSetDetail } from "../api";
import { Card, ErrorBanner, Mono, PageHeader, Spinner, truncate } from "../components/ui";

export default function TaskSetDetailPage() {
  const { id = "" } = useParams();
  const [detail, setDetail] = useState<TaskSetDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    api
      .tasksetDetail(id)
      .then(setDetail)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
  }, [id]);

  return (
    <div>
      <PageHeader
        title={detail ? detail.info.name : "任务集详情"}
        sub={detail ? `模式 ${detail.info.mode} · 共 ${detail.info.task_count} 个任务(每分组最多预览 20 条)` : undefined}
        actions={<Link to="/tasksets" className="btn-ghost">返回任务集</Link>}
      />

      {error && <ErrorBanner message={error} />}
      {!detail && !error && <Spinner />}

      {detail &&
        Object.entries(detail.tasks_by_split).map(([split, tasks]) => (
          <Card key={split} title={`${split}(${detail.info.counts_by_split[split] ?? tasks.length} 条)`} className="mb-6">
            <div className="overflow-x-auto -m-4">
              <table className="w-full" data-testid={`tasks-table-${split}`}>
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
        ))}
    </div>
  );
}
