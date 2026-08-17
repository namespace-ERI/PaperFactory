#!/usr/bin/env python3
"""Validate PaperBench rubric files or fully published paper packages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rubric_lib import load_json, validate_package, validate_rubric, write_json


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="*")
    parser.add_argument("--root", type=Path, default=project_root)
    parser.add_argument("--paper", action="append", dest="paper_ids")
    parser.add_argument("--packages", action="store_true", help="validate complete paper directories")
    parser.add_argument("--report", type=Path, help="write the combined JSON report")
    parser.add_argument("--fail-on-warning", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    targets = list(args.paths)
    if args.paper_ids:
        targets.extend(args.root / "paper_sources" / paper_id for paper_id in args.paper_ids)
    if not targets:
        targets = sorted((args.root / "paper_sources").glob("*/rubric.json"))
    reports: list[dict] = []
    failed = False
    for target in targets:
        target = target.resolve()
        package_mode = args.packages or target.is_dir()
        if package_mode:
            paper_dir = target if target.is_dir() else target.parent
            report = validate_package(paper_dir)
            label = paper_dir.name
        else:
            report = validate_rubric(load_json(target))
            report["path"] = str(target)
            label = target.parent.name + "/" + target.name
        reports.append(report)
        warnings = report.get("warnings", [])
        valid = bool(report.get("valid")) and not (args.fail_on_warning and warnings)
        print(
            f"{'OK' if valid else 'FAIL'} {label}: "
            f"{len(report.get('errors', []))} errors, {len(warnings)} warnings"
        )
        for message in report.get("errors", []):
            print(f"  ERROR {message}")
        for message in warnings:
            print(f"  WARNING {message}")
        failed = failed or not valid
    combined = {"valid": not failed, "reports": reports}
    if args.report:
        write_json(args.report, combined)
    if not reports:
        print("No rubric files found.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
