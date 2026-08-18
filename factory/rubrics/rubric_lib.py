"""Validation and utility functions shared by the rubric factory CLIs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = {
    "id",
    "requirements",
    "weight",
    "sub_tasks",
    "task_category",
    "finegrained_task_category",
}
TASK_CATEGORIES = {"Code Development", "Code Execution", "Result Analysis"}
RUBRIC_MODES = {"regular", "code-dev"}
CODE_DEV_DERIVATION = "official-code-development-prune-v1"
FINEGRAINED_CATEGORIES = {
    "Environment & Infrastructure Setup",
    "Dataset and Model Acquisition",
    "Data Processing & Preparation",
    "Method Implementation",
    "Experimental Setup",
    "Evaluation, Metrics & Benchmarking",
    "Logging, Analysis & Presentation",
}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_rubric(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a PaperBench-only tree, recursively discarding generator sidecars."""
    children = raw.get("sub_tasks")
    if not isinstance(children, list):
        children = []
    return {
        "id": raw.get("id"),
        "requirements": raw.get("requirements"),
        "weight": raw.get("weight"),
        "sub_tasks": [normalize_rubric(child) for child in children if isinstance(child, dict)],
        "task_category": raw.get("task_category"),
        "finegrained_task_category": raw.get("finegrained_task_category"),
    }


