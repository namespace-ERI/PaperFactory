#!/usr/bin/env python3
"""Publish human-approved rubric/addendum drafts into PaperBench paper packages."""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
from pathlib import Path

from rubric_lib import (
    load_json,
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
    for path in (
        rubric_path,
        addendum_path,
        review_path,
        unresolved_path,
        weight_application_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{paper_id}: missing authoring artifact {path}")

    rubric = load_json(rubric_path)
    rubric_report = validate_rubric(rubric)
    addendum = addendum_path.read_text(encoding="utf-8")
    addendum_report = validate_addendum(addendum)
    review = load_json(review_path)
    unresolved = load_json(unresolved_path)
    weight_application = load_json(weight_application_path)
    blockers: list[str] = []
    blockers.extend(rubric_report["errors"])
    blockers.extend(addendum_report["errors"])
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
