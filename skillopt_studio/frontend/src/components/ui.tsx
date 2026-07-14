// Shared design-system primitives for the studio console.
import { ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { BackendStatus, JobStatus, SkillInfo, TokenUsage } from "../api";
import { dateLocale } from "../i18n";

/** 技能来源的展示顺序(技能库分组与筛选 chips 共用)。 */
export const SOURCE_ORDER = ["sample", "claude", "claude-plugins", "codex", "kiro", "agents", "uploaded"];

export function PageHeader({
  title, sub, actions, kicker = "SKILLEVAL&OPT STUDIO",
}: { title: ReactNode; sub?: string; actions?: ReactNode; kicker?: string }) {
  return (
    <div className="flex items-end justify-between mb-5 gap-4 flex-wrap">
      <div>
        <div className="font-mono text-[10px] tracking-[0.24em] text-amber uppercase mb-1.5">
          {"// "}{kicker}
        </div>
        <h1 className="text-[26px] leading-tight font-extrabold [font-stretch:118%] tracking-[0.01em]">{title}</h1>
        {sub && <p className="text-[12.5px] text-muted mt-1">{sub}</p>}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </div>
  );
}

export function Card({
  title, actions, children, className = "",
}: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={`card ${className}`}>
      {(title || actions) && (
        <div className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="section-title">{title}</div>
          {actions}
        </div>
      )}
      <div className="p-4">{children}</div>
    </div>
  );
}

const STATUS_META: Record<JobStatus, { label: string; className: string; dot: string }> = {
  queued: { label: "status.queued", className: "text-muted border-line2", dot: "bg-muted" },
  running: { label: "status.running", className: "text-warn border-warn/[.35]", dot: "bg-warn pulse-dot" },
  succeeded: { label: "status.succeeded", className: "text-good border-good/40", dot: "bg-good" },
  failed: { label: "status.failed", className: "text-critText border-crit/45", dot: "bg-crit" },
  cancelled: { label: "status.cancelled", className: "text-s5 border-s5/40", dot: "bg-s5" },
};

export function StatusPill({ status }: { status: JobStatus }) {
  const { t } = useTranslation("common");
  const meta = STATUS_META[status] ?? STATUS_META.queued;
  return (
    <span
      data-status={status}
      className={`inline-flex items-center gap-1.5 px-2 py-[3px] border font-mono text-[10px] tracking-[0.05em] whitespace-nowrap ${meta.className}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${meta.dot}`} />
      {t(meta.label)}
    </span>
  );
}

const TONE_CLASSES: Record<string, string> = {
  text: "text-text",
  good: "text-good",
  s1: "text-s1",
  s2: "text-s2",
  amber: "text-amber",
  critText: "text-critText",
  s5: "text-s5",
  muted: "text-muted",
  // legacy tone keys — map to semantic tokens so old call sites stay correct
  green: "text-good",
  cyan: "text-s2",
  red: "text-critText",
  purple: "text-s5",
};

export function StatBadge({ label, value, tone = "text" }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="card px-4 py-3.5 min-w-[7.5rem]">
      <div className="font-mono text-[9.5px] uppercase tracking-[0.2em] text-faint">{label}</div>
      <div className={`text-[28px] leading-none font-extrabold [font-stretch:115%] tracking-[0.01em] mt-2.5 ${TONE_CLASSES[tone] ?? "text-text"}`}>{value}</div>
    </div>
  );
}

