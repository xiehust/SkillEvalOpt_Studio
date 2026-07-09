import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, BackendStatus, SkillInfo, TaskSetInfo } from "../api";
import { BackendSelect, Card, ErrorBanner, Mono, PageHeader, SourceTag, Spinner } from "../components/ui";

export default function Evaluate() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [tasksets, setTasksets] = useState<TaskSetInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [skillId, setSkillId] = useState(searchParams.get("skill") ?? "");
  const [skillQuery, setSkillQuery] = useState("");
  const [tasksetId, setTasksetId] = useState("");
  const [targetBackend, setTargetBackend] = useState("claude_code_exec");
  const [backends, setBackends] = useState<BackendStatus[] | null>(null);
  const [model, setModel] = useState("global.anthropic.claude-opus-4-8");
  const [optimizerModel, setOptimizerModel] = useState("openai.gpt-5.5");
  const [workers, setWorkers] = useState(3);
  const [timeout_, setTimeout_] = useState(900);
  const [limit, setLimit] = useState(0);

  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    Promise.all([api.skills(), api.tasksets()])
      .then(([skillList, tasksetList]) => {
        setSkills(skillList);
        setTasksets(tasksetList);
      })
      .catch((err) => setLoadError(err instanceof ApiError ? err.message : String(err)));
    api.environment().then((env) => setBackends(env.backends)).catch(() => setBackends(null));
  }, []);

  // 按技能来源推荐执行后端:codex 源技能默认 Codex 执行,模型留空走后端默认
  const applyBackend = (backend: string) => {
    setTargetBackend(backend);
    setModel(backend === "codex_exec" ? "" : "global.anthropic.claude-opus-4-8");
  };

  useEffect(() => {
    const skill = skills?.find((s) => s.id === skillId);
    if (skill) applyBackend(skill.source === "codex" ? "codex_exec" : "claude_code_exec");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skillId, skills]);

  const filteredSkills = useMemo(() => {
    const q = skillQuery.trim().toLowerCase();
    if (!q) return skills ?? [];
    return (skills ?? []).filter(
      (skill) => skill.name.toLowerCase().includes(q) || skill.id.toLowerCase().includes(q),
    );
  }, [skills, skillQuery]);

  const selectedTaskset = tasksets?.find((taskset) => taskset.id === tasksetId);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!skillId || !tasksetId) {
      setFormError("请先选择技能和任务集,再发起评估。");
      return;
    }
    if (workers < 1 || workers > 8) {
      setFormError("并发 workers 需在 1-8 之间。");
      return;
    }
    if (timeout_ < 60 || timeout_ > 3600) {
      setFormError("单任务超时需在 60-3600 秒之间。");
      return;
    }
    if (limit < 0) {
      setFormError("任务数上限不能为负数。");
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const job = await api.createJob("eval", {
        skill_id: skillId,
        taskset_id: tasksetId,
        target_backend: targetBackend,
        model: model.trim(),
        optimizer_model: optimizerModel.trim(),
        workers,
        timeout: timeout_,
        ...(limit > 0 ? { limit } : {}),
      });
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err));
      setSubmitting(false);
    }
  };

  return (
    <div>
      <PageHeader title="发起评估" sub="选择技能与任务集,用 LLM 判分器按 rubric 逐任务评估" />
      {loadError && <ErrorBanner message={loadError} />}
      {(skills === null || tasksets === null) && !loadError && <Spinner />}

      {skills !== null && tasksets !== null && (
        <form onSubmit={onSubmit} noValidate className="space-y-6" data-testid="evaluate-form">
          <Card title="① 选择技能">
            <input
              className="input max-w-sm mb-3"
              placeholder="搜索技能…"
              value={skillQuery}
              onChange={(event) => setSkillQuery(event.target.value)}
            />
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3 max-h-72 overflow-y-auto pr-1">
              {filteredSkills.map((skill) => (
                <label
                  key={skill.id}
                  data-skill-option={skill.id}
                  className={`flex items-start gap-2.5 p-3 rounded border cursor-pointer transition-colors ${
                    skillId === skill.id
                      ? "border-green bg-green/5"
                      : "border-line bg-panel2 hover:border-muted"
                  }`}
                >
                  <input
                    type="radio"
                    name="skill"
                    className="mt-1 accent-[#A6DB4C]"
                    checked={skillId === skill.id}
                    onChange={() => setSkillId(skill.id)}
                  />
                  <span className="min-w-0">
                    <span className="flex items-center gap-2 text-sm font-medium">
                      <span className="truncate">{skill.name}</span>
                      <SourceTag source={skill.source} />
                    </span>
                    <Mono className="block text-[11px] text-muted/70 truncate mt-0.5">{skill.id}</Mono>
                  </span>
                </label>
              ))}
              {filteredSkills.length === 0 && (
                <div className="text-sm text-muted col-span-full py-4">
                  没有匹配的技能——可以先到
                  <Link to="/skills" className="text-cyan mx-1">技能库</Link>
                  上传一个 zip。
                </div>
              )}
            </div>
          </Card>

          <Card title="② 选择任务集">
            {tasksets.length === 0 ? (
              <div className="text-sm text-muted">
                还没有任务集,先到
                <Link to="/tasksets" className="text-cyan mx-1">任务集页</Link>
                上传一个 tasks.json。
              </div>
            ) : (
              <div className="grid gap-2 md:grid-cols-2">
                {tasksets.map((taskset) => (
                  <label
                    key={taskset.id}
                    data-taskset-option={taskset.id}
                    className={`flex items-start gap-2.5 p-3 rounded border cursor-pointer transition-colors ${
                      tasksetId === taskset.id
                        ? "border-green bg-green/5"
                        : "border-line bg-panel2 hover:border-muted"
                    }`}
                  >
                    <input
                      type="radio"
                      name="taskset"
                      className="mt-1 accent-[#A6DB4C]"
                      checked={tasksetId === taskset.id}
                      onChange={() => setTasksetId(taskset.id)}
                    />
                    <span>
                      <span className="text-sm font-medium">{taskset.name}</span>
                      <span className="block text-xs text-muted mt-0.5">
                        {taskset.mode === "single"
                          ? `single · ${taskset.task_count} 个任务`
                          : `split · 评估使用 test 分组(${taskset.counts_by_split.test ?? 0} 个任务)`}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            )}
            {selectedTaskset && selectedTaskset.mode === "split" && (
              <p className="text-xs text-muted mt-3">
                split 任务集评估时只跑 test 分组;想跑全部任务请用 single 模式任务集。
              </p>
            )}
          </Card>

          <Card title="③ 运行参数">
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              <BackendSelect value={targetBackend} onChange={applyBackend} statuses={backends} />
              <div>
                <label className="label">目标模型(执行任务)</label>
                <input
                  className="input font-mono" value={model} placeholder="留空 = 后端默认模型"
                  onChange={(e) => setModel(e.target.value)}
                />
              </div>
              <div>
                <label className="label">判分模型(judge)</label>
                <input
                  className="input font-mono"
                  value={optimizerModel}
                  onChange={(e) => setOptimizerModel(e.target.value)}
                />
              </div>
              <div>
                <label className="label">并发 workers(1-8)</label>
                <input
                  type="number" min={1} max={8} className="input font-mono"
                  value={workers} onChange={(e) => setWorkers(Number(e.target.value))}
                />
              </div>
              <div>
                <label className="label">单任务超时(60-3600 秒)</label>
                <input
                  type="number" min={60} max={3600} className="input font-mono"
                  value={timeout_} onChange={(e) => setTimeout_(Number(e.target.value))}
                />
              </div>
              <div>
                <label className="label">任务数上限(0 = 全部)</label>
                <input
                  type="number" min={0} className="input font-mono"
                  value={limit} onChange={(e) => setLimit(Number(e.target.value))}
                />
              </div>
            </div>
          </Card>

          {formError && (
            <div data-testid="evaluate-error">
              <ErrorBanner message={formError} />
            </div>
          )}

          <button type="submit" className="btn-primary" disabled={submitting} data-testid="evaluate-submit">
            {submitting ? "创建任务中…" : "▶ 发起评估"}
          </button>
        </form>
      )}
    </div>
  );
}
