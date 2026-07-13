import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api, ApiError, BackendStatus, SkillInfo } from "../api";
import { BackendSelect, ErrorBanner, Mono, SourceTag, Spinner } from "./ui";

/**
 * “AI 自动生成”标签页:选定待评估技能 + 执行后端,提交 taskgen 作业。
 * 生成结果不直接落库——作业完成后在详情页审阅,再导入手动编辑器保存。
 */
export default function GenerateTaskSetForm() {
  const { t } = useTranslation("wizards");
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
      setFormError(t("taskgen.errNoSkill"));
      return;
    }
    if (count < 1 || count > 30) {
      setFormError(t("taskgen.errCountRange", { min: 1, max: 30 }));
      return;
    }
    if (timeout_ < 60 || timeout_ > 3600) {
      setFormError(t("taskgen.errTimeoutRange", { min: 60, max: 3600 }));
      return;
    }
    if (!backendAvailable) {
      setFormError(t("taskgen.errBackendUnavailable"));
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
        {t("taskgen.introPre")}<b>{t("taskgen.introBold")}</b>{t("taskgen.introPost")}
      </p>

      <div>
        <label className="label">{t("taskgen.skillLabel")}</label>
        {skills.length === 0 ? (
          <div className="text-sm text-muted">
            {t("taskgen.noSkillPre")}<Link to="/skills" className="text-s1 mx-1">{t("picker.skillLibraryLink")}</Link>{t("taskgen.noSkillPost")}
          </div>
        ) : (
          <>
            <input
              className="input max-w-sm mb-2"
              placeholder={t("picker.searchSkillPlaceholder")}
              value={skillQuery}
              onChange={(event) => setSkillQuery(event.target.value)}
              data-testid="gen-skill-search"
            />
            <div className="grid gap-2 md:grid-cols-2 max-h-56 overflow-y-auto pr-1">
              {filteredSkills.map((skill) => (
                <label
                  key={skill.id}
                  data-skill-option={skill.id}
                  className={`flex items-start gap-2.5 p-2.5 border cursor-pointer transition-colors ${
                    skillId === skill.id ? "border-amber bg-amber/[.13]" : "border-line bg-panel2 hover:border-faint"
                  }`}
                >
                  <input
                    type="radio"
                    name="gen-skill"
                    className="mt-1 accent-amber"
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
                <div className="text-sm text-muted col-span-full py-4">{t("picker.noSkillMatch")}</div>
              )}
            </div>
          </>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <BackendSelect value={targetBackend} onChange={applyBackend} statuses={backends} />
        <div>
          <label className="label">{t("taskgen.modelLabel")}</label>
          <input
            className="input"
            value={model}
            placeholder={targetBackend === "codex_exec" ? t("taskgen.modelPlaceholderCodex") : t("taskgen.modelPlaceholderDefault")}
            onChange={(event) => setModel(event.target.value)}
            data-testid="gen-model"
          />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <label className="label">{t("taskgen.countLabel", { min: 1, max: 30 })}</label>
          <input
            type="number"
            className="input"
            value={count}
            onChange={(event) => setCount(Number(event.target.value))}
            data-testid="gen-count"
          />
        </div>
        <div>
          <label className="label">{t("taskgen.timeoutLabel", { min: 60, max: 3600 })}</label>
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
        <label className="label">{t("taskgen.guidanceLabel")}</label>
        <textarea
          className="input text-sm min-h-[64px]"
          value={guidance}
          placeholder={t("taskgen.guidancePlaceholder")}
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
        {submitting ? t("taskgen.submitting") : t("taskgen.submit")}
      </button>
    </form>
  );
}
