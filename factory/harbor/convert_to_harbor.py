#!/usr/bin/env python3
"""Convert authored PaperBench packages into the processed Harbor task format.

The output contract mirrors the reference batch supplied for this factory:
``manifest.jsonl`` plus ``harbor_task/<task-id>/`` directories containing
task.toml, instruction.md, resource_metadata.json, environment, and tests.
PaperBench has no factory-authored oracle solution, so processed tasks omit the
optional Harbor ``solution/`` directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 rjob images
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # Keep source-tree tests usable before dependency install.
        from pip._vendor import tomli as tomllib


RUBRICS_FACTORY_DIR = Path(__file__).resolve().parents[1] / "rubrics"
sys.path.insert(0, str(RUBRICS_FACTORY_DIR))
from rubric_lib import (  # noqa: E402
    CODE_DEV_DERIVATION,
    paperbench_code_only_rubric,
    validate_addendum,
    validate_rubric,
)


HARBOR_TEMPLATE_TASK = Path(__file__).resolve().with_name("templates") / "processed_task"
OFFICIAL_PAPERBENCH_INSTRUCTIONS = (
    Path(__file__).resolve().with_name("templates") / "instructions.official.txt"
)
OFFICIAL_PAPERBENCH_CODE_DEV_INSTRUCTIONS = (
    Path(__file__).resolve().with_name("templates") / "instructions.code-dev.official.txt"
)
OFFICIAL_PAPERBENCH_INSTRUCTIONS_SHA256 = (
    "712ed3968de5b8d98b96e25e7d33c95552c460649201743d8535e84c344bac56"
)
OFFICIAL_PAPERBENCH_CODE_DEV_INSTRUCTIONS_SHA256 = (
    "65a75977810a1bca53e69767740c07f5c71c6d632838ebd32ba22d69e2a49d9e"
)
HARBOR_WORKDIR = "/workspace"
HARBOR_PAPER_DIR = "/workspace/paper"
HARBOR_SUBMISSION_DIR = "/workspace/submission"
HARBOR_AGENT_TRAJECTORY = "/logs/agent/trajectory.json"
HARBOR_GPU_TYPE = "H200"
HARBOR_RESOURCE_METADATA_VERSION = "harbor_resource_metadata_v3"
AGENT_TIME_LIMIT_HOURS = 12
AGENT_RUNTIME_NOTE = (
    f"You are expected to work until you have spent {AGENT_TIME_LIMIT_HOURS} hours on this task. "
    "You may iteratively develop your solution within these constraints, but bear in mind that "
    "program runtime also counts towards the time limit. This is a difficult task and we expect "
    "a good solution to require using the full time limit."
)
OFFICIAL_ADDITIONAL_NOTES = f"""

