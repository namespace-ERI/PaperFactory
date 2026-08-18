#!/usr/bin/env python3
"""Convert authored PaperBench packages into the processed Harbor task format.

The output contract mirrors the reference batch supplied for this factory:
``manifest.jsonl`` plus ``harbor_task/<task-id>/`` directories containing
task.toml, instruction.md, resource_metadata.json, environment, tests, and a
non-oracle smoke-test solution fixture.
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
from pathlib import Path
from typing import Any, Iterable


RUBRICS_FACTORY_DIR = Path(__file__).resolve().parents[1] / "rubrics"
sys.path.insert(0, str(RUBRICS_FACTORY_DIR))
from rubric_lib import validate_addendum, validate_rubric  # noqa: E402


HARBOR_TEMPLATE_TASK = Path(__file__).resolve().with_name("templates") / "processed_task"
OFFICIAL_PAPERBENCH_INSTRUCTIONS = (
    Path(__file__).resolve().with_name("templates") / "instructions.official.txt"
)
OFFICIAL_PAPERBENCH_INSTRUCTIONS_SHA256 = (
    "712ed3968de5b8d98b96e25e7d33c95552c460649201743d8535e84c344bac56"
)
HARBOR_PAPER_DIR = "/workspace/paper"
HARBOR_SUBMISSION_DIR = "/workspace/submission"
PREFERRED_KINDS = ["json", "image", "pdf", "text", "code", "shell", "archive", "binary"]
REQUIRED_TASK_FILES = {
    "task.toml",
    "instruction.md",
    "resource_metadata.json",
    "tests/test.sh",
    "tests/llm_rubric_judge.py",
    "tests/judge_config.json",
    "tests/rubric.json",
    "tests/judge.addendum.md",
    "solution/reproduce.sh",
    "solution/README.md",
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
    return [paper for paper in papers if paper["id"] in selected]


def default_template_task() -> Path:
    if not HARBOR_TEMPLATE_TASK.is_dir():
        raise FileNotFoundError(
            f"the bundled Harbor template is unavailable: {HARBOR_TEMPLATE_TASK}"
        )
    return HARBOR_TEMPLATE_TASK


def render_harbor_instructions(path: Path, reproduction_timeout_sec: int) -> bytes:
    raw = path.read_bytes()
    actual_hash = sha256_bytes(raw)
    if actual_hash != OFFICIAL_PAPERBENCH_INSTRUCTIONS_SHA256:
        raise ValueError(
            "instructions file is not the pinned official PaperBench instructions: "
            f"expected {OFFICIAL_PAPERBENCH_INSTRUCTIONS_SHA256}, found {actual_hash}"
        )
    text = raw.decode("utf-8")
    text = text.replace("/home/paper", HARBOR_PAPER_DIR)
    text = text.replace("/home/submission", HARBOR_SUBMISSION_DIR)
    old_runtime = "for a maximum runtime of 7 days."
    minutes = reproduction_timeout_sec / 60
    if minutes.is_integer():
        duration = f"{reproduction_timeout_sec} seconds ({int(minutes)} minutes)"
    else:
        duration = f"{reproduction_timeout_sec} seconds"
    new_runtime = (
        f"for a maximum runtime of {duration}. Design reproduce.sh as a fast, "
        "deterministic reproduction entry point: prioritize the scoped core evidence, "
        "reuse committed lightweight fixtures where appropriate, and do not rely on "
        "full-scale training or large downloads completing during grading."
    )
    if old_runtime not in text:
        raise ValueError("official instructions no longer contain the expected runtime clause")
    text = text.replace(old_runtime, new_runtime)
    if "/home/paper" in text or "/home/submission" in text or "7 days" in text:
        raise ValueError("failed to adapt PaperBench instructions to the Harbor runtime contract")
    return text.encode("utf-8")


def validate_template(template: Path) -> None:
    for relative in (
        "solution/README.md",
        "solution/reproduce.sh",
        "tests/test.sh",
        "tests/llm_rubric_judge.py",
        "tests/judge_config.json",
    ):
        if not (template / relative).is_file():
            raise FileNotFoundError(f"Harbor template is missing {relative}: {template}")


def template_title(template: Path) -> str:
    config = read_json(template / "tests" / "judge_config.json")
    title = config.get("title") if isinstance(config, dict) else None
    if not isinstance(title, str) or not title:
        raise ValueError("template judge_config.json has no title")
    return title


def render_template(path: Path, *, old_title: str, new_title: str) -> str:
    text = path.read_text(encoding="utf-8")
    if old_title not in text:
        raise ValueError(f"template title not found in {path}")
    return text.replace(old_title, new_title)


def rubric_leaf_count(node: Any) -> int:
    if not isinstance(node, dict):
        return 0
    children = node.get("sub_tasks")
    if not isinstance(children, list) or not children:
        return 1
    return sum(rubric_leaf_count(child) for child in children)


def select_authored_file(
    root: Path,
    paper_id: str,
    *,
    published_name: str,
    draft_name: str,
    require_approved: bool,
) -> tuple[Path, str]:
    published = root / "paper_sources" / paper_id / published_name
    draft = root / "design" / paper_id / "rubric_authoring" / draft_name
    if published.is_file():
        return published, "published"
    if require_approved:
        raise FileNotFoundError(f"{paper_id}: approved {published_name} is required: {published}")
    if draft.is_file():
        return draft, "authoring-draft"
    raise FileNotFoundError(
        f"{paper_id}: neither published nor draft {published_name} exists"
    )


GENERIC_JUDGE_ADDENDUM = """# Judge-only evaluation guidance

