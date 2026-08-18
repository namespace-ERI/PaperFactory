#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


def weight_of(leaf: dict[str, Any]) -> float:
    try:
        return max(0.0, float(leaf.get("weight", 0.0)))
    except (TypeError, ValueError):
        return 0.0


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


def compact_rubric(leaves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for leaf in leaves:
        rows.append(
            {
                "leaf_id": str(leaf.get("id") or ""),
                "requirements": str(leaf.get("requirements") or ""),
                "weight": weight_of(leaf),
                "task_category": leaf.get("task_category"),
                "finegrained_task_category": leaf.get("finegrained_task_category"),
            }
        )
    return rows


def build_prompt(
    *,
    paper_id: str,
    title: str,
    leaves: list[dict[str, Any]],
    paper: dict[str, str],
    judge_addendum: str,
    raw_submission: dict[str, Any],
    executed_submission: dict[str, Any],
    reproduction: dict[str, str],
    code_only: bool,
) -> str:
    mode_text = (
        "Code-Dev mode: grade only code development evidence. Ignore missing large-scale execution unless a code leaf explicitly requires runnable smoke support."
        if code_only
        else "Full reproduction mode: grade code, execution, results, and analysis evidence after considering reproduction logs and generated artifacts."
    )
    schema = {
        "leaves": [
            {
                "leaf_id": "leaf identifier from rubric",
                "score": 0.0,
                "rationale": "short explanation grounded in concrete submission evidence",
                "evidence_paths": ["relative/path.py"],
                "invalid_reason": "",
            }
        ],
        "overall_rationale": "short overall assessment",
    }
    return (
        "You are an expert judge for a paper reproduction task. Grade strictly using the hidden rubric leaves. "
        "Do not reward generic claims, keyword stuffing, or text that merely restates the paper. "
        "A score of 1.0 means the submission gives concrete, relevant, and mostly correct evidence for the leaf. "
        "A score of 0.5 means partial, plausible evidence exists but is incomplete or only small-scale. "
        "A score of 0.0 means the evidence is missing, unsupported, or unrelated. "
        "Use reproduction failures as negative evidence for execution/result leaves, but still award code-development credit when the implementation is clear. "
        f"{mode_text} Return JSON only with this schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"PAPER ID: {paper_id}\nTITLE: {title}\n\n"
        f"PUBLIC ADDENDUM:\n{paper['addendum']}\n\n"
        f"BLACKLIST SUMMARY:\n{paper['blacklist']}\n\n"
        f"HIDDEN JUDGE ADDENDUM:\n{judge_addendum[:8000]}\n\n"
        f"RUBRIC LEAVES TO SCORE:\n{json.dumps(compact_rubric(leaves), ensure_ascii=False, indent=2)}\n\n"
        f"PAPER MARKDOWN EXCERPT:\n{paper['paper_md']}\n\n"
        f"RAW SUBMISSION FILES:\n{json.dumps(raw_submission.get('files', []), ensure_ascii=False, indent=2)[:12000]}\n\n"
        f"RAW SUBMISSION TEXT SNIPPETS:\n{raw_submission.get('text', '')[:25000]}\n\n"
        f"EXECUTED SUBMISSION FILES:\n{json.dumps(executed_submission.get('files', []), ensure_ascii=False, indent=2)[:16000]}\n\n"
        f"EXECUTED SUBMISSION TEXT SNIPPETS:\n{executed_submission.get('text', '')[:45000]}\n\n"
        f"REPRODUCTION SUMMARY:\n{json.dumps(reproduction, ensure_ascii=False, indent=2)[:16000]}\n"
    )


def call_llm(prompt: str) -> dict[str, Any]:
    mock = os.environ.get("PAPERBENCH_JUDGE_MOCK_RESPONSE")
    if mock:
        return extract_json_object(mock)
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

    # Keep one request only: retrying the same oversized request after a timeout
    # doubles verifier latency and turns infrastructure failures into task zeros.
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
    for row in raw:
        if not isinstance(row, dict):
            invalid_count += 1
            continue
        leaf_id = str(row.get("leaf_id") or row.get("id") or "")
        if leaf_id not in valid_ids:
            invalid_count += 1
            continue
        score = clipped(row.get("score"))
        scores[leaf_id] = score
        details.append(
            {
                "leaf_id": leaf_id,
                "score": score,
                "rationale": str(row.get("rationale") or "")[:2000],
                "evidence_paths": row.get("evidence_paths") if isinstance(row.get("evidence_paths"), list) else [],
                "invalid_reason": str(row.get("invalid_reason") or "")[:1000],
            }
        )
    return scores, details, invalid_count


def grade(args: argparse.Namespace) -> int:
    rubric = read_json(args.rubric)
    leaves = filtered_leaves(rubric, code_only=args.code_only)
    total_weight = sum(weight_of(leaf) for leaf in leaves)
    reproduction_ran = clipped(args.reproduction_ran)
    reproduction_success = clipped(args.reproduction_success)
    details_base = {
        "paper_id": "",
        "title": "",
        "code_only": bool(args.code_only),
        "leaf_count": len(leaves),
        "rubric_weight_total": total_weight,
        "reproduction_ran": reproduction_ran,
        "reproduction_success": reproduction_success,
    }
    try:
        config = read_json(args.judge_config)
        details_base["paper_id"] = str(config.get("paper_id") or "")
        details_base["title"] = str(config.get("title") or "")
    except Exception:
        config = {}
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
            code_only=args.code_only,
        )
        write_json(args.reward_json, reward)
        write_json(args.details_json, {**details_base, "ok": True, "reason": "missing submission directory", "leaves": []})
        return 0
    readme = args.submission_dir / "README.md"
    reproduce = args.submission_dir / "reproduce.sh"
    format_score = 1.0 if readme.is_file() and reproduce.is_file() else 0.25
    raw_submission = collect_submission(args.submission_dir)
    executed_submission = collect_submission(args.executed_submission_dir)
    paper = paper_excerpt(args.paper_dir)
    judge_addendum = read_text(args.judge_addendum, 20000)
    repro = reproduction_summary(args.reproduction_dir)
    try:
        prompt = build_prompt(
            paper_id=str(config.get("paper_id") or ""),
            title=str(config.get("title") or ""),
            leaves=leaves,
            paper=paper,
            judge_addendum=judge_addendum,
            raw_submission=raw_submission,
            executed_submission=executed_submission,
            reproduction=repro,
            code_only=args.code_only,
        )
        parsed = call_llm(prompt)
        scores, leaf_details, invalid_count = parse_leaf_scores(parsed, leaves)
        weighted = 0.0
        if total_weight <= 0:
            weighted = sum(scores.get(str(leaf.get("id") or ""), 0.0) for leaf in leaves) / max(1, len(leaves))
        else:
            for leaf in leaves:
                weighted += (weight_of(leaf) / total_weight) * scores.get(str(leaf.get("id") or ""), 0.0)
        score = clipped(weighted) * format_score
        reward = numeric_reward(
            score=score,
            format_score=format_score,
            submission_present=1.0,
            judge_available=1.0,
            llm_parse_ok=1.0,
            leaf_count=len(leaves),
            invalid_leaf_count=invalid_count + max(0, len(leaves) - len(scores)),
            rubric_weight_total=total_weight,
            reproduction_ran=reproduction_ran,
            reproduction_success=reproduction_success,
            code_only=args.code_only,
        )
        write_json(args.reward_json, reward)
        write_json(
            args.details_json,
            {
                **details_base,
                "ok": True,
                "score": score,
                "format_score": format_score,
                "judge": parsed,
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
            code_only=args.code_only,
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
