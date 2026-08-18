#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


TEXT_SUFFIXES = {
    ".py", ".sh", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv",
    ".rst", ".log", ".out", ".err", ".html", ".tex",
}

PRIORITY_BASENAMES = {
    "readme.md": 0,
    "reproduce.sh": 0,
    "requirements.txt": 1,
    "pyproject.toml": 1,
    "environment.yml": 1,
    "environment.yaml": 1,
    "results.json": 1,
    "metrics.json": 1,
    "results.csv": 1,
    "metrics.csv": 1,
}
SOURCE_SUFFIXES = {".py", ".sh", ".ipynb", ".toml", ".yaml", ".yml"}
RESULT_MARKERS = {"result", "metric", "score", "summary", "evaluation", "report"}
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "")
        if value and not value.strip().startswith("${"):
            return value
    return ""


def clipped(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def numeric_reward(
    *,
    score: float,
    format_score: float,
    submission_present: float,
    judge_available: float,
    llm_parse_ok: float,
    leaf_count: int,
    invalid_leaf_count: int,
    rubric_weight_total: float,
    reproduction_ran: float,
    reproduction_success: float,
    code_only: bool,
) -> dict[str, float]:
    score = clipped(score)
    return {
        "score": score,
        "weighted_score": score,
        "reward": score,
        "paperbench_score": score,
        "format": clipped(format_score),
        "submission_present": clipped(submission_present),
        "judge_available": clipped(judge_available),
        "llm_parse_ok": clipped(llm_parse_ok),
        "leaf_count": float(leaf_count),
        "invalid_leaf_count": float(invalid_leaf_count),
        "rubric_weight_total": float(rubric_weight_total),
        "reproduction_ran": clipped(reproduction_ran),
        "reproduction_success": clipped(reproduction_success),
        "code_only": 1.0 if code_only else 0.0,
    }


def rubric_leaves(node: Any) -> list[dict[str, Any]]:
    if not isinstance(node, dict):
        return []
    children = node.get("sub_tasks") if isinstance(node.get("sub_tasks"), list) else []
    if not children:
        return [node]
    out: list[dict[str, Any]] = []
    for child in children:
        out.extend(rubric_leaves(child))
    return out


def filtered_leaves(rubric: Any, *, code_only: bool) -> list[dict[str, Any]]:
    leaves = [leaf for leaf in rubric_leaves(rubric) if isinstance(leaf, dict)]
    if not code_only:
        return leaves
    code_leaves = [leaf for leaf in leaves if leaf.get("task_category") == "Code Development"]
    return code_leaves


def rubric_leaf_contexts(
    node: Any,
    ancestor_requirements: tuple[str, ...] = (),
) -> list[tuple[dict[str, Any], list[str]]]:
    """Return every leaf with the requirements of its ancestor nodes."""
    if not isinstance(node, dict):
        return []
    children = node.get("sub_tasks") if isinstance(node.get("sub_tasks"), list) else []
    if not children:
        return [(node, list(ancestor_requirements))]
    requirement = str(node.get("requirements") or "").strip()
    next_ancestors = ancestor_requirements + ((requirement,) if requirement else ())
    out: list[tuple[dict[str, Any], list[str]]] = []
    for child in children:
        out.extend(rubric_leaf_contexts(child, next_ancestors))
    return out


def filtered_leaf_contexts(
    rubric: Any,
    *,
    code_only: bool,
) -> list[tuple[dict[str, Any], list[str]]]:
    contexts = rubric_leaf_contexts(rubric)
    if not code_only:
        return contexts
    return [
        (leaf, ancestors)
        for leaf, ancestors in contexts
        if leaf.get("task_category") == "Code Development"
    ]


def weight_of(leaf: dict[str, Any]) -> float:
    try:
        return max(0.0, float(leaf.get("weight", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def score_rubric_tree(
    node: Any,
    scores: dict[str, float],
    *,
    code_only: bool,
) -> float | None:
    """Aggregate scores exactly as PaperBench does: bottom-up at every branch.

    Rubric weights are local sibling weights, not globally comparable leaf
    weights.  In code-only mode, non-code leaves are pruned before each
    remaining sibling group is normalized.
    """
    if not isinstance(node, dict):
        return None
    children = node.get("sub_tasks") if isinstance(node.get("sub_tasks"), list) else []
    if not children:
        if code_only and node.get("task_category") != "Code Development":
            return None
        return scores.get(str(node.get("id") or ""), 0.0)

    graded_children: list[tuple[float, float]] = []
    for child in children:
        child_score = score_rubric_tree(child, scores, code_only=code_only)
        if child_score is not None and isinstance(child, dict):
            graded_children.append((weight_of(child), child_score))
    if not graded_children:
        return None
    total_weight = sum(weight for weight, _ in graded_children)
    if total_weight <= 0:
        return 0.0
    return sum(weight * score for weight, score in graded_children) / total_weight


def read_text(path: Path, max_chars: int) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n[truncated {len(text) - max_chars} chars]\n"
    return text


def submission_priority(path: Path, root: Path) -> tuple[int, int, str]:
    relative = path.relative_to(root)
    rel = relative.as_posix()
    basename = path.name.lower()
    lowered = rel.lower()
    if basename in PRIORITY_BASENAMES:
        rank = PRIORITY_BASENAMES[basename]
    elif any(marker in lowered for marker in RESULT_MARKERS):
        rank = 2
    elif path.suffix.lower() in SOURCE_SUFFIXES:
        rank = 3
    elif path.suffix.lower() in TEXT_SUFFIXES:
        rank = 4
    else:
        rank = 5
    return rank, len(relative.parts), lowered


def collect_submission(
    root: Path,
    *,
    max_files: int = 200,
    max_chars_per_file: int = 3500,
    max_total_chars: int = 60000,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    total_chars = 0
    if not root.is_dir():
        return {"exists": False, "files": files, "text": ""}
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_file() and not (set(path.relative_to(root).parts) & IGNORED_PARTS)
    ]
    for path in sorted(candidates, key=lambda value: submission_priority(value, root)):
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        row: dict[str, Any] = {"path": rel, "bytes": size}
        if path.suffix.lower() in TEXT_SUFFIXES and total_chars < max_total_chars and size <= 2_000_000:
            text = path.read_text(encoding="utf-8", errors="replace")
            snippet = text[:max_chars_per_file]
            row["snippet"] = snippet
            total_chars += len(snippet)
        files.append(row)
        if len(files) >= max_files:
            break
    compact_text_parts = []
    for row in files:
        snippet = row.get("snippet")
        if isinstance(snippet, str) and snippet.strip():
            compact_text_parts.append(f"### {row['path']}\n{snippet}")
    return {"exists": True, "files": files, "text": "\n\n".join(compact_text_parts)[:max_total_chars]}


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        payload = json.loads(fenced.group(1))
        if isinstance(payload, dict):
            return payload
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain a JSON object")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("LLM response JSON is not an object")
    return payload


def paper_excerpt(paper_dir: Path) -> dict[str, str]:
    paper_md = read_text(paper_dir / "paper.md", 24000)
    addendum = read_text(paper_dir / "addendum.md", 8000)
    blacklist = read_text(paper_dir / "blacklist.txt", 4000)
    return {"paper_md": paper_md, "addendum": addendum, "blacklist": blacklist}


def reproduction_summary(reproduction_dir: Path) -> dict[str, str]:
    return {
        "exit_code": read_text(reproduction_dir / "exit_code.txt", 200),
        "stdout": read_text(reproduction_dir / "stdout.txt", 6000),
        "stderr": read_text(reproduction_dir / "stderr.txt", 6000),
        "file_manifest": read_text(reproduction_dir / "file_manifest.json", 12000),
    }


def compact_leaf(leaf: dict[str, Any]) -> dict[str, Any]:
    return {
        "leaf_id": str(leaf.get("id") or ""),
        "requirements": str(leaf.get("requirements") or ""),
        "weight": weight_of(leaf),
        "task_category": leaf.get("task_category"),
        "finegrained_task_category": leaf.get("finegrained_task_category"),
    }


def build_leaf_prompt(
    *,
    paper_id: str,
    title: str,
    leaf: dict[str, Any],
    ancestor_requirements: list[str],
    paper: dict[str, str],
    judge_addendum: str,
    raw_submission: dict[str, Any],
    executed_submission: dict[str, Any],
    reproduction: dict[str, str],
    code_only: bool,
) -> str:
    mode_text = (
        "Code-Dev mode: grade exclusively from the committed source-code evidence. "
        "Do not require or infer credit from reproduce.sh, runtime logs, generated artifacts, "
        "experiment execution, or reproduced results."
        if code_only
        else "Full reproduction mode: grade code, execution, results, and analysis evidence after considering reproduction logs and generated artifacts."
    )
    evidence_rule = (
        "Assess only whether the committed implementation satisfies each Code Development leaf. "
        if code_only
        else "Use reproduction failures as negative evidence for execution/result leaves, but still award code-development credit when the implementation is clear. "
    )
    leaf_id = str(leaf.get("id") or "")
    schema = {
        "leaf_id": leaf_id,
        "score": 0,
        "rationale": "short explanation grounded in concrete submission evidence",
        "evidence_paths": ["relative/path.py"],
        "invalid_reason": "",
    }
    submission_evidence = (
        f"RAW SUBMISSION FILES:\n{json.dumps(raw_submission.get('files', []), ensure_ascii=False, indent=2)[:12000]}\n\n"
        f"RAW SUBMISSION TEXT SNIPPETS:\n{raw_submission.get('text', '')[:25000]}\n"
    )
    if not code_only:
        submission_evidence += (
            f"\nEXECUTED SUBMISSION FILES:\n{json.dumps(executed_submission.get('files', []), ensure_ascii=False, indent=2)[:16000]}\n\n"
            f"EXECUTED SUBMISSION TEXT SNIPPETS:\n{executed_submission.get('text', '')[:45000]}\n\n"
            f"REPRODUCTION SUMMARY:\n{json.dumps(reproduction, ensure_ascii=False, indent=2)[:16000]}\n"
        )
    return (
        "You are an expert judge for a paper reproduction task. Grade exactly one hidden rubric leaf. "
        "Do not reward generic claims, keyword stuffing, or text that merely restates the paper. "
        "Every leaf is binary. Return score 1 only when the submission satisfies the complete leaf criterion "
        "with concrete, relevant, and correct evidence; otherwise return score 0. "
        "Do not return partial or fractional leaf scores. "
        f"{evidence_rule}{mode_text} Return JSON only with this schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"PAPER ID: {paper_id}\nTITLE: {title}\n\n"
        f"PUBLIC ADDENDUM:\n{paper['addendum']}\n\n"
        f"BLACKLIST SUMMARY:\n{paper['blacklist']}\n\n"
        f"HIDDEN JUDGE ADDENDUM:\n{judge_addendum[:8000]}\n\n"
        f"EXPECTED LEAF ID: {leaf_id}\n"
        f"ANCESTOR REQUIREMENTS (context only):\n{json.dumps(ancestor_requirements, ensure_ascii=False, indent=2)}\n\n"
        f"RUBRIC LEAF TO SCORE:\n{json.dumps(compact_leaf(leaf), ensure_ascii=False, indent=2)}\n\n"
        f"PAPER MARKDOWN EXCERPT:\n{paper['paper_md']}\n\n"
        f"{submission_evidence}"
    )


def isolate_leaf_response(parsed: dict[str, Any], leaf_id: str) -> dict[str, Any]:
    """Normalize direct and legacy multi-leaf JSON to one requested leaf."""
    raw = parsed.get("leaves")
    if not isinstance(raw, list):
        raw = parsed.get("items")
    if isinstance(raw, list):
        matching = [
            row
            for row in raw
            if isinstance(row, dict)
            and str(row.get("leaf_id") or row.get("id") or "") == leaf_id
        ]
        return {"leaves": matching}
    if str(parsed.get("leaf_id") or parsed.get("id") or "") == leaf_id:
        return {"leaves": [parsed]}
    mapped = parsed.get(leaf_id)
    if isinstance(mapped, dict):
        return {"leaves": [{"leaf_id": leaf_id, **mapped}]}
    return parsed


def call_llm(prompt: str, *, leaf_id: str = "") -> dict[str, Any]:
    mock = os.environ.get("PAPERBENCH_JUDGE_MOCK_RESPONSE")
    if mock:
        parsed = extract_json_object(mock)
        return isolate_leaf_response(parsed, leaf_id) if leaf_id else parsed
    api_key = env_value("JUDGE_LLM_API_KEY")
    base_url = env_value("JUDGE_LLM_BASE_URL")
    model = env_value("PAPERBENCH_JUDGE_MODEL", "HARBOR_PAPERBENCH_JUDGE_MODEL", "MODEL_NAME") or "gpt-5.5"
    if not api_key:
        raise RuntimeError("JUDGE_LLM_API_KEY is required for PaperBench LLM judge")
    endpoint = (base_url.rstrip("/") if base_url else "https://api.openai.com/v1") + "/chat/completions"
    # The cluster's forward proxy cannot resolve Kubernetes service names.  Make
    # the configured judge endpoint an explicit bypass in both cases because
    # urllib gives lowercase proxy variables precedence on Unix.
    judge_host = urllib.parse.urlsplit(endpoint).hostname
    if judge_host:
        for key in ("NO_PROXY", "no_proxy"):
            entries = [item.strip() for item in os.environ.get(key, "").split(",") if item.strip()]
            if judge_host not in entries:
                entries.append(judge_host)
            os.environ[key] = ",".join(entries)
    messages = [
        {"role": "system", "content": "You are a strict paper reproduction judge. Return valid JSON only."},
        {"role": "user", "content": prompt},
    ]

    def post(payload: dict[str, Any]) -> str:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=int(os.environ.get("PAPERBENCH_JUDGE_TIMEOUT_SEC", "600"))) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"LLM judge HTTP {exc.code}: {body}") from exc
        payload = json.loads(body)
        return payload["choices"][0]["message"].get("content") or ""

    # Each leaf gets one request. Retrying it after a timeout would make verifier
    # latency unpredictable; a failed leaf is instead isolated and scored zero.
    # Do not send temperature for gpt-5.5-compatible upstreams that reject it.
    base_payload = {"model": model, "messages": messages}
    content = post(base_payload)
    return extract_json_object(content)


def parse_leaf_scores(parsed: dict[str, Any], leaves: list[dict[str, Any]]) -> tuple[dict[str, float], list[dict[str, Any]], int]:
    raw = parsed.get("leaves")
    if not isinstance(raw, list):
        raw = parsed.get("items")
    if not isinstance(raw, list):
        raise ValueError("judge JSON missing leaves list")
    valid_ids = {str(leaf.get("id") or "") for leaf in leaves}
    scores: dict[str, float] = {}
    details: list[dict[str, Any]] = []
    invalid_count = 0
    seen_ids: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            invalid_count += 1
            continue
        leaf_id = str(row.get("leaf_id") or row.get("id") or "")
        if leaf_id not in valid_ids:
            invalid_count += 1
            continue
        if leaf_id in seen_ids:
            invalid_count += 1
            continue
        seen_ids.add(leaf_id)
        raw_score = row.get("score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)) or float(raw_score) not in {0.0, 1.0}:
            score = 0.0
            invalid_count += 1
            parser_invalid_reason = "leaf score must be exactly 0 or 1"
        else:
            score = float(raw_score)
            parser_invalid_reason = ""
        scores[leaf_id] = score
        details.append(
            {
                "leaf_id": leaf_id,
                "score": score,
                "rationale": str(row.get("rationale") or "")[:2000],
                "evidence_paths": row.get("evidence_paths") if isinstance(row.get("evidence_paths"), list) else [],
                "invalid_reason": (
                    parser_invalid_reason or str(row.get("invalid_reason") or "")[:1000]
                ),
            }
        )
    return scores, details, invalid_count


def grade_leaf_requests(
    *,
    rubric: Any,
    paper_id: str,
    title: str,
    paper: dict[str, str],
    judge_addendum: str,
    raw_submission: dict[str, Any],
    executed_submission: dict[str, Any],
    reproduction: dict[str, str],
    code_only: bool,
    max_workers: int,
) -> tuple[dict[str, float], list[dict[str, Any]], dict[str, int]]:
    contexts = filtered_leaf_contexts(rubric, code_only=code_only)
    if not contexts:
        return {}, [], {"request_count": 0, "request_success_count": 0, "parse_success_count": 0}
    if max_workers <= 0:
        raise ValueError("judge max_workers must be positive")

    def grade_one(
        index: int,
        leaf: dict[str, Any],
        ancestors: list[str],
    ) -> tuple[int, str, float, dict[str, Any], bool, bool]:
        leaf_id = str(leaf.get("id") or "")
        try:
            prompt = build_leaf_prompt(
                paper_id=paper_id,
                title=title,
                leaf=leaf,
                ancestor_requirements=ancestors,
                paper=paper,
                judge_addendum=judge_addendum,
                raw_submission=raw_submission,
                executed_submission=executed_submission,
                reproduction=reproduction,
                code_only=code_only,
            )
            parsed = isolate_leaf_response(call_llm(prompt, leaf_id=leaf_id), leaf_id)
            scores, details, invalid_count = parse_leaf_scores(parsed, [leaf])
            missing = leaf_id not in scores
            parse_ok = invalid_count == 0 and not missing
            detail = details[0] if details else {
                "leaf_id": leaf_id,
                "score": 0.0,
                "rationale": "",
                "evidence_paths": [],
                "invalid_reason": "judge response omitted the requested leaf",
            }
            detail["judge_available"] = True
            detail["llm_parse_ok"] = parse_ok
            return index, leaf_id, scores.get(leaf_id, 0.0), detail, True, parse_ok
        except Exception as exc:
            detail = {
                "leaf_id": leaf_id,
                "score": 0.0,
                "rationale": "",
                "evidence_paths": [],
                "invalid_reason": f"{type(exc).__name__}: {exc}"[:1000],
                "judge_available": False,
                "llm_parse_ok": False,
            }
            return index, leaf_id, 0.0, detail, False, False

    completed = []
    worker_count = min(max_workers, len(contexts))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(grade_one, index, leaf, ancestors)
            for index, (leaf, ancestors) in enumerate(contexts)
        ]
        for future in as_completed(futures):
            completed.append(future.result())
    completed.sort(key=lambda row: row[0])
    scores = {leaf_id: score for _, leaf_id, score, _, _, _ in completed}
    details = [detail for _, _, _, detail, _, _ in completed]
    summary = {
        "request_count": len(completed),
        "request_success_count": sum(1 for *_, available, _ in completed if available),
        "parse_success_count": sum(1 for *_, parse_ok in completed if parse_ok),
        "max_workers": worker_count,
    }
    return scores, details, summary


def grade(args: argparse.Namespace) -> int:
    rubric = read_json(args.rubric)
    try:
        config = read_json(args.judge_config)
    except Exception:
        config = {}
    code_only = bool(args.code_only or config.get("code_only"))
    leaves = filtered_leaves(rubric, code_only=code_only)
    total_weight = sum(weight_of(leaf) for leaf in leaves)
    reproduction_ran = clipped(args.reproduction_ran)
    reproduction_success = clipped(args.reproduction_success)
    details_base = {
        "paper_id": "",
        "title": "",
        "code_only": code_only,
        "leaf_count": len(leaves),
        "rubric_weight_total": total_weight,
        "reproduction_ran": reproduction_ran,
        "reproduction_success": reproduction_success,
    }
    try:
        details_base["paper_id"] = str(config.get("paper_id") or "")
        details_base["title"] = str(config.get("title") or "")
    except Exception:
        pass
    submission_validation: dict[str, Any] = {}
    try:
        submission_validation = read_json(args.submission_validation)
    except Exception as exc:
        submission_validation = {
            "submission_valid": False,
            "validation_error": f"{type(exc).__name__}: {exc}",
        }
    details_base["submission_validation"] = submission_validation
    if not submission_validation.get("submission_valid"):
        submission_present = 1.0 if submission_validation.get("submission_dir_exists") else 0.0
        reward = numeric_reward(
            score=0.0,
            format_score=0.0,
            submission_present=submission_present,
            judge_available=0.0,
            llm_parse_ok=0.0,
            leaf_count=len(leaves),
            invalid_leaf_count=len(leaves),
            rubric_weight_total=total_weight,
            reproduction_ran=reproduction_ran,
            reproduction_success=reproduction_success,
            code_only=code_only,
        )
        write_json(args.reward_json, reward)
        write_json(
            args.details_json,
            {
                **details_base,
                "ok": True,
                "reason": "submission failed Git repository validation",
                "leaves": [],
            },
        )
        return 0
    if not args.submission_dir.is_dir():
        reward = numeric_reward(
            score=0.0,
            format_score=0.0,
            submission_present=0.0,
            judge_available=0.0,
            llm_parse_ok=0.0,
            leaf_count=len(leaves),
            invalid_leaf_count=len(leaves),
            rubric_weight_total=total_weight,
            reproduction_ran=reproduction_ran,
            reproduction_success=reproduction_success,
            code_only=code_only,
        )
        write_json(args.reward_json, reward)
        write_json(
            args.details_json,
            {**details_base, "ok": True, "reason": "missing cleaned submission directory", "leaves": []},
        )
        return 0
    readme = args.submission_dir / "README.md"
    reproduce = args.submission_dir / "reproduce.sh"
    format_score = 1.0 if readme.is_file() and (code_only or reproduce.is_file()) else 0.25
    raw_submission = collect_submission(args.submission_dir)
    executed_submission = collect_submission(args.executed_submission_dir)
    paper = paper_excerpt(args.paper_dir)
    judge_addendum = read_text(args.judge_addendum, 20000)
    repro = reproduction_summary(args.reproduction_dir)
    try:
        configured_workers = os.environ.get(
            "PAPERBENCH_JUDGE_MAX_WORKERS",
            str(config.get("max_workers") or "100"),
        )
        max_workers = int(configured_workers)
        scores, leaf_details, judge_summary = grade_leaf_requests(
            rubric=rubric,
            paper_id=str(config.get("paper_id") or ""),
            title=str(config.get("title") or ""),
            paper=paper,
            judge_addendum=judge_addendum,
            raw_submission=raw_submission,
            executed_submission=executed_submission,
            reproduction=repro,
            code_only=code_only,
            max_workers=max_workers,
        )
        request_count = judge_summary["request_count"]
        request_success_count = judge_summary["request_success_count"]
        parse_success_count = judge_summary["parse_success_count"]
        invalid_count = request_count - parse_success_count
        weighted = score_rubric_tree(rubric, scores, code_only=code_only)
        score = clipped(weighted if weighted is not None else 0.0)
        reward = numeric_reward(
            score=score,
            format_score=format_score,
            submission_present=1.0,
            judge_available=(request_success_count / request_count if request_count else 0.0),
            llm_parse_ok=(parse_success_count / request_count if request_count else 0.0),
            leaf_count=len(leaves),
            invalid_leaf_count=invalid_count,
            rubric_weight_total=total_weight,
            reproduction_ran=reproduction_ran,
            reproduction_success=reproduction_success,
            code_only=code_only,
        )
        write_json(args.reward_json, reward)
        write_json(
            args.details_json,
            {
                **details_base,
                "ok": request_success_count > 0,
                "score": score,
                "format_score": format_score,
                "judge": {"mode": "per_leaf", **judge_summary},
                "leaves": leaf_details,
                "submission_files": raw_submission.get("files", []),
                "executed_submission_files": executed_submission.get("files", []),
                "reproduction": repro,
            },
        )
    except Exception as exc:
        reward = numeric_reward(
            score=0.0,
            format_score=format_score,
            submission_present=1.0,
            judge_available=0.0,
            llm_parse_ok=0.0,
            leaf_count=len(leaves),
            invalid_leaf_count=len(leaves),
            rubric_weight_total=total_weight,
            reproduction_ran=reproduction_ran,
            reproduction_success=reproduction_success,
            code_only=code_only,
        )
        write_json(args.reward_json, reward)
        write_json(args.details_json, {**details_base, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Grade a PaperBench-style Harbor submission with an LLM rubric judge.")
    parser.add_argument("--submission-dir", type=Path, required=True)
    parser.add_argument("--executed-submission-dir", type=Path, required=True)
    parser.add_argument("--paper-dir", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--judge-addendum", type=Path, required=True)
    parser.add_argument("--judge-config", type=Path, required=True)
    parser.add_argument("--submission-validation", type=Path, required=True)
    parser.add_argument("--reproduction-dir", type=Path, required=True)
    parser.add_argument("--reproduction-ran", required=True)
    parser.add_argument("--reproduction-success", required=True)
    parser.add_argument("--reward-json", type=Path, required=True)
    parser.add_argument("--details-json", type=Path, required=True)
    parser.add_argument("--code-only", action="store_true")
    args = parser.parse_args()
    return grade(args)


if __name__ == "__main__":
    raise SystemExit(main())
