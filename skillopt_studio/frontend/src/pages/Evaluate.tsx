import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api, ApiError, BackendStatus, SkillInfo, TaskSetInfo } from "../api";
import { BackendSelect, Card, ErrorBanner, Mono, PageHeader, SourceFilterChips, SourceTag, Spinner } from "../components/ui";

export default function Evaluate() {
  const { t } = useTranslation("wizards");
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [tasksets, setTasksets] = useState<TaskSetInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [skillId, setSkillId] = useState(searchParams.get("skill") ?? "");
  const [targetMode, setTargetMode] = useState<"skill" | "plugin">("skill");
  const [pluginKey, setPluginKey] = useState("");
  const [pluginSkillIds, setPluginSkillIds] = useState<string[]>([]);
  const [skillQuery, setSkillQuery] = useState("");
  const [skillSource, setSkillSource] = useState("");
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
    return (skills ?? []).filter(
      (skill) =>
        (!skillSource || skill.source === skillSource) &&
        (!q || skill.name.toLowerCase().includes(q) || skill.id.toLowerCase().includes(q)),
    );
  }, [skills, skillQuery, skillSource]);

  const pluginGroups = useMemo(() => {
    const grouped = new Map<string, { key: string; name: string; source: string; skills: SkillInfo[] }>();
    for (const skill of skills ?? []) {
      if (!skill.plugin) continue;
      const key = `${skill.source}::${skill.plugin}`;
      const group = grouped.get(key) ?? { key, name: skill.plugin, source: skill.source, skills: [] };
      group.skills.push(skill);
      grouped.set(key, group);
    }
    return [...grouped.values()]
      .filter((group) => group.skills.length >= 2)
      .map((group) => ({ ...group, skills: group.skills.sort((a, b) => a.name.localeCompare(b.name)) }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [skills]);
  const filteredPluginGroups = useMemo(() => {
    const query = skillQuery.trim().toLowerCase();
    if (!query) return pluginGroups;
    return pluginGroups.filter(
      (group) =>
        group.name.toLowerCase().includes(query)
        || group.skills.some(
          (skill) =>
            skill.name.toLowerCase().includes(query)
            || skill.id.toLowerCase().includes(query),
        ),
    );
  }, [pluginGroups, skillQuery]);
  const selectedPlugin = pluginGroups.find((group) => group.key === pluginKey);

  const selectedTaskset = tasksets?.find((taskset) => taskset.id === tasksetId);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const hasTarget = targetMode === "skill"
      ? Boolean(skillId)
      : Boolean(selectedPlugin) && pluginSkillIds.length >= 2;
    if (!hasTarget || !tasksetId) {
      setFormError(t("evaluate.errNoSkillTaskset"));
      return;
    }
    if (workers < 1 || workers > 8) {
      setFormError(t("picker.workersRangeError", { min: 1, max: 8 }));
      return;
    }
    if (timeout_ < 60 || timeout_ > 3600) {
      setFormError(t("picker.timeoutRangeError", { min: 60, max: 3600 }));
      return;
    }
    if (limit < 0) {
      setFormError(t("evaluate.errLimitNegative"));
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const job = await api.createJob("eval", {
        target_mode: targetMode,
        ...(targetMode === "skill"
          ? { skill_id: skillId }
          : { skill_ids: pluginSkillIds, plugin: selectedPlugin?.name }),
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
      <PageHeader title={t("evaluate.title")} sub={t("evaluate.subtitle")} />
      {loadError && <ErrorBanner message={loadError} />}
      {(skills === null || tasksets === null) && !loadError && <Spinner />}

      {skills !== null && tasksets !== null && (
        <form onSubmit={onSubmit} noValidate className="space-y-6" data-testid="evaluate-form">
          <Card title={t("evaluate.selectTargetTitle")}>
            <div className="inline-flex border border-line bg-panel2 mb-3" data-testid="eval-target-mode">
              {(["skill", "plugin"] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  className={`min-w-28 px-3 py-2 text-sm transition-colors ${
                    targetMode === mode ? "bg-amber/15 text-amber" : "text-muted hover:text-text"
                  }`}
                  onClick={() => { setTargetMode(mode); setSkillQuery(""); setFormError(null); }}
                  data-testid={`eval-mode-${mode}`}
                >
                  {t(`taskgen.mode.${mode}`)}
                </button>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-3 mb-3">
              <input
                className="input max-w-sm !w-auto flex-1 min-w-[14rem]"
                placeholder={
                  targetMode === "skill"
                    ? t("picker.searchSkillPlaceholder")
                    : t("taskgen.searchPluginPlaceholder")
                }
                value={skillQuery}
                onChange={(event) => setSkillQuery(event.target.value)}
              />
              {targetMode === "skill" && (
                <SourceFilterChips skills={skills} value={skillSource} onChange={setSkillSource} />
              )}
            </div>
            {targetMode === "skill" ? (
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3 max-h-72 overflow-y-auto pr-1">
              {filteredSkills.map((skill) => (
                <label
                  key={skill.id}
                  data-skill-option={skill.id}
                  className={`flex items-start gap-2.5 p-3 border cursor-pointer transition-colors ${
                    skillId === skill.id
                      ? "border-amber bg-amber/[.13]"
                      : "border-line bg-panel2 hover:border-muted"
                  }`}
                >
                  <input
                    type="radio"
                    name="skill"
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
                <div className="text-sm text-muted col-span-full py-4">
                  {t("evaluate.noSkillMatchPre")}
                  <Link to="/skills" className="text-s1 mx-1">{t("picker.skillLibraryLink")}</Link>
                  {t("evaluate.noSkillMatchPost")}
                </div>
              )}
            </div>
            ) : (
              <>
                <div className="grid gap-2 md:grid-cols-2 max-h-56 overflow-y-auto pr-1">
                  {filteredPluginGroups.map((group) => (
                      <label
                        key={group.key}
                        data-plugin-option={group.name}
                        className={`flex items-start gap-2.5 p-3 border cursor-pointer ${
                          pluginKey === group.key ? "border-amber bg-amber/[.13]" : "border-line bg-panel2 hover:border-muted"
                        }`}
                      >
                        <input
                          type="radio"
                          name="eval-plugin"
                          className="mt-1 accent-amber"
                          checked={pluginKey === group.key}
                          onChange={() => {
                            setPluginKey(group.key);
                            setPluginSkillIds(group.skills.map((skill) => skill.id));
                            applyBackend(group.source === "codex" ? "codex_exec" : "claude_code_exec");
                          }}
                        />
                        <span>
                          <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium">
                            {group.name}<SourceTag source={group.source} />
                          </span>
                          <span className="block text-xs text-muted mt-0.5">
                            {t("taskgen.pluginSkillCount", { count: group.skills.length })}
                          </span>
                        </span>
                      </label>
                    ))}
                  {filteredPluginGroups.length === 0 && (
                    <div className="text-sm text-muted col-span-full py-4">
                      {t("taskgen.noPluginMatch")}
                    </div>
                  )}
                </div>
                {selectedPlugin && (
                  <div className="mt-3 border border-line bg-panel2/40 p-3" data-testid="eval-plugin-skills">
                    <div className="text-xs text-muted mb-2">
                      <span className="text-text font-medium">{selectedPlugin.name}</span>
                      {" · "}
                      {t("taskgen.selectedPluginSkills", {
                        selected: pluginSkillIds.length,
                        total: selectedPlugin.skills.length,
                      })}
                    </div>
                    <div className="grid gap-2 md:grid-cols-2">
                      {selectedPlugin.skills.map((skill) => (
                        <label key={skill.id} className="flex items-center gap-2 min-w-0 cursor-pointer">
                          <input
                            type="checkbox"
                            className="accent-amber"
                            checked={pluginSkillIds.includes(skill.id)}
                            onChange={() => setPluginSkillIds((current) =>
                              current.includes(skill.id)
                                ? current.filter((id) => id !== skill.id)
                                : [...current, skill.id],
                            )}
                          />
                          <span className="text-sm truncate">{skill.name}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </Card>

          <Card title={t("evaluate.selectTasksetTitle")}>
            {tasksets.length === 0 ? (
              <div className="text-sm text-muted">
                {t("evaluate.noTasksetPre")}
                <Link to="/tasksets" className="text-s1 mx-1">{t("picker.tasksetPageLink")}</Link>
                {t("evaluate.noTasksetPost")}
              </div>
            ) : (
              <div className="grid gap-2 md:grid-cols-2">
                {tasksets.map((taskset) => (
                  <label
                    key={taskset.id}
                    data-taskset-option={taskset.id}
                    className={`flex items-start gap-2.5 p-3 border cursor-pointer transition-colors ${
                      tasksetId === taskset.id
                        ? "border-amber bg-amber/[.13]"
                        : "border-line bg-panel2 hover:border-muted"
                    }`}
                  >
                    <input
                      type="radio"
                      name="taskset"
                      className="mt-1 accent-amber"
                      checked={tasksetId === taskset.id}
                      onChange={() => setTasksetId(taskset.id)}
                    />
                    <span>
                      <span className="text-sm font-medium">{taskset.name}</span>
                      <span className="block text-xs text-muted mt-0.5">
                        {taskset.mode === "single"
                          ? t("evaluate.tasksetSingle", { n: taskset.task_count })
                          : t("evaluate.tasksetSplit", { n: taskset.counts_by_split.test ?? 0 })}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            )}
            {selectedTaskset && selectedTaskset.mode === "split" && (
              <p className="text-xs text-muted mt-3">
                {t("evaluate.splitNote")}
              </p>
            )}
          </Card>

          <Card title={t("evaluate.runParamsTitle")}>
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              <BackendSelect value={targetBackend} onChange={applyBackend} statuses={backends} />
              <div>
                <label className="label">{t("picker.targetModelLabel")}</label>
                <input
                  className="input font-mono" value={model} placeholder={t("picker.targetModelPlaceholder")}
                  onChange={(e) => setModel(e.target.value)}
                />
              </div>
              <div>
                <label className="label">{t("evaluate.judgeModelLabel")}</label>
                <input
                  className="input font-mono"
                  value={optimizerModel}
                  onChange={(e) => setOptimizerModel(e.target.value)}
                />
              </div>
              <div>
                <label className="label">{t("picker.workersLabel", { min: 1, max: 8 })}</label>
                <input
                  type="number" min={1} max={8} className="input font-mono"
                  value={workers} onChange={(e) => setWorkers(Number(e.target.value))}
                />
              </div>
              <div>
                <label className="label">{t("picker.timeoutLabel", { min: 60, max: 3600 })}</label>
                <input
                  type="number" min={60} max={3600} className="input font-mono"
                  value={timeout_} onChange={(e) => setTimeout_(Number(e.target.value))}
                />
              </div>
              <div>
                <label className="label">{t("evaluate.limitLabel")}</label>
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
            {submitting ? t("picker.creatingJob") : `▶ ${t("common:actions.evaluate")}`}
          </button>
        </form>
      )}
    </div>
  );
}
