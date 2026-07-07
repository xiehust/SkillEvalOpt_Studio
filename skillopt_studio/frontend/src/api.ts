// Typed client for the SkillOpt Studio backend (mirrors skillopt_studio/models.py).
import { useEffect, useRef, useState } from "react";

export interface SkillInfo {
  id: string;
  name: string;
  source: string;
  path: string;
  description: string;
  files_count: number;
  has_support_files: boolean;
}

export interface SkillDetail extends SkillInfo {
  skill_md: string;
  file_tree: string[];
}

export interface TaskSetInfo {
  id: string;
  name: string;
  mode: "single" | "split";
  task_count: number;
  counts_by_split: Record<string, number>;
  created_at: string;
  updated_at?: string | null;
}

/** Task objects keep unknown ride-along fields so editor round-trips never drop data. */
export interface TaskItem {
  id: string;
  question: string;
  rubric: string;
  task_type?: string;
  files?: Record<string, string>;
  [key: string]: unknown;
}

export interface TaskSetDetail {
  info: TaskSetInfo;
  tasks_by_split: Record<string, TaskItem[]>;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface JobInfo {
  id: string;
  type: string;
  status: JobStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  params: Record<string, unknown>;
  out_root: string | null;
  exit_code: number | null;
  error: string | null;
}

export interface DashboardJobRow extends JobInfo {
  progress?: string;
  pass_rate?: number | null;
}

export interface DashboardData {
  running: DashboardJobRow[];
  recent: DashboardJobRow[];
  totals: { by_status: Record<string, number> };
}

export interface LogChunk {
  content: string;
  next_offset: number;
}

export interface EvalRow {
  id: string;
  task_type?: string;
  hard?: number;
  soft?: number;
  judge_reason?: string;
  duration_s?: number;
  error?: string;
  judge_error?: string;
}

export interface EvalResults {
  type: "eval";
  summary: { tasks: number; pass_rate: number; soft_mean: number; duration_s: number };
  rows: EvalRow[];
}

export interface TrainStep {
  step: number;
  epoch: number;
  action: string;
  selection_hard: number | null;
  selection_soft: number | null;
  current_score: number | null;
  best_score: number | null;
  best_step: number | null;
  skill_len: number | null;
  wall_time_s: number | null;
}

export interface TrainResults {
  type: "train";
  summary: {
    steps: TrainStep[];
    best_step: number | null;
    best_score: number | null;
    baseline_selection_hard: number | null;
    test_scores: { baseline: number | null; best: number | null; final: number | null };
    totals: {
      steps: number;
      accepts: number | null;
      rejects: number | null;
      skips: number | null;
      wall_time_s: number | null;
    };
    token_totals: Record<string, number>;
    finished: boolean;
  };
  skill_diff: string;
}

export interface TaskgenSummary {
  count?: number;
  requested_count?: number;
  backend?: string;
  model?: string;
  skill?: string;
  attempts?: number;
  duration_s?: number;
}

export interface TaskgenResults {
  type: "taskgen";
  tasks: TaskItem[];
  summary: TaskgenSummary;
}

export type JobResults = EvalResults | TrainResults | TaskgenResults;

export interface ArtifactDir {
  kind: "dir";
  path: string;
  dirs: string[];
  files: { name: string; size: number }[];
}

export interface ArtifactFile {
  kind: "text" | "binary";
  path: string;
  size: number;
  truncated?: boolean;
  content?: string;
}

export type ArtifactEntry = ArtifactDir | ArtifactFile;

export interface BackendStatus {
  backend: string;
  cli: string;
  available: boolean;
  path: string | null;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "无法连接后端服务");
  }
  if (!response.ok) {
    // Session expired / not logged in — flip the app back to the login gate
    // (the login endpoint's own 401 is a wrong-credentials error, not expiry).
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event("studio-unauthorized"));
    }
    let message = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (body.detail) message = JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body — keep the HTTP status message */
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

export interface AuthStatus {
  auth_required: boolean;
  authenticated: boolean;
}

export const api = {
  health: () => request<{ status: string }>("/api/health"),
  authStatus: () => request<AuthStatus>("/api/auth/status"),
  login: (username: string, password: string) =>
    request<{ ok: boolean }>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  environment: () => request<{ backends: BackendStatus[] }>("/api/environment"),
  skills: () => request<SkillInfo[]>("/api/skills"),
  skillDetail: (id: string) => request<SkillDetail>(`/api/skills/${encodeURIComponent(id)}`),
  uploadSkill: (file: File, name?: string) => {
    const form = new FormData();
    form.append("file", file);
    if (name) form.append("name", name);
    return request<SkillInfo>("/api/skills/upload", { method: "POST", body: form });
  },
  tasksets: () => request<TaskSetInfo[]>("/api/tasksets"),
  tasksetDetail: (id: string, full = false) =>
    request<TaskSetDetail>(`/api/tasksets/${encodeURIComponent(id)}${full ? "?full=1" : ""}`),
  createTaskset: (name: string, mode: "single" | "split", files: Record<string, File>) => {
    const form = new FormData();
    form.append("name", name);
    form.append("mode", mode);
    for (const [key, file] of Object.entries(files)) form.append(key, file);
    return request<TaskSetInfo>("/api/tasksets", { method: "POST", body: form });
  },
  createTasksetItems: (name: string, mode: "single" | "split", tasksBySplit: Record<string, TaskItem[]>) =>
    request<TaskSetInfo>("/api/tasksets/items", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, mode, tasks_by_split: tasksBySplit }),
    }),
  updateTaskset: (id: string, payload: { name?: string; tasks_by_split: Record<string, TaskItem[]> }) =>
    request<TaskSetInfo>(`/api/tasksets/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  deleteTaskset: (id: string) =>
    request<{ ok: boolean }>(`/api/tasksets/${encodeURIComponent(id)}`, { method: "DELETE" }),
  jobs: () => request<JobInfo[]>("/api/jobs"),
  job: (id: string) => request<JobInfo>(`/api/jobs/${encodeURIComponent(id)}`),
  createJob: (type: string, params: Record<string, unknown>) =>
    request<JobInfo>("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, params }),
    }),
  cancelJob: (id: string) =>
    request<JobInfo>(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  jobLog: (id: string, offset: number) =>
    request<LogChunk>(`/api/jobs/${encodeURIComponent(id)}/log?offset=${offset}`),
  jobResults: (id: string) => request<JobResults>(`/api/jobs/${encodeURIComponent(id)}/results`),
  jobArtifacts: (id: string, path = "") =>
    request<ArtifactEntry>(
      `/api/jobs/${encodeURIComponent(id)}/artifacts?path=${encodeURIComponent(path)}`,
    ),
  dashboard: () => request<DashboardData>("/api/dashboard"),
};

/** Poll a fetcher on an interval; pauses while the tab is hidden. */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs = 3000, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;

    const tick = async () => {
      if (document.hidden) {
        timer = window.setTimeout(tick, intervalMs);
        return;
      }
      try {
        const result = await fetcherRef.current();
        if (alive) {
          setData(result);
          setError(null);
        }
      } catch (err) {
        if (alive) setError(err instanceof ApiError ? err : new ApiError(0, String(err)));
      } finally {
        if (alive) {
          setLoading(false);
          timer = window.setTimeout(tick, intervalMs);
        }
      }
    };
    setLoading(true);
    tick();
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps

  return { data, error, loading };
}
