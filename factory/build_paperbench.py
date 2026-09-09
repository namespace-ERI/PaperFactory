#!/usr/bin/env python3
"""Build complete PaperBench Harbor tasks in task -> rubric -> Harbor order.

This is the factory's top-level entry point. The default retains the staged
task -> rubric -> Harbor order. ``--stream-papers`` authors up to
``--paper-workers`` papers concurrently and exports completed Harbor tasks
through one serialized coordinator.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
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


def ready_task_packages(
    root: Path, paper_ids: list[str]
) -> tuple[list[str], dict[str, str]]:
    ready: list[str] = []
    skipped: dict[str, str] = {}
    for paper_id in paper_ids:
        paper_dir = root / "paper_sources" / paper_id
        design_dir = root / "design" / paper_id
        missing = [
            str(path)
            for path in (
            paper_dir / "config.yaml",
            paper_dir / "paper.pdf",
            paper_dir / "paper.md",
            paper_dir / "blacklist.txt",
            paper_dir / "assets",
            design_dir / "task_metadata.json",
            design_dir / "source_provenance.json",
            )
            if not path.exists()
        ]
        if missing:
            skipped[paper_id] = "missing task package outputs: " + ", ".join(missing)
        else:
            ready.append(paper_id)
    return ready, skipped


def verify_task_packages(root: Path, paper_ids: list[str]) -> None:
    _, skipped = ready_task_packages(root, paper_ids)
    if skipped:
        raise RuntimeError(
            "task stage completed without required outputs:\n- "
            + "\n- ".join(f"{paper_id}: {reason}" for paper_id, reason in skipped.items())
        )


def ready_harbor_papers(
    root: Path,
    paper_ids: list[str],
    *,
    rubric_mode: str,
    require_approved: bool,
) -> tuple[list[str], dict[str, str]]:
    """Return papers with complete validated authoring bundles and skip reasons."""
    from harbor.convert_to_harbor import select_authored_bundle

    ready: list[str] = []
    skipped: dict[str, str] = {}
    for paper_id in paper_ids:
        try:
            select_authored_bundle(
                root,
                paper_id,
                rubric_mode=rubric_mode,
                require_approved=require_approved,
            )
        except FileNotFoundError as exc:
            skipped[paper_id] = str(exc)
        else:
            ready.append(paper_id)
    return ready, skipped


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
        "--continue-on-task-error",
        action="store_true",
        help="skip failed task packages while continuing with complete papers",
    )
    task_group.add_argument(
        "--task-workers",
        type=int,
        default=4,
        help="number of task paper packages to build concurrently",
    )
    task_group.add_argument(
        "--asset-workers",
        type=int,
        default=4,
        help="number of semantic paper figure downloads per paper",
    )
    task_group.add_argument(
        "--stream-papers",
        action="store_true",
        help="complete task, rubric, and incremental Harbor export one paper at a time",
    )
    task_group.add_argument("--no-split", action="store_true")
    task_group.add_argument("--split-name")

    rubric_group = parser.add_argument_group("rubric stage")
    rubric_group.add_argument(
        "--rubric-mode",
        choices=("regular", "code-dev"),
        default="regular",
        help="regular grades implementation/execution/results; code-dev grades implementation only",
    )
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
    rubric_group.add_argument(
        "--continue-on-rubric-error",
        action="store_true",
        help="skip failed rubric papers while continuing and exporting successful papers",
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
    harbor_group.add_argument("--harbor-judge-model", default="glm-5-3")
    harbor_group.add_argument("--harbor-agent-timeout-sec", type=int, default=43200)
    harbor_group.add_argument(
        "--harbor-verifier-timeout-sec",
        "--harbor-timeout-sec",
        dest="harbor_verifier_timeout_sec",
        type=int,
        default=609000,
    )
    harbor_group.add_argument(
        "--harbor-reproduction-timeout-sec",
        type=int,
        default=604800,
        help="reproduce.sh verifier budget; defaults to the official PaperBench seven days",
    )
    harbor_group.add_argument(
        "--harbor-judge-request-timeout-sec",
        type=int,
        default=600,
        help="single LLM judge request timeout; timeout failures are not retried",
    )
    harbor_group.add_argument(
        "--harbor-judge-max-workers",
        type=int,
        default=100,
        help="maximum concurrent per-leaf judge requests; defaults to official PaperBench's 100",
    )
    harbor_group.add_argument(
        "--harbor-judge-context-window-tokens",
        type=int,
        default=400000,
    )
    harbor_group.add_argument(
        "--harbor-docker-image",
        default="registry-v2.h.pjlab.org.cn/ailab-llmagent/linjiahang-p-ml:common",
    )
    return parser.parse_args()


def task_command(
    args: argparse.Namespace,
    *,
    factory_dir: Path,
    root: Path,
    paper_list: Path,
    paper_ids: list[str],
) -> list[str]:
    command = [
        sys.executable,
        "-B",
        str(factory_dir / "task" / "build_tasks.py"),
        "--paper-list",
        str(paper_list),
        "--output-root",
        str(root),
        "--workers",
        str(args.task_workers),
        "--asset-workers",
        str(args.asset_workers),
    ]
    for paper_id in paper_ids:
        command.extend(["--paper", paper_id])
    if args.source_root:
        command.extend(["--source-root", str(args.source_root.resolve())])
    if args.offline:
        command.append("--offline")
    if args.force_task:
        command.append("--force")
    if args.continue_on_task_error:
        command.append("--continue-on-error")
    if args.no_split:
        command.append("--no-split")
    if args.split_name:
        command.extend(["--split-name", args.split_name])
    return command


def rubric_base_command(
    args: argparse.Namespace, *, factory_dir: Path, root: Path
) -> list[str]:
    command = [
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
        "--rubric-mode",
        args.rubric_mode,
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
        command.extend(["--guide", str(args.guide.resolve())])
    if args.mock_responses_dir:
        command.extend(["--mock-responses-dir", str(args.mock_responses_dir.resolve())])
    if args.overwrite_rubric:
        command.append("--overwrite")
    if args.resume_rubric:
        command.append("--resume")
    if args.continue_on_rubric_error:
        command.append("--continue-on-error")
    return command


def harbor_base_command(
    args: argparse.Namespace,
    *,
    script: Path,
    root: Path,
    paper_list: Path,
) -> list[str]:
    command = [
        sys.executable,
        "-B",
        str(script),
        "--root",
        str(root),
        "--paper-list",
        str(paper_list),
        "--output-parent",
        str((args.harbor_output_parent or (root / "papers")).resolve()),
        "--judge-model",
        args.harbor_judge_model,
        "--agent-timeout-sec",
        str(args.harbor_agent_timeout_sec),
        "--verifier-timeout-sec",
        str(args.harbor_verifier_timeout_sec),
        "--reproduction-timeout-sec",
        str(args.harbor_reproduction_timeout_sec),
        "--judge-request-timeout-sec",
        str(args.harbor_judge_request_timeout_sec),
        "--judge-max-workers",
        str(args.harbor_judge_max_workers),
        "--judge-context-window-tokens",
        str(args.harbor_judge_context_window_tokens),
        "--docker-image",
        args.harbor_docker_image,
        "--rubric-mode",
        args.rubric_mode,
    ]
    if args.batch_id:
        command.extend(["--batch-id", args.batch_id])
    if args.harbor_template_task:
        command.extend(["--template-task", str(args.harbor_template_task.resolve())])
    if args.harbor_instructions_file:
        command.extend(["--instructions-file", str(args.harbor_instructions_file.resolve())])
    if args.require_approved:
        command.append("--require-approved")
    return command


def selected_model(
    args: argparse.Namespace, *, paper_id: str, all_paper_ids: list[str]
) -> str | None:
    if args.model_switch_after is None:
        return args.model
    return (
        args.model
        if all_paper_ids.index(paper_id) < args.model_switch_after
        else args.second_model
    )


def run_streaming_pipeline(
    args: argparse.Namespace,
    *,
    factory_dir: Path,
    root: Path,
    paper_list: Path,
    paper_ids: list[str],
) -> tuple[list[str], dict[str, str]]:
    """Build papers concurrently and serialize each incremental Harbor export.

    Task package construction already owns its paper-level worker pool.  Rubric
    authoring is submitted one paper per future so ``--paper-workers`` remains
    effective in streaming mode, while Harbor export stays in this coordinator
    thread to protect the shared manifest and batch directory.
    """
    if not args.batch_id:
        args.batch_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    rubric_base = rubric_base_command(args, factory_dir=factory_dir, root=root)
    incremental_base = harbor_base_command(
        args,
        script=factory_dir / "harbor" / "export_incremental.py",
        root=root,
        paper_list=paper_list,
    )
    successful: list[str] = []
    skipped: dict[str, str] = {}

    run(
        task_command(
            args,
            factory_dir=factory_dir,
            root=root,
            paper_list=paper_list,
            paper_ids=paper_ids,
        ),
        label=f"1/3 Build PaperBench tasks ({len(paper_ids)} papers)",
    )
    task_paper_ids, task_skipped = ready_task_packages(root, paper_ids)
    if task_skipped and not args.continue_on_task_error:
        raise RuntimeError(
            "task stage completed without required outputs:\n- "
            + "\n- ".join(
                f"{paper_id}: {reason}" for paper_id, reason in task_skipped.items()
            )
        )
    skipped.update(task_skipped)
    for paper_id, reason in task_skipped.items():
        print(f"Skipping {paper_id} after task stage: {reason}", flush=True)
    if not task_paper_ids:
        return [], skipped

    already_ready, _ = ready_harbor_papers(
        root,
        task_paper_ids,
        rubric_mode=args.rubric_mode,
        require_approved=args.require_approved,
    )
    already_ready_set = set(already_ready)
    successful.extend(already_ready)
    authoring_paper_ids = [
        paper_id for paper_id in task_paper_ids if paper_id not in already_ready_set
    ]

    if already_ready:
        incremental_command = list(incremental_base)
        for selected_id in paper_ids:
            incremental_command.extend(["--paper", selected_id])
        run(
            incremental_command,
            label=f"3/3 Synchronize {len(already_ready)} completed Harbor tasks",
        )
    if not authoring_paper_ids:
        return [paper_id for paper_id in paper_ids if paper_id in already_ready_set], skipped

    def author_rubric(paper_id: str) -> tuple[str, str | None]:
        model = selected_model(args, paper_id=paper_id, all_paper_ids=paper_ids)
        rubric_command = [*rubric_base, "--paper", paper_id]
        if model:
            rubric_command.extend(["--model", model])
        run(
            rubric_command,
            label=f"2/3 Build PaperBench rubric: {paper_id}",
        )
        ready, rubric_skipped = ready_harbor_papers(
            root,
            [paper_id],
            rubric_mode=args.rubric_mode,
            require_approved=args.require_approved,
        )
        if not ready:
            return paper_id, rubric_skipped[paper_id]
        return paper_id, None

    worker_count = min(args.paper_workers, len(authoring_paper_ids))
    print(
        f"\nStreaming rubric authoring with {worker_count} concurrent papers; "
        "Harbor exports remain serialized.",
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(author_rubric, paper_id): paper_id
            for paper_id in authoring_paper_ids
        }
        for future in concurrent.futures.as_completed(futures):
            paper_id = futures[future]
            try:
                _, reason = future.result()
            except Exception as exc:
                if not args.continue_on_rubric_error:
                    raise RuntimeError(
                        f"parallel streaming rubric authoring failed for {paper_id}"
                    ) from exc
                reason = f"{type(exc).__name__}: {exc}"
            if reason is not None:
                skipped[paper_id] = reason
                print(
                    f"Skipping {paper_id} after rubric stage: {reason}", flush=True
                )
                continue

            successful.append(paper_id)
            incremental_command = list(incremental_base)
            for selected_id in paper_ids:
                incremental_command.extend(["--paper", selected_id])
            run(
                incremental_command,
                label=f"3/3 Export completed Harbor task after {paper_id}",
            )

    successful_set = set(successful)
    return [paper_id for paper_id in paper_ids if paper_id in successful_set], skipped


def main() -> None:
    args = parse_args()
    factory_dir = Path(__file__).resolve().parent
    root = args.root.resolve()
    paper_list = args.paper_list.resolve()
    paper_ids = load_selected_ids(paper_list, args.paper_ids)
    if not paper_ids:
        raise ValueError("no papers selected")
    if min(args.task_workers, args.asset_workers, args.paper_workers, args.workers) < 1:
        raise ValueError("all worker counts must be at least 1")
    for paper_id in paper_ids:
        if not re.fullmatch(r"[a-z0-9]+(?:[-._:][a-z0-9]+)*", paper_id):
            raise ValueError(f"invalid paper id: {paper_id}")
    if (args.second_model is None) != (args.model_switch_after is None):
        raise ValueError("--second-model and --model-switch-after must be used together")
    if args.model_switch_after is not None and not 1 <= args.model_switch_after < len(
        paper_ids
    ):
        raise ValueError("--model-switch-after must split the selected paper list")

    if args.stream_papers:
        harbor_paper_ids, skipped_papers = run_streaming_pipeline(
            args,
            factory_dir=factory_dir,
            root=root,
            paper_list=paper_list,
            paper_ids=paper_ids,
        )
        if not harbor_paper_ids:
            raise RuntimeError("no selected paper completed the task and rubric stages")
        harbor_command = harbor_base_command(
            args,
            script=factory_dir / "harbor" / "convert_to_harbor.py",
            root=root,
            paper_list=paper_list,
        )
        for paper_id in harbor_paper_ids:
            harbor_command.extend(["--paper", paper_id])
        if args.overwrite_harbor:
            harbor_command.append("--overwrite")
        run(harbor_command, label="Finalize processed Harbor batch")
        print("\nFactory completed one paper at a time:", flush=True)
        for paper_id in harbor_paper_ids:
            print(f"- {paper_id}", flush=True)
        if skipped_papers:
            print("Skipped papers:", flush=True)
            for paper_id, reason in skipped_papers.items():
                print(f"- {paper_id}: {reason}", flush=True)
        return

    run(
        task_command(
            args,
            factory_dir=factory_dir,
            root=root,
            paper_list=paper_list,
            paper_ids=paper_ids,
        ),
        label="1/3 Build PaperBench tasks",
    )
    task_paper_ids = paper_ids
    task_skipped_papers: dict[str, str] = {}
    if args.continue_on_task_error:
        task_paper_ids, task_skipped_papers = ready_task_packages(root, paper_ids)
        for paper_id, reason in task_skipped_papers.items():
            print(f"Skipping rubric authoring for {paper_id}: {reason}", flush=True)
        if not task_paper_ids:
            raise RuntimeError("no selected paper produced a complete task package")
    else:
        verify_task_packages(root, paper_ids)

    rubric_base = rubric_base_command(args, factory_dir=factory_dir, root=root)
    if args.model_switch_after is not None:
        primary_ids = set(paper_ids[: args.model_switch_after])
        rubric_groups = [
            (
                [paper_id for paper_id in task_paper_ids if paper_id in primary_ids],
                args.model,
                "primary model",
            ),
            (
                [paper_id for paper_id in task_paper_ids if paper_id not in primary_ids],
                args.second_model,
                "second model",
            ),
        ]
    else:
        rubric_groups = [(task_paper_ids, args.model, "single model")]

    for group_ids, group_model, group_label in rubric_groups:
        if not group_ids:
            continue
        rubric_command = list(rubric_base)
        for paper_id in group_ids:
            rubric_command.extend(["--paper", paper_id])
        if group_model:
            rubric_command.extend(["--model", group_model])
        run(
            rubric_command,
            label=f"2/3 Build PaperBench rubrics ({group_label}: {len(group_ids)} papers)",
        )

    harbor_paper_ids = task_paper_ids
    skipped_papers: dict[str, str] = dict(task_skipped_papers)
    if args.continue_on_rubric_error:
        harbor_paper_ids, rubric_skipped_papers = ready_harbor_papers(
            root,
            task_paper_ids,
            rubric_mode=args.rubric_mode,
            require_approved=args.require_approved,
        )
        skipped_papers.update(rubric_skipped_papers)
        for paper_id, reason in rubric_skipped_papers.items():
            print(f"Skipping Harbor conversion for {paper_id}: {reason}", flush=True)
        if not harbor_paper_ids:
            raise RuntimeError("no selected paper produced a complete rubric for Harbor export")

    harbor_command = harbor_base_command(
        args,
        script=factory_dir / "harbor" / "convert_to_harbor.py",
        root=root,
        paper_list=paper_list,
    )
    for paper_id in harbor_paper_ids:
        harbor_command.extend(["--paper", paper_id])
    if args.overwrite_harbor:
        harbor_command.append("--overwrite")
    run(harbor_command, label="3/3 Convert to processed Harbor format")

    print("\nFactory completed in strict task -> rubric -> Harbor order:", flush=True)
    for paper_id in harbor_paper_ids:
        print(
            f"- {paper_id}: {root / 'paper_sources' / paper_id} + "
            f"{root / 'design' / paper_id / 'rubric_authoring'}",
            flush=True,
        )
    if skipped_papers:
        print("Skipped papers with incomplete rubric authoring:", flush=True)
        for paper_id in skipped_papers:
            print(f"- {paper_id}", flush=True)
    print(
        "Harbor output may contain authoring drafts unless --require-approved was used; "
        "formal benchmark publication still requires human rubric review."
    )


if __name__ == "__main__":
    main()
