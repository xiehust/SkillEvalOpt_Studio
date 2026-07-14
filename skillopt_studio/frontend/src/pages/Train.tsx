import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api, ApiError, BackendStatus, SkillInfo, TaskSetInfo } from "../api";
import { buildPluginGroups, filterPluginGroups } from "../components/pluginGroups";
import { BackendSelect, Card, ErrorBanner, Mono, PageHeader, SourceFilterChips, SourceTag, Spinner } from "../components/ui";

export default function Train() {
  const { t } = useTranslation("wizards");
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [tasksets, setTasksets] = useState<TaskSetInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [targetMode, setTargetMode] = useState<"skill" | "plugin">("skill");
  const [skillId, setSkillId] = useState(searchParams.get("skill") ?? "");
  const [pluginKey, setPluginKey] = useState("");
  const [pluginSkillIds, setPluginSkillIds] = useState<string[]>([]);
  const [trainablePluginSkillIds, setTrainablePluginSkillIds] = useState<string[]>([]);
  const [skillQuery, setSkillQuery] = useState("");
  const [skillSource, setSkillSource] = useState("");
  const [mdFiles, setMdFiles] = useState<string[]>([]);
  const [trainableFiles, setTrainableFiles] = useState<string[]>([]);
  const [tasksetId, setTasksetId] = useState("");
  const [splitRatio, setSplitRatio] = useState("4:3:3");

  const [numEpochs, setNumEpochs] = useState(2);
  const [gateMetric, setGateMetric] = useState("soft");
  const [learningRate, setLearningRate] = useState(4);
  const [maxSkillsPerCandidate, setMaxSkillsPerCandidate] = useState(2);
  const [maxSkillRegression, setMaxSkillRegression] = useState(0);
  const [evalTest, setEvalTest] = useState(false);
  const [targetBackend, setTargetBackend] = useState("claude_code_exec");
  const [backends, setBackends] = useState<BackendStatus[] | null>(null);
  const [targetModel, setTargetModel] = useState("global.anthropic.claude-opus-4-8");
  const [optimizerModel, setOptimizerModel] = useState("openai.gpt-5.5");
  const [workers, setWorkers] = useState(3);
  const [timeout_, setTimeout_] = useState(900);

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

  // 按技能来源推荐执行后端;codex 后端时模型留空(走后端默认)
  const applyBackend = (backend: string) => {
    setTargetBackend(backend);
    setTargetModel(backend === "codex_exec" ? "" : "global.anthropic.claude-opus-4-8");
  };

  useEffect(() => {
    if (targetMode !== "skill") return;
    const skill = skills?.find((s) => s.id === skillId);
    if (skill) applyBackend(skill.source === "codex" ? "codex_exec" : "claude_code_exec");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skillId, skills, targetMode]);

  // trainable_files candidates: .md files inside the skill dir, minus SKILL.md
  useEffect(() => {
    setTrainableFiles([]);
    setMdFiles([]);
    if (targetMode !== "skill" || !skillId) return;
    api
      .skillDetail(skillId)
      .then((detail) => {
        setMdFiles(detail.file_tree.filter((f) => f.toLowerCase().endsWith(".md") && f !== "SKILL.md"));
      })
      .catch(() => setMdFiles([]));
  }, [skillId, targetMode]);

  const filteredSkills = useMemo(() => {
    const q = skillQuery.trim().toLowerCase();
    return (skills ?? []).filter(
      (skill) =>
        (!skillSource || skill.source === skillSource) &&
        (!q || skill.name.toLowerCase().includes(q) || skill.id.toLowerCase().includes(q)),
    );
  }, [skills, skillQuery, skillSource]);

  const pluginGroups = useMemo(
    () => buildPluginGroups(skills ?? []),
    [skills],
  );
  const filteredPluginGroups = useMemo(
    () => filterPluginGroups(pluginGroups, skillQuery),
    [pluginGroups, skillQuery],
  );
  const selectedPlugin = pluginGroups.find((group) => group.key === pluginKey);

  const selectedTaskset = tasksets?.find((taskset) => taskset.id === tasksetId);
  const selectedSkill = skills?.find((skill) => skill.id === skillId);

  const toggleTrainable = (file: string) => {
    setTrainableFiles((current) =>
      current.includes(file) ? current.filter((f) => f !== file) : [...current, file],
    );
  };

  const selectPlugin = (key: string) => {
    const group = pluginGroups.find((item) => item.key === key);
    if (!group) return;
    const ids = group.skills.map((skill) => skill.id);
    setPluginKey(group.key);
    setPluginSkillIds(ids);
    setTrainablePluginSkillIds(ids);
    setMaxSkillsPerCandidate(Math.min(2, ids.length));
    applyBackend(group.source === "codex" ? "codex_exec" : "claude_code_exec");
  };

  const toggleTrainablePluginSkill = (id: string) => {
    setTrainablePluginSkillIds((current) => {
      const next = current.includes(id)
        ? current.filter((skillIdToKeep) => skillIdToKeep !== id)
        : [...current, id];
      setMaxSkillsPerCandidate((value) => Math.min(value, Math.max(1, next.length)));
      return next;
    });
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const hasTarget = targetMode === "skill"
      ? Boolean(skillId)
      : Boolean(selectedPlugin)
        && pluginSkillIds.length >= 2
        && trainablePluginSkillIds.length >= 1;
    if (!hasTarget || !tasksetId) {
      setFormError(t("train.errNoSkillTaskset"));
      return;
    }
    if (numEpochs < 1 || numEpochs > 10) {
      setFormError(t("train.errEpochsRange", { min: 1, max: 10 }));
      return;
    }
    if (learningRate < 1 || learningRate > 16) {
      setFormError(t("train.errLearningRateRange", { min: 1, max: 16 }));
      return;
    }
    if (
      targetMode === "plugin"
      && (maxSkillsPerCandidate < 1 || maxSkillsPerCandidate > trainablePluginSkillIds.length)
    ) {
      setFormError(t("train.errMaxSkillsRange"));
      return;
    }
    if (targetMode === "plugin" && (maxSkillRegression < 0 || maxSkillRegression > 1)) {
      setFormError(t("train.errRegressionRange"));
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
    if (selectedTaskset?.mode === "single" && !/^[1-9]\d*:[1-9]\d*:[1-9]\d*$/.test(splitRatio)) {
      setFormError(t("train.errSplitRatio"));
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const job = await api.createJob("train", {
        target_mode: targetMode,
        ...(targetMode === "skill"
          ? { skill_id: skillId }
          : {
              skill_ids: pluginSkillIds,
              trainable_skill_ids: trainablePluginSkillIds,
              plugin: selectedPlugin?.name,
              max_skills_per_candidate: maxSkillsPerCandidate,
              max_skill_regression: maxSkillRegression,
            }),
        taskset_id: tasksetId,
        target_backend: targetBackend,
        target_model: targetModel.trim(),
        optimizer_model: optimizerModel.trim(),
        num_epochs: numEpochs,
        gate_metric: gateMetric,
        learning_rate: learningRate,
        eval_test: evalTest,
        workers,
        timeout: timeout_,
        ...(targetMode === "skill" && trainableFiles.length > 0
          ? { trainable_files: trainableFiles }
          : {}),
        ...(selectedTaskset?.mode === "single" ? { split_ratio: splitRatio } : {}),
      });
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err));
      setSubmitting(false);
    }
  };

  return (
    <div>
      <PageHeader
        title={t("train.title")}
        sub={t("train.subtitle")}
      />
      {loadError && <ErrorBanner message={loadError} />}
      {(skills === null || tasksets === null) && !loadError && <Spinner />}

      {skills !== null && tasksets !== null && (
        <form onSubmit={onSubmit} noValidate className="space-y-6" data-testid="train-form">
          <Card title={t("train.selectTargetTitle")}>
            <div className="inline-flex border border-line bg-panel2 mb-3" data-testid="train-target-mode">
              {(["skill", "plugin"] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  className={`min-w-28 px-3 py-2 text-sm transition-colors ${
                    targetMode === mode ? "bg-amber/15 text-amber" : "text-muted hover:text-text"
                  }`}
                  onClick={() => {
                    setTargetMode(mode);
                    setSkillQuery("");
                    setFormError(null);
                  }}
                  data-testid={`train-mode-${mode}`}
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
                    className={`flex min-w-0 items-start gap-2.5 p-3 border cursor-pointer transition-colors ${
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
              </div>
            ) : (
              <div className="grid gap-2 md:grid-cols-2 max-h-72 overflow-y-auto pr-1">
                {filteredPluginGroups.map((group) => (
                  <label
                    key={group.key}
                    data-plugin-option={group.name}
                    className={`flex min-w-0 items-start gap-2.5 p-3 border cursor-pointer transition-colors ${
                      pluginKey === group.key
                        ? "border-amber bg-amber/[.13]"
                        : "border-line bg-panel2 hover:border-muted"
                    }`}
                  >
                    <input
                      type="radio"
                      name="train-plugin"
                      className="mt-1 accent-amber"
                      checked={pluginKey === group.key}
                      onChange={() => selectPlugin(group.key)}
                    />
                    <span className="min-w-0">
                      <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                        <span className="truncate">{group.name}</span>
                        <SourceTag source={group.source} />
                      </span>
                      <span className="block text-xs text-muted mt-0.5">
                        {t("taskgen.pluginSkillCount", { count: group.skills.length })}
                      </span>
                    </span>
                  </label>
                ))}
                {filteredPluginGroups.length === 0 && (
                  <p className="text-sm text-muted py-4 md:col-span-2">
                    {t("taskgen.noPluginMatch")}
                  </p>
                )}
              </div>
            )}
          </Card>

          <Card
            title={
              targetMode === "skill"
                ? t("train.trainableFilesTitle")
                : t("train.trainableSkillsTitle")
            }
          >
            {targetMode === "skill" ? (
              <>
                {!skillId && <p className="text-sm text-muted">{t("train.selectSkillFirst")}</p>}
                {skillId && mdFiles.length === 0 && (
                  <p className="text-sm text-muted" data-testid="no-trainable-hint">
                    {selectedSkill?.has_support_files
                      ? t("train.noTrainableSupport")
                      : t("train.singleFileSkill")}
                  </p>
                )}
                {skillId && mdFiles.length > 0 && (
                  <div data-testid="trainable-files">
                    <p className="text-xs text-muted mb-3">
                      {t("train.trainableHint")}
                    </p>
                    <div className="grid gap-2 md:grid-cols-2">
                      {mdFiles.map((file) => (
                        <label
                          key={file}
                          className="flex min-w-0 items-center gap-2.5 p-2.5 border border-line bg-panel2 cursor-pointer hover:border-faint"
                        >
                          <input
                            type="checkbox"
                            className="accent-amber"
                            checked={trainableFiles.includes(file)}
                            onChange={() => toggleTrainable(file)}
                          />
                          <Mono className="text-xs">{file}</Mono>
                        </label>
                      ))}
                    </div>
                  </div>
                )}
              </>
            ) : selectedPlugin ? (
              <div data-testid="trainable-plugin-skills">
                <p className="text-xs text-muted mb-3">
                  {t("train.trainableSkillsHint", {
                    selected: trainablePluginSkillIds.length,
                    total: selectedPlugin.skills.length,
                  })}
                </p>
                <div className="grid gap-2 md:grid-cols-2">
                  {selectedPlugin.skills.map((skill) => (
                    <label
                      key={skill.id}
                      className="flex min-w-0 items-center gap-2.5 p-2.5 border border-line bg-panel2 cursor-pointer hover:border-faint"
                    >
                      <input
                        type="checkbox"
                        className="accent-amber"
                        checked={trainablePluginSkillIds.includes(skill.id)}
                        onChange={() => toggleTrainablePluginSkill(skill.id)}
                      />
                      <span className="min-w-0">
                        <span className="block text-sm">{skill.name}</span>
                        <Mono className="block text-[11px] text-muted truncate">{skill.id}</Mono>
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted">{t("train.selectPluginFirst")}</p>
            )}
          </Card>

          <Card title={t("train.tasksetTitle")}>
            {tasksets.length === 0 ? (
              <div className="text-sm text-muted">
                {t("train.noTasksetPre")}
                <Link to="/tasksets" className="text-s1 mx-1">{t("picker.tasksetPageLink")}</Link>
                {t("train.noTasksetPost")}
              </div>
            ) : (
              <div className="grid gap-2 md:grid-cols-2">
                {tasksets.map((taskset) => (
                  <label
                    key={taskset.id}
                    data-taskset-option={taskset.id}
                    className={`flex min-w-0 items-start gap-2.5 p-3 border cursor-pointer transition-colors ${
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
                          ? t("train.tasksetSingle", { n: taskset.task_count })
                          : `split · ${Object.entries(taskset.counts_by_split)
                              .map(([split, count]) => `${split}:${count}`)
                              .join(" ")}`}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            )}
            {selectedTaskset?.mode === "single" && (
              <div className="mt-4 max-w-xs" data-testid="split-ratio-field">
                <label className="label">{t("train.splitRatioLabel")}</label>
                <input
                  className="input font-mono"
                  value={splitRatio}
                  onChange={(event) => setSplitRatio(event.target.value)}
                />
                <p className="text-xs text-muted mt-1.5">
                  {t("train.splitRatioNote")}
                </p>
              </div>
            )}
          </Card>

          <Card title={t("train.trainParamsTitle")}>
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              <div>
                <label className="label">{t("train.numEpochsLabel")}</label>
                <input
                  type="number" min={1} max={10} className="input font-mono"
                  value={numEpochs} onChange={(e) => setNumEpochs(Number(e.target.value))}
                />
                <p className="text-xs text-muted mt-1.5">{t("train.numEpochsHint")}</p>
              </div>
              <div>
                <label className="label">{t("train.gateMetricLabel")}</label>
                <select className="input" value={gateMetric} onChange={(e) => setGateMetric(e.target.value)}>
                  <option value="hard">{t("train.gateHard")}</option>
                  <option value="soft">{t("train.gateSoft")}</option>
                  <option value="mixed">{t("train.gateMixed")}</option>
                </select>
                <p className="text-xs text-muted mt-1.5">
                  {t("train.gateMetricHint")}
                </p>
              </div>
              <div>
                <label className="label">{t("train.learningRateLabel")}</label>
                <input
                  type="number" min={1} max={16} className="input font-mono"
                  value={learningRate} onChange={(e) => setLearningRate(Number(e.target.value))}
                />
                <p className="text-xs text-muted mt-1.5">{t("train.learningRateHint")}</p>
              </div>
              {targetMode === "plugin" && (
                <>
                  <div>
                    <label className="label">{t("train.maxSkillsLabel")}</label>
                    <input
                      type="number"
                      min={1}
                      max={Math.max(1, trainablePluginSkillIds.length)}
                      className="input font-mono"
                      value={maxSkillsPerCandidate}
                      onChange={(event) => setMaxSkillsPerCandidate(Number(event.target.value))}
                    />
                    <p className="text-xs text-muted mt-1.5">{t("train.maxSkillsHint")}</p>
                  </div>
                  <div>
                    <label className="label">{t("train.maxRegressionLabel")}</label>
                    <input
                      type="number"
                      min={0}
                      max={1}
                      step={0.01}
                      className="input font-mono"
                      value={maxSkillRegression}
                      onChange={(event) => setMaxSkillRegression(Number(event.target.value))}
                    />
                    <p className="text-xs text-muted mt-1.5">
                      {t("train.maxRegressionHint")}
                    </p>
                  </div>
                </>
              )}
              <BackendSelect value={targetBackend} onChange={applyBackend} statuses={backends} />
              <div>
                <label className="label">{t("picker.targetModelLabel")}</label>
                <input
                  className="input font-mono" value={targetModel} placeholder={t("picker.targetModelPlaceholder")}
                  onChange={(e) => setTargetModel(e.target.value)}
                />
              </div>
              <div>
                <label className="label">{t("train.optimizerModelLabel")}</label>
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
              <div className="flex items-center gap-2 pt-6">
                <input
                  id="eval-test"
                  type="checkbox"
                  className="accent-amber"
                  checked={evalTest}
                  onChange={(e) => setEvalTest(e.target.checked)}
                />
                <label htmlFor="eval-test" className="text-sm">
                  {t("train.evalTestLabel")}
                  <span className="block text-xs text-muted">{t("train.evalTestHint")}</span>
                </label>
              </div>
            </div>
          </Card>

          {formError && (
            <div data-testid="train-error">
              <ErrorBanner message={formError} />
            </div>
          )}

          <button type="submit" className="btn-primary" disabled={submitting} data-testid="train-submit">
            {submitting ? t("picker.creatingJob") : `↻ ${t("common:actions.train")}`}
          </button>
        </form>
      )}
    </div>
  );
}
