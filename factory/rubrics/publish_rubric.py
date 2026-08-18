#!/usr/bin/env python3
"""Publish human-approved rubric/addendum drafts into PaperBench paper packages."""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
from pathlib import Path

from rubric_lib import (
    CODE_DEV_DERIVATION,
    load_json,
    paperbench_code_only_rubric,
    sha256,
    validate_addendum,
    validate_rubric,
    write_json,
)


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=project_root)
    parser.add_argument("--paper", action="append", dest="paper_ids", required=True)
    parser.add_argument("--approved-by", required=True, help="human reviewer name or stable ID")
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--replace", action="store_true", help="replace already-published rubric/addendum files"
    )
    return parser.parse_args()


def publish_one(args: argparse.Namespace, paper_id: str) -> None:
    root = args.root.resolve()
    paper_dir = root / "paper_sources" / paper_id
    authoring_dir = root / "design" / paper_id / "rubric_authoring"
    rubric_path = authoring_dir / "rubric.draft.json"
    addendum_path = authoring_dir / "addendum.draft.md"
    judge_addendum_path = authoring_dir / "judge.addendum.draft.md"
    review_path = authoring_dir / "quality_review.json"
    unresolved_path = authoring_dir / "unresolved_questions.json"
    weight_application_path = authoring_dir / "rubric_weight_application.json"
    provenance_path = authoring_dir / "authoring_provenance.json"
    for path in (
        rubric_path,
        addendum_path,
        review_path,
        unresolved_path,
        weight_application_path,
        provenance_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{paper_id}: missing authoring artifact {path}")

    provenance = load_json(provenance_path)
    rubric_mode = provenance.get("rubric_mode", "regular")
    if (
        rubric_mode == "code-dev"
        and provenance.get("code_dev_derivation") != CODE_DEV_DERIVATION
    ):
        raise RuntimeError(
            f"{paper_id}: code-dev draft was not derived with {CODE_DEV_DERIVATION}"
        )
    rubric = load_json(rubric_path)
    rubric_report = validate_rubric(rubric, rubric_mode=rubric_mode)
    addendum = addendum_path.read_text(encoding="utf-8")
    addendum_report = validate_addendum(addendum)
    review = load_json(review_path)
    unresolved = load_json(unresolved_path)
    weight_application = load_json(weight_application_path)
    blockers: list[str] = []
    blockers.extend(rubric_report["errors"])
    blockers.extend(addendum_report["errors"])
    if rubric_mode == "code-dev":
        full_rubric_path = authoring_dir / "rubric.full.draft.json"
        if not full_rubric_path.is_file():
            blockers.append("code-dev publication is missing rubric.full.draft.json")
        else:
            actual_full_sha = sha256(full_rubric_path)
            if provenance.get("full_rubric_sha256") != actual_full_sha:
                blockers.append("complete source rubric hash does not match provenance")
            full_rubric = load_json(full_rubric_path)
            full_report = validate_rubric(full_rubric, rubric_mode="regular")
            blockers.extend(
                f"complete source rubric: {error}" for error in full_report["errors"]
            )
            if paperbench_code_only_rubric(full_rubric) != rubric:
                blockers.append(
                    "code-dev rubric is not the deterministic official pruning of the "
                    "complete source rubric"
                )
    if not isinstance(review, dict):
        blockers.append("quality_review.json must contain an object")
    elif review.get("blocking_issues"):
        blockers.append("quality_review.json still contains blocking_issues")
    if not isinstance(unresolved, list):
        blockers.append("unresolved_questions.json must contain an array")
    elif unresolved:
        blockers.append("unresolved_questions.json is not empty")
    if not isinstance(weight_application, dict) or not weight_application.get("valid"):
        blockers.append("rubric weight plan was not completely and validly applied")
    if blockers:
        raise RuntimeError(
            f"{paper_id}: refusing publication:\n- " + "\n- ".join(blockers)
        )

    targets = [paper_dir / "rubric.json", paper_dir / "addendum.md"]
    existing = [path for path in targets if path.exists()]
    if existing and not args.replace:
        raise FileExistsError(
            f"{paper_id}: published files already exist: "
            + ", ".join(path.name for path in existing)
            + "; use --replace only after review"
        )
    paper_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rubric_path, paper_dir / "rubric.json")
    shutil.copy2(addendum_path, paper_dir / "addendum.md")
    if judge_addendum_path.is_file():
        shutil.copy2(judge_addendum_path, paper_dir / "judge.addendum.md")
    approval = {
        "paper_id": paper_id,
        "rubric_mode": rubric_mode,
        "authoring_mode": provenance.get("authoring_mode", rubric_mode),
        "code_dev_derivation": provenance.get("code_dev_derivation"),
        "full_rubric_sha256": provenance.get("full_rubric_sha256"),
        "approved_by": args.approved_by,
        "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "notes": args.notes,
        "rubric_sha256": sha256(paper_dir / "rubric.json"),
        "addendum_sha256": sha256(paper_dir / "addendum.md"),
        "judge_addendum_sha256": (
            sha256(paper_dir / "judge.addendum.md")
            if judge_addendum_path.is_file()
            else None
        ),
        "rubric_stats": rubric_report["stats"],
        "warnings_reviewed": rubric_report["warnings"] + addendum_report["warnings"],
    }
    write_json(authoring_dir / "human_approval.json", approval)
    print(
        f"published {paper_id}: {rubric_report['stats']['leaves']} leaves, "
        f"approved by {args.approved_by}"
    )


def main() -> None:
    args = parse_args()
    if not args.approved_by.strip():
        raise ValueError("--approved-by cannot be empty")
    for paper_id in args.paper_ids:
        publish_one(args, paper_id)


if __name__ == "__main__":
    main()
