import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, BackendStatus, SkillInfo, TaskSetInfo } from "../api";
import { BackendSelect, Card, ErrorBanner, Mono, PageHeader, SourceTag, Spinner } from "../components/ui";

export default function Train() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [tasksets, setTasksets] = useState<TaskSetInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [skillId, setSkillId] = useState(searchParams.get("skill") ?? "");
  const [skillQuery, setSkillQuery] = useState("");
  const [mdFiles, setMdFiles] = useState<string[]>([]);
  const [trainableFiles, setTrainableFiles] = useState<string[]>([]);
  const [tasksetId, setTasksetId] = useState("");
  const [splitRatio, setSplitRatio] = useState("4:3:3");

  const [numEpochs, setNumEpochs] = useState(2);
  const [gateMetric, setGateMetric] = useState("soft");
  const [learningRate, setLearningRate] = useState(4);
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
    const skill = skills?.find((s) => s.id === skillId);
    if (skill) applyBackend(skill.source === "codex" ? "codex_exec" : "claude_code_exec");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skillId, skills]);

  // trainable_files candidates: .md files inside the skill dir, minus SKILL.md
  useEffect(() => {
    setTrainableFiles([]);
    setMdFiles([]);
    if (!skillId) return;
    api
      .skillDetail(skillId)
      .then((detail) => {
        setMdFiles(detail.file_tree.filter((f) => f.toLowerCase().endsWith(".md") && f !== "SKILL.md"));
      })
      .catch(() => setMdFiles([]));
  }, [skillId]);

  const filteredSkills = useMemo(() => {
    const q = skillQuery.trim().toLowerCase();
    if (!q) return skills ?? [];
    return (skills ?? []).filter(
      (skill) => skill.name.toLowerCase().includes(q) || skill.id.toLowerCase().includes(q),
    );
  }, [skills, skillQuery]);

  const selectedTaskset = tasksets?.find((taskset) => taskset.id === tasksetId);
  const selectedSkill = skills?.find((skill) => skill.id === skillId);

  const toggleTrainable = (file: string) => {
    setTrainableFiles((current) =>
      current.includes(file) ? current.filter((f) => f !== file) : [...current, file],
    );
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!skillId || !tasksetId) {
      setFormError("请先选择技能和任务集,再发起训练。");
      return;
    }
    if (numEpochs < 1 || numEpochs > 10) {
      setFormError("训练轮数需在 1-10 之间。");
      return;
    }
    if (learningRate < 1 || learningRate > 16) {
      setFormError("学习率需在 1-16 之间。");
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
    if (selectedTaskset?.mode === "single" && !/^[1-9]\d*:[1-9]\d*:[1-9]\d*$/.test(splitRatio)) {
      setFormError("分割比例格式应为 train:val:test,例如 4:3:3。");
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const job = await api.createJob("train", {
        skill_id: skillId,
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
        ...(trainableFiles.length > 0 ? { trainable_files: trainableFiles } : {}),
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
        title="发起训练"
        sub="以任务集为训练数据迭代优化技能文档,闸门通过才接受编辑,产出 best_skill.md"
      />
      {loadError && <ErrorBanner message={loadError} />}
      {(skills === null || tasksets === null) && !loadError && <Spinner />}

      {skills !== null && tasksets !== null && (
        <form onSubmit={onSubmit} noValidate className="space-y-6" data-testid="train-form">
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
            </div>
          </Card>

          <Card title="② 可训练文件(可选)">
            {!skillId && <p className="text-sm text-muted">先选择技能。</p>}
            {skillId && mdFiles.length === 0 && (
              <p className="text-sm text-muted" data-testid="no-trainable-hint">
                {selectedSkill?.has_support_files
                  ? "该技能没有可训练的支撑 .md 文件,将只训练 SKILL.md。"
                  : "单文件技能——没有支撑文件,将只训练 SKILL.md。"}
              </p>
            )}
            {skillId && mdFiles.length > 0 && (
              <div data-testid="trainable-files">
                <p className="text-xs text-muted mb-3">
                  SKILL.md 恒为可训练主文档(不在下方列表);勾选的支撑文档会与它打包成一个 bundle 一起训练。
                </p>
                <div className="grid gap-2 md:grid-cols-2">
                  {mdFiles.map((file) => (
                    <label
                      key={file}
                      className="flex items-center gap-2.5 p-2.5 rounded border border-line bg-panel2 cursor-pointer hover:border-muted"
                    >
                      <input
                        type="checkbox"
                        className="accent-[#A6DB4C]"
                        checked={trainableFiles.includes(file)}
                        onChange={() => toggleTrainable(file)}
                      />
                      <Mono className="text-xs">{file}</Mono>
                    </label>
                  ))}
                </div>
              </div>
            )}
          </Card>

          <Card title="③ 任务集">
            {tasksets.length === 0 ? (
              <div className="text-sm text-muted">
                还没有任务集,先到
                <Link to="/tasksets" className="text-cyan mx-1">任务集页</Link>
                创建。
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
                          ? `single · ${taskset.task_count} 个任务(训练时按比例自动分割)`
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
                <label className="label">分割比例 train:val:test</label>
                <input
                  className="input font-mono"
                  value={splitRatio}
                  onChange={(event) => setSplitRatio(event.target.value)}
                />
                <p className="text-xs text-muted mt-1.5">
                  single 任务集会按此比例确定性分割为训练 / 验证 / 测试三组。
                </p>
              </div>
            )}
          </Card>

          <Card title="④ 训练参数">
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              <div>
                <label className="label">训练轮数 num_epochs</label>
                <input
                  type="number" min={1} max={10} className="input font-mono"
                  value={numEpochs} onChange={(e) => setNumEpochs(Number(e.target.value))}
                />
                <p className="text-xs text-muted mt-1.5">每轮完整过一遍训练集。</p>
              </div>
              <div>
                <label className="label">闸门指标 gate_metric</label>
                <select className="input" value={gateMetric} onChange={(e) => setGateMetric(e.target.value)}>
                  <option value="hard">hard — 严格通过率</option>
                  <option value="soft">soft — 部分得分均值</option>
                  <option value="mixed">mixed — 两者加权</option>
                </select>
                <p className="text-xs text-muted mt-1.5">
                  编辑只有让验证集该指标严格提升才被接受;任务含多条精确规范时建议 soft。
                </p>
              </div>
              <div>
                <label className="label">学习率 learning_rate</label>
                <input
                  type="number" min={1} max={16} className="input font-mono"
                  value={learningRate} onChange={(e) => setLearningRate(Number(e.target.value))}
                />
                <p className="text-xs text-muted mt-1.5">每步最多接受的编辑条数上限。</p>
              </div>
              <BackendSelect value={targetBackend} onChange={applyBackend} statuses={backends} />
              <div>
                <label className="label">目标模型(执行任务)</label>
                <input
                  className="input font-mono" value={targetModel} placeholder="留空 = 后端默认模型"
                  onChange={(e) => setTargetModel(e.target.value)}
                />
              </div>
              <div>
                <label className="label">优化器 / 判分模型</label>
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
              <div className="flex items-center gap-2 pt-6">
                <input
                  id="eval-test"
                  type="checkbox"
                  className="accent-[#A6DB4C]"
                  checked={evalTest}
                  onChange={(e) => setEvalTest(e.target.checked)}
                />
                <label htmlFor="eval-test" className="text-sm">
                  训练结束后跑 test 分组
                  <span className="block text-xs text-muted">开启会多花一轮模型调用,冒烟建议关。</span>
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
            {submitting ? "创建任务中…" : "↻ 发起训练"}
          </button>
        </form>
      )}
    </div>
  );
}
