import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, SkillInfo } from "../api";
import {
  Card, EmptyState, ErrorBanner, Mono, PageHeader, SourceTag, Spinner, truncate,
} from "../components/ui";

const SOURCE_ORDER = ["sample", "claude", "codex", "kiro", "agents", "uploaded"];

export default function Skills() {
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadOk, setUploadOk] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const reload = async () => {
    try {
      setSkills(await api.skills());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  };

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onUpload = async (file: File) => {
    if (file.size > 50 * 1024 * 1024) {
      setUploadError(`zip 为 ${(file.size / 1024 / 1024).toFixed(1)}MB,超过 50MB 上限。`);
      setUploadOk(null);
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    setUploading(true);
    setUploadError(null);
    setUploadOk(null);
    try {
      const info = await api.uploadSkill(file);
      setUploadOk(`已上传技能 ${info.name}`);
      await reload();
    } catch (err) {
      setUploadError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const filtered = (skills ?? []).filter((skill) => {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return (
      skill.name.toLowerCase().includes(q) ||
      skill.id.toLowerCase().includes(q) ||
      skill.description.toLowerCase().includes(q)
    );
  });
  const grouped = SOURCE_ORDER.map((source) => ({
    source,
    items: filtered.filter((skill) => skill.source === source),
  })).filter((group) => group.items.length > 0);
  const otherItems = filtered.filter((skill) => !SOURCE_ORDER.includes(skill.source));
  if (otherItems.length > 0) grouped.push({ source: "other", items: otherItems });

  return (
    <div>
      <PageHeader
        title="技能库"
        sub="扫描本机 claude / codex / kiro / agents 四个技能源,以及 Studio 上传的技能"
        actions={
          <>
            <input
              ref={fileRef}
              type="file"
              accept=".zip"
              className="hidden"
              data-testid="skill-zip-input"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) onUpload(file);
              }}
            />
            <button
              className="btn-primary"
              disabled={uploading}
              onClick={() => fileRef.current?.click()}
            >
              {uploading ? "上传中…" : "上传技能 zip"}
            </button>
          </>
        }
      />

      {uploadError && (
        <div className="mb-4" data-testid="upload-error">
          <ErrorBanner message={uploadError} />
        </div>
      )}
      {uploadOk && (
        <div
          className="card border-green/40 bg-green/5 px-4 py-3 text-sm text-green mb-4"
          data-testid="upload-ok"
        >
          {uploadOk}
        </div>
      )}
      {error && <ErrorBanner message={error} />}

      <div className="mb-5">
        <input
          className="input max-w-md"
          placeholder="搜索技能名称 / ID / 描述…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      {skills === null && !error && <Spinner />}
      {skills !== null && filtered.length === 0 && (
        <EmptyState
          title={query ? "没有匹配的技能" : "没有发现技能"}
          hint={
            query
              ? "换个关键词试试。"
              : "四个本机技能源中都没有含 SKILL.md 的目录,可以先上传一个技能 zip。"
          }
        />
      )}

      <div className="space-y-6">
        {grouped.map((group) => (
          <Card
            key={group.source}
            title={
              <span className="flex items-center gap-2">
                <SourceTag source={group.source} />
                <span className="text-muted normal-case tracking-normal">
                  {group.items.length} 个技能
                </span>
              </span>
            }
          >
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {group.items.map((skill) => (
                <Link
                  key={skill.id}
                  to={`/skills/${encodeURIComponent(skill.id)}`}
                  data-skill-id={skill.id}
                  className="block bg-panel2 border border-line rounded-md p-4 hover:border-cyan transition-colors"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-medium text-sm truncate">{skill.name}</div>
                    {skill.has_support_files && (
                      <span className="text-[10px] text-cyan border border-cyan/40 rounded px-1.5 py-0.5 shrink-0">
                        {skill.files_count} 文件
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-muted mt-1.5 leading-relaxed min-h-[2rem]">
                    {truncate(skill.description || "(无描述)", 90)}
                  </div>
                  <Mono className="text-[11px] text-muted/70 block mt-2 truncate">{skill.id}</Mono>
                </Link>
              ))}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
