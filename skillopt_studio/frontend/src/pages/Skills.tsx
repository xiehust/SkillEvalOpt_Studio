import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { api, ApiError, SkillInfo } from "../api";
import {
  Card, EmptyState, ErrorBanner, Mono, PageHeader, Pagination, SOURCE_ORDER, SourceFilterChips,
  SourceTag, Spinner, truncate, usePagination,
} from "../components/ui";

export default function Skills() {
  const { t } = useTranslation("skills");
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
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
      setUploadError(t("upload.tooLarge", { size: (file.size / 1024 / 1024).toFixed(1) }));
      setUploadOk(null);
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    setUploading(true);
    setUploadError(null);
    setUploadOk(null);
    try {
      const info = await api.uploadSkill(file);
      setUploadOk(t("upload.success", { name: info.name }));
      await reload();
    } catch (err) {
      setUploadError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const filtered = (skills ?? []).filter((skill) => {
    if (sourceFilter && skill.source !== sourceFilter) return false;
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return (
      skill.name.toLowerCase().includes(q) ||
      skill.id.toLowerCase().includes(q) ||
      skill.description.toLowerCase().includes(q)
    );
  });
  // 分页作用于按来源排序后的扁平列表,再对当前页条目分组,保证翻页顺序与展示顺序一致。
  const ordered = [...filtered].sort((a, b) => {
    const ai = SOURCE_ORDER.indexOf(a.source);
    const bi = SOURCE_ORDER.indexOf(b.source);
    return (ai === -1 ? SOURCE_ORDER.length : ai) - (bi === -1 ? SOURCE_ORDER.length : bi);
  });
  const { page, setPage, pageSize, setPageSize, pageCount, pageItems, total } = usePagination(ordered);
  // 分组标题显示该来源在完整筛选结果中的总数,而非当前页条数
  const countBySource = (source: string) =>
    source === "other"
      ? ordered.filter((skill) => !SOURCE_ORDER.includes(skill.source)).length
      : ordered.filter((skill) => skill.source === source).length;
  const grouped = SOURCE_ORDER.map((source) => ({
    source,
    items: pageItems.filter((skill) => skill.source === source),
  })).filter((group) => group.items.length > 0);
  const otherItems = pageItems.filter((skill) => !SOURCE_ORDER.includes(skill.source));
  if (otherItems.length > 0) grouped.push({ source: "other", items: otherItems });

  return (
    <div>
      <PageHeader
        title={t("header.title")}
        sub={t("header.sub")}
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
              {uploading ? t("upload.uploading") : t("upload.button")}
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
          className="card border-good/40 px-4 py-3 text-sm text-good mb-4"
          data-testid="upload-ok"
        >
          {uploadOk}
        </div>
      )}
      {error && <ErrorBanner message={error} />}

      <div className="mb-5 flex flex-wrap items-center gap-3">
        <input
          className="input max-w-md !w-auto flex-1 min-w-[16rem]"
          placeholder={t("search.placeholder")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <SourceFilterChips skills={skills ?? []} value={sourceFilter} onChange={setSourceFilter} />
      </div>

      {skills === null && !error && <Spinner />}
      {skills !== null && filtered.length === 0 && (
        <EmptyState
          title={query || sourceFilter ? t("empty.noMatchTitle") : t("empty.noSkillsTitle")}
          hint={query || sourceFilter ? t("empty.noMatchHint") : t("empty.noSkillsHint")}
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
                  {t("card.count", { n: countBySource(group.source) })}
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
                  className="block bg-panel2 border border-line p-4 hover:border-faint transition-colors"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-medium text-sm truncate">{skill.name}</div>
                    {skill.has_support_files && (
                      <span className="font-mono text-[10px] text-s2 border border-s2/40 px-1.5 py-0.5 shrink-0">
                        {t("card.filesChip", { n: skill.files_count })}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-muted mt-1.5 leading-relaxed min-h-[2rem]">
                    {truncate(skill.description || t("card.noDescription"), 90)}
                  </div>
                  <Mono className="text-[11px] text-muted/70 block mt-2 truncate">{skill.id}</Mono>
                </Link>
              ))}
            </div>
          </Card>
        ))}
      </div>
      {filtered.length > 0 && (
        <Pagination
          page={page} pageCount={pageCount} pageSize={pageSize} total={total}
          onPage={setPage} onPageSize={setPageSize}
        />
      )}
    </div>
  );
}