Use the submitted source code, `reproduce.sh`, reproduction logs, and generated artifacts as evidence.

- Code Development leaves require concrete implementation; prose-only claims are insufficient.
- Code Execution leaves require the corresponding path to be invoked by `reproduce.sh` and evidenced by logs or artifacts.
- Result Analysis leaves require generated evidence supporting the stated comparison, tolerance, or trend.
- Award partial credit leaf by leaf. A later execution failure does not erase independently verifiable implementation evidence.
- Do not require a particular file layout when an equivalent implementation is clear and inspectable.
- Penalize fabricated, pre-written, or unsupported result claims that cannot be connected to executable code.
"""


def resource_profile(metadata: dict[str, Any]) -> dict[str, Any]:
    compute = metadata.get("compute") if isinstance(metadata.get("compute"), dict) else {}
    accelerator = str(compute.get("accelerator", "")).lower()
    notes = str(compute.get("notes", "")).lower()
    cpu_only = "cpu" in accelerator and not any(token in accelerator for token in ("gpu", "h200", "a100", "h100"))
    if cpu_only:
        paper_profile = "cpu_sufficient"
        rollout_profile = "docker_cpu_smoke"
        rollout_gpus = 0
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
    if relative.parts and relative.parts[0] in {"tests", "solution"}:
        return "hidden_or_solution"
    return "package"


def build_data_profile(task_dir: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for top in ("environment", "tests", "solution"):
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
        "gpu_tier": "consumer_20g",
        "gpu_required": True,
        "estimated_vram_gb": profile["vram"],
        "training_time_tier": "under_6h",
        "confidence": 0.75,
        "reason": (
            "Deterministic estimate from the selected paper's authoring metadata and declared "
            f"compute envelope; classified as {profile['paper_resource_profile']}. The verifier "
            "uses one GPU so the processed Harbor consistency contract selects consumer_20g."
        ),
        "attempts": 1,
    }
    return {
        "schema_version": "harbor_resource_metadata_v1",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_profile": data_profile,
        "resource_estimate": estimate,
        "estimator": {
            "operator": "paperbench_factory_metadata_estimator",
            "model": "deterministic-rules-v1",
            "prompt_version": "paperbench_harbor_resource_rules_v1",
            "validation_history": [[]],
            "declared_max_gpus": 1,
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
    timeout_sec: int,
    reproduction_timeout_sec: int,
    judge_request_timeout_sec: int,
    docker_image: str,
) -> str:
    profile = resource_profile(metadata)
    data = resource_metadata["data_profile"]
    estimate = resource_metadata["resource_estimate"]
    keywords = sorted({paper_id, "paper-reproduction", "paperbench", "research"})
    return f'''schema_version = "1.4"
artifacts = ["/workspace/submission"]

[task]
name = {json_string(f"mlcoding/{task_id}")}
description = {json_string(f"Reproduce core methods and experiments from {title}")}
keywords = {toml_array(keywords)}

[metadata]
benchmark = "paperbench"
source_format = "paperbench_official_style_harbor_adapted"
paper_id = {json_string(paper_id)}
rubric_leaf_count = {leaf_count}
oracle_available = false
scoring_method = "llm_rubric_judge"
paperbench_mode = "llm_full"
code_only = false
paper_resource_profile = {json_string(profile["paper_resource_profile"])}
rollout_resource_profile = {json_string(profile["rollout_resource_profile"])}
reproduction_resource_profile = "gpu_capable"
rollout_profile = "research_long_horizon_v1"
score_ladder_version = "task_internal_score_ladder_v1"
artifact_stop_first_valid = false
pipeline_commit = {json_string(pipeline_commit)}
source_task_key = {json_string(paper_id)}
resource_metadata_version = "harbor_resource_metadata_v1"
resource_metadata_file = "resource_metadata.json"
data_total_bytes = {data["total_bytes"]}
data_file_count = {data["file_count"]}
data_kinds = {toml_array(data["data_kinds"])}
gpu_tier = {json_string(estimate["gpu_tier"])}
gpu_required = true
estimated_training_time_tier = {json_string(estimate["training_time_tier"])}
estimated_vram_gb_typical = {estimate["estimated_vram_gb"]["typical"]:g}
resource_estimate_confidence = {estimate["confidence"]:g}
resource_estimate_model = "deterministic-rules-v1"
resource_estimate_prompt_version = "paperbench_harbor_resource_rules_v1"

[agent]
timeout_sec = {timeout_sec}

[verifier]
timeout_sec = {timeout_sec}
environment_mode = "separate"

[verifier.env]
PAPERBENCH_JUDGE_MODEL = {json_string(judge_model)}
PAPERBENCH_JUDGE_TIMEOUT_SEC = {json_string(str(judge_request_timeout_sec))}
PAPERBENCH_REPRODUCTION_TIMEOUT_SEC = {json_string(str(reproduction_timeout_sec))}

[verifier.environment]
build_timeout_sec = 900
network_mode = "public"
os = "linux"
cpus = 2
memory_mb = 4096
storage_mb = 16384
gpus = 1
docker_image = {json_string(docker_image)}
workdir = "/tests"

[environment]
build_timeout_sec = 1200
network_mode = "public"
os = "linux"
cpus = 4
memory_mb = 8192
storage_mb = 16384
gpus = {profile["rollout_gpus"]}
docker_image = {json_string(docker_image)}
workdir = "/workspace"
'''


def copy_paper_environment(source: Path, destination: Path, addendum: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("paper.pdf", "paper.md", "blacklist.txt"):
        path = source / name
        if not path.is_file():
            raise FileNotFoundError(f"missing PaperBench input: {path}")
        shutil.copy2(path, destination / name)
    shutil.copy2(addendum, destination / "addendum.md")
    assets = source / "assets"
    if assets.is_dir():
        shutil.copytree(assets, destination / "assets", dirs_exist_ok=True)
    else:
        (destination / "assets").mkdir()


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
        "solution/reproduce.sh",
    ):
        path = task_dir / relative
        if path.is_file():
            path.chmod(0o755)


def pipeline_fingerprint(template: Path, instructions_file: Path) -> str:
    digest = hashlib.sha256()
    paths = [
        ("convert_to_harbor.py", Path(__file__).resolve()),
        ("instructions.official.txt", instructions_file),
    ]
    paths.extend(
        (f"template/{relative}", template / relative)
        for relative in (
            "solution/README.md",
            "solution/reproduce.sh",
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
    template_old_title: str,
    require_approved: bool,
    pipeline_commit: str,
    judge_model: str,
    timeout_sec: int,
    reproduction_timeout_sec: int,
    judge_request_timeout_sec: int,
    docker_image: str,
    instructions_content: bytes,
) -> dict[str, Any]:
    paper_id = paper["id"]
    title = paper.get("title")
    if not isinstance(title, str) or not title:
        raise ValueError(f"{paper_id}: title is required")
    metadata_path = root / "design" / paper_id / "task_metadata.json"
    metadata = read_json(metadata_path) if metadata_path.is_file() else paper
    rubric_path, rubric_status = select_authored_file(
        root,
        paper_id,
        published_name="rubric.json",
        draft_name="rubric.draft.json",
        require_approved=require_approved,
    )
    addendum_path, addendum_status = select_authored_file(
        root,
        paper_id,
        published_name="addendum.md",
        draft_name="addendum.draft.md",
        require_approved=require_approved,
    )
    rubric = read_json(rubric_path)
    rubric_report = validate_rubric(rubric)
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
    leaf_count = rubric_leaf_count(rubric)
    if leaf_count <= 0:
        raise ValueError(f"{paper_id}: rubric has no leaves")

    tests_dir = output_task_dir / "tests"
    solution_dir = output_task_dir / "solution"
    paper_output = output_task_dir / "environment" / "paper"
    tests_dir.mkdir(parents=True, exist_ok=True)
    solution_dir.mkdir(parents=True, exist_ok=True)
    copy_paper_environment(root / "paper_sources" / paper_id, paper_output, addendum_path)

    shutil.copy2(template / "tests" / "test.sh", tests_dir / "test.sh")
    shutil.copy2(
        template / "tests" / "llm_rubric_judge.py",
        tests_dir / "llm_rubric_judge.py",
    )
    shutil.copy2(rubric_path, tests_dir / "rubric.json")
    judge_addendum_candidates = [
        root / "paper_sources" / paper_id / "judge.addendum.md",
        root / "design" / paper_id / "rubric_authoring" / "judge.addendum.draft.md",
    ]
    judge_addendum = next((path for path in judge_addendum_candidates if path.is_file()), None)
    if judge_addendum:
        shutil.copy2(judge_addendum, tests_dir / "judge.addendum.md")
    else:
        (tests_dir / "judge.addendum.md").write_text(
            GENERIC_JUDGE_ADDENDUM, encoding="utf-8"
        )
    write_json(
        tests_dir / "judge_config.json",
        {
            "judge_mode": "llm_full",
            "code_only": False,
            "paper_id": paper_id,
            "title": title,
            "judge_model_env": "PAPERBENCH_JUDGE_MODEL",
        },
    )

    shutil.copy2(template / "solution" / "reproduce.sh", solution_dir / "reproduce.sh")
    (solution_dir / "README.md").write_text(
        render_template(
            template / "solution" / "README.md",
            old_title=template_old_title,
            new_title=title,
        ),
        encoding="utf-8",
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
            timeout_sec=timeout_sec,
            reproduction_timeout_sec=reproduction_timeout_sec,
            judge_request_timeout_sec=judge_request_timeout_sec,
            docker_image=docker_image,
        ),
        encoding="utf-8",
    )
    normalize_task_permissions(output_task_dir)
    return {
        "paper_id": paper_id,
        "rubric_source": rubric_status,
        "addendum_source": addendum_status,
        "rubric_leaf_count": leaf_count,
        "data_profile": data_profile,
    }


def manifest_row(
    *, task_id: str, paper_id: str, batch_id: str, source_index: int
) -> dict[str, Any]:
    source_name = f"paperbench-{source_index:04d}"
    return {
        "artifact_paths": ["/workspace/submission"],
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
            "solution",
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
        }
        actual_tests = {
            path.name for path in (task_dir / "tests").iterdir()
        } if (task_dir / "tests").is_dir() else set()
        if actual_tests != expected_tests:
            errors.append(f"{task_id}: tests entries do not match Harbor contract")
        expected_solution = {"reproduce.sh", "README.md"}
        actual_solution = {
            path.name for path in (task_dir / "solution").iterdir()
        } if (task_dir / "solution").is_dir() else set()
        if actual_solution != expected_solution:
            errors.append(f"{task_id}: solution entries do not match Harbor contract")
        expected_paper = {"paper.pdf", "paper.md", "addendum.md", "blacklist.txt", "assets"}
        paper_dir = task_dir / "environment" / "paper"
        actual_paper = {path.name for path in paper_dir.iterdir()} if paper_dir.is_dir() else set()
        if actual_paper != expected_paper:
            errors.append(f"{task_id}: environment/paper entries do not match Harbor contract")
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
            for forbidden in ("/home/paper", "/home/submission", "7 days"):
                if forbidden in instruction_text:
                    errors.append(f"{task_id}: instruction.md contains stale contract text {forbidden!r}")
            for required in (HARBOR_PAPER_DIR, HARBOR_SUBMISSION_DIR):
                if required not in instruction_text:
                    errors.append(f"{task_id}: instruction.md missing {required}")
        if template_task:
            for relative in (
                "tests/test.sh",
                "tests/llm_rubric_judge.py",
                "solution/reproduce.sh",
            ):
                if (task_dir / relative).is_file() and (
                    task_dir / relative
                ).read_bytes() != (template_task / relative).read_bytes():
                    errors.append(f"{task_id}: {relative} differs from Harbor reference template")
        task_toml = task_dir / "task.toml"
        if task_toml.is_file():
            toml_text = task_toml.read_text(encoding="utf-8")
            for required_text in (
                'schema_version = "1.4"',
                'artifacts = ["/workspace/submission"]',
                f'paper_id = {json_string(str(row.get("paper_id", "")))}',
                'scoring_method = "llm_rubric_judge"',
                'environment_mode = "separate"',
                'workdir = "/tests"',
                'workdir = "/workspace"',
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
        judge_path = task_dir / "tests" / "llm_rubric_judge.py"
        if judge_path.is_file():
            judge_text = judge_path.read_text(encoding="utf-8")
            for required in (
                'api_key = env_value("JUDGE_LLM_API_KEY")',
                'base_url = env_value("JUDGE_LLM_BASE_URL")',
            ):
                if required not in judge_text:
                    errors.append(f"{task_id}: judge is missing {required}")
            if '"temperature"' in judge_text or "'temperature'" in judge_text:
                errors.append(f"{task_id}: judge sends unsupported temperature")
        for path in [task_dir, *task_dir.rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            expected = 0o755 if path.is_dir() or path.relative_to(task_dir).as_posix() in {
                "tests/test.sh",
                "tests/llm_rubric_judge.py",
                "solution/reproduce.sh",
            } else 0o644
            if mode != expected:
                errors.append(
                    f"{task_id}: {path.relative_to(task_dir)} mode is {mode:o}, expected {expected:o}"
                )
        metadata_path = task_dir / "resource_metadata.json"
        if metadata_path.is_file():
            resource = read_json(metadata_path)
            if resource.get("schema_version") != "harbor_resource_metadata_v1":
                errors.append(f"{task_id}: invalid resource metadata schema")
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
        default=OFFICIAL_PAPERBENCH_INSTRUCTIONS,
        help="pinned official PaperBench instructions used as the base for Harbor path/runtime adaptation",
    )
    parser.add_argument("--require-approved", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--judge-model", default="gpt-5.5")
    parser.add_argument("--timeout-sec", type=int, default=21600)
    parser.add_argument(
        "--reproduction-timeout-sec",
        type=int,
        default=900,
        help="reproduce.sh verifier budget; rendered into both task.toml and instruction.md",
    )
    parser.add_argument(
        "--judge-request-timeout-sec",
        type=int,
        default=600,
        help="single LLM judge request timeout; the judge does not retry timeouts",
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
    old_title = template_title(template)
    instructions_file = args.instructions_file.resolve()
    if not instructions_file.is_file():
        raise FileNotFoundError(f"PaperBench instructions file is missing: {instructions_file}")
    if args.timeout_sec <= 0 or args.reproduction_timeout_sec <= 0 or args.judge_request_timeout_sec <= 0:
        raise ValueError("all timeout values must be positive")
    if args.reproduction_timeout_sec + args.judge_request_timeout_sec + 60 > args.timeout_sec:
        raise ValueError(
            "--timeout-sec must leave at least 60 seconds beyond reproduction and judge request budgets"
        )
    instructions_content = render_harbor_instructions(
        instructions_file, args.reproduction_timeout_sec
    )
    output_parent = args.output_parent.resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    final_dir = output_parent / batch_id
    if final_dir.exists() and not args.overwrite:
        raise FileExistsError(f"Harbor batch already exists: {final_dir}")

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
                template_old_title=old_title,
                require_approved=args.require_approved,
                pipeline_commit=pipeline_commit,
                judge_model=args.judge_model,
                timeout_sec=args.timeout_sec,
                reproduction_timeout_sec=args.reproduction_timeout_sec,
                judge_request_timeout_sec=args.judge_request_timeout_sec,
                docker_image=args.docker_image,
                instructions_content=instructions_content,
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
        )
        if not report["valid"]:
            raise RuntimeError("invalid Harbor batch:\n- " + "\n- ".join(report["errors"]))
        if final_dir.exists():
            shutil.rmtree(final_dir)
        staging.replace(final_dir)
    print(f"Harbor batch ready: {final_dir} ({len(papers)} tasks)")


if __name__ == "__main__":
    main()
