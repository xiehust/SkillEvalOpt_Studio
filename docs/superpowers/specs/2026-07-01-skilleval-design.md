# skilleval — 自有 Skill 评估功能（最小版）设计

- 日期：2026-07-01
- 状态：待评审
- 形态：SkillOpt 内新模块（`skillopt/envs/skilleval/` + `scripts/evaluate_skill.py`）

## 背景与目标

SkillOpt 现有的 `scripts/eval_only.py` 只能对**已注册 benchmark env** 的 skill 跑分，
无法评估用户自己写的任意 skill（如 `~/.claude/skills/` 下的 SKILL.md）。

本功能回答的问题是：**「我这个 skill 写得好不好？」**——把任意自有 skill 装进
Claude Code CLI，在用户自备的任务集上跑 agentic rollout，用 LLM judge 按每个任务的
rubric 打分，产出汇总 + 逐任务明细 + 成本统计的评估报告。

### 需求决策记录

| 维度 | 决策 |
|---|---|
| 评估对象 | 任意自有 skill（单个），不限于内置 benchmark |
| 执行环境 | Claude Code CLI（agentic），复用 `claude_code_exec` backend |
| 任务集来源 | 用户自备 JSON（约定格式） |
| 打分方式 | LLM judge，按任务自带 rubric |
| 报告内容 | 汇总分、逐任务明细、成本统计 |
| 落地形态 | SkillOpt 内新模块，与现有 env 架构同构 |

### 明确不做（后续迭代）

- **无 skill 对照组**（`--baseline`）——用户明确不需要
- **reflect 改进建议**——后续再加，基础设施（analyst prompts）已存在
- **任务自动生成**——后续再加
- **EnvAdapter / train.py 注册**——最小版只服务评估脚本；任务格式与项目
  item 约定兼容，后续接入训练时只需补 adapter

## 架构

### 新增文件

```
skillopt/envs/skilleval/
├── __init__.py
├── dataloader.py     # 任务集加载 + 格式校验
├── rollout.py        # 驱动 Claude Code CLI 跑任务
└── evaluator.py      # LLM judge 打分
scripts/evaluate_skill.py   # CLI 入口 + 报告生成
tests/test_skilleval.py     # 单元测试
```

### 复用的现有基础设施

| 现有组件 | 用途 |
|---|---|
| `skillopt/model/codex_harness.py` `_prepare_workdir()` | 把 skill 写入 work_dir 的 `.agents/skills/skillopt-target/SKILL.md`，写入任务文件与预置文件 |
| `skillopt/model/codex_harness.py` `run_claude_code_exec()` | 驱动 Claude Code（SDK/CLI 双模式、空响应重试、artifact 持久化） |
| `skillopt/model/__init__.py` `chat_optimizer()` | judge 模型调用（与 optimizer 模型共用配置） |
| `skillopt/model/backend_config.py` `configure_claude_code_exec()` | backend 配置，`.env` 约定与 train.py 一致 |
| `skillopt/datasets/base.py` `_load_json_or_jsonl()` | JSON/JSONL 加载 |

## 组件设计

### 1. 任务集格式（`dataloader.py`）

沿用项目 item 约定（每条必有 `id`），JSON 数组或 JSONL：

