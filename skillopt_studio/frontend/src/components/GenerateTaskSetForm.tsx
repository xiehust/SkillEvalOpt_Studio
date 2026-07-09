import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, BackendStatus, SkillInfo } from "../api";
import { BackendSelect, ErrorBanner, Mono, SourceTag, Spinner } from "./ui";

/**
 * “AI 自动生成”标签页:选定待评估技能 + 执行后端,提交 taskgen 作业。
 * 生成结果不直接落库——作业完成后在详情页审阅,再导入手动编辑器保存。
 */
export default function GenerateTaskSetForm() {
  const navigate = useNavigate();
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [backends, setBackends] = useState<BackendStatus[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [skillId, setSkillId] = useState("");
  const [skillQuery, setSkillQuery] = useState("");
  const [targetBackend, setTargetBackend] = useState("claude_code_exec");
  const [model, setModel] = useState("global.anthropic.claude-opus-4-8");
  const [count, setCount] = useState(5);
  const [guidance, setGuidance] = useState("");
  const [timeout_, setTimeout_] = useState(900);

  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    api.skills().then(setSkills).catch((err) =>
      setLoadError(err instanceof ApiError ? err.message : String(err)),
    );
    api.environment().then((env) => setBackends(env.backends)).catch(() => setBackends(null));
  }, []);

  // 与评估向导一致:按技能来源推荐后端;codex 模型留空 = CLI 自身默认
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

  const backendAvailable =
    backends === null || backends.find((s) => s.backend === targetBackend)?.available !== false;

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!skillId) {
      setFormError("请先选择要生成任务的技能。");
      return;
    }
    if (count < 1 || count > 30) {
      setFormError("生成数量需在 1-30 之间。");
      return;
    }
    if (timeout_ < 60 || timeout_ > 3600) {
      setFormError("超时需在 60-3600 秒之间。");
      return;
    }
    if (!backendAvailable) {
      setFormError("所选执行后端的 CLI 未安装,无法提交。");
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const job = await api.createJob("taskgen", {
        skill_id: skillId,
        target_backend: targetBackend,
        model: model.trim(),
        count,
        guidance: guidance.trim(),
        timeout: timeout_,
      });
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err));
      setSubmitting(false);
    }
  };

  if (loadError) return <ErrorBanner message={loadError} />;
  if (skills === null) return <Spinner />;

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-4" data-testid="taskset-generate-form">
      <p className="text-xs text-muted">
        由 AI agent 阅读所选技能后自动撰写评估任务。生成结果<b>不会直接保存</b>:作业完成后在详情页审阅,
        点击“导入为新任务集”进入手动编辑器确认再保存。
      </p>

      <div>
        <label className="label">待评估技能</label>
        {skills.length === 0 ? (
          <div className="text-sm text-muted">
            还没有技能——先到<Link to="/skills" className="text-cyan mx-1">技能库</Link>上传一个。
          </div>
        ) : (
          <>
            <input
              className="input max-w-sm mb-2"
              placeholder="搜索技能…"
              value={skillQuery}
              onChange={(event) => setSkillQuery(event.target.value)}
              data-testid="gen-skill-search"
            />
            <div className="grid gap-2 md:grid-cols-2 max-h-56 overflow-y-auto pr-1">
              {filteredSkills.map((skill) => (
                <label
                  key={skill.id}
                  data-skill-option={skill.id}
                  className={`flex items-start gap-2.5 p-2.5 rounded border cursor-pointer transition-colors ${
                    skillId === skill.id ? "border-green bg-green/5" : "border-line bg-panel2 hover:border-muted"
                  }`}
                >
                  <input
                    type="radio"
                    name="gen-skill"
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
                <div className="text-sm text-muted col-span-full py-4">没有匹配的技能。</div>
              )}
            </div>
          </>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <BackendSelect value={targetBackend} onChange={applyBackend} statuses={backends} />
        <div>
          <label className="label">模型(可选)</label>
          <input
            className="input"
            value={model}
            placeholder={targetBackend === "codex_exec" ? "留空 = codex CLI 配置的默认模型" : "模型 ID"}
            onChange={(event) => setModel(event.target.value)}
            data-testid="gen-model"
          />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <label className="label">生成数量(1-30)</label>
          <input
            type="number"
            className="input"
            value={count}
            onChange={(event) => setCount(Number(event.target.value))}
            data-testid="gen-count"
          />
        </div>
        <div>
          <label className="label">超时(秒,60-3600)</label>
          <input
            type="number"
            className="input"
            value={timeout_}
            onChange={(event) => setTimeout_(Number(event.target.value))}
            data-testid="gen-timeout"
          />
        </div>
      </div>

      <div>
        <label className="label">生成指引(可选)</label>
        <textarea
          className="input text-sm min-h-[64px]"
          value={guidance}
          placeholder="例如:侧重边界场景;任务需要读取输入文件;难度递进"
          onChange={(event) => setGuidance(event.target.value)}
          data-testid="gen-guidance"
        />
      </div>

      {formError && (
        <div data-testid="taskset-generate-error">
          <ErrorBanner message={formError} />
        </div>
      )}

      <button type="submit" className="btn-primary" disabled={submitting} data-testid="taskset-generate-submit">
        {submitting ? "提交中…" : "开始生成"}
      </button>
    </form>
  );
}