def paperbench_code_only_rubric(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the official PaperBench Code-Dev view of a complete rubric tree.

    This mirrors ``TaskNode.code_only()`` / ``reduce_to_category``: retain only
    Code Development leaves, retain exactly the ancestors that still have a
    retained descendant, and preserve every retained node's original local
    weight.  Scoring then naturally renormalizes the remaining siblings at
    each level.  The root fallback matches PaperBench's behavior for a rubric
    with no Code Development leaf.
    """
    tree = normalize_rubric(raw)

    def prune(node: dict[str, Any]) -> dict[str, Any] | None:
        children = node["sub_tasks"]
        if not children:
            return node if node.get("task_category") == "Code Development" else None
        retained = [child for child in (prune(value) for value in children) if child]
        if not retained:
            return None
        # TaskNode.set_sub_tasks() clears task_category whenever children
        # remain.  Preserve the other fields and original local weight.
        return {**node, "sub_tasks": retained, "task_category": None}

    pruned = prune(tree)
    if pruned is not None:
        return pruned
    return {
        **tree,
        "sub_tasks": [],
        "task_category": "Code Development",
    }


def validate_rubric(raw: Any, *, rubric_mode: str = "regular") -> dict[str, Any]:
    if rubric_mode not in RUBRIC_MODES:
        raise ValueError(f"unsupported rubric mode: {rubric_mode!r}")
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    leaves: list[dict[str, Any]] = []
    node_count = 0
    max_depth = 0
    effective_weights: list[dict[str, Any]] = []

    if not isinstance(raw, dict):
        return {
            "valid": False,
            "errors": ["rubric root must be a JSON object"],
            "warnings": [],
            "stats": {},
            "effective_leaf_weights": [],
        }

    def visit(node: Any, path: str, depth: int, effective: float) -> None:
        nonlocal node_count, max_depth
        node_count += 1
        max_depth = max(max_depth, depth)
        if not isinstance(node, dict):
            errors.append(f"{path}: node must be an object")
            return
        missing = REQUIRED_FIELDS - set(node)
        extra = set(node) - REQUIRED_FIELDS
        if missing:
            errors.append(f"{path}: missing fields: {', '.join(sorted(missing))}")
        if extra:
            errors.append(f"{path}: unsupported fields: {', '.join(sorted(extra))}")

        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(f"{path}: id must be a non-empty string")
            node_id = path
        else:
            if node_id in seen:
                errors.append(f"{path}: duplicate id: {node_id}")
            seen.add(node_id)
            if not ID_RE.fullmatch(node_id):
                errors.append(f"{path}: id is not kebab-case: {node_id}")

        requirement = node.get("requirements")
        if not isinstance(requirement, str) or not requirement.strip():
            errors.append(f"{path}: requirements must be a non-empty string")
            requirement = ""

        weight = node.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            errors.append(f"{path}: weight must be a number")
        elif not math.isfinite(weight) or weight < 0:
            errors.append(f"{path}: weight must be finite and non-negative")
        elif not isinstance(weight, int) or weight not in {0, 1, 2, 3}:
            warnings.append(f"{path}: prefer small integer weights 0/1/2/3 (found {weight})")

        children = node.get("sub_tasks")
        if not isinstance(children, list):
            errors.append(f"{path}: sub_tasks must be an array")
            children = []
        is_leaf = len(children) == 0
        category = node.get("task_category")
        fine = node.get("finegrained_task_category")
        if is_leaf:
            leaves.append(node)
            if category not in TASK_CATEGORIES:
                errors.append(f"{path}: leaf has invalid task_category: {category!r}")
            if fine not in FINEGRAINED_CATEGORIES:
                errors.append(
                    f"{path}: leaf has invalid finegrained_task_category: {fine!r}"
                )
            conjunctions = len(re.findall(r"\b(?:and|plus)\b|以及|并且|同时|且", requirement, re.I))
            if conjunctions >= 2:
                warnings.append(
                    f"{path}: requirement may contain multiple independently failing conditions"
                )
            if category == "Result Analysis":
                vague_match = re.search(r"\b(match(?:es|ed)?|same as|consistent with)\b|一致|匹配", requirement, re.I)
                has_rule = bool(
                    re.search(
                        r"within|tolerance|higher|lower|improv|decreas|increas|ranking|trend|"
                        r"at least|at most|no more than|no less than|[<>]=?|≈|~|±|"
                        r"优于|低于|高于|趋势|容差|至少|至多",
                        requirement,
                        re.I,
                    )
                )
                if vague_match and not has_rule:
                    warnings.append(
                        f"{path}: Result Analysis says results match without a tolerance or trend rule"
                    )
            effective_weights.append(
                {"id": node_id, "task_category": category, "effective_weight": effective}
            )
            return

        if category is not None:
            errors.append(f"{path}: internal node task_category must be null")
        if fine is not None:
            errors.append(f"{path}: internal node finegrained_task_category must be null")
        if len(children) == 1:
            warnings.append(f"{path}: internal node has only one child")
        numeric_weights = [
            child.get("weight")
            for child in children
            if isinstance(child, dict)
            and isinstance(child.get("weight"), (int, float))
            and not isinstance(child.get("weight"), bool)
            and math.isfinite(child.get("weight"))
            and child.get("weight") >= 0
        ]
        denominator = sum(numeric_weights)
        if denominator <= 0:
            errors.append(f"{path}: all direct children have zero or invalid weight")
        for index, child in enumerate(children):
            child_weight = child.get("weight", 0) if isinstance(child, dict) else 0
            fraction = child_weight / denominator if denominator > 0 and isinstance(child_weight, (int, float)) else 0
            visit(child, f"{path}.sub_tasks[{index}]", depth + 1, effective * fraction)

    visit(raw, "root", 0, 1.0)
    categories = {category: 0 for category in sorted(TASK_CATEGORIES)}
    for leaf in leaves:
        if leaf.get("task_category") in categories:
            categories[leaf["task_category"]] += 1
    for category, count in categories.items():
        if count == 0:
            warnings.append(f"rubric contains no {category} leaves")
    if rubric_mode == "code-dev":
        non_code_leaves = [
            leaf.get("id")
            for leaf in leaves
            if leaf.get("task_category") != "Code Development"
        ]
        if non_code_leaves:
            errors.append(
                "code-dev rubric contains non-Code Development leaves: "
                + ", ".join(str(node_id) for node_id in non_code_leaves[:20])
            )
    if max_depth > 9:
        warnings.append(f"tree depth {max_depth} exceeds the usual PaperBench range (up to 9)")
    if raw.get("weight") != 1:
        warnings.append("root weight should normally be 1")
    effective_weights.sort(key=lambda item: item["effective_weight"], reverse=True)
    if effective_weights:
        total = sum(item["effective_weight"] for item in effective_weights)
        if not math.isclose(total, 1.0, rel_tol=1e-8, abs_tol=1e-8):
            errors.append(f"effective leaf weights sum to {total:.12f}, expected 1")
        tiny = [item["id"] for item in effective_weights if 0 < item["effective_weight"] < 0.001]
        if tiny:
            warnings.append(
                f"{len(tiny)} leaves have effective weight below 0.1%; inspect over-deep branches"
            )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "rubric_mode": rubric_mode,
            "nodes": node_count,
            "leaves": len(leaves),
            "max_depth": max_depth,
            "leaf_categories": categories,
        },
        "effective_leaf_weights": effective_weights,
    }


def validate_addendum(text: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not text.strip():
        errors.append("addendum is empty")
    required_topics = {
        "scope": ("scope", "范围"),
        "approved adaptations": ("approved adaptation", "允许的调整", "批准的调整"),
        "required comparisons and evidence": (
            "required comparison",
            "required evidence",
            "必要比较",
            "所需证据",
        ),
        "clarifications": ("clarification", "澄清"),
        "out of scope": ("out of scope", "范围外", "不在范围"),
    }
    lowered = text.lower()
    for label, alternatives in required_topics.items():
        if not any(value in lowered for value in alternatives):
            warnings.append(f"addendum has no recognizable '{label}' section")
    if re.search(r"\b(?:todo|tbd|fixme)\b|待定|待补充|未知", text, re.I):
        errors.append("addendum contains unresolved TODO/TBD placeholders")
    if len(text) < 300:
        warnings.append("addendum is unusually short")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def validate_package(paper_dir: Path, *, rubric_mode: str = "regular") -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    for name in ("config.yaml", "paper.pdf", "paper.md", "blacklist.txt", "rubric.json", "addendum.md"):
        if not (paper_dir / name).is_file():
            errors.append(f"missing required file: {name}")
    rubric_report: dict[str, Any] = {}
    addendum_report: dict[str, Any] = {}
    if (paper_dir / "rubric.json").is_file():
        try:
            rubric_report = validate_rubric(
                load_json(paper_dir / "rubric.json"), rubric_mode=rubric_mode
            )
            errors.extend(f"rubric: {value}" for value in rubric_report["errors"])
            warnings.extend(f"rubric: {value}" for value in rubric_report["warnings"])
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"cannot load rubric.json: {exc}")
    if (paper_dir / "addendum.md").is_file():
        addendum_report = validate_addendum(
            (paper_dir / "addendum.md").read_text(encoding="utf-8")
        )
        errors.extend(f"addendum: {value}" for value in addendum_report["errors"])
        warnings.extend(f"addendum: {value}" for value in addendum_report["warnings"])
    return {
        "valid": not errors,
        "paper_dir": str(paper_dir),
        "errors": errors,
        "warnings": warnings,
        "rubric": rubric_report,
        "addendum": addendum_report,
    }
