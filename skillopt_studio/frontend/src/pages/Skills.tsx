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
  const [pluginFilter, setPluginFilter] = useState("");
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
    if (pluginFilter && skill.plugin !== pluginFilter) return false;
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return (
      skill.name.toLowerCase().includes(q) ||
      skill.id.toLowerCase().includes(q) ||
      (skill.plugin?.toLowerCase().includes(q) ?? false) ||
      skill.description.toLowerCase().includes(q)
    );
  });
  const pluginSkills = (skills ?? []).filter((skill) => skill.source === "claude-plugins" && skill.plugin);
  const plugins = [...new Set(pluginSkills.map((skill) => skill.plugin as string))]
    .sort((a, b) => a.localeCompare(b));
  const selectSource = (source: string) => {
    setSourceFilter(source);
    if (source !== "claude-plugins") setPluginFilter("");
  };
  // 分页作用于按来源排序后的扁平列表,再对当前页条目分组,保证翻页顺序与展示顺序一致。
  const ordered = [...filtered].sort((a, b) => {
    const ai = SOURCE_ORDER.indexOf(a.source);
    const bi = SOURCE_ORDER.indexOf(b.source);
    const sourceDelta =
      (ai === -1 ? SOURCE_ORDER.length : ai) - (bi === -1 ? SOURCE_ORDER.length : bi);
    if (sourceDelta !== 0) return sourceDelta;
    if (a.source === "claude-plugins") {
      return (a.plugin ?? "").localeCompare(b.plugin ?? "");
    }
    return 0;
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
  const groupByPlugin = (items: SkillInfo[]) => {
    const groups = new Map<string, SkillInfo[]>();
    items.forEach((skill) => {
      const key = skill.plugin ?? "";
      groups.set(key, [...(groups.get(key) ?? []), skill]);
    });
    return [...groups.entries()].map(([plugin, pluginItems]) => ({ plugin, items: pluginItems }));
  };
  const countByPlugin = (plugin: string) =>
    ordered.filter((skill) => skill.source === "claude-plugins" && (skill.plugin ?? "") === plugin).length;
  const renderSkillGrid = (items: SkillInfo[]) => (
    <div className="grid min-w-0 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {items.map((skill) => (
        <Link
          key={skill.id}
          to={`/skills/${encodeURIComponent(skill.id)}`}
          data-skill-id={skill.id}
          className="block min-w-0 bg-panel2 border border-line p-4 hover:border-faint transition-colors"
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
  );

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

      <div className="mb-5 flex min-w-0 flex-wrap items-center gap-3">
        <input
          className="input max-w-md !w-full min-w-0 flex-1 sm:!w-auto sm:min-w-[16rem]"
          placeholder={t("search.placeholder")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <SourceFilterChips skills={skills ?? []} value={sourceFilter} onChange={selectSource} />
      </div>
      {sourceFilter === "claude-plugins" && plugins.length > 0 && (
        <div
          className="mb-5 flex flex-wrap items-center gap-1.5"
          data-testid="plugin-filter"
        >
          <span className="font-mono text-[10px] tracking-[0.08em] text-muted mr-1">
            {t("plugin.label")}
          </span>
          <button
            type="button"
            className={`font-mono text-[10.5px] px-2.5 py-1.5 border ${
              pluginFilter === ""
                ? "border-amber text-amber bg-amber/[.13]"
                : "border-line2 text-muted bg-well hover:border-faint hover:text-text"
            } max-w-full break-words`}
            onClick={() => setPluginFilter("")}
          >
            {t("plugin.all")} · {pluginSkills.length}
          </button>
          {plugins.map((plugin) => (
            <button
              key={plugin}
              type="button"
              data-plugin-chip={plugin}
              className={`font-mono text-[10.5px] px-2.5 py-1.5 border ${
                pluginFilter === plugin
                  ? "border-amber text-amber bg-amber/[.13]"
                  : "border-line2 text-muted bg-well hover:border-faint hover:text-text"
              } max-w-full break-words`}
              onClick={() => setPluginFilter(pluginFilter === plugin ? "" : plugin)}
            >
              {plugin} · {pluginSkills.filter((skill) => skill.plugin === plugin).length}
            </button>
          ))}
        </div>
      )}

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
            className="min-w-0 overflow-hidden"
            title={
              <span className="flex flex-wrap items-center gap-2">
                <SourceTag source={group.source} />
                <span className="text-muted normal-case tracking-normal">
                  {t("card.count", { n: countBySource(group.source) })}
                </span>
              </span>
            }
          >
            {group.source === "claude-plugins" ? (
              <div className="space-y-5">
                {groupByPlugin(group.items).map((pluginGroup) => (
                  <section key={pluginGroup.plugin || "unknown"} className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2 mb-2.5 pb-2 border-b border-line">
                      <span className="font-mono text-[11px] text-text">
                        {pluginGroup.plugin || t("plugin.unknown")}
                      </span>
                      <span className="font-mono text-[10px] text-muted">
                        {t("card.count", { n: countByPlugin(pluginGroup.plugin) })}
                      </span>
                    </div>
                    {renderSkillGrid(pluginGroup.items)}
                  </section>
                ))}
              </div>
            ) : renderSkillGrid(group.items)}
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
