#!/usr/bin/env python3
"""Export each complete PaperBench rubric to its Harbor task immediately."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import convert_to_harbor as harbor


def task_id_for(batch_id: str, paper_id: str) -> str:
    suffix = harbor.sha256_bytes(f"{batch_id}:{paper_id}".encode("utf-8"))[:6]
    return f"{batch_id}-research-paperbench-{suffix}"


def write_manifest(
    batch_dir: Path,
    *,
    papers: list[dict[str, Any]],
    batch_id: str,
) -> None:
    harbor_root = batch_dir / "harbor_task"
    actual = {path.name for path in harbor_root.iterdir() if path.is_dir()}
    expected = {task_id_for(batch_id, paper["id"]) for paper in papers}
    unknown = sorted(actual - expected)
    if unknown:
        raise RuntimeError(f"incremental Harbor directory contains unknown tasks: {unknown}")
    rows = [
        harbor.manifest_row(
            task_id=task_id_for(batch_id, paper["id"]),
            paper_id=paper["id"],
            batch_id=batch_id,
            source_index=index,
        )
        for index, paper in enumerate(papers)
        if task_id_for(batch_id, paper["id"]) in actual
    ]
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    temporary = batch_dir / f".manifest.jsonl.{os.getpid()}"
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(batch_dir / "manifest.jsonl")


def completed_final_batch_task_count(
    batch_dir: Path, *, papers: list[dict[str, Any]], batch_id: str
) -> int | None:
    manifest_path = batch_dir / "manifest.jsonl"
    harbor_root = batch_dir / "harbor_task"
    if not manifest_path.is_file() or not harbor_root.is_dir():
        return None
    expected = {task_id_for(batch_id, paper["id"]) for paper in papers}
    actual = {path.name for path in harbor_root.iterdir() if path.is_dir()}
    if actual != expected:
        return None
    rows = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if {row.get("task_id") for row in rows} != expected or len(rows) != len(expected):
        return None
    return len(actual)


def export_ready_once(args: argparse.Namespace) -> tuple[int, int, int]:
    root = args.root.resolve()
    paper_list = args.paper_list.resolve()
    papers = harbor.select_papers(harbor.load_paper_list(paper_list), args.paper_ids)
    template = (args.template_task or harbor.default_template_task()).resolve()
    harbor.validate_template(template)
    instructions_file = (
        args.instructions_file
        or (
            harbor.OFFICIAL_PAPERBENCH_CODE_DEV_INSTRUCTIONS
            if args.rubric_mode == "code-dev"
            else harbor.OFFICIAL_PAPERBENCH_INSTRUCTIONS
        )
    ).resolve()
    instructions_content = harbor.render_harbor_instructions(
        instructions_file, rubric_mode=args.rubric_mode
    )
    output_parent = args.output_parent.resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    batch_dir = output_parent / args.batch_id
    marker = harbor.incremental_marker_path(output_parent, args.batch_id)
    finalizing = harbor.finalizing_marker_path(output_parent, args.batch_id)
    if finalizing.is_file():
        return 0, 0, len(papers)
    if batch_dir.exists() and not marker.is_file():
        completed = completed_final_batch_task_count(
            batch_dir, papers=papers, batch_id=args.batch_id
        )
        if completed == len(papers):
            report = harbor.validate_harbor_batch(
                batch_dir,
                instructions_content=instructions_content,
                template_task=template,
                rubric_mode=args.rubric_mode,
            )
            if report["valid"]:
                return 0, completed, len(papers)
        raise FileExistsError(f"refusing to modify non-incremental Harbor batch: {batch_dir}")
    if not batch_dir.exists():
        ready = False
        for paper in papers:
            try:
                harbor.select_authored_bundle(
                    root,
                    paper["id"],
                    rubric_mode=args.rubric_mode,
                    require_approved=args.require_approved,
                )
            except FileNotFoundError:
                continue
            ready = True
            break
        if not ready:
            return 0, 0, len(papers)
    (batch_dir / "harbor_task").mkdir(parents=True, exist_ok=True)
    marker.write_text("incremental Harbor export in progress\n", encoding="utf-8")

    pipeline_commit = harbor.pipeline_fingerprint(template, instructions_file)
    exported = 0
    for paper in papers:
        if finalizing.is_file():
            break
        paper_id = paper["id"]
        task_id = task_id_for(args.batch_id, paper_id)
        target = batch_dir / "harbor_task" / task_id
        if target.is_dir():
            continue
        try:
            harbor.select_authored_bundle(
                root,
                paper_id,
                rubric_mode=args.rubric_mode,
                require_approved=args.require_approved,
            )
        except FileNotFoundError:
            continue
        with tempfile.TemporaryDirectory(
            prefix=f".{task_id}-", dir=batch_dir / "harbor_task"
        ) as temporary:
            staged = Path(temporary) / task_id
            staged.mkdir()
            result = harbor.build_one(
                root=root,
                output_task_dir=staged,
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
            staged.replace(target)
        write_manifest(batch_dir, papers=papers, batch_id=args.batch_id)
        report = harbor.validate_harbor_batch(
            batch_dir,
            instructions_content=instructions_content,
            template_task=template,
            rubric_mode=args.rubric_mode,
        )
        if not report["valid"]:
            raise RuntimeError(
                f"incremental Harbor batch is invalid after {paper_id}:\n- "
                + "\n- ".join(report["errors"])
            )
        exported += 1
        print(
            f"exported {paper_id} -> {task_id}: "
            f"{result['rubric_leaf_count']} leaves ({report['tasks']}/{len(papers)} tasks)",
            flush=True,
        )
    total = sum(
        1
        for paper in papers
        if (batch_dir / "harbor_task" / task_id_for(args.batch_id, paper["id"])).is_dir()
    )
    return exported, total, len(papers)


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=project_root)
    parser.add_argument("--paper-list", type=Path, required=True)
    parser.add_argument("--paper", action="append", dest="paper_ids")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output-parent", type=Path, default=project_root / "papers")
    parser.add_argument("--template-task", type=Path)
    parser.add_argument("--instructions-file", type=Path)
    parser.add_argument("--rubric-mode", choices=("regular", "code-dev"), default="regular")
    parser.add_argument("--require-approved", action="store_true")
    parser.add_argument("--judge-model", default="glm-5-3")
    parser.add_argument("--agent-timeout-sec", type=int, default=43200)
    parser.add_argument("--verifier-timeout-sec", type=int, default=609000)
    parser.add_argument("--reproduction-timeout-sec", type=int, default=604800)
    parser.add_argument("--judge-request-timeout-sec", type=int, default=600)
    parser.add_argument("--judge-max-workers", type=int, default=100)
    parser.add_argument("--judge-context-window-tokens", type=int, default=400000)
    parser.add_argument(
        "--docker-image",
        default="registry-v2.h.pjlab.org.cn/ailab-llmagent/linjiahang-p-ml:common",
    )
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-sec", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not re.fullmatch(r"\d{8}-\d{6}", args.batch_id):
        raise ValueError("--batch-id must have format YYYYMMDD-HHMMSS")
    if args.poll_sec < 1:
        raise ValueError("--poll-sec must be positive")
    while True:
        exported, total, expected = export_ready_once(args)
        print(f"incremental Harbor progress: {total}/{expected} (+{exported})", flush=True)
        finalizing = harbor.finalizing_marker_path(args.output_parent.resolve(), args.batch_id)
        if total == expected or finalizing.is_file() or not args.watch:
            return
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    main()
