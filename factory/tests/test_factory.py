from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


FACTORY = Path(__file__).resolve().parents[1]
FACTORY_SCRIPT = FACTORY / "build_paperbench.py"
TASK_SCRIPT = FACTORY / "task" / "build_tasks.py"
PUBLISH_SCRIPT = FACTORY / "rubrics" / "publish_rubric.py"
JUDGE_TEMPLATE = (
    FACTORY / "harbor" / "templates" / "processed_task" / "tests" / "llm_rubric_judge.py"
)
VERIFIER_TEMPLATE = (
    FACTORY / "harbor" / "templates" / "processed_task" / "tests" / "test.sh"
)
sys.path.insert(0, str(FACTORY / "rubrics"))

from rubric_lib import paperbench_code_only_rubric, validate_rubric  # noqa: E402


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def valid_tree() -> dict:
    return {
        "id": "root",
        "requirements": "The scoped core contribution has been reproduced.",
        "weight": 1,
        "sub_tasks": [
            {
                "id": "method",
                "requirements": "The scoped method has been implemented and evaluated.",
                "weight": 3,
                "sub_tasks": [
                    {
                        "id": "method-implementation",
                        "requirements": "The implementation contains the method defined in Section 3.",
                        "weight": 3,
                        "sub_tasks": [],
                        "task_category": "Code Development",
                        "finegrained_task_category": "Method Implementation",
                    },
                    {
                        "id": "main-execution",
                        "requirements": "The main experiment from Table 1 is executed by reproduce.sh.",
                        "weight": 2,
                        "sub_tasks": [],
                        "task_category": "Code Execution",
                        "finegrained_task_category": "Evaluation, Metrics & Benchmarking",
                    },
                    {
                        "id": "main-trend",
                        "requirements": "The generated results show Method X outperforming Baseline Y.",
                        "weight": 3,
                        "sub_tasks": [],
                        "task_category": "Result Analysis",
                        "finegrained_task_category": "Evaluation, Metrics & Benchmarking",
                    },
                ],
                "task_category": None,
                "finegrained_task_category": None,
            },
            {
                "id": "interface",
                "requirements": "The reproduction interface is usable.",
                "weight": 1,
                "sub_tasks": [
                    {
                        "id": "entrypoint",
                        "requirements": "The root reproduce.sh runs the scoped workflow.",
                        "weight": 1,
                        "sub_tasks": [],
                        "task_category": "Code Execution",
                        "finegrained_task_category": "Environment & Infrastructure Setup",
                    },
                    {
                        "id": "machine-readable-results",
                        "requirements": "The evaluation writes machine-readable metrics.",
                        "weight": 1,
                        "sub_tasks": [],
                        "task_category": "Code Development",
                        "finegrained_task_category": "Logging, Analysis & Presentation",
                    },
                ],
                "task_category": None,
                "finegrained_task_category": None,
            },
        ],
        "task_category": None,
        "finegrained_task_category": None,
    }


class RubricValidationTests(unittest.TestCase):
    def test_valid_tree_and_effective_weights(self) -> None:
        report = validate_rubric(valid_tree())
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["stats"]["leaves"], 5)
        self.assertAlmostEqual(
            sum(item["effective_weight"] for item in report["effective_leaf_weights"]),
            1.0,
        )

    def test_duplicate_id_and_bad_internal_category_fail(self) -> None:
        rubric = valid_tree()
        rubric["task_category"] = "Code Development"
        rubric["sub_tasks"][1]["sub_tasks"][0]["id"] = "method-implementation"
        report = validate_rubric(rubric)
        self.assertFalse(report["valid"])
        self.assertTrue(any("duplicate id" in error for error in report["errors"]))
        self.assertTrue(any("internal node" in error for error in report["errors"]))

    def test_code_dev_mode_rejects_execution_and_result_leaves(self) -> None:
        report = validate_rubric(valid_tree(), rubric_mode="code-dev")
        self.assertFalse(report["valid"])
        self.assertTrue(
            any("non-Code Development leaves" in error for error in report["errors"])
        )

    def test_official_code_dev_pruning_preserves_tree_and_local_weights(self) -> None:
        rubric = paperbench_code_only_rubric(valid_tree())
        report = validate_rubric(rubric, rubric_mode="code-dev")
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(
            [node["id"] for node in rubric["sub_tasks"]], ["method", "interface"]
        )
        self.assertEqual([node["weight"] for node in rubric["sub_tasks"]], [3, 1])
        self.assertEqual(
            [node["id"] for node in rubric["sub_tasks"][0]["sub_tasks"]],
            ["method-implementation"],
        )
        self.assertEqual(
            [node["id"] for node in rubric["sub_tasks"][1]["sub_tasks"]],
            ["machine-readable-results"],
        )
        effective = {
            row["id"]: row["effective_weight"]
            for row in report["effective_leaf_weights"]
        }
        self.assertEqual(effective, {"method-implementation": 0.75, "machine-readable-results": 0.25})