export function EmptyState({
  title, hint, action,
}: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center py-14 text-center">
      <div className="w-10 h-10 border border-dashed border-line2 flex items-center justify-center mb-3">
        <span className="text-faint text-lg font-mono">∅</span>
      </div>
      <div className="font-mono text-[11px] tracking-[0.08em] text-muted">{title}</div>
      {hint && <div className="text-xs text-faint mt-1.5 max-w-sm">{hint}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  const { t } = useTranslation("common");
  return (
    <div className="flex items-center gap-2.5 font-mono text-[11px] tracking-[0.12em] text-faint py-8 justify-center">
      <span className="w-4 h-4 rounded-full border-2 border-line2 border-t-amber animate-spin" />
      {label ?? t("loading")}
    </div>
  );
}

export function ErrorBanner({ message, retryHint }: { message: string; retryHint?: string }) {
  const { t } = useTranslation("common");
  return (
    <div className="card border-crit/45 border-l-[3px] border-l-crit px-4 py-3 text-sm text-critText" role="alert">
      <span className="font-mono font-semibold mr-2 tracking-[0.05em]">{t("errorPrefix")}</span>
      {message}
      {retryHint && <span className="text-muted ml-2">{retryHint}</span>}
    </div>
  );
}

export const SOURCE_COLORS: Record<string, string> = {
  sample: "text-s2 border-s2/40",
  claude: "text-amber border-amber/40",
  "claude-plugins": "text-amber border-amber/40",
  codex: "text-s1 border-s1/40",
  kiro: "text-s3 border-s3/45",
  agents: "text-s5 border-s5/40",
  uploaded: "text-serious border-serious/40",
};

const SOURCE_LABEL_KEYS: Record<string, string> = {
  sample: "source.sample",
  "claude-plugins": "source.claudePlugins",
};

export function SourceTag({ source }: { source: string }) {
  const { t } = useTranslation("common");
  const color = SOURCE_COLORS[source] ?? "text-muted border-line2";
  const labelKey = SOURCE_LABEL_KEYS[source];
  return (
    <span className={`inline-block px-2 py-[3px] border font-mono text-[10px] tracking-[0.05em] ${color}`}>
      {labelKey ? t(labelKey) : source}
    </span>
  );
}

/** 按技能来源筛选的 chips 行(技能库 + 评估/训练向导共用);"" = 全部。 */
export function SourceFilterChips({
  skills, value, onChange,
}: { skills: SkillInfo[]; value: string; onChange: (source: string) => void }) {
  const { t } = useTranslation("common");
  const present = new Set(skills.map((skill) => skill.source));
  const sources = SOURCE_ORDER.filter((source) => present.has(source))
    .concat([...present].filter((source) => !SOURCE_ORDER.includes(source)).sort());
  if (sources.length < 2) return null;
  const chip = "font-mono text-[10.5px] tracking-[0.05em] px-2.5 py-1.5 border cursor-pointer whitespace-nowrap";
  const off = "border-line2 text-muted bg-well hover:border-faint hover:text-text";
  const on = "border-amber text-amber bg-amber/[.13]";
  return (
    <div className="flex flex-wrap gap-1.5" data-testid="source-filter">
      <button type="button" className={`${chip} ${value === "" ? on : off}`} onClick={() => onChange("")}>
        {t("filterAll")}
      </button>
      {sources.map((source) => (
        <button
          key={source}
          type="button"
          data-source-chip={source}
          className={`${chip} ${value === source ? on : off}`}
          onClick={() => onChange(value === source ? "" : source)}
        >
          {SOURCE_LABEL_KEYS[source] ? t(SOURCE_LABEL_KEYS[source]) : source}
        </button>
      ))}
    </div>
  );
}

/** 内置样例任务集徽标(与 SourceTag 视觉一致)。 */
export function SampleTag() {
  const { t } = useTranslation("common");
  return (
    <span className="inline-block px-2 py-[3px] border font-mono text-[10px] tracking-[0.05em] text-s2 border-s2/40">
      {t("source.sample")}
    </span>
  );
}

export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`font-mono text-[0.92em] ${className}`}>{children}</span>;
}

export const EXEC_BACKEND_OPTIONS = [
  { value: "claude_code_exec", label: "claude_code_exec — Claude Code CLI" },
  { value: "codex_exec", label: "codex_exec — Codex CLI" },
];

/** 执行后端下拉 + CLI 安装检测提示(两个向导共用)。 */
export function BackendSelect({
  value, onChange, statuses,
}: { value: string; onChange: (backend: string) => void; statuses: BackendStatus[] | null }) {
  const { t } = useTranslation("common");
  const current = statuses?.find((status) => status.backend === value);
  return (
    <div>
      <label className="label">{t("backendSelect.label")}</label>
      <select
        className="input"
        data-testid="backend-select"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {EXEC_BACKEND_OPTIONS.map((option) => {
          const status = statuses?.find((s) => s.backend === option.value);
          const suffix = status && !status.available ? t("backendSelect.notDetectedSuffix") : "";
          return (
            <option key={option.value} value={option.value}>
              {option.label}{suffix}
            </option>
          );
        })}
      </select>
      {current && current.available && (
        <p className="text-xs text-good mt-1.5">
          {t("backendSelect.detected", { cli: current.cli })}<Mono>{current.path}</Mono>
        </p>
      )}
      {current && !current.available && (
        <p className="text-xs text-critText mt-1.5" data-testid="backend-warning">
          {t("backendSelect.notDetected", { cli: current.cli })}
        </p>
      )}
      <p className="text-xs text-muted mt-1.5">{t("backendSelect.hint")}</p>
    </div>
  );
}