ADDITIONAL NOTES
------
- **Compute**: You have access to a NVIDIA {HARBOR_GPU_TYPE} GPU with the appropriate drivers installed.
- **Total Runtime**: {AGENT_RUNTIME_NOTE}
- **API keys**: You have been granted API keys for services necessary for replicating your paper. The API keys are available to you at `{HARBOR_WORKDIR}/agent.env`. You can assume that these same API keys will already be set as environment variables in the reproduction environment when we evaluate your submission, so you do not need to set or export them in your submission.
- You have root access in your environment.
- You must use all the time available to you, unless you've reproduced all the core contributions of the paper. Do not stop until you've reproduced them.
- Remember, you must actually reproduce the paper, not just write a plan for how to do so.
"""
VERIFIER_OVERHEAD_SEC = 600
PREFERRED_KINDS = ["json", "image", "pdf", "text", "code", "shell", "archive", "binary"]
MARKDOWN_ASSET_RE = re.compile(
    r"!\[[^\]]*\]\(\s*<?(?P<path>(?:\./)?assets/[^)>\s]+)>?"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
HTML_ASSET_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*([\"'])"
    r"(?P<path>(?:\./)?assets/[^\"']+)\1[^>]*>",
    re.IGNORECASE,
)


def incremental_marker_path(output_parent: Path, batch_id: str) -> Path:
    return output_parent / f".{batch_id}.incremental"


def finalizing_marker_path(output_parent: Path, batch_id: str) -> Path:
    return output_parent / f".{batch_id}.finalizing"
REQUIRED_TASK_FILES = {
    "task.toml",
    "instruction.md",
    "resource_metadata.json",
    "tests/test.sh",
    "tests/llm_rubric_judge.py",
    "tests/judge_config.json",
    "tests/rubric.json",
    "tests/judge.addendum.md",
    "tests/paper/paper.pdf",
    "tests/paper/paper.md",
    "tests/paper/addendum.md",
    "tests/paper/blacklist.txt",
    "environment/paper/paper.pdf",
    "environment/paper/paper.md",
    "environment/paper/addendum.md",
    "environment/paper/blacklist.txt",
}
MANIFEST_KEYS = {
    "artifact_paths",
    "benchmark",
    "competition_id",
    "metric",
    "paper_id",
    "promoted_public_data_paths",
    "removed_paths",
    "source_format",
    "source_seed_task_id",
    "source_task_dir",
    "source_task_key",
    "source_task_name",
    "task_id",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_array(values: Iterable[str]) -> str:
    return "[" + ", ".join(json_string(value) for value in values) + "]"


def load_paper_list(path: Path) -> list[dict[str, Any]]:
    raw = read_json(path)
    papers = raw if isinstance(raw, list) else raw.get("papers") if isinstance(raw, dict) else None
    if not isinstance(papers, list) or not all(isinstance(item, dict) for item in papers):
        raise ValueError("paper list must be an array or an object containing a papers array")
    return papers


def select_papers(papers: list[dict[str, Any]], requested: list[str] | None) -> list[dict[str, Any]]:
    ids = [paper.get("id") for paper in papers]
    if not all(isinstance(paper_id, str) and paper_id for paper_id in ids):
        raise ValueError("every paper-list entry must have a non-empty string id")
    if len(ids) != len(set(ids)):
        raise ValueError("paper-list IDs are not unique")
    selected = set(requested or ids)
    unknown = sorted(selected - set(ids))
    if unknown:
        raise ValueError(f"unknown paper ids: {', '.join(unknown)}")
    result = [paper for paper in papers if paper["id"] in selected]
    for paper in result:
        selected_asset_files(paper)
    return result


def selected_asset_files(paper: dict[str, Any]) -> list[str]:
    """Read the task-author-curated list of necessary PaperBench assets."""
    raw = paper.get("asset_files")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{paper.get('id')}: asset_files must be a list")
    selected: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{paper.get('id')}: asset_files entries must be non-empty strings")
        normalized = value.strip().replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith("assets/"):
            normalized = normalized[len("assets/") :]
        path = PurePosixPath(normalized)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"{paper.get('id')}: unsafe asset_files path {value!r}")
        selected.append(path.as_posix())
    if len(selected) != len(set(selected)):
        raise ValueError(f"{paper.get('id')}: asset_files contains duplicate paths")
    return selected


def local_asset_path(reference: str) -> str:
    normalized = reference.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized[len("assets/") :] if normalized.startswith("assets/") else normalized


def render_curated_paper_markdown(source: Path, selected: list[str]) -> tuple[str, list[str]]:
    text = source.read_text(encoding="utf-8", errors="replace")
    selected_set = set(selected)
    omitted: list[str] = []

    def replace(match: re.Match[str]) -> str:
        relative = local_asset_path(match.group("path"))
        if relative in selected_set:
            return match.group(0)
        omitted.append(relative)
        return ""

    text = MARKDOWN_ASSET_RE.sub(replace, text)
    text = HTML_ASSET_RE.sub(replace, text)
    return text, list(dict.fromkeys(omitted))


def markdown_asset_references(text: str) -> set[str]:
    matches = [*MARKDOWN_ASSET_RE.finditer(text), *HTML_ASSET_RE.finditer(text)]
    return {local_asset_path(match.group("path")) for match in matches}


def markdown_asset_reference_list(text: str) -> list[str]:
    matches = sorted(
        [*MARKDOWN_ASSET_RE.finditer(text), *HTML_ASSET_RE.finditer(text)],
        key=lambda match: match.start(),
    )
    return list(dict.fromkeys(local_asset_path(match.group("path")) for match in matches))


def resolved_asset_files(
    paper: dict[str, Any], source: Path
) -> tuple[list[str], str]:
    if "asset_files" in paper:
        return selected_asset_files(paper), "explicit-necessary-assets-v1"
    paper_md = source / "paper.md"
    if not paper_md.is_file():
        raise FileNotFoundError(f"missing PaperBench input: {paper_md}")
    references = markdown_asset_reference_list(
        paper_md.read_text(encoding="utf-8", errors="replace")
    )
    for reference in references:
        path = PurePosixPath(reference)
        if path.is_absolute() or not path.parts or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise ValueError(f"{paper.get('id')}: unsafe paper.md asset path {reference!r}")
        if not (source / "assets" / Path(*path.parts)).is_file():
            raise FileNotFoundError(
                f"{paper.get('id')}: paper.md references missing asset {reference}"
            )
    return references, "paper-markdown-referenced-assets-v1"


def default_template_task() -> Path:
    if not HARBOR_TEMPLATE_TASK.is_dir():
        raise FileNotFoundError(
            f"the bundled Harbor template is unavailable: {HARBOR_TEMPLATE_TASK}"
        )
    return HARBOR_TEMPLATE_TASK


def render_harbor_instructions(path: Path, *, rubric_mode: str) -> bytes:
    raw = path.read_bytes()
    actual_hash = sha256_bytes(raw)
    expected_hash = (
        OFFICIAL_PAPERBENCH_CODE_DEV_INSTRUCTIONS_SHA256
        if rubric_mode == "code-dev"
        else OFFICIAL_PAPERBENCH_INSTRUCTIONS_SHA256
    )
    if actual_hash != expected_hash:
        raise ValueError(
            "instructions file is not the pinned official PaperBench instructions: "
            f"expected {expected_hash}, found {actual_hash}"
        )
    text = raw.decode("utf-8")
    text = text.replace("/home/paper", HARBOR_PAPER_DIR)
    text = text.replace("/home/submission", HARBOR_SUBMISSION_DIR)
    if rubric_mode == "code-dev":
        if "The code will not be executed during grading." not in text:
            raise ValueError("official code-dev instructions lost the no-execution contract")
        if "for a maximum runtime of 7 days" in text or "reproduce.sh" in text:
            raise ValueError("official code-dev instructions unexpectedly require reproduction")
    else:
        old_gpu = "NVIDIA A10 GPU"
        if text.count(old_gpu) != 1:
            raise ValueError("official instructions no longer contain exactly one A10 GPU clause")
        text = text.replace(old_gpu, f"NVIDIA {HARBOR_GPU_TYPE} GPU")
        if "NVIDIA A10 GPU" in text:
            raise ValueError(
                f"failed to adapt PaperBench instructions to the {HARBOR_GPU_TYPE} runtime"
            )
    text = text.rstrip() + "\n" + OFFICIAL_ADDITIONAL_NOTES
    if f"NVIDIA {HARBOR_GPU_TYPE} GPU" not in text or AGENT_RUNTIME_NOTE not in text:
        raise ValueError("failed to append the official runtime notes")
    return text.encode("utf-8")


def validate_template(template: Path) -> None:
    for relative in (
        "tests/test.sh",
        "tests/llm_rubric_judge.py",
        "tests/judge_config.json",
    ):
        if not (template / relative).is_file():
            raise FileNotFoundError(f"Harbor template is missing {relative}: {template}")


def rubric_leaf_count(node: Any) -> int:
    if not isinstance(node, dict):
        return 0
    children = node.get("sub_tasks")
    if not isinstance(children, list) or not children:
        return 1
    return sum(rubric_leaf_count(child) for child in children)


def explicitly_blocking_questions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, dict) and item.get("blocking") is True
    ]


def select_authored_bundle(
    root: Path,
    paper_id: str,
    *,
    rubric_mode: str,
    require_approved: bool,
) -> tuple[Path, Path, str, str]:
    paper_dir = root / "paper_sources" / paper_id
    authoring_dir = root / "design" / paper_id / "rubric_authoring"
    published = (paper_dir / "rubric.json", paper_dir / "addendum.md")
    draft = (authoring_dir / "rubric.draft.json", authoring_dir / "addendum.draft.md")
    approval_path = authoring_dir / "human_approval.json"
    provenance_path = authoring_dir / "authoring_provenance.json"
    approval = read_json(approval_path) if approval_path.is_file() else {}
    provenance = read_json(provenance_path) if provenance_path.is_file() else {}
    published_mode = approval.get("rubric_mode", "regular") if isinstance(approval, dict) else "regular"
    draft_mode = provenance.get("rubric_mode", "regular") if isinstance(provenance, dict) else "regular"

    acceptable_modes = {rubric_mode}
    if rubric_mode == "code-dev":
        # Official PaperBench derives Code-Dev by pruning the complete regular
        # rubric, so a reviewed regular bundle is a valid (and preferred)
        # source for a code-dev Harbor view.
        acceptable_modes.add("regular")
    if all(path.is_file() for path in published) and published_mode in acceptable_modes:
        return published[0], published[1], "published", published_mode
    if require_approved:
        raise FileNotFoundError(
            f"{paper_id}: approved rubric/addendum source for {rubric_mode!r} is required; "
            f"acceptable source modes={sorted(acceptable_modes)}"
        )
    if (
        all(path.is_file() for path in draft)
        and provenance_path.is_file()
        and draft_mode in acceptable_modes
    ):
        review_path = authoring_dir / "quality_review.json"
        unresolved_path = authoring_dir / "unresolved_questions.json"
        if not review_path.is_file() or not unresolved_path.is_file():
            raise FileNotFoundError(
                f"{paper_id}: authoring draft is missing its semantic review artifacts"
            )
        review = read_json(review_path)
        unresolved = read_json(unresolved_path)
        blockers = review.get("blocking_issues") if isinstance(review, dict) else None
        review_unresolved = (
            review.get("unresolved_questions") if isinstance(review, dict) else None
        )
        if not isinstance(blockers, list):
            raise FileNotFoundError(
                f"{paper_id}: authoring draft has a malformed semantic review"
            )
        if not isinstance(review_unresolved, list):
            raise FileNotFoundError(
                f"{paper_id}: authoring draft has a malformed semantic review"
            )
        if not isinstance(unresolved, list):
            raise FileNotFoundError(
                f"{paper_id}: authoring draft has malformed unresolved questions"
            )
        return draft[0], draft[1], "authoring-draft", draft_mode
    raise FileNotFoundError(
        f"{paper_id}: no complete rubric/addendum pair for mode {rubric_mode!r}; "
        f"published mode={published_mode!r}, draft mode={draft_mode!r}"
    )


def resource_profile(metadata: dict[str, Any]) -> dict[str, Any]:
    compute = metadata.get("compute") if isinstance(metadata.get("compute"), dict) else {}
    accelerator = str(compute.get("accelerator", "")).lower()
    notes = str(compute.get("notes", "")).lower()
    cpu_only = "cpu" in accelerator and not any(token in accelerator for token in ("gpu", "h200", "a100", "h100"))
    if cpu_only:
        paper_profile = "cpu_sufficient"
        # PaperBench's official instruction promises an available GPU even when
        # the selected reproduction itself can run on CPU.
        rollout_profile = "gpu_capable"
        rollout_gpus = 1
        vram = {"min": 1.0, "typical": 2.0, "max": 6.0}
    elif any(token in accelerator + " " + notes for token in ("gpu", "h200", "a100", "h100")):
        paper_profile = "gpu_relevant"
        rollout_profile = "gpu_capable"
        rollout_gpus = 1
        vram = {"min": 2.0, "typical": 8.0, "max": 20.0}
    else:
        paper_profile = "gpu_required_likely"
        rollout_profile = "gpu_capable"
        rollout_gpus = 1
        vram = {"min": 2.0, "typical": 8.0, "max": 20.0}
    return {
        "paper_resource_profile": paper_profile,
        "rollout_resource_profile": rollout_profile,
        "rollout_gpus": rollout_gpus,
        "vram": vram,
    }


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".py":
        return "code"
    if suffix == ".sh":
        return "shell"
    if suffix in {".md", ".txt", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".rst", ".log", ".html", ".tex"}:
        return "text"
    if suffix in {".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz"}:
        return "archive"
    return "binary"


def file_scope(relative: Path) -> str:
    if relative.parts and relative.parts[0] == "environment":
        return "visible"
    if relative.parts and relative.parts[0] == "tests":
        return "hidden_or_solution"
    return "package"


def build_data_profile(task_dir: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for top in ("environment", "tests"):
        base = task_dir / top
        if not base.exists():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            relative = path.relative_to(task_dir)
            rows.append(
                {
                    "kind": file_kind(path),
                    "scope": file_scope(relative),
                    "bytes": path.stat().st_size,
                }
            )
    kinds = [kind for kind in PREFERRED_KINDS if any(row["kind"] == kind for row in rows)]
    kind_counts = {kind: sum(row["kind"] == kind for row in rows) for kind in kinds}
    kind_bytes = {
        kind: sum(row["bytes"] for row in rows if row["kind"] == kind) for kind in kinds
    }
    scopes = ["visible", "hidden_or_solution", "package"]
    largest = sorted(rows, key=lambda row: row["bytes"], reverse=True)[:10]
    return {
        "total_bytes": sum(row["bytes"] for row in rows),
        "file_count": len(rows),
        "data_kinds": kinds,
        "kind_counts": kind_counts,
        "kind_bytes": kind_bytes,
        "scope_bytes": {
            scope: sum(row["bytes"] for row in rows if row["scope"] == scope)
            for scope in scopes
        },
        "scope_file_counts": {
            scope: sum(row["scope"] == scope for row in rows) for scope in scopes
        },
        "table_shapes": [],
        "largest_files": largest,
    }


def make_resource_metadata(
    *, metadata: dict[str, Any], data_profile: dict[str, Any]
) -> dict[str, Any]:
    profile = resource_profile(metadata)
    estimate = {
        "gpu_tier": HARBOR_GPU_TYPE,
        "gpu_required": True,
        "gpu_count": 1,
        "estimated_vram_gb": profile["vram"],
        "training_time_tier": "under_12h",
        "confidence": 0.75,
        "reason": (
            "Deterministic estimate from the selected paper's authoring metadata and declared "
            f"compute envelope; classified as {profile['paper_resource_profile']}. The verifier "
            f"uses one {HARBOR_GPU_TYPE} GPU, matching the task runtime contract."
        ),
        "attempts": 1,
    }
    return {
        "schema_version": HARBOR_RESOURCE_METADATA_VERSION,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_profile": data_profile,
        "resource_estimate": estimate,
        "estimator": {
            "operator": "paperbench_factory_metadata_estimator",
            "model": "deterministic-rules-v1",
            "prompt_version": "paperbench_harbor_resource_rules_v1",
            "validation_history": [[]],
            "declared_max_gpus": 1,
            "declared_gpu_tier": HARBOR_GPU_TYPE,
        },
    }


def make_task_toml(
    *,
    task_id: str,
    paper_id: str,
    title: str,
    leaf_count: int,
    metadata: dict[str, Any],
    resource_metadata: dict[str, Any],
    pipeline_commit: str,
    judge_model: str,
    agent_timeout_sec: int,
    verifier_timeout_sec: int,
    reproduction_timeout_sec: int,
    judge_request_timeout_sec: int,
    judge_max_workers: int,
    judge_context_window_tokens: int,
    docker_image: str,
    rubric_mode: str,
) -> str:
    profile = resource_profile(metadata)
    data = resource_metadata["data_profile"]
    estimate = resource_metadata["resource_estimate"]
    keywords = sorted({paper_id, "paper-reproduction", "paperbench", "research"})
    code_only = rubric_mode == "code-dev"
    description = f"Reproduce core methods and experiments from {title}"
    paperbench_mode = "llm_code_dev" if code_only else "llm_full"
    reproduction_profile = "not_executed" if code_only else "gpu_capable"
    code_dev_metadata = (
        f"code_dev_derivation = {json_string(CODE_DEV_DERIVATION)}\n"
        if code_only
        else ""
    )
    return f'''schema_version = "1.4"
artifacts = [{json_string(HARBOR_SUBMISSION_DIR)}, {json_string(HARBOR_AGENT_TRAJECTORY)}]

[task]
name = {json_string(f"mlcoding/{task_id}")}
description = {json_string(description)}
keywords = {toml_array(keywords)}

[metadata]
benchmark = "paperbench"
source_format = "paperbench_official_style_harbor_adapted"
source_native_contract = "paperbench_authored_task_v1"
construction_format = "native_rollout_task_v1"
paper_id = {json_string(paper_id)}
native_task_id = {json_string(paper_id)}
rubric_leaf_count = {leaf_count}
oracle_available = false
reference_solution_available = false
scoring_method = "llm_rubric_judge"
paperbench_mode = {json_string(paperbench_mode)}
code_only = {str(code_only).lower()}
rubric_mode = {json_string(rubric_mode)}
{code_dev_metadata}paper_resource_profile = {json_string(profile["paper_resource_profile"])}
rollout_resource_profile = {json_string(profile["rollout_resource_profile"])}
reproduction_resource_profile = {json_string(reproduction_profile)}
rollout_profile = "research_long_horizon_v1"
score_ladder_version = "task_internal_score_ladder_v1"
artifact_stop_first_valid = false
pipeline_commit = {json_string(pipeline_commit)}
source_task_key = {json_string(paper_id)}
resource_metadata_version = {json_string(HARBOR_RESOURCE_METADATA_VERSION)}
resource_metadata_file = "resource_metadata.json"
data_total_bytes = {data["total_bytes"]}
data_file_count = {data["file_count"]}
data_kinds = {toml_array(data["data_kinds"])}
gpu_tier = {json_string(estimate["gpu_tier"])}
gpu_required = true
gpu_count = 1
estimated_training_time_tier = {json_string(estimate["training_time_tier"])}
estimated_vram_gb_typical = {estimate["estimated_vram_gb"]["typical"]:g}
resource_estimate_confidence = {estimate["confidence"]:g}
resource_estimate_model = "deterministic-rules-v1"
resource_estimate_prompt_version = "paperbench_harbor_resource_rules_v1"

[agent]
timeout_sec = {agent_timeout_sec}

[verifier]
timeout_sec = {verifier_timeout_sec}
environment_mode = "separate"

[verifier.env]
PAPERBENCH_JUDGE_MODEL = {json_string(judge_model)}
PAPERBENCH_JUDGE_TIMEOUT_SEC = {json_string(str(judge_request_timeout_sec))}
PAPERBENCH_JUDGE_MAX_WORKERS = {json_string(str(judge_max_workers))}
PAPERBENCH_JUDGE_CONTEXT_WINDOW_TOKENS = {json_string(str(judge_context_window_tokens))}
PAPERBENCH_REPRODUCTION_TIMEOUT_SEC = {json_string(str(reproduction_timeout_sec))}

[verifier.environment]
build_timeout_sec = 900
network_mode = "public"
os = "linux"
cpus = 4
memory_mb = 16384
storage_mb = 51200
gpus = 1
gpu_types = [{json_string(HARBOR_GPU_TYPE)}]
docker_image = {json_string(docker_image)}
workdir = "/tests"

[environment]
build_timeout_sec = 900
network_mode = "public"
os = "linux"
cpus = 8
memory_mb = 32768
storage_mb = 51200
gpus = {profile["rollout_gpus"]}
gpu_types = [{json_string(HARBOR_GPU_TYPE)}]
docker_image = {json_string(docker_image)}
workdir = {json_string(HARBOR_WORKDIR)}
'''


def copy_paper_environment(
    source: Path,
    destination: Path,
    addendum: Path,
    *,
    include_assets: bool = True,
    asset_files: list[str] | None = None,
) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("paper.pdf", "blacklist.txt"):
        path = source / name
        if not path.is_file():
            raise FileNotFoundError(f"missing PaperBench input: {path}")
        shutil.copy2(path, destination / name)
    paper_md = source / "paper.md"
    if not paper_md.is_file():
        raise FileNotFoundError(f"missing PaperBench input: {paper_md}")
    selected = asset_files or []
    rendered_markdown, omitted = render_curated_paper_markdown(paper_md, selected)
    (destination / "paper.md").write_text(rendered_markdown, encoding="utf-8")
    shutil.copy2(addendum, destination / "addendum.md")
    if include_assets:
        assets = source / "assets"
        output_assets = destination / "assets"
        output_assets.mkdir()
        for relative_name in selected:
            relative = Path(*PurePosixPath(relative_name).parts)
            asset = assets / relative
            if not asset.is_file():
                raise FileNotFoundError(f"selected PaperBench asset does not exist: {asset}")
            target = output_assets / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asset, target)
    return omitted


def normalize_task_permissions(task_dir: Path) -> None:
    """Make generated snapshots readable and executable without world-writable files."""
    for path in [task_dir, *task_dir.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)
    for relative in (
        "tests/test.sh",
        "tests/llm_rubric_judge.py",
    ):
        path = task_dir / relative
        if path.is_file():
            path.chmod(0o755)


def pipeline_fingerprint(template: Path, instructions_file: Path) -> str:
    digest = hashlib.sha256()
    paths = [
        ("convert_to_harbor.py", Path(__file__).resolve()),
        ("rubrics/rubric_lib.py", RUBRICS_FACTORY_DIR / "rubric_lib.py"),
        (f"instructions/{instructions_file.name}", instructions_file),
    ]
    paths.extend(
        (f"template/{relative}", template / relative)
        for relative in (
            "tests/test.sh",
            "tests/llm_rubric_judge.py",
            "tests/judge_config.json",
        )
    )
    for label, path in paths:
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:40]


def build_one(
    *,
    root: Path,
    output_task_dir: Path,
    paper: dict[str, Any],
    task_id: str,
    template: Path,
    require_approved: bool,
    pipeline_commit: str,
    judge_model: str,
    agent_timeout_sec: int,
    verifier_timeout_sec: int,
    reproduction_timeout_sec: int,
    judge_request_timeout_sec: int,
    judge_max_workers: int,
    judge_context_window_tokens: int,
    docker_image: str,
    instructions_content: bytes,
    rubric_mode: str,
) -> dict[str, Any]:
    paper_id = paper["id"]
    title = paper.get("title")
    if not isinstance(title, str) or not title:
        raise ValueError(f"{paper_id}: title is required")
    metadata_path = root / "design" / paper_id / "task_metadata.json"
    metadata = read_json(metadata_path) if metadata_path.is_file() else paper
    rubric_path, addendum_path, authored_status, source_rubric_mode = select_authored_bundle(
        root,
        paper_id,
        rubric_mode=rubric_mode,
        require_approved=require_approved,
    )
    source_record_path = (
        root / "design" / paper_id / "rubric_authoring" / "human_approval.json"
        if authored_status == "published"
        else root / "design" / paper_id / "rubric_authoring" / "authoring_provenance.json"
    )
    source_record = read_json(source_record_path) if source_record_path.is_file() else {}
    if (
        rubric_mode == "code-dev"
        and source_rubric_mode == "code-dev"
        and source_record.get("code_dev_derivation") != CODE_DEV_DERIVATION
    ):
        raise ValueError(
            f"{paper_id}: code-dev source was not derived with {CODE_DEV_DERIVATION}; "
            "regenerate it or supply a complete regular rubric"
        )
    source_rubric = read_json(rubric_path)
    rubric = (
        paperbench_code_only_rubric(source_rubric)
        if rubric_mode == "code-dev"
        else source_rubric
    )
    rubric_report = validate_rubric(rubric, rubric_mode=rubric_mode)
    if not rubric_report["valid"]:
        raise ValueError(
            f"{paper_id}: invalid rubric:\n- " + "\n- ".join(rubric_report["errors"])
        )
    addendum_report = validate_addendum(addendum_path.read_text(encoding="utf-8"))
    if not addendum_report["valid"]:
        raise ValueError(
            f"{paper_id}: invalid addendum:\n- " + "\n- ".join(addendum_report["errors"])
        )
    if require_approved:
        approval_path = (
            root / "design" / paper_id / "rubric_authoring" / "human_approval.json"
        )
        if not approval_path.is_file():
            raise FileNotFoundError(f"{paper_id}: missing human approval: {approval_path}")
        approval = read_json(approval_path)
        if not isinstance(approval, dict):
            raise ValueError(f"{paper_id}: human approval must be a JSON object")
        expected_hashes = {
            "rubric_sha256": sha256_file(rubric_path),
            "addendum_sha256": sha256_file(addendum_path),
        }
        for key, expected in expected_hashes.items():
            if approval.get(key) != expected:
                raise ValueError(f"{paper_id}: approved hash does not match {key}")
        approved_judge_addendum = root / "paper_sources" / paper_id / "judge.addendum.md"
        actual_judge_addendum_hash = (
            sha256_file(approved_judge_addendum)
            if approved_judge_addendum.is_file()
            else None
        )
        if approval.get("judge_addendum_sha256") != actual_judge_addendum_hash:
            raise ValueError(
                f"{paper_id}: approved hash does not match judge_addendum_sha256"
            )
    leaf_count = rubric_leaf_count(rubric)
    if leaf_count <= 0:
        raise ValueError(f"{paper_id}: rubric has no leaves")
    judge_waves = (leaf_count + judge_max_workers - 1) // judge_max_workers
    # Official SimpleJudge performs file ranking, leaf grading, and score
    # parsing as separate model calls for each leaf.
    required_timeout = judge_waves * judge_request_timeout_sec * 3 + VERIFIER_OVERHEAD_SEC
    if rubric_mode != "code-dev":
        required_timeout += reproduction_timeout_sec
    if required_timeout > verifier_timeout_sec:
        raise ValueError(
            f"{paper_id}: --verifier-timeout-sec={verifier_timeout_sec} is too small for {leaf_count} leaves "
            f"at --judge-max-workers={judge_max_workers}; need at least {required_timeout} seconds"
        )

    tests_dir = output_task_dir / "tests"
    paper_output = output_task_dir / "environment" / "paper"
    tests_dir.mkdir(parents=True, exist_ok=True)
    paper_source = root / "paper_sources" / paper_id
    paper_assets, paper_asset_policy = resolved_asset_files(paper, paper_source)
    omitted_assets = copy_paper_environment(
        paper_source,
        paper_output,
        addendum_path,
        asset_files=paper_assets,
    )
    # A separate verifier must grade against immutable task-authored text, not
    # an agent-writable /workspace/paper transferred back as an artifact. Assets are
    # agent inputs only and are not consumed by the official judge, so copying
    # them into the verifier snapshot would duplicate every paper image.
    copy_paper_environment(
        paper_source,
        tests_dir / "paper",
        addendum_path,
        include_assets=False,
        asset_files=paper_assets,
    )

    shutil.copy2(template / "tests" / "test.sh", tests_dir / "test.sh")
    shutil.copy2(
        template / "tests" / "llm_rubric_judge.py",
        tests_dir / "llm_rubric_judge.py",
    )
    # In Code-Dev this is the deterministic official code-only view, not an
    # unmodified copy of the complete source rubric.
    write_json(tests_dir / "rubric.json", rubric)
    judge_addendum = (
        root / "paper_sources" / paper_id / "judge.addendum.md"
        if authored_status == "published"
        else root / "design" / paper_id / "rubric_authoring" / "judge.addendum.draft.md"
    )
    if not judge_addendum.is_file():
        judge_addendum = None
    if judge_addendum:
        shutil.copy2(judge_addendum, tests_dir / "judge.addendum.md")
    else:
        # Official PaperBench treats the judge addendum as optional task data.
        # Harbor needs the path to exist, so the transport adaptation is an
        # empty file rather than factory-authored grading policy.
        (tests_dir / "judge.addendum.md").write_text("", encoding="utf-8")
    write_json(
        tests_dir / "judge_config.json",
        {
            "judge_mode": "llm_code_dev" if rubric_mode == "code-dev" else "llm_full",
            "code_only": rubric_mode == "code-dev",
            "rubric_mode": rubric_mode,
            "code_dev_derivation": (
                CODE_DEV_DERIVATION if rubric_mode == "code-dev" else None
            ),
            "paper_id": paper_id,
            "source_paperbench_id": str(metadata.get("source_paperbench_id") or paper_id),
            # Official PaperBench special-cases the large `pinn` rubric in
            # create_judge.py by retaining only the final five prior nodes.
            "max_prior_nodes": (
                5 if str(metadata.get("source_paperbench_id") or paper_id) == "pinn" else None
            ),
            "title": title,
            "judge_model_env": "PAPERBENCH_JUDGE_MODEL",
            "request_mode": "per_leaf",
            "max_workers": judge_max_workers,
            "context_window_tokens": judge_context_window_tokens,
            "paper_asset_policy": paper_asset_policy,
            "paper_asset_files": paper_assets,
            "omitted_uncurated_asset_references": omitted_assets,
        },
    )

    (output_task_dir / "instruction.md").write_bytes(instructions_content)

    data_profile = build_data_profile(output_task_dir)
    resource_metadata = make_resource_metadata(metadata=metadata, data_profile=data_profile)
    write_json(output_task_dir / "resource_metadata.json", resource_metadata)
    (output_task_dir / "task.toml").write_text(
        make_task_toml(
            task_id=task_id,
            paper_id=paper_id,
            title=title,
            leaf_count=leaf_count,
            metadata=metadata,
            resource_metadata=resource_metadata,
            pipeline_commit=pipeline_commit,
            judge_model=judge_model,
            agent_timeout_sec=agent_timeout_sec,
            verifier_timeout_sec=verifier_timeout_sec,
            reproduction_timeout_sec=reproduction_timeout_sec,
            judge_request_timeout_sec=judge_request_timeout_sec,
            judge_max_workers=judge_max_workers,
            judge_context_window_tokens=judge_context_window_tokens,
            docker_image=docker_image,
            rubric_mode=rubric_mode,
        ),
        encoding="utf-8",
    )
    normalize_task_permissions(output_task_dir)
    return {
        "paper_id": paper_id,
        "rubric_source": authored_status,
        "source_rubric_mode": source_rubric_mode,
        "code_dev_derivation": (
            CODE_DEV_DERIVATION
            if rubric_mode == "code-dev"
            else None
        ),
        "addendum_source": authored_status,
        "rubric_leaf_count": leaf_count,
        "rubric_mode": rubric_mode,
        "data_profile": data_profile,
    }


def manifest_row(
    *, task_id: str, paper_id: str, batch_id: str, source_index: int
) -> dict[str, Any]:
    source_name = f"paperbench-{source_index:04d}"
    return {
        "artifact_paths": [HARBOR_SUBMISSION_DIR, HARBOR_AGENT_TRAJECTORY],
        "benchmark": "paperbench",
        "competition_id": "",
        "metric": "llm_rubric_judge",
        "paper_id": paper_id,
        "promoted_public_data_paths": [],
        "removed_paths": ["environment/Dockerfile", "tests/Dockerfile"],
        "source_format": "paperbench_official_style_harbor_adapted",
        "source_seed_task_id": "",
        "source_task_dir": f"agent_training/tasks/{batch_id}/{source_name}",
        "source_task_key": paper_id,
        "source_task_name": f"paperbench/{source_name}",
        "task_id": task_id,
    }


def validate_harbor_batch(
    batch_dir: Path,
    *,
    instructions_content: bytes | None = None,
    template_task: Path | None = None,
    rubric_mode: str = "regular",
) -> dict[str, Any]:
    errors: list[str] = []
    manifest_path = batch_dir / "manifest.jsonl"
    if not manifest_path.is_file():
        return {"valid": False, "errors": ["missing manifest.jsonl"], "tasks": 0}
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"manifest line {line_number}: {exc}")
            continue
        if not isinstance(row, dict):
            errors.append(f"manifest line {line_number}: row is not an object")
            continue
        if set(row) != MANIFEST_KEYS:
            errors.append(f"manifest line {line_number}: keys do not match Harbor contract")
        rows.append(row)
    task_ids = [row.get("task_id") for row in rows]
    if len(task_ids) != len(set(task_ids)):
        errors.append("manifest task IDs are not unique")
    actual_dirs = {
        path.name for path in (batch_dir / "harbor_task").glob("*") if path.is_dir()
    }
    if actual_dirs != set(task_ids):
        errors.append("harbor_task directories do not exactly match manifest task IDs")
    for row in rows:
        task_id = row.get("task_id")
        if not isinstance(task_id, str):
            continue
        task_dir = batch_dir / "harbor_task" / task_id
        expected_top = {
            "task.toml",
            "instruction.md",
            "resource_metadata.json",
            "tests",
            "environment",
        }
        actual_top = {path.name for path in task_dir.iterdir()} if task_dir.is_dir() else set()
        if actual_top != expected_top:
            errors.append(f"{task_id}: top-level entries do not match Harbor contract")
        expected_tests = {
            "test.sh",
            "llm_rubric_judge.py",
            "judge_config.json",
            "rubric.json",
            "judge.addendum.md",
            "paper",
        }
        actual_tests = {
            path.name for path in (task_dir / "tests").iterdir()
        } if (task_dir / "tests").is_dir() else set()
        if actual_tests != expected_tests:
            errors.append(f"{task_id}: tests entries do not match Harbor contract")
        expected_agent_paper = {
            "paper.pdf",
            "paper.md",
            "addendum.md",
            "blacklist.txt",
            "assets",
        }
        expected_verifier_paper = {
            "paper.pdf",
            "paper.md",
            "addendum.md",
            "blacklist.txt",
        }
        paper_dir = task_dir / "environment" / "paper"
        actual_paper = {path.name for path in paper_dir.iterdir()} if paper_dir.is_dir() else set()
        if actual_paper != expected_agent_paper:
            errors.append(f"{task_id}: environment/paper entries do not match Harbor contract")
        verifier_paper_dir = task_dir / "tests" / "paper"
        actual_verifier_paper = (
            {path.name for path in verifier_paper_dir.iterdir()}
            if verifier_paper_dir.is_dir()
            else set()
        )
        if actual_verifier_paper != expected_verifier_paper:
            errors.append(f"{task_id}: tests/paper entries do not match Harbor contract")
        elif paper_dir.is_dir():
            for name in expected_verifier_paper:
                if (paper_dir / name).read_bytes() != (verifier_paper_dir / name).read_bytes():
                    errors.append(f"{task_id}: verifier paper snapshot differs for {name}")
        judge_config_path = task_dir / "tests" / "judge_config.json"
        expected_assets: set[str] = set()
        if judge_config_path.is_file():
            asset_config = read_json(judge_config_path)
            configured_assets = asset_config.get("paper_asset_files", [])
            if (
                asset_config.get("paper_asset_policy")
                not in {
                    "explicit-necessary-assets-v1",
                    "paper-markdown-referenced-assets-v1",
                }
                or not isinstance(configured_assets, list)
                or not all(isinstance(value, str) for value in configured_assets)
            ):
                errors.append(f"{task_id}: invalid PaperBench asset-selection metadata")
            else:
                expected_assets = set(configured_assets)
        assets_dir = paper_dir / "assets"
        actual_assets = {
            path.relative_to(assets_dir).as_posix()
            for path in assets_dir.rglob("*")
            if path.is_file()
        } if assets_dir.is_dir() else set()
        if actual_assets != expected_assets:
            errors.append(f"{task_id}: environment assets do not match selected paper assets")
        if (paper_dir / "paper.md").is_file():
            references = markdown_asset_references(
                (paper_dir / "paper.md").read_text(encoding="utf-8", errors="replace")
            )
            missing_references = sorted(references - actual_assets)
            if missing_references:
                errors.append(
                    f"{task_id}: paper.md references unselected assets: "
                    + ", ".join(missing_references)
                )
        for relative in REQUIRED_TASK_FILES:
            if not (task_dir / relative).is_file():
                errors.append(f"{task_id}: missing {relative}")
        if (task_dir / "environment" / "Dockerfile").exists():
            errors.append(f"{task_id}: processed task must not contain environment/Dockerfile")
        if (task_dir / "tests" / "Dockerfile").exists():
            errors.append(f"{task_id}: processed task must not contain tests/Dockerfile")
        instruction_path = task_dir / "instruction.md"
        if instructions_content is not None and instruction_path.is_file():
            if instruction_path.read_bytes() != instructions_content:
                errors.append(f"{task_id}: instruction.md differs from the rendered Harbor contract")
            instruction_text = instruction_path.read_text(encoding="utf-8")
            for required in (HARBOR_PAPER_DIR, HARBOR_SUBMISSION_DIR):
                if required not in instruction_text:
                    errors.append(f"{task_id}: instruction.md missing {required}")
            if AGENT_RUNTIME_NOTE not in instruction_text:
                errors.append(f"{task_id}: instruction.md is missing the 12-hour task-work limit")
            if f"NVIDIA {HARBOR_GPU_TYPE} GPU" not in instruction_text:
                errors.append(
                    f"{task_id}: instruction.md does not contain the "
                    f"{HARBOR_GPU_TYPE}-only GPU adaptation"
                )
            if rubric_mode == "code-dev":
                if "The code will not be executed during grading." not in instruction_text:
                    errors.append(f"{task_id}: instruction.md lost the official code-dev contract")
                if "for a maximum runtime of 7 days" in instruction_text or "reproduce.sh" in instruction_text:
                    errors.append(f"{task_id}: code-dev instruction unexpectedly requires reproduction")
            else:
                if "for a maximum runtime of 7 days" not in instruction_text:
                    errors.append(f"{task_id}: instruction.md changed the official seven-day runtime")
                if "NVIDIA A10 GPU" in instruction_text:
                    errors.append(f"{task_id}: instruction.md retained the unsupported A10 GPU")
        if template_task:
            for relative in (
                "tests/test.sh",
                "tests/llm_rubric_judge.py",
            ):
                if (task_dir / relative).is_file() and (
                    task_dir / relative
                ).read_bytes() != (template_task / relative).read_bytes():
                    errors.append(f"{task_id}: {relative} differs from Harbor reference template")
        task_config: dict[str, Any] | None = None
        task_toml = task_dir / "task.toml"
        if task_toml.is_file():
            toml_text = task_toml.read_text(encoding="utf-8")
            try:
                task_config = tomllib.loads(toml_text)
            except tomllib.TOMLDecodeError as exc:
                errors.append(f"{task_id}: task.toml is invalid TOML: {exc}")
            for required_text in (
                'schema_version = "1.4"',
                f'artifacts = [{json_string(HARBOR_SUBMISSION_DIR)}, {json_string(HARBOR_AGENT_TRAJECTORY)}]',
                'source_native_contract = "paperbench_authored_task_v1"',
                'construction_format = "native_rollout_task_v1"',
                f'paper_id = {json_string(str(row.get("paper_id", "")))}',
                f'native_task_id = {json_string(str(row.get("paper_id", "")))}',
                'reference_solution_available = false',
                'scoring_method = "llm_rubric_judge"',
                f'rubric_mode = {json_string(rubric_mode)}',
                f'code_only = {str(rubric_mode == "code-dev").lower()}',
                f'resource_metadata_version = {json_string(HARBOR_RESOURCE_METADATA_VERSION)}',
                f'gpu_tier = {json_string(HARBOR_GPU_TYPE)}',
                'gpu_count = 1',
                'environment_mode = "separate"',
                'workdir = "/tests"',
                f'workdir = {json_string(HARBOR_WORKDIR)}',
            ):
                if required_text not in toml_text:
                    errors.append(f"{task_id}: task.toml missing {required_text}")
            for forbidden in (
                "LLM_API_KEY",
                "LLM_BASE_URL",
                'JUDGE_LLM_API_KEY = "${',
                'JUDGE_LLM_BASE_URL = "${',
            ):
                if forbidden in toml_text:
                    errors.append(f"{task_id}: task.toml contains forbidden env template {forbidden!r}")
            if task_config is not None:
                task_metadata = task_config.get("metadata")
                if not isinstance(task_metadata, dict):
                    errors.append(f"{task_id}: task.toml missing [metadata]")
                elif rubric_mode == "code-dev" and task_metadata.get(
                    "code_dev_derivation"
                ) != CODE_DEV_DERIVATION:
                    errors.append(f"{task_id}: task.toml code-dev derivation mismatch")
                for section in ("environment", "verifier.environment"):
                    value: Any = task_config
                    for key in section.split("."):
                        value = value.get(key) if isinstance(value, dict) else None
                    if not isinstance(value, dict):
                        errors.append(f"{task_id}: task.toml missing [{section}]")
                        continue
                    if value.get("gpus") != 1:
                        errors.append(f"{task_id}: [{section}].gpus must be 1")
                    if value.get("gpu_types") != [HARBOR_GPU_TYPE]:
                        errors.append(
                            f'{task_id}: [{section}].gpu_types must be ["{HARBOR_GPU_TYPE}"]'
                        )
                agent_config = task_config.get("agent", {})
                verifier_config = task_config.get("verifier", {})
                if not isinstance(agent_config.get("timeout_sec"), int) or agent_config["timeout_sec"] <= 0:
                    errors.append(f"{task_id}: [agent].timeout_sec must be a positive integer")
                if not isinstance(verifier_config.get("timeout_sec"), int) or verifier_config["timeout_sec"] <= 0:
                    errors.append(f"{task_id}: [verifier].timeout_sec must be a positive integer")
                verifier_env = verifier_config.get("env", {})
                if not isinstance(verifier_env, dict) or not str(
                    verifier_env.get("PAPERBENCH_JUDGE_CONTEXT_WINDOW_TOKENS", "")
                ).isdigit():
                    errors.append(f"{task_id}: verifier judge context window is missing or invalid")
        if judge_config_path.is_file():
            judge_config = read_json(judge_config_path)
            if judge_config.get("rubric_mode") != rubric_mode:
                errors.append(f"{task_id}: judge_config rubric_mode mismatch")
            if judge_config.get("code_only") is not (rubric_mode == "code-dev"):
                errors.append(f"{task_id}: judge_config code_only mismatch")
            if judge_config.get("request_mode") != "per_leaf":
                errors.append(f"{task_id}: judge_config request_mode must be per_leaf")
            if not isinstance(judge_config.get("max_workers"), int) or judge_config["max_workers"] <= 0:
                errors.append(f"{task_id}: judge_config max_workers must be a positive integer")
            expected_max_prior_nodes = (
                5 if judge_config.get("source_paperbench_id") == "pinn" else None
            )
            if judge_config.get("max_prior_nodes") != expected_max_prior_nodes:
                errors.append(
                    f"{task_id}: judge_config max_prior_nodes does not match official PaperBench"
                )
            if (
                not isinstance(judge_config.get("context_window_tokens"), int)
                or judge_config["context_window_tokens"] <= 10000
            ):
                errors.append(f"{task_id}: judge_config context_window_tokens must exceed 10000")
            if rubric_mode == "code-dev" and judge_config.get(
                "code_dev_derivation"
            ) != CODE_DEV_DERIVATION:
                errors.append(f"{task_id}: judge_config code-dev derivation mismatch")
        rubric_path = task_dir / "tests" / "rubric.json"
        if rubric_path.is_file():
            rubric_report = validate_rubric(read_json(rubric_path), rubric_mode=rubric_mode)
            if not rubric_report["valid"]:
                errors.extend(
                    f"{task_id}: rubric mode validation: {error}"
                    for error in rubric_report["errors"]
                )
        judge_path = task_dir / "tests" / "llm_rubric_judge.py"
        if judge_path.is_file():
            judge_text = judge_path.read_text(encoding="utf-8")
            for required in (
                'api_key = env_value("JUDGE_LLM_API_KEY")',
                'base_url = env_value("JUDGE_LLM_BASE_URL")',
                "def score_rubric_tree(",
                "def grade_leaf_requests(",
                "ThreadPoolExecutor",
                'parser.add_argument("--submission-validation", type=Path, required=True)',
                'parser.add_argument("--monitor-result", type=Path, required=True)',
                'parser_invalid_reason = "leaf score must be exactly 0 or 1"',
            ):
                if required not in judge_text:
                    errors.append(f"{task_id}: judge is missing {required}")
            if '"temperature"' in judge_text or "'temperature'" in judge_text:
                errors.append(f"{task_id}: judge sends unsupported temperature")
        test_script_path = task_dir / "tests" / "test.sh"
        if test_script_path.is_file():
            test_script_text = test_script_path.read_text(encoding="utf-8")
            for required in (
                "MAX_COMMITTED_BYTES = 1_000_000_000",
                'git -C "${CLEAN_SUBMISSION_DIR}" clean -fd',
                'reproduce.log.creation_time',
                "2>&1 | tee reproduce.log",
                '"tracked_worktree_clean"',
                '"committed_size_ok"',
                '--submission-validation "${LOGS_DIR}/preflight.json"',
                '--monitor-result "${LOGS_DIR}/monitor.json"',
                '--submission-dir "${CLEAN_SUBMISSION_DIR}"',
            ):
                if required not in test_script_text:
                    errors.append(f"{task_id}: verifier is missing {required}")
        for path in [task_dir, *task_dir.rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            expected = 0o755 if path.is_dir() or path.relative_to(task_dir).as_posix() in {
                "tests/test.sh",
                "tests/llm_rubric_judge.py",
            } else 0o644
            if mode != expected:
                errors.append(
                    f"{task_id}: {path.relative_to(task_dir)} mode is {mode:o}, expected {expected:o}"
                )
        metadata_path = task_dir / "resource_metadata.json"
        if metadata_path.is_file():
            resource = read_json(metadata_path)
            if resource.get("schema_version") != HARBOR_RESOURCE_METADATA_VERSION:
                errors.append(f"{task_id}: invalid resource metadata schema")
            estimate = resource.get("resource_estimate", {})
            if estimate.get("gpu_tier") != HARBOR_GPU_TYPE:
                errors.append(f"{task_id}: resource metadata GPU tier is not {HARBOR_GPU_TYPE}")
            if estimate.get("gpu_required") is not True or estimate.get("gpu_count") != 1:
                errors.append(f"{task_id}: resource metadata must require one GPU")
            estimator = resource.get("estimator", {})
            if estimator.get("declared_gpu_tier") != HARBOR_GPU_TYPE:
                errors.append(f"{task_id}: estimator GPU tier is not {HARBOR_GPU_TYPE}")
            current = build_data_profile(task_dir)
            if resource.get("data_profile") != current:
                errors.append(f"{task_id}: resource data profile does not match task files")
    return {"valid": not errors, "errors": errors, "tasks": len(rows)}


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=project_root)
    parser.add_argument("--paper-list", type=Path, default=project_root / "manifest.json")
    parser.add_argument("--paper", action="append", dest="paper_ids")
    parser.add_argument("--batch-id", help="YYYYMMDD-HHMMSS; defaults to current UTC time")
    parser.add_argument("--output-parent", type=Path, default=project_root / "papers")
    parser.add_argument("--template-task", type=Path)
    parser.add_argument(
        "--instructions-file",
        type=Path,
        help="path to the pinned official PaperBench instructions (content hash is verified)",
    )
    parser.add_argument(
        "--rubric-mode",
        choices=("regular", "code-dev"),
        default="regular",
        help="regular grades implementation/execution/results; code-dev grades implementation only",
    )
    parser.add_argument("--require-approved", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--judge-model", default="glm-5-3")
    parser.add_argument(
        "--agent-timeout-sec",
        type=int,
        default=43200,
        help="agent rollout budget; independent of the seven-day reproduction verifier budget",
    )
    parser.add_argument(
        "--verifier-timeout-sec",
        "--timeout-sec",
        dest="verifier_timeout_sec",
        type=int,
        default=609000,
        help="whole verifier budget; --timeout-sec is retained as a compatibility alias",
    )
    parser.add_argument(
        "--reproduction-timeout-sec",
        type=int,
        default=604800,
        help="reproduce.sh verifier budget; defaults to the official PaperBench seven days",
    )
    parser.add_argument(
        "--judge-request-timeout-sec",
        type=int,
        default=600,
        help="single LLM judge request timeout; the judge does not retry timeouts",
    )
    parser.add_argument(
        "--judge-max-workers",
        type=int,
        default=100,
        help="maximum concurrent per-leaf LLM judge requests; defaults to official PaperBench's 100",
    )
    parser.add_argument(
        "--judge-context-window-tokens",
        type=int,
        default=400000,
        help="judge model context window used by official-style token budgeting",
    )
    parser.add_argument(
        "--docker-image",
        default="registry-v2.h.pjlab.org.cn/ailab-llmagent/linjiahang-p-ml:common",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    paper_list = args.paper_list.resolve()
    papers = select_papers(load_paper_list(paper_list), args.paper_ids)
    if not papers:
        raise ValueError("no papers selected")
    batch_id = args.batch_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"\d{8}-\d{6}", batch_id):
        raise ValueError("--batch-id must have format YYYYMMDD-HHMMSS")
    template = (args.template_task or default_template_task()).resolve()
    validate_template(template)
    instructions_file = (
        args.instructions_file
        or (
            OFFICIAL_PAPERBENCH_CODE_DEV_INSTRUCTIONS
            if args.rubric_mode == "code-dev"
            else OFFICIAL_PAPERBENCH_INSTRUCTIONS
        )
    ).resolve()
    if not instructions_file.is_file():
        raise FileNotFoundError(f"PaperBench instructions file is missing: {instructions_file}")
    if (
        args.agent_timeout_sec <= 0
        or args.verifier_timeout_sec <= 0
        or args.reproduction_timeout_sec <= 0
        or args.judge_request_timeout_sec <= 0
    ):
        raise ValueError("all timeout values must be positive")
    if args.judge_max_workers <= 0:
        raise ValueError("--judge-max-workers must be positive")
    if args.judge_context_window_tokens <= 10000:
        raise ValueError("--judge-context-window-tokens must exceed 10000")
    required_timeout = args.judge_request_timeout_sec * 3 + VERIFIER_OVERHEAD_SEC
    if args.rubric_mode != "code-dev":
        required_timeout += args.reproduction_timeout_sec
    if required_timeout > args.verifier_timeout_sec:
        raise ValueError(
            "--verifier-timeout-sec must leave 600 seconds beyond enabled verifier budgets"
        )
    instructions_content = render_harbor_instructions(
        instructions_file,
        rubric_mode=args.rubric_mode,
    )
    output_parent = args.output_parent.resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    final_dir = output_parent / batch_id
    incremental_marker = incremental_marker_path(output_parent, batch_id)
    finalizing_marker = finalizing_marker_path(output_parent, batch_id)
    if final_dir.exists() and not args.overwrite and not incremental_marker.is_file():
        raise FileExistsError(f"Harbor batch already exists: {final_dir}")
    finalizing_marker.write_text("full Harbor conversion in progress\n", encoding="utf-8")

    pipeline_commit = pipeline_fingerprint(template, instructions_file)
    with tempfile.TemporaryDirectory(prefix=f".{batch_id}-", dir=output_parent) as temporary:
        staging = Path(temporary) / batch_id
        harbor_root = staging / "harbor_task"
        harbor_root.mkdir(parents=True)
        rows: list[dict[str, Any]] = []
        conversion: list[dict[str, Any]] = []
        for index, paper in enumerate(papers):
            paper_id = paper["id"]
            suffix = sha256_bytes(f"{batch_id}:{paper_id}".encode("utf-8"))[:6]
            task_id = f"{batch_id}-research-paperbench-{suffix}"
            output_task = harbor_root / task_id
            output_task.mkdir()
            result = build_one(
                root=root,
                output_task_dir=output_task,
                paper=paper,
                task_id=task_id,
                template=template,
                require_approved=args.require_approved,
                pipeline_commit=pipeline_commit,
                judge_model=args.judge_model,
                agent_timeout_sec=args.agent_timeout_sec,
                verifier_timeout_sec=args.verifier_timeout_sec,
                reproduction_timeout_sec=args.reproduction_timeout_sec,
                judge_request_timeout_sec=args.judge_request_timeout_sec,
                judge_max_workers=args.judge_max_workers,
                judge_context_window_tokens=args.judge_context_window_tokens,
                docker_image=args.docker_image,
                instructions_content=instructions_content,
                rubric_mode=args.rubric_mode,
            )
            rows.append(
                manifest_row(
                    task_id=task_id,
                    paper_id=paper_id,
                    batch_id=batch_id,
                    source_index=index,
                )
            )
            conversion.append({"task_id": task_id, **result})
            print(
                f"built Harbor task {task_id}: {result['rubric_leaf_count']} leaves, "
                f"{result['data_profile']['file_count']} resource files"
            )
        manifest_text = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        )
        (staging / "manifest.jsonl").write_text(manifest_text, encoding="utf-8")
        report = validate_harbor_batch(
            staging,
            instructions_content=instructions_content,
            template_task=template,
            rubric_mode=args.rubric_mode,
        )
        if not report["valid"]:
            raise RuntimeError("invalid Harbor batch:\n- " + "\n- ".join(report["errors"]))
        if final_dir.exists():
            shutil.rmtree(final_dir)
        staging.replace(final_dir)
    incremental_marker.unlink(missing_ok=True)
    finalizing_marker.unlink(missing_ok=True)
    print(f"Harbor batch ready: {final_dir} ({len(papers)} tasks)")


if __name__ == "__main__":
    main()
