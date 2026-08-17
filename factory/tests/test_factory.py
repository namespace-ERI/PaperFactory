from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


FACTORY = Path(__file__).resolve().parents[1]
FACTORY_SCRIPT = FACTORY / "build_paperbench.py"
TASK_SCRIPT = FACTORY / "task" / "build_tasks.py"
PUBLISH_SCRIPT = FACTORY / "rubrics" / "publish_rubric.py"
sys.path.insert(0, str(FACTORY / "rubrics"))

from rubric_lib import validate_rubric  # noqa: E402


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


class EndToEndFactoryTests(unittest.TestCase):
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
            official_instructions = (
                FACTORY.parents[2]
                / "Bench"
                / "PaperBench"
                / "source"
                / "project"
                / "paperbench"
                / "paperbench"
                / "instructions"
                / "instructions.txt"
            )
            self.assertEqual(
                (harbor_task / "instruction.md").read_bytes(),
                official_instructions.read_bytes(),
            )
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
