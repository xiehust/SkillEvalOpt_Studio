import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { api, ApiError, SkillDetail } from "../api";
import { Card, ErrorBanner, Mono, PageHeader, SourceTag, Spinner } from "../components/ui";

export default function SkillDetailPage() {
  const { id = "" } = useParams();
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    api
      .skillDetail(id)
      .then(setDetail)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
  }, [id]);

  return (
    <div>
      <PageHeader
        title={detail?.name ?? "技能详情"}
        sub={detail ? detail.path : undefined}
        actions={
          <>
            {detail && (
              <Link
                to={`/evaluate?skill=${encodeURIComponent(detail.id)}`}
                className="btn-primary"
              >
                评估此技能
              </Link>
            )}
            <Link to="/skills" className="btn-ghost">返回技能库</Link>
          </>
        }
      />

      {error && <ErrorBanner message={error} />}
      {!detail && !error && <Spinner />}

      {detail && (
        <div className="grid gap-6 lg:grid-cols-[1fr_18rem]">
          <Card
            title={
              <span className="flex items-center gap-2">
                SKILL.md
                <SourceTag source={detail.source} />
              </span>
            }
          >
            <div className="prose-dark max-w-none" data-testid="skill-md">
              <ReactMarkdown>{detail.skill_md}</ReactMarkdown>
            </div>
          </Card>
          <Card title={`文件树(${detail.file_tree.length})`}>
            <ul className="space-y-1" data-testid="file-tree">
              {detail.file_tree.map((file) => (
                <li key={file} className="text-xs">
                  <Mono className={file === "SKILL.md" ? "text-green" : "text-text/80"}>
                    {file}
                  </Mono>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      )}
    </div>
  );
}