```json
[
  {
    "id": "task_001",
    "question": "把 data/report.csv 汇总成月度统计表",
    "rubric": "输出必须包含12个月份行；金额列求和正确；给出输出文件路径",
    "files": {"data/report.csv": "month,amount\n..."},
    "task_type": "data-processing"
  }
]
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 任务唯一标识（字符串） |
| `question` | ✅ | 发给 agent 的任务描述，写入 work_dir 的 `task.md` |
| `rubric` | ✅ | judge 的验收标准（自然语言 checklist） |
| `files` | — | 预置进 work_dir 的文件，`{相对路径: 文本内容}` |
| `task_type` | — | 分类统计用，缺省 `"default"` |

校验规则：缺 `id`/`question`/`rubric` 或 `id` 重复 → 启动时立即报错（fail fast），
不进入 rollout。

### 2. Rollout（`rollout.py`）

```
run_batch(items, skill_content, out_root, workers, timeout, model) -> list[dict]
```

每个任务（`ThreadPoolExecutor` 并发，`workers` 可配）：

1. `work_dir = out_root/rollouts/<task_id>/`
2. `_prepare_workdir(work_dir, skill_md=skill_content, task_text=item["question"], extra_files=item.get("files"))`
3. `response, raw = run_claude_code_exec(work_dir=..., prompt=固定引导词, model=..., timeout=...)`
   - 固定引导词与现有 env 一致：要求先读 `.agents/skills/skillopt-target/SKILL.md` 再做 `task.md` 中的任务
4. 记录 `duration_s`、`response`、raw transcript（落盘在 work_dir，不进内存结果）

错误处理：

- 单任务超时/异常**不中断整批**——该任务**跳过 judge**，直接记
  `hard=0`, `soft=0.0`，`error` 字段保留异常信息
- `run_claude_code_exec` 自带空响应重试，rollout 层不再重试

### 3. Judge（`evaluator.py`）

```
judge(item, response, artifacts_listing) -> dict
```

- 调 `chat_optimizer()`，judge prompt 包含：任务 `question`、`rubric`、agent 的
  `response`、work_dir 产物文件清单（文件名 + 大小；用于验证「生成了某文件」类 rubric）
- 强制 JSON 输出：`{"pass": bool, "score": float(0~1), "reason": str}`
- 解析失败重试一次；仍失败 → `hard=0`, `soft=0.0`, `judge_error` 标记（**不静默**，
  报告中单独列出）
- 输出结果 dict 沿用项目 rollout 结果约定：

```python
{"id": ..., "hard": 0/1, "soft": 0.0~1.0, "judge_reason": ...,
 "duration_s": ..., "response": ..., "task_type": ...,
 "error": ...(可选), "judge_error": ...(可选)}
```

### 4. CLI 与报告（`scripts/evaluate_skill.py`）

```bash
python scripts/evaluate_skill.py \
    --skill ~/.claude/skills/my-skill/SKILL.md \
    --tasks data/my_tasks.json \
    --out_root outputs/skilleval_myskill \
    [--workers 4] [--timeout 600] [--limit 0] [--model <model>]
```

- backend/模型配置沿用 `.env` 约定（`configure_claude_code_exec` 等），CLI 参数可覆盖
- `--limit N`：只跑前 N 个任务（调试用）

输出到 `out_root/`：

- **`results.json`** — 全部任务结果原始字段，供程序化消费
- **`report.md`** — 人读报告：
  - 汇总：任务数、通过率（hard 均值）、soft 均值、按 `task_type` 分组小计
  - 逐任务明细表：id / 通过与否 / soft / judge 理由摘要 / 耗时
  - 成本统计：每任务与总耗时；token 用量尽力从 raw transcript 解析，解析不到标 `n/a`
  - 失败与 `judge_error` 任务单独列出
- **`rollouts/<task_id>/`** — 每任务 work_dir（含 transcript artifact），供人工排查

## 测试策略

| 层 | 方式 |
|---|---|
| dataloader | 单测：合法样例通过；缺字段 / `id` 重复报错 |
| evaluator | 单测：mock `chat_optimizer`，测 JSON 解析、畸形输出重试、最终失败标记 |
| rollout | 单测：mock `run_claude_code_exec`，测并发编排、单任务异常隔离、超时记账 |
| 报告 | 单测：给定构造好的结果列表，断言 report.md / results.json 关键内容 |
| 端到端 | 冒烟说明放 `docs/guide/`（真跑 1 个任务），不进 CI |

## 里程碑之后（非本期）

1. `--baseline` 无 skill 对照组（run_batch 传空 skill 再跑一遍 + 报告对比列）
2. reflect 改进建议（复用 analyst prompts → 生成 skill 修改建议章节）
3. `SkillEvalAdapter` 注册进 train.py —— 同一份任务集直接做 skill 优化，形成
   「评估 → 发现不足 → 训练优化」闭环
4. 任务自动生成辅助命令（LLM 读 skill 生成任务草稿，人工审核后使用）
