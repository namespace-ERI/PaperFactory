#!/bin/bash
set -u

TESTS_DIR="${HARBOR_TESTS_DIR:-/tests}"
WORKSPACE_DIR="${HARBOR_WORKSPACE_DIR:-/workspace}"
LOGS_DIR="${HARBOR_LOGS_DIR:-/logs/verifier}"
REPRODUCTION_ENABLED="1"
REPRODUCTION_TIMEOUT_SEC="${PAPERBENCH_REPRODUCTION_TIMEOUT_SEC:-900}"

mkdir -p "${LOGS_DIR}"
SUBMISSION_DIR="${WORKSPACE_DIR}/submission"
PAPER_DIR="${WORKSPACE_DIR}/paper"
REPRO_DIR="${LOGS_DIR}/reproduction"
EXECUTED_SUBMISSION_DIR="${REPRO_DIR}/workspace/submission"
rm -rf "${REPRO_DIR}"
mkdir -p "${EXECUTED_SUBMISSION_DIR}"

python - <<'PY' "${LOGS_DIR}/preflight.json" "${PAPER_DIR}" "${SUBMISSION_DIR}" "${REPRODUCTION_TIMEOUT_SEC}"
import json, os, sys
from pathlib import Path

out = Path(sys.argv[1])
paper = Path(sys.argv[2])
submission = Path(sys.argv[3])
payload = {
    "paper_dir": str(paper),
    "paper_dir_exists": paper.is_dir(),
    "paper_pdf_exists": (paper / "paper.pdf").is_file(),
    "paper_markdown_exists": (paper / "paper.md").is_file(),
    "addendum_exists": (paper / "addendum.md").is_file(),
    "submission_dir": str(submission),
    "submission_dir_exists": submission.is_dir(),
    "readme_exists": (submission / "README.md").is_file(),
    "reproduce_sh_exists": (submission / "reproduce.sh").is_file(),
    "judge_api_key_available": bool(os.environ.get("JUDGE_LLM_API_KEY")),
    "judge_base_url_available": bool(os.environ.get("JUDGE_LLM_BASE_URL")),
    "reproduction_timeout_sec": int(sys.argv[4]),
}
payload["ready"] = all(
    payload[key]
    for key in (
        "paper_dir_exists",
        "paper_pdf_exists",
        "paper_markdown_exists",
        "addendum_exists",
        "submission_dir_exists",
        "readme_exists",
        "reproduce_sh_exists",
        "judge_api_key_available",
        "judge_base_url_available",
    )
)
out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

REPRO_RAN=0
REPRO_SUCCESS=0
if [ -d "${SUBMISSION_DIR}" ]; then
  cp -a "${SUBMISSION_DIR}/." "${EXECUTED_SUBMISSION_DIR}/" 2>/dev/null || true
  if [ "${REPRODUCTION_ENABLED}" = "1" ] && [ -f "${EXECUTED_SUBMISSION_DIR}/reproduce.sh" ]; then
    REPRO_RAN=1
    chmod +x "${EXECUTED_SUBMISSION_DIR}/reproduce.sh" 2>/dev/null || true
    (
      cd "${EXECUTED_SUBMISSION_DIR}"
      timeout "${REPRODUCTION_TIMEOUT_SEC}" bash ./reproduce.sh \
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
  --submission-dir "${SUBMISSION_DIR}" \
  --executed-submission-dir "${EXECUTED_SUBMISSION_DIR}" \
  --paper-dir "${PAPER_DIR}" \
  --rubric "${TESTS_DIR}/rubric.json" \
  --judge-addendum "${TESTS_DIR}/judge.addendum.md" \
  --judge-config "${TESTS_DIR}/judge_config.json" \
  --reproduction-dir "${REPRO_DIR}" \
   \
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
