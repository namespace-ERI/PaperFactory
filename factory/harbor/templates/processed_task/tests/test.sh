#!/bin/bash
set -u

TESTS_DIR="${HARBOR_TESTS_DIR:-/tests}"
WORKSPACE_DIR="${HARBOR_WORKSPACE_DIR:-/workspace}"
LOGS_DIR="${HARBOR_LOGS_DIR:-/logs/verifier}"
CODE_ONLY="$(python -c 'import json,sys; print(1 if json.load(open(sys.argv[1], encoding="utf-8")).get("code_only") else 0)' "${TESTS_DIR}/judge_config.json")"
REPRODUCTION_ENABLED="$([ "${CODE_ONLY}" = "1" ] && echo 0 || echo 1)"
REPRODUCTION_TIMEOUT_SEC="${PAPERBENCH_REPRODUCTION_TIMEOUT_SEC:-604800}"

mkdir -p "${LOGS_DIR}"
SUBMISSION_DIR="${WORKSPACE_DIR}/submission"
PAPER_DIR="${HARBOR_PAPER_DIR:-${TESTS_DIR}/paper}"
REPRO_DIR="${LOGS_DIR}/reproduction"
CLEAN_SUBMISSION_DIR="${REPRO_DIR}/clean/workspace/submission"
EXECUTED_SUBMISSION_DIR="${REPRO_DIR}/executed/workspace/submission"
rm -rf "${REPRO_DIR}"
mkdir -p "${REPRO_DIR}"

python - <<'PY' "${LOGS_DIR}/preflight.json" "${PAPER_DIR}" "${SUBMISSION_DIR}" "${REPRODUCTION_TIMEOUT_SEC}" "${CODE_ONLY}"
import json, os, shutil, subprocess, sys
from pathlib import Path

MAX_COMMITTED_BYTES = 1_000_000_000

out = Path(sys.argv[1])
paper = Path(sys.argv[2])
submission = Path(sys.argv[3])


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(submission), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def succeeded(result: subprocess.CompletedProcess[bytes]) -> bool:
    return result.returncode == 0


git_available = shutil.which("git") is not None
inside = git("rev-parse", "--is-inside-work-tree") if git_available else None
repo_root = git("rev-parse", "--show-toplevel") if git_available else None
head = git("rev-parse", "--verify", "HEAD") if git_available else None
status = git("status", "--porcelain=v1", "-z", "--untracked-files=no") if git_available else None
readme_tracked = git("ls-files", "--error-unmatch", "--", "README.md") if git_available else None
reproduce_tracked = git("ls-files", "--error-unmatch", "--", "reproduce.sh") if git_available else None
tree = git("ls-tree", "-rlz", "--full-tree", "HEAD") if git_available else None

committed_bytes = 0
committed_file_count = 0
if tree is not None and succeeded(tree):
    for record in tree.stdout.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        header, _path = record.split(b"\t", 1)
        fields = header.split()
        if len(fields) >= 4 and fields[1] == b"blob":
            try:
                committed_bytes += int(fields[3])
                committed_file_count += 1
            except ValueError:
                pass

try:
    resolved_submission = submission.resolve()
    resolved_repo_root = Path(repo_root.stdout.decode().strip()).resolve() if repo_root is not None and succeeded(repo_root) else None
except OSError:
    resolved_submission = submission
    resolved_repo_root = None

payload = {
    "code_only": sys.argv[5] == "1",
    "paper_dir": str(paper),
    "paper_dir_exists": paper.is_dir(),
    "paper_pdf_exists": (paper / "paper.pdf").is_file(),
    "paper_markdown_exists": (paper / "paper.md").is_file(),
    "addendum_exists": (paper / "addendum.md").is_file(),
    "submission_dir": str(submission),
    "submission_dir_exists": submission.is_dir(),
    "readme_exists": (submission / "README.md").is_file(),
    "reproduce_sh_exists": (submission / "reproduce.sh").is_file(),
    "git_available": git_available,
    "git_is_repo": bool(inside is not None and succeeded(inside) and inside.stdout.strip() == b"true"),
    "git_repo_root_matches_submission": resolved_repo_root == resolved_submission,
    "git_head_exists": bool(head is not None and succeeded(head)),
    "readme_tracked": bool(readme_tracked is not None and succeeded(readme_tracked)),
    "reproduce_sh_tracked": bool(reproduce_tracked is not None and succeeded(reproduce_tracked)),
    "tracked_worktree_clean": bool(status is not None and succeeded(status) and not status.stdout),
    "committed_file_count": committed_file_count,
    "committed_bytes": committed_bytes,
    "max_committed_bytes": MAX_COMMITTED_BYTES,
    "committed_size_ok": bool(tree is not None and succeeded(tree) and committed_bytes <= MAX_COMMITTED_BYTES),
    "judge_api_key_available": bool(os.environ.get("JUDGE_LLM_API_KEY")),
    "judge_base_url_available": bool(os.environ.get("JUDGE_LLM_BASE_URL")),
    "reproduction_timeout_sec": int(sys.argv[4]),
}
required_submission_checks = [
    "submission_dir_exists",
    "readme_exists",
    "git_available",
    "git_is_repo",
    "git_repo_root_matches_submission",
    "git_head_exists",
    "readme_tracked",
    "tracked_worktree_clean",
    "committed_size_ok",
]
if not payload["code_only"]:
    required_submission_checks.extend(("reproduce_sh_exists", "reproduce_sh_tracked"))
