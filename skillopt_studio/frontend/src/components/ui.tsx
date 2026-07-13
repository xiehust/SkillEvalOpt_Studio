// Shared design-system primitives for the studio console.
import { ReactNode, useState } from "react";
import { BackendStatus, JobStatus, TokenUsage } from "../api";

export function PageHeader({ title, sub, actions }: { title: ReactNode; sub?: string; actions?: ReactNode }) {
  return (
    <div className="flex items-start justify-between mb-6 gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-wide">{title}</h1>
        {sub && <p className="text-sm text-muted mt-1">{sub}</p>}
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
  queued: { label: "排队中", className: "text-muted border-muted/40 bg-muted/10", dot: "bg-muted" },
  running: { label: "运行中", className: "text-amber border-amber/40 bg-amber/10", dot: "bg-amber pulse-dot" },
  succeeded: { label: "成功", className: "text-green border-green/40 bg-green/10", dot: "bg-green" },
  failed: { label: "失败", className: "text-red border-red/40 bg-red/10", dot: "bg-red" },
  cancelled: { label: "已取消", className: "text-purple border-purple/40 bg-purple/10", dot: "bg-purple" },
};

export function StatusPill({ status }: { status: JobStatus }) {
  const meta = STATUS_META[status] ?? STATUS_META.queued;
  return (
    <span
      data-status={status}
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full border text-xs font-medium ${meta.className}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}

const TONE_CLASSES: Record<string, string> = {
  text: "text-text",
  green: "text-green",
  cyan: "text-cyan",
  amber: "text-amber",
  red: "text-red",
  purple: "text-purple",
  muted: "text-muted",
};

export function StatBadge({ label, value, tone = "text" }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="card px-4 py-3 min-w-[7.5rem]">
      <div className="text-[11px] uppercase tracking-widest text-muted">{label}</div>
      <div className={`text-2xl font-mono font-semibold mt-1 ${TONE_CLASSES[tone] ?? "text-text"}`}>{value}</div>
    </div>
  );
}

export function EmptyState({
  title, hint, action,
}: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center py-14 text-center">
      <div className="w-10 h-10 rounded border border-dashed border-line flex items-center justify-center mb-3">
        <span className="text-muted text-lg">∅</span>
      </div>
      <div className="text-sm text-text/80 font-medium">{title}</div>
      {hint && <div className="text-xs text-muted mt-1 max-w-sm">{hint}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Spinner({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-muted text-sm py-8 justify-center">
      <span className="w-4 h-4 rounded-full border-2 border-line border-t-cyan animate-spin" />
      {label}
    </div>
  );
}

export function ErrorBanner({ message, retryHint }: { message: string; retryHint?: string }) {
  return (
    <div className="card border-red/40 bg-red/5 px-4 py-3 text-sm text-red" role="alert">
      <span className="font-semibold mr-2">出错了:</span>
      {message}
      {retryHint && <span className="text-muted ml-2">{retryHint}</span>}
    </div>
  );
}

export const SOURCE_COLORS: Record<string, string> = {
  sample: "text-cyan border-cyan/40 bg-cyan/10",
  claude: "text-green border-green/40 bg-green/10",
  "claude-plugins": "text-green border-green/40 bg-green/5",
  codex: "text-cyan border-cyan/40 bg-cyan/10",
  kiro: "text-amber border-amber/40 bg-amber/10",
  agents: "text-purple border-purple/40 bg-purple/10",
  uploaded: "text-red border-red/40 bg-red/10",
};

const SOURCE_LABELS: Record<string, string> = {
  sample: "内置样例",
  "claude-plugins": "claude 插件",
};

export function SourceTag({ source }: { source: string }) {
  const color = SOURCE_COLORS[source] ?? "text-muted border-line bg-panel2";
  return (
    <span className={`inline-block px-2 py-0.5 rounded border text-xs font-mono ${color}`}>
      {SOURCE_LABELS[source] ?? source}
    </span>
  );
}

/** 内置样例任务集徽标(与 SourceTag 视觉一致)。 */
export function SampleTag() {
  return (
    <span className="inline-block px-2 py-0.5 rounded border text-xs font-mono text-cyan border-cyan/40 bg-cyan/10">
      内置样例
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
  const current = statuses?.find((status) => status.backend === value);
  return (
    <div>
      <label className="label">执行后端</label>
      <select
        className="input"
        data-testid="backend-select"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {EXEC_BACKEND_OPTIONS.map((option) => {
          const status = statuses?.find((s) => s.backend === option.value);
          const suffix = status && !status.available ? "(未检测到 CLI)" : "";
          return (
            <option key={option.value} value={option.value}>
              {option.label}{suffix}
            </option>
          );
        })}
      </select>
      {current && current.available && (
        <p className="text-xs text-green mt-1.5">
          ✓ 已检测到 {current.cli} CLI:<Mono>{current.path}</Mono>
        </p>
      )}
      {current && !current.available && (
        <p className="text-xs text-red mt-1.5" data-testid="backend-warning">
          ✗ 未检测到 {current.cli} CLI —— 请先安装并完成登录,否则提交会被拒绝。
        </p>
      )}
      <p className="text-xs text-muted mt-1.5">按技能来源自动推荐:codex 源技能默认用 Codex 执行。</p>
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
  if (total <= PAGE_SIZES[0]) return null;
  return (
    <div className="flex items-center justify-between gap-3 mt-4" data-testid="pagination">
      <span className="text-xs text-muted">共 {total} 条</span>
      <div className="flex items-center gap-2">
        <select
          className="input !w-auto !py-1 text-xs"
          value={pageSize}
          data-testid="page-size"
          onChange={(event) => onPageSize(Number(event.target.value))}
        >
          {PAGE_SIZES.map((size) => (
            <option key={size} value={size}>{size} 条/页</option>
          ))}
        </select>
        <button
          type="button"
          className="btn-ghost !px-2.5 !py-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed"
          disabled={page <= 1}
          data-testid="page-prev"
          onClick={() => onPage(page - 1)}
        >
          上一页
        </button>
        <span className="text-xs text-muted whitespace-nowrap">第 {page} / {pageCount} 页</span>
        <button
          type="button"
          className="btn-ghost !px-2.5 !py-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed"
          disabled={page >= pageCount}
          data-testid="page-next"
          onClick={() => onPage(page + 1)}
        >
          下一页
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
  return isNaN(date.getTime()) ? iso : date.toLocaleString("zh-CN", { hour12: false });
}

export function jobDuration(job: { started_at: string | null; finished_at: string | null }): string {
  if (!job.started_at) return "—";
  const start = new Date(job.started_at).getTime();
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  if (isNaN(start) || isNaN(end)) return "—";
  return formatDuration((end - start) / 1000);
}
