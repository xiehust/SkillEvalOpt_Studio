"""Built-in sample skills and task sets, materialized from the repository.

``materialize_samples`` copies the paper checkpoint skills (``ckpt/``) and the
skilleval demo skills/tasks (``data/skilleval_demo/``) into the studio data
root at startup, so a fresh Studio always offers something runnable.  Samples
are read-only from the user's perspective and rebuilt on every startup — the
repository is the source of truth, never the materialized copy.

Fidelity invariant: a sample's ``SKILL.md`` is a byte-identical copy of its
source artifact.  Display name and description live in a hidden sidecar file
(:data:`SIDECAR_FILE`) instead of injected frontmatter.  The sidecar starts
with a dot, so ``collect_support_files`` (skilleval rollout) never copies it
into an evaluation workspace, and the skill file API refuses to serve it.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from skillopt_studio.config import StudioConfig

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SIDECAR_FILE = ".studio_sample.json"

# Benchmark→skilleval conversion subset sizes (first-N per split, deterministic).
SEARCHQA_LIMITS = {"train": 12, "val": 6, "test": 12}
SEARCHQA_SOURCE = "data/searchqa_split"
LIVEMATH_LIMITS = {"train": 12, "val": 6, "test": 12}
LIVEMATH_SOURCE = "data/livemathematicianbench_split"
OFFICEQA_LIMITS = {"train": 12, "val": 6, "test": 12}
OFFICEQA_SOURCE = "data/officeqa_split"
OFFICEQA_DOCS_SOURCE = "data/officeqa_docs_official"
# Oracle document context embedded per task; hard cap so one outlier page
# doesn't blow up the task file (truncation is marked in the text).
OFFICEQA_MAX_CONTEXT_CHARS = 16000


@dataclass(frozen=True)
class SampleSkill:
    slug: str
    source: str  # relative to PROJECT_ROOT
    kind: str  # "single_file" | "directory"
    name: str
    description: str


@dataclass(frozen=True)
class SampleTaskSet:
    taskset_id: str
    name: str
    mode: str  # "single" | "split"
    # split name -> source file relative to PROJECT_ROOT ("searchqa" converts instead)
    sources: dict


_NO_DATA_NOTE = "论文 Table 1 checkpoint（GPT-5.5 优化产物，字节级原样）。无内置任务集，可用 AI 任务生成或自带任务集评测。"

SAMPLE_SKILLS: tuple[SampleSkill, ...] = (
    SampleSkill(
        slug="searchqa-gpt5.5",
        source="ckpt/searchqa/gpt5.5_skill.md",
        kind="single_file",
        name="SearchQA 检索问答（论文 checkpoint）",
        description="论文 Table 1 checkpoint（GPT-5.5 优化产物，字节级原样）。可配 sample-searchqa 任务集直接评测。",
    ),
    SampleSkill(
        slug="alfworld-gpt5.5",
        source="ckpt/alfworld/gpt5.5_skill.md",
        kind="single_file",
        name="ALFWorld 具身任务（论文 checkpoint）",
        description=_NO_DATA_NOTE,
    ),
    SampleSkill(
        slug="docvqa-gpt5.5",
        source="ckpt/docvqa/gpt5.5_skill.md",
        kind="single_file",
        name="DocVQA 文档问答（论文 checkpoint）",
        description=_NO_DATA_NOTE,
    ),
    SampleSkill(
        slug="livemath-gpt5.5",
        source="ckpt/livemath/gpt5.5_skill.md",
        kind="single_file",
        name="LiveMathematicianBench 数学（论文 checkpoint）",
        description=_NO_DATA_NOTE,
    ),
    SampleSkill(
        slug="officeqa-gpt5.5",
        source="ckpt/officeqa/gpt5.5_skill.md",
        kind="single_file",
        name="OfficeQA 办公问答（论文 checkpoint）",
        description=_NO_DATA_NOTE,
    ),
    SampleSkill(
        slug="spreadsheetbench-gpt5.5",
        source="ckpt/spreadsheetbench/gpt5.5_skill.md",
        kind="single_file",
        name="SpreadsheetBench 表格操作（论文 checkpoint）",
        description=_NO_DATA_NOTE,
    ),
    SampleSkill(
        slug="logtriage",
        source="data/skilleval_demo/logtriage_skill",
        kind="directory",
        name="日志分析演示 skill（弱基线）",
        description="含解析脚本与格式文档的多文件演示 skill；配 sample-logtriage 任务集可评测，也适合作为训练起点。",
    ),
    SampleSkill(
        slug="logtriage-v2",
        source="data/skilleval_demo/logtriage_skill_v2",
        kind="directory",
        name="日志分析演示 skill v2（多文档训练）",
        description="多文档训练演示：references/report-template.md 是故意错误的模板，供 trainable_files 训练修复；配 sample-logtriage 任务集。",
    ),
    SampleSkill(
        slug="report",
        source="data/skilleval_demo/report_skill/initial.md",
        kind="single_file",
        name="CSV 报表演示 skill（弱基线）",
        description="极简弱基线，配 sample-report 或 sample-xlsx 任务集，适合演示评测与训练提升。",
    ),
)

SAMPLE_TASKSETS: tuple[SampleTaskSet, ...] = (
    SampleTaskSet(
        taskset_id="sample-logtriage",
        name="日志分析（logtriage）演示任务集",
        mode="split",
        sources={
            "train": "data/skilleval_demo/logtriage_tasks/train/items.json",
            "val": "data/skilleval_demo/logtriage_tasks/val/items.json",
            "test": "data/skilleval_demo/logtriage_tasks/test/items.json",
        },
    ),
    SampleTaskSet(
        taskset_id="sample-report",
        name="CSV 报表（report）演示任务集",
        mode="split",
        sources={
            "train": "data/skilleval_demo/report_tasks/train/items.json",
            "val": "data/skilleval_demo/report_tasks/val/items.json",
            "test": "data/skilleval_demo/report_tasks/test/items.json",
        },
    ),
    SampleTaskSet(
        taskset_id="sample-xlsx",
        name="Excel 处理（xlsx）演示任务集",
        mode="single",
        sources={"tasks": "data/skilleval_demo/xlsx_tasks.json"},
    ),
    SampleTaskSet(
        taskset_id="sample-searchqa",
        name="SearchQA 检索问答子集（12/6/12）",
        mode="split",
        sources={"convert": "searchqa"},
    ),
    SampleTaskSet(
        taskset_id="sample-livemath",
        name="LiveMathematicianBench 数学选择题子集（12/6/12）",
        mode="split",
        sources={"convert": "livemath"},
    ),
    SampleTaskSet(
        taskset_id="sample-officeqa",
        name="OfficeQA 办公文档问答子集（12/6/12）",
        mode="split",
        sources={"convert": "officeqa"},
    ),
)


def _write_sidecar(target_dir: Path, sample: SampleSkill) -> None:
    (target_dir / SIDECAR_FILE).write_text(
        json.dumps({"name": sample.name, "description": sample.description}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def materialize_sample_skills(config: StudioConfig) -> None:
    """Rebuild studio_root/samples/skills/<slug>/ from the repository sources.

    A missing source is skipped with a warning (e.g. a trimmed checkout); the
    remaining samples still materialize.
    """
    for sample in SAMPLE_SKILLS:
        source = PROJECT_ROOT / sample.source
        if not source.exists():
            logger.warning("sample skill %r skipped: source %s missing", sample.slug, source)
            continue
        target_dir = config.samples_skills_dir / sample.slug
        if target_dir.exists():
            shutil.rmtree(target_dir)
        if sample.kind == "single_file":
            target_dir.mkdir(parents=True)
            shutil.copyfile(source, target_dir / "SKILL.md")
        else:
            shutil.copytree(
                source, target_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        _write_sidecar(target_dir, sample)


def _convert_searchqa(split_path: Path, limit: int) -> list[dict]:
    """First-N materialized SearchQA items → skilleval tasks with answer rubrics."""
    items = json.loads(split_path.read_text(encoding="utf-8"))[:limit]
    tasks = []
    for item in items:
        answers = [str(a) for a in item.get("answers") or []]
        tasks.append(
            {
                "id": str(item["id"]),
                "task_type": "searchqa",
                "question": (
                    "回答下面的检索问答题。请只给出最终答案本身（简短、无多余说明）。\n\n"
                    f"问题：{item['question']}\n\n## 检索到的上下文\n{item.get('context', '')}"
                ),
                "rubric": (
                    f"标准答案（命中任一即可）：{json.dumps(answers, ensure_ascii=False)}。"
                    "判分：若最终答案与任一标准答案在忽略大小写、首尾空白与常见标点差异后一致"
                    "（或明确等价，如数字的不同写法），得 1.0；否则得 0.0。只看最终答案，不看推理过程。"
                ),
            }
        )
    return tasks


def _convert_livemath(split_path: Path, limit: int) -> list[dict]:
    """First-N LiveMathematicianBench items → multiple-choice skilleval tasks."""
    items = json.loads(split_path.read_text(encoding="utf-8"))[:limit]
    tasks = []
    for item in items:
        choices = "\n".join(f"{c['label']}. {c['text']}" for c in item["choices"])
        correct = item["correct_choice"]["label"]
        tasks.append(
            {
                # loader ids look like "202602:8" — ':' is filesystem-safe per
                # skilleval validation (only / \ .. are rejected)
                "id": item["id"].replace(":", "-"),
                "task_type": "livemath",
                "question": (
                    "回答下面的数学单选题。请只给出最终选项字母（如 A）。\n\n"
                    f"题目：{item['question']}\n\n选项：\n{choices}"
                ),
                "rubric": (
                    f"标准答案：{correct}。判分：最终答案明确选择 {correct}"
                    "（选项字母一致即可，允许附带该选项原文）得 1.0；选择其他选项、"
                    "多选或未明确给出选项得 0.0。只看最终选择，不看推理过程。"
                ),
            }
        )
    return tasks


def _convert_officeqa(split_dir: Path, limit: int) -> list[dict]:
    """First-N OfficeQA items → QA tasks with the oracle document embedded."""
    from skillopt.envs.officeqa.dataloader import OfficeQADataLoader
    from skillopt.envs.officeqa.tool_runtime import (
        build_oracle_parsed_pages_context,
        resolve_docs_roots,
    )

    loader = OfficeQADataLoader(split_dir=str(split_dir.parent), split_mode="split_dir")
    items = loader.load_split_items(str(split_dir))[:limit]
    roots = resolve_docs_roots([str(PROJECT_ROOT / OFFICEQA_DOCS_SOURCE)])
    tasks = []
    for item in items:
        context = build_oracle_parsed_pages_context(
            item["source_files"], item["source_docs"], roots
        )
        if len(context) > OFFICEQA_MAX_CONTEXT_CHARS:
            context = context[:OFFICEQA_MAX_CONTEXT_CHARS] + "\n…（文档已截断）"
        tasks.append(
            {
                "id": item["id"],
                "task_type": "officeqa",
                "question": (
                    "根据下面的财政公报文档内容回答问题。请只给出最终答案本身（简短、无多余说明）。\n\n"
                    f"问题：{item['question']}\n\n## 参考文档\n{context}"
                ),
                "rubric": (
                    f"标准答案：{item['ground_truth']!r}。判分：若最终答案与标准答案在忽略"
                    "大小写、首尾空白、千分位逗号与常见标点差异后一致（或数值明确相等），"
                    "得 1.0；否则得 0.0。只看最终答案，不看推理过程。"
                ),
            }
        )
    return tasks


def _convert_benchmark_split(sample: SampleTaskSet, kind: str) -> dict[str, list[dict]] | None:
    """Shared shape for converted benchmark subsets; None when data is absent."""
    if kind == "searchqa":
        root, limits = PROJECT_ROOT / SEARCHQA_SOURCE, SEARCHQA_LIMITS
        convert = lambda split, path: _convert_searchqa(path, limits[split])  # noqa: E731
        probe = {s: root / s / "items.json" for s in limits}
    elif kind == "livemath":
        root, limits = PROJECT_ROOT / LIVEMATH_SOURCE, LIVEMATH_LIMITS
        convert = lambda split, path: _convert_livemath(path, limits[split])  # noqa: E731
        probe = {s: root / s / "items.json" for s in limits}
    else:  # officeqa — needs both the split CSVs and the parsed docs
        root, limits = PROJECT_ROOT / OFFICEQA_SOURCE, OFFICEQA_LIMITS
        convert = lambda split, path: _convert_officeqa(path.parent, limits[split])  # noqa: E731
        probe = {s: root / s / "items.csv" for s in limits}
        if not (PROJECT_ROOT / OFFICEQA_DOCS_SOURCE).is_dir():
            logger.warning(
                "sample task set %r skipped: %s not downloaded",
                sample.taskset_id, OFFICEQA_DOCS_SOURCE,
            )
            return None
    if not all(p.is_file() for p in probe.values()):
        logger.warning(
            "sample task set %r skipped: %s not materialized", sample.taskset_id, root
        )
        return None
    return {s: convert(s, p) for s, p in probe.items()}


def _load_taskset_sources(sample: SampleTaskSet) -> dict[str, list[dict]] | None:
    """Tasks per split for one sample task set; None when source data is absent."""
    kind = sample.sources.get("convert")
    if kind:
        return _convert_benchmark_split(sample, kind)
    tasks_by_split: dict[str, list[dict]] = {}
    for split, rel in sample.sources.items():
        path = PROJECT_ROOT / rel
        if not path.is_file():
            logger.warning("sample task set %r skipped: source %s missing", sample.taskset_id, path)
            return None
        tasks_by_split[split] = json.loads(path.read_text(encoding="utf-8"))
    return tasks_by_split


def materialize_sample_tasksets(config: StudioConfig) -> None:
    """Rebuild the sample task sets under tasksets_dir (meta carries sample=true).

    An existing entry is replaced only when its meta says ``sample: true`` — a
    user task set that happens to occupy a sample id is never touched.
    """
    from skillopt_studio import tasksets  # local import to avoid cycle at module load

    for sample in SAMPLE_TASKSETS:
        tasks_by_split = _load_taskset_sources(sample)
        if tasks_by_split is None:
            continue
        existing = tasksets.get_taskset(config, sample.taskset_id, include_samples=True)
        if existing is not None:
            if not existing.sample:
                logger.warning(
                    "sample task set %r skipped: id occupied by a user task set",
                    sample.taskset_id,
                )
                continue
            shutil.rmtree(config.tasksets_dir / sample.taskset_id)
        try:
            tasksets.create_taskset_from_items(
                config, sample.taskset_id, sample.mode, tasks_by_split,
                display_name=sample.name, sample=True,
            )
        except ValueError:
            logger.warning("sample task set %r failed validation", sample.taskset_id, exc_info=True)


def materialize_samples(config: StudioConfig) -> None:
    """Materialize all built-in samples; no-op unless config.samples_enabled."""
    if not config.samples_enabled:
        return
    materialize_sample_skills(config)
    materialize_sample_tasksets(config)
