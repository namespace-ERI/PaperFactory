#!/usr/bin/env python3
"""Build complete PaperBench Harbor tasks in task -> rubric -> Harbor order.

This is the factory's top-level entry point. It first materializes and verifies
all selected paper task packages. Only after every task succeeds does it launch
rubric/addendum authoring, then converts those exact paper IDs to Harbor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_selected_ids(paper_list: Path, requested: list[str] | None) -> list[str]:
    raw: Any = json.loads(paper_list.read_text(encoding="utf-8"))
    papers = raw if isinstance(raw, list) else raw.get("papers") if isinstance(raw, dict) else None
    if not isinstance(papers, list):
        raise ValueError("paper list must be an array or an object containing 'papers'")
    ids = [item.get("id") for item in papers if isinstance(item, dict)]
    if len(ids) != len(papers) or not all(isinstance(value, str) for value in ids):
        raise ValueError("every paper-list entry must have a string id")
    if len(ids) != len(set(ids)):
        raise ValueError("paper-list IDs are not unique")
    selected = requested or ids
    unknown = sorted(set(selected) - set(ids))
    if unknown:
        raise ValueError(f"unknown paper ids: {', '.join(unknown)}")
    return [paper_id for paper_id in ids if paper_id in set(selected)]


def run(command: list[str], *, label: str) -> None:
    print(f"\n=== {label} ===", flush=True)
    print(" ".join(command), flush=True)
    subprocess.run(command, check=True)


def verify_task_packages(root: Path, paper_ids: list[str]) -> None:
    missing: list[str] = []
    for paper_id in paper_ids:
        paper_dir = root / "paper_sources" / paper_id
        design_dir = root / "design" / paper_id
        for path in (
            paper_dir / "config.yaml",
            paper_dir / "paper.pdf",
            paper_dir / "paper.md",
            paper_dir / "blacklist.txt",
            paper_dir / "assets",
            design_dir / "task_metadata.json",
            design_dir / "source_provenance.json",
        ):
            if not path.exists():
                missing.append(str(path))
    if missing:
        raise RuntimeError(
            "task stage completed without required outputs:\n- " + "\n- ".join(missing)
        )


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-list", type=Path, default=project_root / "manifest.json")
    parser.add_argument("--root", type=Path, default=project_root)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--paper", action="append", dest="paper_ids")

    task_group = parser.add_argument_group("task stage")
    task_group.add_argument("--offline", action="store_true")
    task_group.add_argument("--force-task", action="store_true")
    task_group.add_argument(
        "--task-workers",
        type=int,
        default=4,
        help="number of task paper packages to build concurrently",
    )
    task_group.add_argument("--no-split", action="store_true")
    task_group.add_argument("--split-name")

    rubric_group = parser.add_argument_group("rubric stage")
    rubric_group.add_argument("--guide", type=Path)
    rubric_group.add_argument("--model")
    rubric_group.add_argument(
        "--second-model",
        help="model for papers after --model-switch-after",
    )
    rubric_group.add_argument(
        "--model-switch-after",
        type=int,
        help="use --model for the first N selected papers and --second-model thereafter",
    )
    rubric_group.add_argument("--api-key-env", default="OPENAI_API_KEY")
    rubric_group.add_argument(
        "--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    rubric_group.add_argument("--mock-responses-dir", type=Path)
    rubric_group.add_argument("--chunk-chars", type=int, default=50_000)
    rubric_group.add_argument("--workers", type=int, default=3)
    rubric_group.add_argument(
        "--paper-workers",
        type=int,
        default=1,
        help="number of papers whose rubrics are authored concurrently",
    )
    rubric_group.add_argument("--target-leaves", default="40-120")
    rubric_group.add_argument("--repair-rounds", type=int, default=1)
    rubric_group.add_argument("--max-completion-tokens", type=int, default=24_000)
    rubric_group.add_argument("--timeout", type=int, default=300)
    rubric_group.add_argument("--retries", type=int, default=4)
    rubric_group.add_argument("--overwrite-rubric", action="store_true")
    rubric_group.add_argument(
        "--resume-rubric",
        action="store_true",
        help="skip complete rubrics and restart only incomplete generated drafts",
    )

    harbor_group = parser.add_argument_group("Harbor conversion stage")
    harbor_group.add_argument("--batch-id", help="YYYYMMDD-HHMMSS")
    harbor_group.add_argument(
        "--harbor-output-parent",
        type=Path,
        help="default: <root>/papers",
    )
    harbor_group.add_argument("--harbor-template-task", type=Path)
    harbor_group.add_argument("--harbor-instructions-file", type=Path)
    harbor_group.add_argument("--require-approved", action="store_true")
    harbor_group.add_argument("--overwrite-harbor", action="store_true")
    harbor_group.add_argument("--harbor-judge-model", default="gpt-5.5")
    harbor_group.add_argument("--harbor-timeout-sec", type=int, default=21600)
    harbor_group.add_argument(
        "--harbor-docker-image",
        default="registry-v2.h.pjlab.org.cn/ailab-llmagent/linjiahang-p-ml:common",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    factory_dir = Path(__file__).resolve().parent
    root = args.root.resolve()
    paper_list = args.paper_list.resolve()
    paper_ids = load_selected_ids(paper_list, args.paper_ids)
    if not paper_ids:
        raise ValueError("no papers selected")
    for paper_id in paper_ids:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", paper_id):
            raise ValueError(f"invalid paper id: {paper_id}")

    task_command = [
        sys.executable,
        "-B",
        str(factory_dir / "task" / "build_tasks.py"),
        "--paper-list",
        str(paper_list),
        "--output-root",
        str(root),
        "--workers",
        str(args.task_workers),
    ]
    for paper_id in paper_ids:
        task_command.extend(["--paper", paper_id])
    if args.source_root:
        task_command.extend(["--source-root", str(args.source_root.resolve())])
    if args.offline:
        task_command.append("--offline")
    if args.force_task:
        task_command.append("--force")
    if args.no_split:
        task_command.append("--no-split")
    if args.split_name:
        task_command.extend(["--split-name", args.split_name])
    run(task_command, label="1/3 Build PaperBench tasks")
    verify_task_packages(root, paper_ids)

    rubric_base_command = [
        sys.executable,
        "-B",
        str(factory_dir / "rubrics" / "create_rubrics.py"),
        "--root",
        str(root),
        "--api-key-env",
        args.api_key_env,
        "--base-url",
        args.base_url,
        "--chunk-chars",
        str(args.chunk_chars),
        "--workers",
        str(args.workers),
        "--paper-workers",
        str(args.paper_workers),
        "--target-leaves",
        args.target_leaves,
        "--repair-rounds",
        str(args.repair_rounds),
        "--max-completion-tokens",
        str(args.max_completion_tokens),
        "--timeout",
        str(args.timeout),
        "--retries",
        str(args.retries),
    ]
    if args.guide:
        rubric_base_command.extend(["--guide", str(args.guide.resolve())])
    if args.mock_responses_dir:
        rubric_base_command.extend(
            ["--mock-responses-dir", str(args.mock_responses_dir.resolve())]
        )
    if args.overwrite_rubric:
        rubric_base_command.append("--overwrite")
    if args.resume_rubric:
        rubric_base_command.append("--resume")

    if (args.second_model is None) != (args.model_switch_after is None):
        raise ValueError("--second-model and --model-switch-after must be used together")
    if args.model_switch_after is not None:
        if not 1 <= args.model_switch_after < len(paper_ids):
            raise ValueError("--model-switch-after must split the selected paper list")
        rubric_groups = [
            (paper_ids[: args.model_switch_after], args.model, "primary model"),
            (paper_ids[args.model_switch_after :], args.second_model, "second model"),
        ]
    else:
        rubric_groups = [(paper_ids, args.model, "single model")]

    for group_ids, group_model, group_label in rubric_groups:
        rubric_command = list(rubric_base_command)
        for paper_id in group_ids:
            rubric_command.extend(["--paper", paper_id])
        if group_model:
            rubric_command.extend(["--model", group_model])
        run(
            rubric_command,
            label=f"2/3 Build PaperBench rubrics ({group_label}: {len(group_ids)} papers)",
        )

    harbor_command = [
        sys.executable,
        "-B",
        str(factory_dir / "harbor" / "convert_to_harbor.py"),
        "--root",
        str(root),
        "--paper-list",
        str(paper_list),
        "--output-parent",
        str((args.harbor_output_parent or (root / "papers")).resolve()),
        "--judge-model",
        args.harbor_judge_model,
        "--timeout-sec",
        str(args.harbor_timeout_sec),
        "--docker-image",
        args.harbor_docker_image,
    ]
    for paper_id in paper_ids:
        harbor_command.extend(["--paper", paper_id])
    if args.batch_id:
        harbor_command.extend(["--batch-id", args.batch_id])
    if args.harbor_template_task:
        harbor_command.extend(
            ["--template-task", str(args.harbor_template_task.resolve())]
        )
    if args.harbor_instructions_file:
        harbor_command.extend(
            ["--instructions-file", str(args.harbor_instructions_file.resolve())]
        )
    if args.require_approved:
        harbor_command.append("--require-approved")
    if args.overwrite_harbor:
        harbor_command.append("--overwrite")
    run(harbor_command, label="3/3 Convert to processed Harbor format")

    print("\nFactory completed in strict task -> rubric -> Harbor order:", flush=True)
    for paper_id in paper_ids:
        print(
            f"- {paper_id}: {root / 'paper_sources' / paper_id} + "
            f"{root / 'design' / paper_id / 'rubric_authoring'}",
            flush=True,
        )
    print(
        "Harbor output may contain authoring drafts unless --require-approved was used; "
        "formal benchmark publication still requires human rubric review."
    )


if __name__ == "__main__":
    main()