class HarborTemplateTests(unittest.TestCase):
    @staticmethod
    def load_judge_module():
        spec = importlib.util.spec_from_file_location("paperbench_judge_template", JUDGE_TEMPLATE)
        if spec is None or spec.loader is None:
            raise AssertionError("cannot load judge template")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_submission_collection_prioritizes_core_files(self) -> None:
        module = self.load_judge_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git" / "objects").mkdir(parents=True)
            for index in range(220):
                (root / f"artifact-{index:03d}.bin").write_bytes(b"x")
            (root / ".git" / "config").write_text("ignored", encoding="utf-8")
            (root / "README.md").write_text("core readme", encoding="utf-8")
            (root / "reproduce.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "method.py").write_text("def method(): pass\n", encoding="utf-8")
            collected = module.collect_submission(root)
            paths = [row["path"] for row in collected["files"]]
            self.assertEqual(paths[:2], ["README.md", "reproduce.sh"])
            self.assertIn("src/method.py", paths)
            self.assertNotIn(".git/config", paths)
            self.assertEqual(len(paths), 200)

    def test_binary_leaf_scores_and_recursive_tree_weighting(self) -> None:
        module = self.load_judge_module()
        rubric = valid_tree()
        leaves = module.filtered_leaves(rubric, code_only=False)
        parsed = {
            "leaves": [
                {"leaf_id": "method-implementation", "score": 1},
                {"leaf_id": "main-execution", "score": 0},
                {"leaf_id": "main-trend", "score": 0.5},
                {"leaf_id": "entrypoint", "score": 0},
                {"leaf_id": "machine-readable-results", "score": 0},
            ]
        }
        scores, details, invalid_count = module.parse_leaf_scores(parsed, leaves)
        self.assertEqual(invalid_count, 1)
        self.assertEqual(scores["main-trend"], 0.0)
        self.assertEqual(
            next(row for row in details if row["leaf_id"] == "main-trend")["invalid_reason"],
            "leaf score must be exactly 0 or 1",
        )
        # Official recursive aggregation: root method branch 3/4, then the
        # implementation leaf 3/8 within that branch => 9/32.  Flattening the
        # leaf-local weights would incorrectly produce 3/10.
        score = module.score_rubric_tree(rubric, scores, code_only=False)
        self.assertAlmostEqual(score, 9 / 32)
        self.assertNotAlmostEqual(score, 3 / 10)

    def test_code_dev_prompt_excludes_execution_and_result_evidence(self) -> None:
        module = self.load_judge_module()
        prompt = module.build_leaf_prompt(
            paper_id="fixture",
            title="Fixture",
            leaf={
                "id": "implementation",
                "requirements": "Implement the method.",
                "weight": 1,
                "task_category": "Code Development",
                "finegrained_task_category": "Method Implementation",
            },
            ancestor_requirements=["Implement the scoped contribution."],
            paper={"paper_md": "method", "addendum": "scope", "blacklist": ""},
            judge_addendum="judge code only",
            raw_submission={"files": [], "text": "source code"},
            executed_submission={"files": [], "text": "must not appear"},
            reproduction={"stdout": "must not appear"},
            code_only=True,
        )
        self.assertIn("grade exclusively from the committed source-code evidence", prompt)
        self.assertIn("RAW SUBMISSION", prompt)
        self.assertNotIn("EXECUTED SUBMISSION", prompt)
        self.assertNotIn("REPRODUCTION SUMMARY", prompt)
        self.assertNotIn("Use reproduction failures", prompt)
        self.assertNotIn("must not appear", prompt)

    def test_judge_uses_one_request_per_leaf_and_isolates_failures(self) -> None:
        module = self.load_judge_module()
        rubric = valid_tree()
        requested: list[str] = []
        original_call_llm = module.call_llm

        def fake_call_llm(prompt: str, *, leaf_id: str = "") -> dict:
            requested.append(leaf_id)
            self.assertIn(f"EXPECTED LEAF ID: {leaf_id}", prompt)
            if leaf_id == "main-execution":
                raise TimeoutError("fixture timeout")
            return {"leaf_id": leaf_id, "score": 1, "rationale": "fixture"}

        module.call_llm = fake_call_llm
        try:
            scores, details, summary = module.grade_leaf_requests(
                rubric=rubric,
                paper_id="fixture",
                title="Fixture",
                paper={"paper_md": "paper", "addendum": "scope", "blacklist": ""},
                judge_addendum="",
                raw_submission={"files": [], "text": "source"},
                executed_submission={"files": [], "text": "results"},
                reproduction={"stdout": "ran"},
                code_only=False,
                max_workers=3,
            )
        finally:
            module.call_llm = original_call_llm

        leaves = module.filtered_leaves(rubric, code_only=False)
        self.assertCountEqual(requested, [leaf["id"] for leaf in leaves])
        self.assertEqual(summary["request_count"], len(leaves))
        self.assertEqual(summary["request_success_count"], len(leaves) - 1)
        self.assertEqual(summary["parse_success_count"], len(leaves) - 1)
        self.assertEqual(summary["max_workers"], 3)
        self.assertEqual(scores["main-execution"], 0.0)
        self.assertEqual(scores["method-implementation"], 1.0)
        failed = next(row for row in details if row["leaf_id"] == "main-execution")
        self.assertFalse(failed["judge_available"])
        self.assertIn("TimeoutError", failed["invalid_reason"])

    def test_verifier_cleans_untracked_files_and_rejects_dirty_tracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests_dir = root / "tests"
            shutil.copytree(VERIFIER_TEMPLATE.parent, tests_dir)
            dump(tests_dir / "rubric.json", valid_tree())
            dump(tests_dir / "judge_config.json", {"paper_id": "fixture", "title": "Fixture"})
            (tests_dir / "judge.addendum.md").write_text("", encoding="utf-8")

            workspace = root / "home"
            paper = workspace / "paper"
            submission = workspace / "submission"
            paper.mkdir(parents=True)
            submission.mkdir()
            (paper / "paper.pdf").write_bytes(b"%PDF-1.4\n")
            (paper / "paper.md").write_text("# Fixture\n", encoding="utf-8")
            (paper / "addendum.md").write_text("# Scope\n", encoding="utf-8")
            (submission / "README.md").write_text("# Reproduction\n", encoding="utf-8")
            (submission / "reproduce.sh").write_text(
                "#!/bin/bash\nmkdir -p results\nprintf 'ok\\n' > results/metrics.txt\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(submission), "init", "-q"], check=True)
            subprocess.run(
                ["git", "-C", str(submission), "config", "user.email", "fixture@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(submission), "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(["git", "-C", str(submission), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(submission), "commit", "-qm", "fixture"],
                check=True,
            )
            (submission / "untracked-secret.txt").write_text("must not be scored\n", encoding="utf-8")

            mock_response = {
                "leaves": [
                    {"leaf_id": leaf["id"], "score": 1, "rationale": "fixture"}
                    for leaf in [
                        item
                        for branch in valid_tree()["sub_tasks"]
                        for item in branch["sub_tasks"]
                    ]
                ]
            }
            env = {
                **os.environ,
                "HARBOR_TESTS_DIR": str(tests_dir),
                "HARBOR_WORKSPACE_DIR": str(workspace),
                "HARBOR_LOGS_DIR": str(root / "logs-valid"),
                "JUDGE_LLM_API_KEY": "fixture-key",
                "JUDGE_LLM_BASE_URL": "http://judge.invalid/v1",
                "PAPERBENCH_REPRODUCTION_TIMEOUT_SEC": "10",
                "PAPERBENCH_JUDGE_MOCK_RESPONSE": json.dumps(mock_response),
            }
            verifier = subprocess.run(
                ["bash", str(VERIFIER_TEMPLATE)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verifier.returncode, 0, verifier.stdout + verifier.stderr)
            valid_preflight = load_json(root / "logs-valid" / "preflight.json")
            self.assertTrue(valid_preflight["submission_valid"])
            self.assertTrue(valid_preflight["git_clean_ok"])
            self.assertTrue(valid_preflight["tracked_worktree_clean"])
            self.assertLessEqual(valid_preflight["committed_bytes"], 1_000_000_000)
            clean_submission = root / "logs-valid" / "reproduction" / "clean" / "home" / "submission"
            executed_submission = root / "logs-valid" / "reproduction" / "executed" / "home" / "submission"
            self.assertFalse((clean_submission / "untracked-secret.txt").exists())
            self.assertFalse((executed_submission / "untracked-secret.txt").exists())
            self.assertTrue((executed_submission / "results" / "metrics.txt").is_file())
            self.assertEqual(load_json(root / "logs-valid" / "reward.json")["score"], 1.0)

            (submission / "README.md").write_text("uncommitted tracked change\n", encoding="utf-8")
            env["HARBOR_LOGS_DIR"] = str(root / "logs-dirty")
            verifier = subprocess.run(
                ["bash", str(VERIFIER_TEMPLATE)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verifier.returncode, 0, verifier.stdout + verifier.stderr)
            dirty_preflight = load_json(root / "logs-dirty" / "preflight.json")
            self.assertFalse(dirty_preflight["tracked_worktree_clean"])
            self.assertFalse(dirty_preflight["submission_valid"])
            self.assertEqual(load_json(root / "logs-dirty" / "reward.json")["score"], 0.0)
            self.assertFalse(
                (root / "logs-dirty" / "reproduction" / "executed" / "home" / "submission").exists()
            )


class EndToEndFactoryTests(unittest.TestCase):
    def test_code_dev_rubric_authoring_and_harbor_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper_id = "code-dev-fixture"
            paper_dir = root / "paper_sources" / paper_id
            design_dir = root / "design" / paper_id
            paper_dir.mkdir(parents=True)
            design_dir.mkdir(parents=True)
            (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n")
            (paper_dir / "paper.md").write_text(
                "# Code Dev Fixture\n\n## Method\nThe paper defines modules A and B.\n",
                encoding="utf-8",
            )
            (paper_dir / "blacklist.txt").write_text("", encoding="utf-8")
            (paper_dir / "assets").mkdir()
            dump(
                design_dir / "task_metadata.json",
                {"paper_id": paper_id, "title": "Code Dev Fixture"},
            )
            dump(
                root / "paperlist.json",
                {"papers": [{"id": paper_id, "title": "Code Dev Fixture"}]},
            )

            code_tree = {
                "id": "root",
                "requirements": "The paper's core method is implemented.",
                "weight": 1,
                "sub_tasks": [
                    {
                        "id": "core-method",
                        "requirements": "The core method modules are implemented.",
                        "weight": 1,
                        "sub_tasks": [
                            {
                                "id": "core-method-module-a",
                                "requirements": "Module A implements the transformation defined in the Method section.",
                                "weight": 2,
                                "sub_tasks": [],
                                "task_category": "Code Development",
                                "finegrained_task_category": "Method Implementation",
                            },
                            {
                                "id": "core-method-module-b",
                                "requirements": "Module B connects the paper's stated inputs to Module A.",
                                "weight": 1,
                                "sub_tasks": [],
                                "task_category": "Code Development",
                                "finegrained_task_category": "Method Implementation",
                            },
                            {
                                "id": "core-method-execution",
                                "requirements": "The main experiment executes the implemented method.",
                                "weight": 1,
                                "sub_tasks": [],
                                "task_category": "Code Execution",
                                "finegrained_task_category": "Experimental Setup",
                            },
                            {
                                "id": "core-method-result",
                                "requirements": "The generated metric has the paper's reported trend.",
                                "weight": 1,
                                "sub_tasks": [],
                                "task_category": "Result Analysis",
                                "finegrained_task_category": "Evaluation, Metrics & Benchmarking",
                            },
                        ],
                        "task_category": None,
                        "finegrained_task_category": None,
                    }
                ],
                "task_category": None,
                "finegrained_task_category": None,
            }
            mock = root / "mock"
            dump(
                mock / f"{paper_id}.elements-001.json",
                {
                    "claims": [],
                    "method_components": [],
                    "experiments": [],
                    "resources": [],
                    "ambiguities": [],
                },
            )
            dump(
                mock / f"{paper_id}.matrix.json",
                {
                    "paper_id": paper_id,
                    "contributions": [],
                    "reproduction_contract": {},
                    "unresolved_questions": [],
                },
            )
            addendum = """# Scope
Implement the core method modules described in the paper.

# Approved adaptations
Equivalent source-code organization is allowed.

# Required comparisons and evidence
Concrete committed source code is required; runtime outputs are not required.

# Clarifications
The code will not be executed during grading.

# Out of scope
Experiment execution and result reproduction are out of scope.
"""
            dump(
                mock / f"{paper_id}.addendum.json",
                {"addendum_markdown": addendum, "unresolved_questions": []},
            )
            tree_plan = {
                "root": {
                    "id": "root",
                    "requirements": code_tree["requirements"],
                    "branches": [
                        {
                            "id": "core-method",
                            "requirements": code_tree["sub_tasks"][0]["requirements"],
                            "weight": 1,
                            "contribution_ids": [],
                            "paper_sources": ["Method"],
                            "evidence_groups": ["method implementation"],
                            "leaf_budget": 4,
                        }
                    ],
                },
                "coverage": [],
                "unresolved_questions": [],
                "possible_double_counting": [],
            }
            dump(mock / f"{paper_id}.tree-plan.json", tree_plan)
            dump(
                mock / f"{paper_id}.subtree-core-method.json",
                {
                    "subtree": code_tree["sub_tasks"][0],
                    "coverage": [],
                    "unresolved_questions": [],
                    "possible_double_counting": [],
                },
            )
            dump(
                mock / f"{paper_id}.weighting.json",
                {
                    "weights": [
                        {"node_id": "root", "weight": 1, "rationale": "root"},
                        {"node_id": "core-method", "weight": 1, "rationale": "core"},
                        {"node_id": "core-method-module-a", "weight": 2, "rationale": "primary"},
                        {"node_id": "core-method-module-b", "weight": 1, "rationale": "support"},
                        {"node_id": "core-method-execution", "weight": 1, "rationale": "execution"},
                        {"node_id": "core-method-result", "weight": 1, "rationale": "result"},
                    ],
                    "global_balance": {},
                    "unresolved_questions": [],
                    "warnings": [],
                },
            )
            dump(
                mock / f"{paper_id}.review.json",
                {
                    "blocking_issues": [],
                    "warnings": [],
                    "coverage_gaps": [],
                    "possible_double_counting": [],
                    "unresolved_questions": [],
                    "human_review_checklist": [],
                },
            )

            create_result = subprocess.run(
                [
                    sys.executable,
                    str(FACTORY / "rubrics" / "create_rubrics.py"),
                    "--root",
                    str(root),
                    "--paper",
                    paper_id,
                    "--mock-responses-dir",
                    str(mock),
                    "--rubric-mode",
                    "code-dev",
                    "--repair-rounds",
                    "0",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                create_result.returncode,
                0,
                create_result.stdout + "\n" + create_result.stderr,
            )
            authoring = design_dir / "rubric_authoring"
            rubric = load_json(authoring / "rubric.draft.json")
            full_rubric = load_json(authoring / "rubric.full.draft.json")
            full_report = validate_rubric(full_rubric, rubric_mode="regular")
            self.assertTrue(full_report["valid"], full_report["errors"])
            self.assertEqual(full_report["stats"]["leaves"], 4)
            report = validate_rubric(rubric, rubric_mode="code-dev")
            self.assertTrue(report["valid"], report["errors"])
            self.assertEqual(report["stats"]["leaf_categories"]["Code Development"], 2)
            self.assertEqual(report["stats"]["leaf_categories"]["Code Execution"], 0)
            self.assertEqual(report["stats"]["leaf_categories"]["Result Analysis"], 0)
            self.assertEqual(
                load_json(authoring / "authoring_provenance.json")["rubric_mode"],
                "code-dev",
            )
            self.assertEqual(
                load_json(authoring / "authoring_provenance.json")["authoring_mode"],
                "regular",
            )
            self.assertEqual(
                load_json(authoring / "authoring_provenance.json")["code_dev_derivation"],
                "official-code-development-prune-v1",
            )
            self.assertTrue((authoring / "rubric_tree_plan.json").is_file())
            self.assertTrue((authoring / "rubric_tree_unweighted.json").is_file())
            self.assertTrue((authoring / "rubric_weight_plan.json").is_file())

            # Harbor Code-Dev must also accept a complete regular rubric and
            # derive the same official code-only view during conversion.
            dump(authoring / "rubric.draft.json", full_rubric)
            provenance = load_json(authoring / "authoring_provenance.json")
            provenance["rubric_mode"] = "regular"
            provenance["code_dev_derivation"] = None
            dump(authoring / "authoring_provenance.json", provenance)

            convert_result = subprocess.run(
                [
                    sys.executable,
                    str(FACTORY / "harbor" / "convert_to_harbor.py"),
                    "--root",
                    str(root),
                    "--paper-list",
                    str(root / "paperlist.json"),
                    "--paper",
                    paper_id,
                    "--rubric-mode",
                    "code-dev",
                    "--batch-id",
                    "20260818-120000",
                    "--output-parent",
                    str(root / "papers"),
                    "--timeout-sec",
                    "700",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                convert_result.returncode,
                0,
                convert_result.stdout + "\n" + convert_result.stderr,
            )
            batch = root / "papers" / "20260818-120000"
            manifest = json.loads((batch / "manifest.jsonl").read_text(encoding="utf-8"))
            harbor_task = batch / "harbor_task" / manifest["task_id"]
            instruction = (harbor_task / "instruction.md").read_text(encoding="utf-8")
            self.assertIn("The code will not be executed during grading.", instruction)
            self.assertNotIn("for a maximum runtime of 7 days", instruction)
            judge_config = load_json(harbor_task / "tests" / "judge_config.json")
            self.assertTrue(judge_config["code_only"])
            self.assertEqual(judge_config["rubric_mode"], "code-dev")
            self.assertEqual(judge_config["request_mode"], "per_leaf")
            self.assertEqual(judge_config["max_workers"], 100)
            harbor_rubric = load_json(harbor_task / "tests" / "rubric.json")
            harbor_report = validate_rubric(harbor_rubric, rubric_mode="code-dev")
            self.assertTrue(harbor_report["valid"], harbor_report["errors"])
            self.assertEqual(harbor_report["stats"]["leaves"], 2)
            task_toml = (harbor_task / "task.toml").read_text(encoding="utf-8")
            self.assertIn('paperbench_mode = "llm_code_dev"', task_toml)
            self.assertIn("code_only = true", task_toml)
            self.assertIn('rubric_mode = "code-dev"', task_toml)
            self.assertIn(
                'code_dev_derivation = "official-code-development-prune-v1"',
                task_toml,
            )
            self.assertIn('gpu_tier = "H200"', task_toml)
            self.assertIn("gpu_count = 1", task_toml)
            self.assertEqual(task_toml.count('gpu_types = ["H200"]'), 2)

            workspace = root / "code-dev-home"
            shutil.copytree(harbor_task / "environment" / "paper", workspace / "paper")
            submission = workspace / "submission"
            submission.mkdir()
            (submission / "README.md").write_text("# Code implementation\n", encoding="utf-8")
            (submission / "method.py").write_text("def module_a(value): return value\n", encoding="utf-8")
            (submission / "reproduce.sh").write_text(
                "#!/bin/sh\ntouch REPRODUCE_WAS_RUN\nexit 99\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(submission), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(submission), "config", "user.email", "fixture@example.test"], check=True)
            subprocess.run(["git", "-C", str(submission), "config", "user.name", "Fixture"], check=True)
            subprocess.run(["git", "-C", str(submission), "add", "."], check=True)
            subprocess.run(["git", "-C", str(submission), "commit", "-qm", "fixture"], check=True)
            mock_judge = {
                "leaves": [
                    {"leaf_id": "core-method-module-a", "score": 1},
                    {"leaf_id": "core-method-module-b", "score": 1},
                ]
            }
            logs = root / "code-dev-logs"
            env = {
                **os.environ,
                "HARBOR_TESTS_DIR": str(harbor_task / "tests"),
                "HARBOR_WORKSPACE_DIR": str(workspace),
                "HARBOR_LOGS_DIR": str(logs),
                "JUDGE_LLM_API_KEY": "fixture-key",
                "JUDGE_LLM_BASE_URL": "http://judge.invalid/v1",
                "PAPERBENCH_JUDGE_MOCK_RESPONSE": json.dumps(mock_judge),
            }
            verifier = subprocess.run(
                ["bash", str(harbor_task / "tests" / "test.sh")],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verifier.returncode, 0, verifier.stdout + verifier.stderr)
            preflight = load_json(logs / "preflight.json")
            self.assertTrue(preflight["code_only"])
            self.assertTrue(preflight["submission_valid"])
            self.assertTrue(preflight["reproduce_sh_exists"])
            reward = load_json(logs / "reward.json")
            self.assertEqual(reward["score"], 1.0)
            self.assertEqual(reward["code_only"], 1.0)
            self.assertEqual(reward["reproduction_ran"], 0.0)
            self.assertEqual(
                (logs / "reproduction" / "exit_code.txt").read_text(encoding="utf-8").strip(),
                "not_run",
            )
            self.assertFalse(
                (
                    logs
                    / "reproduction"
                    / "executed"
                    / "home"
                    / "submission"
                    / "REPRODUCE_WAS_RUN"
                ).exists()
            )

    def test_offline_task_build_and_mock_rubric_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "paper.pdf").write_bytes(b"%PDF-1.4\n% test fixture\n")
            markdown = (
                "# Example Paper\n\n## Abstract\nA scoped method.\n\n"
                "## Introduction\nA main claim.\n\n## Method\nMethod X.\n\n"
                "## Experiments\nTable 1 compares Baseline Y.\n\n"
                + "Evidence sentence.\n" * 100
            )
            (source / "paper.md").write_text(markdown, encoding="utf-8")
            (source / "assets").mkdir()
            (source / "assets" / "figure.png").write_bytes(b"png")
            paper_list = {
                "collection_id": "fixture",
                "papers": [
                    {
                        "id": "example-paper",
                        "title": "Example Paper",
                        "pdf_path": "source/paper.pdf",
                        "markdown_path": "source/paper.md",
                        "assets_path": "source/assets",
                        "official_repo": "https://example.test/official.git",
                        "planned_scope": "Reproduce Method X and Table 1.",
                    }
                ],
            }
            dump(root / "paperlist.json", paper_list)
            subprocess.run(
                [
                    sys.executable,
                    str(TASK_SCRIPT),
                    "--paper-list",
                    str(root / "paperlist.json"),
                    "--output-root",
                    str(root),
                    "--offline",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            paper_dir = root / "paper_sources" / "example-paper"
            self.assertTrue((paper_dir / "paper.pdf").is_file())
            self.assertEqual(
                (paper_dir / "blacklist.txt").read_text(encoding="utf-8").strip(),
                "https://example.test/official.git",
            )
            self.assertTrue((root / "splits" / "fixture.txt").is_file())

            mock = root / "mock"
            dump(
                mock / "example-paper.elements-001.json",
                {
                    "claims": [{"claim": "Method X improves the result.", "source": ["Introduction"], "main_text": True}],
                    "method_components": [{"component": "Method X", "source": ["Section 3"], "required_details": []}],
                    "experiments": [{"name": "Table 1", "source": ["Table 1"], "datasets": [], "baselines": ["Baseline Y"], "metrics": [], "reported_trends": ["X outperforms Y"], "main_text": True}],
                    "resources": [],
                    "ambiguities": [],
                },
            )
            matrix = {
                "paper_id": "example-paper",
                "contributions": [
                    {
                        "id": "method-x",
                        "claim": "Method X improves the result.",
                        "paper_sources": ["Section 3", "Table 1"],
                        "core": True,
                        "method_components": ["Method X"],
                        "experiments": ["Table 1"],
                        "inputs": [],
                        "baselines": ["Baseline Y"],
                        "metrics": [],
                        "expected_evidence": ["machine-readable result"],
                        "expected_trends": ["X outperforms Y"],
                        "scope_decision": "include",
                        "scope_reason": "Main-text claim.",
                    }
                ],
                "reproduction_contract": {},
                "unresolved_questions": [],
            }
            dump(mock / "example-paper.matrix.json", matrix)
            addendum = """# Scope
Reproduce Method X and Table 1.

# Approved adaptations
No adaptation beyond the stated scope is approved.

# Required comparisons and evidence
Compare Method X with Baseline Y and emit machine-readable metrics from reproduce.sh.

# Clarifications
Use the protocol stated in the paper.

# Out of scope
Experiments not listed above are out of scope.
"""
            dump(
                mock / "example-paper.addendum.json",
                {"addendum_markdown": addendum, "unresolved_questions": []},
            )
            tree = valid_tree()
            tree_plan = {
                "root": {
                    "id": tree["id"],
                    "requirements": tree["requirements"],
                    "branches": [
                        {
                            "id": branch["id"],
                            "requirements": branch["requirements"],
                            "weight": branch["weight"],
                            "contribution_ids": ["method-x"],
                            "paper_sources": ["Section 3", "Table 1"],
                            "evidence_groups": ["implementation", "execution", "results"],
                            "leaf_budget": len(branch["sub_tasks"]),
                        }
                        for branch in tree["sub_tasks"]
                    ],
                },
                "coverage": [],
                "unresolved_questions": [],
                "possible_double_counting": [],
            }
            dump(mock / "example-paper.tree-plan.json", tree_plan)
            for branch in tree["sub_tasks"]:
                dump(
                    mock / f"example-paper.subtree-{branch['id']}.json",
                    {
                        "subtree": branch,
                        "coverage": [],
                        "unresolved_questions": [],
                        "possible_double_counting": [],
                    },
                )

            def collect_weights(node: dict) -> list[dict]:
                return [
                    {
                        "node_id": node["id"],
                        "weight": node["weight"],
                        "rationale": "Fixture scientific importance.",
                    }
                ] + [
                    item
                    for child in node["sub_tasks"]
                    for item in collect_weights(child)
                ]

            dump(
                mock / "example-paper.weighting.json",
                {
                    "weights": collect_weights(tree),
                    "global_balance": {},
                    "unresolved_questions": [],
                    "warnings": [],
                },
            )
            dump(
                mock / "example-paper.review.json",
                {
                    "blocking_issues": [],
                    "warnings": [],
                    "coverage_gaps": [],
                    "possible_double_counting": [],
                    "unresolved_questions": [],
                    "human_review_checklist": ["Verify paper fidelity."],
                },
            )
            factory_result = subprocess.run(
                [
                    sys.executable,
                    str(FACTORY_SCRIPT),
                    "--root",
                    str(root),
                    "--paper-list",
                    str(root / "paperlist.json"),
                    "--paper",
                    "example-paper",
                    "--offline",
                    "--mock-responses-dir",
                    str(mock),
                    "--repair-rounds",
                    "0",
                    "--batch-id",
                    "20260817-120000",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                factory_result.returncode,
                0,
                factory_result.stdout + "\n" + factory_result.stderr,
            )
            self.assertLess(
                factory_result.stdout.index("1/3 Build PaperBench tasks"),
                factory_result.stdout.index("2/3 Build PaperBench rubrics"),
            )
            self.assertLess(
                factory_result.stdout.index("2/3 Build PaperBench rubrics"),
                factory_result.stdout.index("3/3 Convert to processed Harbor format"),
            )
            authoring = root / "design" / "example-paper" / "rubric_authoring"
            self.assertTrue((authoring / "rubric.draft.json").is_file())
            self.assertTrue((authoring / "rubric_tree_plan.json").is_file())
            self.assertTrue((authoring / "rubric_tree_unweighted.json").is_file())
            self.assertTrue((authoring / "rubric_weight_plan.json").is_file())
            self.assertTrue((authoring / "rubric_subtrees" / "method.json").is_file())
            self.assertEqual(load_json(authoring / "unresolved_questions.json"), [])

            harbor_batch = root / "papers" / "20260817-120000"
            manifest_rows = [
                json.loads(line)
                for line in (harbor_batch / "manifest.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(len(manifest_rows), 1)
            harbor_task = harbor_batch / "harbor_task" / manifest_rows[0]["task_id"]
            self.assertTrue((harbor_task / "task.toml").is_file())
            self.assertTrue((harbor_task / "resource_metadata.json").is_file())
            self.assertTrue((harbor_task / "tests" / "rubric.json").is_file())
            self.assertTrue((harbor_task / "environment" / "paper" / "paper.pdf").is_file())
            instruction = (harbor_task / "instruction.md").read_text(encoding="utf-8")
            official_instruction = (
                FACTORY / "harbor" / "templates" / "instructions.official.txt"
            ).read_text(encoding="utf-8")
            self.assertEqual(
                instruction,
                official_instruction.replace("NVIDIA A10 GPU", "NVIDIA H200 GPU"),
            )
            self.assertIn("/home/paper", instruction)
            self.assertIn("/home/submission", instruction)
            self.assertIn("for a maximum runtime of 7 days", instruction)
            self.assertIn("NVIDIA H200 GPU", instruction)
            self.assertNotIn("NVIDIA A10 GPU", instruction)
            task_toml = (harbor_task / "task.toml").read_text(encoding="utf-8")
            self.assertNotIn("LLM_API_KEY", task_toml)
            self.assertNotIn("LLM_BASE_URL", task_toml)
            self.assertIn('artifacts = ["/home/submission"]', task_toml)
            self.assertIn('workdir = "/home"', task_toml)
            self.assertIn('PAPERBENCH_REPRODUCTION_TIMEOUT_SEC = "604800"', task_toml)
            self.assertIn('PAPERBENCH_JUDGE_TIMEOUT_SEC = "600"', task_toml)
            self.assertIn('PAPERBENCH_JUDGE_MAX_WORKERS = "100"', task_toml)
            self.assertIn('construction_format = "native_rollout_task_v1"', task_toml)
            self.assertIn('source_native_contract = "paperbench_authored_task_v1"', task_toml)
            self.assertIn('native_task_id = "example-paper"', task_toml)
            self.assertIn("reference_solution_available = false", task_toml)
            self.assertIn('resource_metadata_version = "harbor_resource_metadata_v3"', task_toml)
            self.assertIn('gpu_tier = "H200"', task_toml)
            self.assertIn("gpu_count = 1", task_toml)
            self.assertEqual(task_toml.count('gpu_types = ["H200"]'), 2)
            resource_metadata = load_json(harbor_task / "resource_metadata.json")
            self.assertEqual(
                resource_metadata["schema_version"], "harbor_resource_metadata_v3"
            )
            self.assertEqual(resource_metadata["resource_estimate"]["gpu_tier"], "H200")
            self.assertEqual(resource_metadata["resource_estimate"]["gpu_count"], 1)
            self.assertEqual(resource_metadata["estimator"]["declared_gpu_tier"], "H200")
            judge = (harbor_task / "tests" / "llm_rubric_judge.py").read_text(
                encoding="utf-8"
            )
            self.assertIn('api_key = env_value("JUDGE_LLM_API_KEY")', judge)
            self.assertIn('base_url = env_value("JUDGE_LLM_BASE_URL")', judge)
            self.assertIn("def score_rubric_tree(", judge)
            self.assertIn("def grade_leaf_requests(", judge)
            self.assertIn("ThreadPoolExecutor", judge)
            self.assertIn("leaf score must be exactly 0 or 1", judge)
            self.assertNotIn('"temperature"', judge)
            self.assertNotIn("'temperature'", judge)
            self.assertNotIn('"response_format"', judge)
            self.assertIn("content = post(base_payload)", judge)
            test_script = (harbor_task / "tests" / "test.sh").read_text(encoding="utf-8")
            self.assertIn('WORKSPACE_DIR="${HARBOR_WORKSPACE_DIR:-/home}"', test_script)
            self.assertIn('PAPER_DIR="${WORKSPACE_DIR}/paper"', test_script)
            self.assertIn('SUBMISSION_DIR="${WORKSPACE_DIR}/submission"', test_script)
            self.assertIn("MAX_COMMITTED_BYTES = 1_000_000_000", test_script)
            self.assertIn('git -C "${CLEAN_SUBMISSION_DIR}" clean -fd', test_script)
            self.assertIn('--submission-dir "${CLEAN_SUBMISSION_DIR}"', test_script)
            self.assertIn('--submission-validation "${LOGS_DIR}/preflight.json"', test_script)
            self.assertIn('"${LOGS_DIR}/preflight.json"', test_script)
            self.assertEqual(os.stat(harbor_task).st_mode & 0o777, 0o755)
            self.assertEqual(os.stat(harbor_task / "tests" / "test.sh").st_mode & 0o777, 0o755)
            self.assertFalse((harbor_task / "environment" / "Dockerfile").exists())
            self.assertFalse((harbor_task / "tests" / "Dockerfile").exists())

            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(PUBLISH_SCRIPT),
                    "--root",
                    str(root),
                    "--paper",
                    "example-paper",
                    "--approved-by",
                    "test-reviewer",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue((paper_dir / "rubric.json").is_file())
            self.assertTrue((paper_dir / "addendum.md").is_file())
            self.assertTrue((authoring / "human_approval.json").is_file())


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
