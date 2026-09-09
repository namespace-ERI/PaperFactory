#!/usr/bin/env python3
"""Run the complete PaperBench factory for every selected paper in a paperlist.

This is the large-batch entry point. It deliberately delegates all work to
``build_paperbench.py`` so task construction, rubric authoring, and Harbor
conversion cannot drift into a second implementation. By default it authors up
to ``--paper-workers`` papers concurrently and serializes each completed
incremental Harbor export. Completed stages remain resumable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import shlex
import subprocess
import sys

from build_paperbench import load_selected_ids


AGENT_TIMEOUT_SEC = 12 * 60 * 60


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-list", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=project_root)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--paper", action="append", dest="paper_ids")
    parser.add_argument("--rubric-mode", choices=("regular", "code-dev"), default="regular")
    parser.add_argument("--model")
    parser.add_argument("--second-model")
    parser.add_argument("--model-switch-after", type=int)
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--task-workers", type=int, default=1)
    parser.add_argument("--asset-workers", type=int, default=4)
    parser.add_argument("--paper-workers", type=int, default=1)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--batch-id", help="YYYYMMDD-HHMMSS; defaults to current UTC time")
    parser.add_argument("--output-parent", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--force-task", action="store_true")
    parser.add_argument("--overwrite-rubric", action="store_true")
    parser.add_argument("--overwrite-harbor", action="store_true")
    parser.add_argument("--require-approved", action="store_true")
    parser.add_argument("--target-leaves", default="40-120")
    parser.add_argument("--repair-rounds", type=int, default=1)
    parser.add_argument("--max-completion-tokens", type=int, default=24_000)
    parser.add_argument("--request-timeout", type=int, default=300)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--harbor-judge-model", default="glm-5-3")
    parser.add_argument(
        "--incremental-harbor",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="author papers concurrently and serialize each completed Harbor export",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def build_command(args: argparse.Namespace, *, paper_ids: list[str], batch_id: str) -> list[str]:
    factory_dir = Path(__file__).resolve().parent
    root = args.root.resolve()
    command = [
        sys.executable,
        "-B",
        str(factory_dir / "build_paperbench.py"),
        "--paper-list",
        str(args.paper_list.resolve()),
        "--root",
        str(root),
        "--rubric-mode",
        args.rubric_mode,
        "--task-workers",
        str(args.task_workers),
        "--asset-workers",
        str(args.asset_workers),
        "--paper-workers",
        str(args.paper_workers),
        "--workers",
        str(args.workers),
        "--api-key-env",
        args.api_key_env,
        "--target-leaves",
        args.target_leaves,
        "--repair-rounds",
        str(args.repair_rounds),
        "--max-completion-tokens",
        str(args.max_completion_tokens),
        "--timeout",
        str(args.request_timeout),
        "--retries",
        str(args.retries),
        "--batch-id",
        batch_id,
        "--harbor-agent-timeout-sec",
        str(AGENT_TIMEOUT_SEC),
        "--harbor-judge-model",
        args.harbor_judge_model,
        "--continue-on-task-error",
        "--continue-on-rubric-error",
    ]
    for paper_id in paper_ids:
        command.extend(("--paper", paper_id))
    if args.source_root:
        command.extend(("--source-root", str(args.source_root.resolve())))
    if args.model:
        command.extend(("--model", args.model))
    if args.second_model:
        command.extend(("--second-model", args.second_model))
    if args.model_switch_after is not None:
        command.extend(("--model-switch-after", str(args.model_switch_after)))
    if args.base_url:
        command.extend(("--base-url", args.base_url))
    if args.output_parent:
        command.extend(("--harbor-output-parent", str(args.output_parent.resolve())))
    if args.offline:
        command.append("--offline")
    if args.force_task:
        command.append("--force-task")
    if args.overwrite_rubric:
        command.append("--overwrite-rubric")
    else:
        # Large batches are expected to be restarted after transient failures.
        command.append("--resume-rubric")
    if args.overwrite_harbor:
        command.append("--overwrite-harbor")
    if args.require_approved:
        command.append("--require-approved")
    if args.incremental_harbor:
        command.append("--stream-papers")
    return command


def main() -> None:
    args = parse_args()
    if min(args.task_workers, args.asset_workers, args.paper_workers, args.workers) < 1:
        raise ValueError("all worker counts must be at least 1")
    if args.overwrite_rubric and args.dry_run:
        # Allowed, but make the destructive intent especially visible below.
        pass
    if (args.second_model is None) != (args.model_switch_after is None):
        raise ValueError("--second-model and --model-switch-after must be used together")

    paper_list = args.paper_list.resolve()
    paper_ids = load_selected_ids(paper_list, args.paper_ids)
    if not paper_ids:
        raise ValueError("paperlist selected zero papers")
    batch_id = args.batch_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    command = build_command(args, paper_ids=paper_ids, batch_id=batch_id)

    print(f"paperlist: {paper_list}", flush=True)
    print(f"selected papers: {len(paper_ids)}", flush=True)
    print(f"rubric mode: {args.rubric_mode}", flush=True)
    print(f"batch id: {batch_id}", flush=True)
    print(f"agent timeout: {AGENT_TIMEOUT_SEC} seconds (12 hours)", flush=True)
    print(
        "rubric request concurrency ceiling: "
        f"{args.paper_workers * args.workers} "
        f"({args.paper_workers} papers x {args.workers} requests)",
        flush=True,
    )
    print(
        f"incremental Harbor export: {'enabled' if args.incremental_harbor else 'disabled'}",
        flush=True,
    )
    print("command:", shlex.join(command), flush=True)
    if args.dry_run:
        return
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