payload["submission_valid"] = all(payload[key] for key in required_submission_checks)
payload["ready"] = payload["submission_valid"] and all(
    payload[key]
    for key in (
        "paper_dir_exists",
        "paper_pdf_exists",
        "paper_markdown_exists",
        "addendum_exists",
        "judge_api_key_available",
        "judge_base_url_available",
    )
)
out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

AGENT_TRAJECTORY="${HARBOR_AGENT_TRAJECTORY_PATH:-/logs/agent/trajectory.json}"
python - <<'PY' "${LOGS_DIR}/monitor.json" "${PAPER_DIR}/blacklist.txt" "${AGENT_TRAJECTORY}"
import json, re, sys
from pathlib import Path

out = Path(sys.argv[1])
blacklist_path = Path(sys.argv[2])
trajectory_path = Path(sys.argv[3])

blacklist = []
if blacklist_path.is_file():
    blacklist = [
        line.strip()
        for line in blacklist_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.startswith("#")
    ]
if blacklist == ["none"]:
    blacklist = []


def normalize_url(value: str) -> str:
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"[?#].*$", "", value)
    return value.strip()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


violations = []
trajectory_error = ""
trajectory = None
if trajectory_path.is_file():
    try:
        trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    except Exception as exc:
        trajectory_error = f"{type(exc).__name__}: {exc}"

seen = set()
if isinstance(trajectory, dict) and blacklist:
    stack = [trajectory]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            calls = value.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    command_text = "\n".join(strings(call.get("arguments")))
                    if not any(command in command_text for command in ("git clone", "curl", "wget")):
                        continue
                    for blocked in blacklist:
                        if normalize_url(blocked) not in command_text:
                            continue
                        key = (
                            str(value.get("step_id") or ""),
                            str(call.get("tool_call_id") or ""),
                            blocked,
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        violations.append(
                            {
                                "step_id": value.get("step_id"),
                                "tool_call_id": call.get("tool_call_id"),
                                "function_name": call.get("function_name"),
                                "violation": blocked,
                            }
                        )
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)

payload = {
    "monitor": "PaperBench BasicMonitor adapted to Harbor ATIF tool calls",
    "trajectory_path": str(trajectory_path),
    "trajectory_available": trajectory_path.is_file(),
    "trajectory_parse_error": trajectory_error,
    "blacklist_entries": len(blacklist),
    "violations": violations,
    "flagged": bool(violations),
}
out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

SUBMISSION_VALID="$(python -c 'import json,sys; print(1 if json.load(open(sys.argv[1], encoding="utf-8")).get("submission_valid") else 0)' "${LOGS_DIR}/preflight.json")"
MONITOR_FLAGGED="$(python -c 'import json,sys; print(1 if json.load(open(sys.argv[1], encoding="utf-8")).get("flagged") else 0)' "${LOGS_DIR}/monitor.json")"
REPRO_RAN=0
REPRO_SUCCESS=0
if [ "${SUBMISSION_VALID}" = "1" ] && [ "${MONITOR_FLAGGED}" = "0" ]; then
  mkdir -p "${CLEAN_SUBMISSION_DIR}"
  cp -a "${SUBMISSION_DIR}/." "${CLEAN_SUBMISSION_DIR}/"
  git -C "${CLEAN_SUBMISSION_DIR}" clean -fd \
    > "${REPRO_DIR}/git_clean.stdout.txt" 2> "${REPRO_DIR}/git_clean.stderr.txt"
  GIT_CLEAN_RC=$?
  echo "${GIT_CLEAN_RC}" > "${REPRO_DIR}/git_clean.exit_code.txt"
  if [ "${GIT_CLEAN_RC}" -ne 0 ]; then
    SUBMISSION_VALID=0
    python - <<'PY' "${LOGS_DIR}/preflight.json" "${GIT_CLEAN_RC}"
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["submission_valid"] = False
payload["ready"] = False
payload["git_clean_exit_code"] = int(sys.argv[2])
payload["git_clean_ok"] = False
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  else
    python - <<'PY' "${LOGS_DIR}/preflight.json"
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["git_clean_exit_code"] = 0
payload["git_clean_ok"] = True
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  fi
fi

