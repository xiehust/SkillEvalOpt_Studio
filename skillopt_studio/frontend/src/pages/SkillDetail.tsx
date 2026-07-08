import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import ReactMarkdown, { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, ApiError, SkillDetail, SkillFile } from "../api";
import { CodeHighlight, languageForFile, markdownPre } from "../components/highlight";
import { Card, ErrorBanner, Mono, PageHeader, SourceTag, Spinner } from "../components/ui";

/** Resolve a relative markdown href against the directory of the file being viewed. */
function resolveRelative(baseFile: string, href: string): string {
  const clean = href.split(/[?#]/)[0];
  const segments = baseFile.split("/").slice(0, -1);
  for (const part of clean.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") segments.pop();
    else segments.push(part);
  }
  return segments.join("/");
}

/** Absolute URLs, fragments and site-absolute paths keep default anchor behavior. */
const isExternalHref = (href: string) => /^([a-z][a-z0-9+.-]*:|#|\/)/i.test(href);

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function SkillDetailPage() {
  const { id = "" } = useParams();
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // null → SKILL.md (content already in detail); string → support file fetched on demand
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [file, setFile] = useState<SkillFile | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    setActiveFile(null);
    api
      .skillDetail(id)
      .then(setDetail)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
  }, [id]);

  useEffect(() => {
    setFile(null);
    setFileError(null);
    if (!activeFile) return;
    api
      .skillFile(id, activeFile)
      .then(setFile)
      .catch((err) => setFileError(err instanceof ApiError ? err.message : String(err)));
  }, [id, activeFile]);

  const currentPath = activeFile ?? "SKILL.md";

  const openFile = (path: string) => setActiveFile(path === "SKILL.md" ? null : path);

  // Relative links in the markdown open the referenced file in this page's
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
      const target = resolveRelative(currentPath, href);
      return (
        <a
          href={api.skillFileRawUrl(id, target)}
          onClick={(event) => {
            event.preventDefault();
            openFile(target);
          }}
          {...rest}
        >
          {children}
        </a>
      );
    },
  };

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
                <Mono>{currentPath}</Mono>
                <SourceTag source={detail.source} />
              </span>
            }
            actions={
              <span className="flex items-center gap-2">
                {activeFile && (
                  <button className="btn-ghost" onClick={() => setActiveFile(null)}>
                    返回 SKILL.md
                  </button>
                )}
                <a
                  className="btn-ghost"
                  href={api.skillFileRawUrl(id, currentPath)}
                  download
                  data-testid="download-file"
                >
                  下载
                </a>
              </span>
            }
          >
            {activeFile === null ? (
              <div className="prose-dark max-w-none" data-testid="skill-md">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{detail.skill_md}</ReactMarkdown>
              </div>
            ) : fileError ? (
              <ErrorBanner message={fileError} />
            ) : !file ? (
              <Spinner />
            ) : file.kind === "binary" ? (
              <p className="text-sm text-muted py-6 text-center">
                二进制文件({formatSize(file.size)}),无法预览 —— 请使用右上角「下载」。
              </p>
            ) : (
              <div data-testid="skill-file">
                {activeFile.endsWith(".md") ? (
                  <div className="prose-dark max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{file.content ?? ""}</ReactMarkdown>
                  </div>
                ) : (
                  <CodeHighlight
                    language={languageForFile(activeFile)}
                    code={file.content ?? ""}
                    maxHeight="70vh"
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
          <Card title={`文件树(${detail.file_tree.length})`}>
            <ul className="space-y-1" data-testid="file-tree">
              {detail.file_tree.map((path) => (
                <li key={path} className="text-xs">
                  <button
                    className="text-left hover:underline"
                    onClick={() => openFile(path)}
                    title="点击预览"
                  >
                    <Mono className={path === currentPath ? "text-green" : "text-text/80"}>
                      {path}
                    </Mono>
                  </button>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      )}
    </div>
  );
}
