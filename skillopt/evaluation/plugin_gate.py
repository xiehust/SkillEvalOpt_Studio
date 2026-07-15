"""Pure failure attribution and validation gate for Plugin training."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from skillopt.evaluation.gate import GateMetric, select_gate_score

AttributionCategory = Literal[
    "routing",
    "execution",
    "handoff",
    "shared_dependency",
    "task_failure",
    "judge_failure",
]
PluginGateAction = Literal["accept_new_best", "reject"]


@dataclass(frozen=True)
class FailureAttribution:
    task_id: str
    category: AttributionCategory
    responsible_skills: tuple[str, ...]
    gradient_eligible: bool
    target_skills: tuple[str, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict:
        data = {
            "task_id": self.task_id,
            "category": self.category,
            "responsible_skills": list(self.responsible_skills),
            "gradient_eligible": self.gradient_eligible,
            "target_skills": list(self.target_skills),
        }
        if self.reason is not None:
            data["reason"] = self.reason
        return data


@dataclass(frozen=True)
class PluginGateResult:
    action: PluginGateAction
    overall_score: float
    regressions: dict[str, float]
    reasons: tuple[str, ...]


def attribute_failures(
    results: list[dict],
    trainable_skill_names: list[str] | tuple[str, ...],
) -> list[FailureAttribution]:
    """Attribute failed trajectories without inventing responsibility."""
    trainable = set(trainable_skill_names)
    attributions: list[FailureAttribution] = []
    for result in results:
        if float(result.get("hard", 0)) >= 1.0:
            continue

        target_skills = tuple(
            name
            for name in result.get("target_skills", [])
            if isinstance(name, str)
        )
        reason: str | None = None
        if result.get("error"):
            category: AttributionCategory = "task_failure"
            responsible: tuple[str, ...] = ()
            reason = str(result["error"]).strip() or "unknown rollout error"
        elif result.get("judge_error"):
            category = "judge_failure"
            responsible = ()
            reason = str(result["judge_error"]).strip() or "unknown judge error"
        else:
            targets = tuple(
                name
                for name in target_skills
                if name in trainable
            )
            task_type = str(result.get("task_type") or "default")
            if task_type == "routing":
                category = "routing"
            elif task_type == "shared_dependency":
                category = "shared_dependency"
            elif task_type == "integration" or len(result.get("target_skills", [])) > 1:
                category = "handoff"
            else:
                category = "execution"
            responsible = targets

        attributions.append(
            FailureAttribution(
                task_id=str(result.get("id", "")),
                category=category,
                responsible_skills=responsible,
                gradient_eligible=bool(responsible),
                target_skills=target_skills,
                reason=reason,
            )
        )
    return attributions


def select_responsible_skills(
    attributions: list[FailureAttribution],
    runtime_order: list[str] | tuple[str, ...],
    max_skills: int = 2,
) -> list[str]:
    if max_skills <= 0:
        raise ValueError(f"max_skills must be positive, got {max_skills}")
    counts = {name: 0 for name in runtime_order}
    for attribution in attributions:
        if not attribution.gradient_eligible:
            continue
        for name in attribution.responsible_skills:
            if name in counts:
                counts[name] += 1
    order = {name: index for index, name in enumerate(runtime_order)}
    ranked = sorted(
        (name for name, count in counts.items() if count),
        key=lambda name: (-counts[name], order[name]),
    )
    return ranked[:max_skills]


def validate_plugin_coverage(
    items: list[dict],
    skill_names: list[str],
    *,
    split_name: str = "validation",
) -> None:
    covered = {
        name
        for item in items
        for name in item.get("target_skills", [])
        if isinstance(name, str)
    }
    missing = [name for name in skill_names if name not in covered]
    if missing:
        raise ValueError(
            f"{split_name} tasks must target every trainable Plugin Skill; "
            f"missing coverage for: {missing}"
        )


def metric_scores(
    aggregates: dict,
    skill_names: list[str],
    metric: GateMetric = "hard",
    mixed_weight: float = 0.5,
) -> tuple[float, dict[str, float]]:
    overall = aggregates.get("overall") or {}
    overall_score = select_gate_score(
        float(overall.get("hard", 0.0)),
        float(overall.get("soft", 0.0)),
        metric,
        mixed_weight,
    )
    by_skill = aggregates.get("by_skill") or {}
    scores: dict[str, float] = {}
    for name in skill_names:
        values = by_skill.get(name) or {}
        if int(values.get("count", 0) or 0) <= 0:
            raise ValueError(f"Plugin aggregate has no validation coverage for {name!r}")
        scores[name] = select_gate_score(
            float(values.get("hard", 0.0)),
            float(values.get("soft", 0.0)),
            metric,
            mixed_weight,
        )
    return overall_score, scores


def evaluate_plugin_gate(
    current_aggregates: dict,
    candidate_aggregates: dict,
    skill_names: list[str],
    *,
    metric: GateMetric = "hard",
    mixed_weight: float = 0.5,
    max_skill_regression: float = 0.0,
    modified_skill_names: list[str] | tuple[str, ...] | None = None,
) -> PluginGateResult:
    if not 0.0 <= max_skill_regression <= 1.0:
        raise ValueError(
            f"max_skill_regression must be in [0, 1], got {max_skill_regression}"
        )
    current_overall, current_skills = metric_scores(
        current_aggregates, skill_names, metric, mixed_weight
    )
    candidate_overall, candidate_skills = metric_scores(
        candidate_aggregates, skill_names, metric, mixed_weight
    )
    regressions = {
        name: current_skills[name] - candidate_skills[name] for name in skill_names
    }
    reasons: list[str] = []
    if candidate_overall <= current_overall:
        reasons.append(
            f"overall score did not strictly improve: "
            f"{candidate_overall:.6f} <= {current_overall:.6f}"
        )
    for name, regression in regressions.items():
        if regression > max_skill_regression:
            reasons.append(
                f"{name} regressed by {regression:.6f} "
                f"(limit {max_skill_regression:.6f})"
            )
    if modified_skill_names is not None:
        modified = list(dict.fromkeys(modified_skill_names))
        unknown = [name for name in modified if name not in current_skills]
        if unknown:
            raise ValueError(
                f"modified Skills are not trainable Plugin Skills: {unknown}"
            )
        improved = [
            name
            for name in modified
            if candidate_skills[name] > current_skills[name]
        ]
        if not improved:
            scores = ", ".join(
                f"{name} {current_skills[name]:.6f}->{candidate_skills[name]:.6f}"
                for name in modified
            )
            reasons.append(
                "no modified Skill strictly improved its validation score: "
                f"{scores or '(none)'}"
            )
    return PluginGateResult(
        action="reject" if reasons else "accept_new_best",
        overall_score=candidate_overall,
        regressions=regressions,
        reasons=tuple(reasons),
    )