if [ "${MONITOR_FLAGGED}" = "1" ]; then
  echo "monitor_disqualified" > "${REPRO_DIR}/exit_code.txt"
elif [ "${SUBMISSION_VALID}" = "1" ]; then
  mkdir -p "${EXECUTED_SUBMISSION_DIR}"
  cp -a "${CLEAN_SUBMISSION_DIR}/." "${EXECUTED_SUBMISSION_DIR}/"
  if [ "${REPRODUCTION_ENABLED}" = "1" ] && [ -f "${EXECUTED_SUBMISSION_DIR}/reproduce.sh" ]; then
    REPRO_RAN=1
    chmod +x "${EXECUTED_SUBMISSION_DIR}/reproduce.sh" 2>/dev/null || true
    date +%s > "${EXECUTED_SUBMISSION_DIR}/reproduce.log.creation_time"
    (
      cd "${EXECUTED_SUBMISSION_DIR}"
      # Match official PaperBench reproduction: copied virtual environments
      # are discarded and the interleaved stdout/stderr stream is persisted.
      rm -rf venv .venv
      timeout "${REPRODUCTION_TIMEOUT_SEC}" bash -o pipefail -c \
        'bash ./reproduce.sh 2>&1 | tee reproduce.log' \
        > "${REPRO_DIR}/stdout.txt" 2> "${REPRO_DIR}/stderr.txt"
    )
    REPRO_RC=$?
    echo "${REPRO_RC}" > "${REPRO_DIR}/exit_code.txt"
    if [ "${REPRO_RC}" -eq 0 ]; then
      REPRO_SUCCESS=1
    fi
  else
    echo "not_run" > "${REPRO_DIR}/exit_code.txt"
  fi
else
  echo "invalid_submission" > "${REPRO_DIR}/exit_code.txt"
fi

python - <<'PY' "${REPRO_DIR}/file_manifest.json" "${EXECUTED_SUBMISSION_DIR}"
import hashlib, json, os, sys
from pathlib import Path
out = Path(sys.argv[1])
root = Path(sys.argv[2])
rows = []
if root.is_dir():
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        digest = hashlib.sha256()
        try:
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
        except OSError:
            continue
        rows.append({'path': rel, 'bytes': size, 'sha256': digest.hexdigest()})
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({'files': rows}, indent=2, sort_keys=True) + '\n', encoding='utf-8')
PY

python "${TESTS_DIR}/llm_rubric_judge.py" \
  --submission-dir "${CLEAN_SUBMISSION_DIR}" \
  --executed-submission-dir "${EXECUTED_SUBMISSION_DIR}" \
  --paper-dir "${PAPER_DIR}" \
  --rubric "${TESTS_DIR}/rubric.json" \
  --judge-addendum "${TESTS_DIR}/judge.addendum.md" \
  --judge-config "${TESTS_DIR}/judge_config.json" \
  --reproduction-dir "${REPRO_DIR}" \
  --submission-validation "${LOGS_DIR}/preflight.json" \
  --monitor-result "${LOGS_DIR}/monitor.json" \
  --reproduction-ran "${REPRO_RAN}" \
  --reproduction-success "${REPRO_SUCCESS}" \
  --reward-json "${LOGS_DIR}/reward.json" \
  --details-json "${LOGS_DIR}/paperbench_judge_details.json"

if [ ! -f "${LOGS_DIR}/reward.json" ]; then
  echo '{"score":0.0,"weighted_score":0.0,"reward":0.0,"paperbench_score":0.0,"format":0.0,"submission_present":0.0,"judge_available":0.0,"llm_parse_ok":0.0,"leaf_count":0.0,"invalid_leaf_count":0.0,"rubric_weight_total":0.0,"reproduction_ran":0.0,"reproduction_success":0.0,"code_only":0.0}' > "${LOGS_DIR}/reward.json"
fi

python - <<'PY' "${LOGS_DIR}/reward.json" "${LOGS_DIR}/reward.txt"
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
score = float(payload.get('score', payload.get('reward', 0.0)) or 0.0)
Path(sys.argv[2]).write_text(f'{score}\n', encoding='utf-8')
PY

exit 0