export const PAGE_SIZES = [20, 40, 80, 120];

/** 列表分页:page 越界时自动钳制,改每页条数时回到第 1 页。 */
export function usePagination<T>(items: T[], defaultSize = PAGE_SIZES[0]) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSizeRaw] = useState(defaultSize);
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const pageItems = items.slice((safePage - 1) * pageSize, safePage * pageSize);
  const setPageSize = (size: number) => {
    setPageSizeRaw(size);
    setPage(1);
  };
  return { page: safePage, setPage, pageSize, setPageSize, pageCount, pageItems, total: items.length };
}

export function Pagination({
  page, pageCount, pageSize, total, onPage, onPageSize,
}: {
  page: number;
  pageCount: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
  onPageSize: (size: number) => void;
}) {
  const { t } = useTranslation("common");
  if (total <= PAGE_SIZES[0]) return null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 mt-4" data-testid="pagination">
      <span className="font-mono text-[10.5px] text-faint tracking-[0.05em]">{t("pagination.total", { total })}</span>
      <div className="flex flex-wrap items-center justify-end gap-2">
        <select
          className="input !w-auto !py-1 text-xs"
          value={pageSize}
          data-testid="page-size"
          onChange={(event) => onPageSize(Number(event.target.value))}
        >
          {PAGE_SIZES.map((size) => (
            <option key={size} value={size}>{t("pagination.perPage", { size })}</option>
          ))}
        </select>
        <button
          type="button"
          className="btn-ghost !px-2.5 !py-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed"
          disabled={page <= 1}
          data-testid="page-prev"
          onClick={() => onPage(page - 1)}
        >
          {t("pagination.prev")}
        </button>
        <span className="font-mono text-[10.5px] text-faint whitespace-nowrap">{t("pagination.pageOf", { page, pageCount })}</span>
        <button
          type="button"
          className="btn-ghost !px-2.5 !py-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed"
          disabled={page >= pageCount}
          data-testid="page-next"
          onClick={() => onPage(page + 1)}
        >
          {t("pagination.next")}
        </button>
      </div>
    </div>
  );
}

export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

const TOKEN_PARTS: { key: keyof TokenUsage; label: string; full: string }[] = [
  { key: "input", label: "in", full: "input" },
  { key: "cache_write", label: "cw", full: "cache write" },
  { key: "cache_read", label: "cr", full: "cache read" },
  { key: "output", label: "out", full: "output" },
];

/** 任务级 token 消耗四项分列(input / cache write / cache read / output)。 */
export function TokenCell({ tokens }: { tokens?: TokenUsage | null }) {
  if (!tokens) return <span className="text-muted">—</span>;
  const title = TOKEN_PARTS
    .map((part) => `${part.full} ${tokens[part.key].toLocaleString()}`)
    .concat(`total ${tokens.total.toLocaleString()}`)
    .join(" · ");
  return (
    <span className="inline-flex items-center gap-2 whitespace-nowrap" title={title}>
      {TOKEN_PARTS.map((part) => (
        <span key={part.key} className="inline-flex items-baseline gap-0.5">
          <span className="text-[10px] text-muted">{part.label}</span>
          <Mono className="text-xs">{formatTokens(tokens[part.key])}</Mono>
        </span>
      ))}
    </span>
  );
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function truncate(value: string, max = 80): string {
  return value.length > max ? value.slice(0, max - 1) + "…" : value;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return isNaN(date.getTime()) ? iso : date.toLocaleString(dateLocale(), { hour12: false });
}

export function jobSkillLabel(job: { params?: Record<string, unknown> }): string {
  const skillIds = Array.isArray(job.params?.skill_ids)
    ? job.params.skill_ids.map(String).filter(Boolean)
    : [];
  if (skillIds.length > 0) {
    const plugin = String(job.params?.plugin ?? "").trim();
    return plugin
      ? `Plugin · ${plugin} · ${skillIds.length} Skills`
      : `Plugin · ${skillIds.length} Skills`;
  }
  return String(job.params?.skill_id ?? "—");
}

export function jobDuration(job: { started_at: string | null; finished_at: string | null }): string {
  if (!job.started_at) return "—";
  const start = new Date(job.started_at).getTime();
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  if (isNaN(start) || isNaN(end)) return "—";
  return formatDuration((end - start) / 1000);
}
