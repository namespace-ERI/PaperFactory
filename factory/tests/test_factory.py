from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path


FACTORY = Path(__file__).resolve().parents[1]
FACTORY_SCRIPT = FACTORY / "build_paperbench.py"
BATCH_SCRIPT = FACTORY / "run_paperlist_batch.py"
TASK_SCRIPT = FACTORY / "task" / "build_tasks.py"
RUBRIC_SCRIPT = FACTORY / "rubrics" / "create_rubrics.py"
HARBOR_CONVERTER = FACTORY / "harbor" / "convert_to_harbor.py"
PUBLISH_SCRIPT = FACTORY / "rubrics" / "publish_rubric.py"
JUDGE_TEMPLATE = (
    FACTORY / "harbor" / "templates" / "processed_task" / "tests" / "llm_rubric_judge.py"
)
VERIFIER_TEMPLATE = (
    FACTORY / "harbor" / "templates" / "processed_task" / "tests" / "test.sh"
)
OFFICIAL_JUDGE_CONSTANTS = (
    FACTORY.parents[2]
    / "Bench/PaperBench/source/project/paperbench/paperbench/judge/constants.py"
)
sys.path.insert(0, str(FACTORY / "rubrics"))
sys.path.insert(0, str(FACTORY / "harbor"))

from rubric_lib import (  # noqa: E402
    paperbench_code_only_rubric,
    validate_rubric,
)
from export_incremental import completed_final_batch_task_count  # noqa: E402
from convert_to_harbor import select_authored_bundle  # noqa: E402


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def expected_harbor_instruction(rubric_mode: str = "regular") -> str:
    filename = (
        "instructions.code-dev.official.txt"
        if rubric_mode == "code-dev"
        else "instructions.official.txt"
    )
    official = (FACTORY / "harbor" / "templates" / filename).read_text(encoding="utf-8")
    rendered = (
        official.replace("/home/paper", "/workspace/paper")
        .replace("/home/submission", "/workspace/submission")
    )
    if rubric_mode == "regular":
        rendered = rendered.replace("NVIDIA A10 GPU", "NVIDIA H200 GPU")
    runtime_note = (
        "You are expected to work until you have spent 12 hours on this task. You may iteratively "
        "develop your solution within these constraints, but bear in mind that program runtime also "
        "counts towards the time limit. This is a difficult task and we expect a good solution to "
        "require using the full time limit."
    )
    additional_notes = f"""

ADDITIONAL NOTES
------
- **Compute**: You have access to a NVIDIA H200 GPU with the appropriate drivers installed.
- **Total Runtime**: {runtime_note}
- **API keys**: You have been granted API keys for services necessary for replicating your paper. The API keys are available to you at `/workspace/agent.env`. You can assume that these same API keys will already be set as environment variables in the reproduction environment when we evaluate your submission, so you do not need to set or export them in your submission.
- You have root access in your environment.
- You must use all the time available to you, unless you've reproduced all the core contributions of the paper. Do not stop until you've reproduced them.
- Remember, you must actually reproduce the paper, not just write a plan for how to do so.
"""
    return rendered.rstrip() + "\n" + additional_notes


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


class TaskAssetTests(unittest.TestCase):
    @staticmethod
    def load_task_module():
        spec = importlib.util.spec_from_file_location("paperbench_task_builder", TASK_SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_task_builder_accepts_namespaced_source_ids(self) -> None:
        module = self.load_task_module()
        module.validate_entries(
            [
                {
                    "id": "arxiv:1412.6572",
                    "title": "Generative Adversarial Nets",
                    "pdf_url": "https://arxiv.org/pdf/1412.6572",
                }
            ]
        )

    def test_task_builder_rejects_path_like_ids(self) -> None:
        module = self.load_task_module()
        with self.assertRaisesRegex(ValueError, "id must contain"):
            module.validate_entries(
                [
                    {
                        "id": "arxiv/1412.6572",
                        "title": "Invalid path-like id",
                        "pdf_url": "https://arxiv.org/pdf/1412.6572",
                    }
                ]
            )

    def test_null_official_repo_produces_empty_blacklist(self) -> None:
        module = self.load_task_module()
        self.assertEqual(module.blacklist_lines({"official_repo": None}), [])

    def test_task_builder_skips_one_failed_paper_and_continues(self) -> None:
        module = self.load_task_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper_list = root / "paperlist.json"
            dump(
                paper_list,
                {
                    "papers": [
                        {"id": paper_id, "title": paper_id, "pdf_url": "https://example.test/p.pdf"}
                        for paper_id in ("paper-good-a", "paper-bad", "paper-good-b")
                    ]
                },
            )
            calls: list[str] = []

            def fake_build_one(entry, **_kwargs) -> None:
                calls.append(entry["id"])
                if entry["id"] == "paper-bad":
                    raise RuntimeError("download forbidden")

            argv = [
                str(TASK_SCRIPT),
                "--paper-list",
                str(paper_list),
                "--output-root",
                str(root),
                "--workers",
                "1",
                "--continue-on-error",
                "--no-split",
            ]
            with patch.object(module, "build_one", side_effect=fake_build_one), patch.object(
                sys, "argv", argv
            ):
                module.main()

            self.assertEqual(calls, ["paper-good-a", "paper-bad", "paper-good-b"])
            failure = load_json(root / "design" / "paper-bad" / "task_build_failure.json")
            self.assertEqual(failure["error_type"], "RuntimeError")
            self.assertEqual(failure["error"], "download forbidden")

    def test_html_conversion_materializes_only_semantic_figure_media(self) -> None:
        module = self.load_task_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html = root / "paper.html"
            markdown = root / "paper.md"
            assets = root / "assets"
            html.write_text(
                "<html><body><article>"
                "<h1>Paper</h1><p>" + "method evidence " * 100 + "</p>"
                '<img src="site-logo.png" alt="site logo">'
                '<figure><object type="image/svg+xml" data="figures/main.svg"></object>'
                '<figcaption>Main result.</figcaption></figure>'
                '<figure><figure><img src="figures/panel.png" alt="Panel A"></figure>'
                '<figcaption>Panel result.</figcaption></figure>'
                "</article></body></html>",
                encoding="utf-8",
            )

            def fake_fetch(url: str, destination: Path, *, retries: int = 4) -> None:
                del retries
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(url.encode())

            with patch.object(module, "fetch", side_effect=fake_fetch):
                conversion, records, selected = module.html_to_markdown(
                    html,
                    markdown,
                    "https://paper.example/html/1234",
                    asset_destination=assets,
                )

            self.assertEqual(conversion, "paper-html-with-figures")
            self.assertEqual(selected, ["asset_1.svg", "asset_2.png"])
            self.assertEqual(len(records), 2)
            self.assertEqual(
                {path.name for path in assets.iterdir()},
                {"asset_1.svg", "asset_2.png"},
            )
            rendered = markdown.read_text(encoding="utf-8")
            self.assertIn("![](assets/asset_1.svg)", rendered)
            self.assertIn("![Panel A](assets/asset_2.png)", rendered)
            self.assertNotIn("site-logo.png", rendered)

    def test_html_asset_downloads_are_bounded_and_keep_document_order(self) -> None:
        module = self.load_task_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html = root / "paper.html"
            markdown = root / "paper.md"
            assets = root / "assets"
            figures = "".join(
                f'<figure><img src="figures/{index}.png" alt="Figure {index}"></figure>'
                for index in range(6)
            )
            html.write_text(
                "<html><body><article><p>" + "paper evidence " * 100 + "</p>"
                + figures
                + "</article></body></html>",
                encoding="utf-8",
            )
            lock = threading.Lock()
            active = 0
            max_active = 0

            def fake_fetch(url: str, destination: Path, *, retries: int = 4) -> None:
                nonlocal active, max_active
                del retries
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                destination.write_bytes(url.encode())
                with lock:
                    active -= 1

            with patch.object(module, "fetch", side_effect=fake_fetch):
                _conversion, records, selected = module.html_to_markdown(
                    html,
                    markdown,
                    "https://paper.example/html/1234",
                    asset_destination=assets,
                    asset_workers=2,
                )

            self.assertEqual(max_active, 2)
            self.assertEqual(selected, [f"asset_{index}.png" for index in range(1, 7)])
            self.assertEqual(
                [row["local_path"] for row in records],
                [f"assets/asset_{index}.png" for index in range(1, 7)],
            )

    def test_arxiv_source_fallback_uses_only_latex_figure_graphics(self) -> None:
        module = self.load_task_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "source.tar.gz"
            tex = (
                b"\\includegraphics{logo.png}\n"
                b"\\begin{figure}\\includegraphics{main.png}"
                b"\\caption{Main result}\\end{figure}\n"
            )
            with tarfile.open(archive_path, "w:gz") as archive:
                for name, data in (("paper.tex", tex), ("logo.png", b"logo"), ("main.png", b"main")):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))

            def fake_fetch(url: str, destination: Path, *, retries: int = 4) -> None:
                del url, retries
                shutil.copy2(archive_path, destination)

            assets = root / "assets"
            with patch.object(module, "fetch", side_effect=fake_fetch):
                records, selected, markdown = module.arxiv_source_figure_assets(
                    {"pdf_url": "https://arxiv.org/pdf/1234.5678"}, assets
                )
            self.assertEqual(selected, ["asset_1.png"])
            self.assertEqual(len(records), 1)
            self.assertEqual((assets / "asset_1.png").read_bytes(), b"main")
            self.assertIn("![Main result](assets/asset_1.png)", markdown)


class RubricModelClientTests(unittest.TestCase):
    @staticmethod
    def load_rubric_module():
        spec = importlib.util.spec_from_file_location(
            "paperbench_create_rubrics", RUBRIC_SCRIPT
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_empty_and_non_json_upstream_responses_are_retried(self) -> None:
        module = self.load_rubric_module()

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                del exc_type, exc, traceback

            def read(self) -> bytes:
                return self.body

        valid = json.dumps(
            {
                "choices": [
                    {"message": {"content": json.dumps({"ok": True})}}
                ]
            }
        ).encode("utf-8")
        responses = iter((FakeResponse(b""), FakeResponse(b"not-json"), FakeResponse(valid)))
        client = module.OpenAICompatibleClient(
            model="fixture-model",
            api_key="fixture-key",
            base_url="http://model.invalid",
            timeout=1,
            max_completion_tokens=100,
            retries=3,
        )
        with patch.object(
            module.urllib.request, "urlopen", side_effect=lambda *args, **kwargs: next(responses)
        ) as urlopen, patch.object(module.time, "sleep") as sleep:
            result = client.complete(call_name="fixture", system="system", user="user")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_continue_on_error_skips_failed_paper_and_runs_remaining(self) -> None:
        module = self.load_rubric_module()
        args = type(
            "Args",
            (),
            {"paper_workers": 1, "continue_on_error": True},
        )()
        calls: list[str] = []

        def fake_author_one(_args, paper_id: str, _guide: str) -> None:
            calls.append(paper_id)
            if paper_id == "paper-bad":
                raise RuntimeError("quality gate failed")

        with patch.object(module, "author_one", side_effect=fake_author_one):
            failures = module.author_papers(
                args,
                ["paper-good-a", "paper-bad", "paper-good-b"],
                "guide",
            )

        self.assertEqual(calls, ["paper-good-a", "paper-bad", "paper-good-b"])
        self.assertEqual(
            failures,
            {"paper-bad": "RuntimeError: quality gate failed"},
        )

    def test_only_explicitly_blocking_unresolved_questions_block_export(self) -> None:
        module = self.load_rubric_module()
        review = {
            "unresolved_questions": [
                {"question": "missing grading fact", "blocking": True},
                {"question": "gold-run follow-up", "blocking": False},
                {"question": "legacy note without classification"},
                "free-form warning",
            ]
        }

        self.assertEqual(
            module.blocking_unresolved_from(review),
            [{"question": "missing grading fact", "blocking": True}],
        )
        self.assertEqual(len(module.unresolved_from(review)), 4)

    def test_quality_review_receives_complete_paper_text(self) -> None:
        module = self.load_rubric_module()

        class CapturingClient:
            def __init__(self) -> None:
                self.user = ""

            def complete(self, *, call_name: str, system: str, user: str):
                del call_name, system
                self.user = user
                return {"blocking_issues": [], "unresolved_questions": []}

        client = CapturingClient()
        module.review_drafts(
            client,
            paper_id="fixture-paper",
            paper_text="UNIQUE COMPLETE PAPER EVIDENCE",
            matrix={},
            addendum="No addendum.",
            rubric=valid_tree(),
            validation={"valid": True},
            rubric_mode="regular",
        )

        self.assertIn("<paper id=\"fixture-paper\">", client.user)
        self.assertIn("UNIQUE COMPLETE PAPER EVIDENCE", client.user)
        self.assertIn("blocking=true only", client.user)

    def test_rubric_authoring_remains_strict_without_continue_on_error(self) -> None:
        module = self.load_rubric_module()
        args = type(
            "Args",
            (),
            {"paper_workers": 1, "continue_on_error": False},
        )()
        calls: list[str] = []

        def fake_author_one(_args, paper_id: str, _guide: str) -> None:
            calls.append(paper_id)
            if paper_id == "paper-bad":
                raise RuntimeError("quality gate failed")

        with patch.object(module, "author_one", side_effect=fake_author_one):
            with self.assertRaisesRegex(RuntimeError, "quality gate failed"):
                module.author_papers(
                    args,
                    ["paper-good-a", "paper-bad", "paper-never-started"],
                    "guide",
                )

        self.assertEqual(calls, ["paper-good-a", "paper-bad"])


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

    def test_code_dev_validation_uses_official_category_not_filename_keywords(self) -> None:
        rubric = paperbench_code_only_rubric(valid_tree())
        rubric["sub_tasks"][0]["sub_tasks"][0]["requirements"] = (
            "The implementation includes helper code used by reproduce.sh."
        )
        report = validate_rubric(rubric, rubric_mode="code-dev")
        self.assertTrue(report["valid"], report["errors"])

    def test_code_dev_does_not_add_nonofficial_reproduce_contract_filter(self) -> None:
        rubric = paperbench_code_only_rubric(valid_tree())
        rubric["sub_tasks"][0]["sub_tasks"][0]["requirements"] = (
            "The submitted repository contains a root-level executable reproduce.sh "
            "that invokes every experiment."
        )
        report = validate_rubric(rubric, rubric_mode="code-dev")
        self.assertTrue(report["valid"], report["errors"])

class HarborTemplateTests(unittest.TestCase):
    def test_incremental_exporter_accepts_complete_final_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary) / "20260824-120000"
            harbor_root = batch / "harbor_task"
            harbor_root.mkdir(parents=True)
            papers = [{"id": "paper-a"}, {"id": "paper-b"}]
            task_ids = [
                f"20260824-120000-research-paperbench-"
                + __import__("hashlib").sha256(
                    f"20260824-120000:{paper['id']}".encode("utf-8")
                ).hexdigest()[:6]
                for paper in papers
            ]
            for task_id in task_ids:
                (harbor_root / task_id).mkdir()
            (batch / "manifest.jsonl").write_text(
                "".join(json.dumps({"task_id": task_id}) + "\n" for task_id in task_ids),
                encoding="utf-8",
            )
            self.assertEqual(
                completed_final_batch_task_count(
                    batch, papers=papers, batch_id="20260824-120000"
                ),
                2,
            )

    @staticmethod
    def load_converter_module():
        spec = importlib.util.spec_from_file_location(
            "paperbench_harbor_converter", HARBOR_CONVERTER
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def load_judge_module():
        spec = importlib.util.spec_from_file_location("paperbench_judge_template", JUDGE_TEMPLATE)
        if spec is None or spec.loader is None:
            raise AssertionError("cannot load judge template")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_harbor_derives_url_only_assets_from_paper_markdown(self) -> None:
        module = self.load_converter_module()
        with tempfile.TemporaryDirectory() as temporary:
            paper = Path(temporary)
            (paper / "assets").mkdir()
            (paper / "assets" / "asset_1.svg").write_text("svg", encoding="utf-8")
            (paper / "assets" / "asset_2.png").write_bytes(b"png")
            (paper / "paper.md").write_text(
                "![first](assets/asset_1.svg)\n![second](assets/asset_2.png)\n",
                encoding="utf-8",
            )
            selected, policy = module.resolved_asset_files(
                {"id": "url-only-paper"}, paper
            )
            self.assertEqual(selected, ["asset_1.svg", "asset_2.png"])
            self.assertEqual(policy, "paper-markdown-referenced-assets-v1")

    def test_judge_prompts_match_official_constants(self) -> None:
        if not OFFICIAL_JUDGE_CONSTANTS.is_file():
            self.skipTest("official PaperBench checkout is not available")
        module = self.load_judge_module()
        spec = importlib.util.spec_from_file_location(
            "official_paperbench_judge_constants", OFFICIAL_JUDGE_CONSTANTS
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        official = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(official)
        self.assertEqual(module.OFFICIAL_FILE_RANKING_PROMPT, official.FILE_RANKING_PROMPT)
        self.assertEqual(module.OFFICIAL_GRADING_PROMPT, official.GRADING_PROMPT(False))
        self.assertEqual(module.build_judge_task_prompt(False), official.build_judge_task_prompt(False))
        self.assertEqual(module.build_judge_task_prompt(True), official.build_judge_task_prompt(True))

    def test_large_paperlist_runner_uses_resume_and_twelve_hour_agent_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper_list = root / "paperlist.json"
            dump(
                paper_list,
                {
                    "papers": [
                        {"id": "paper-a", "title": "Paper A"},
                        {"id": "paper-b", "title": "Paper B"},
                    ]
                },
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(BATCH_SCRIPT),
                    "--paper-list",
                    str(paper_list),
                    "--root",
                    str(root),
                    "--batch-id",
                    "20260819-120000",
                    "--dry-run",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("selected papers: 2", result.stdout)
            self.assertIn("--resume-rubric", result.stdout)
            self.assertIn("--continue-on-task-error", result.stdout)
            self.assertIn("--continue-on-rubric-error", result.stdout)
            self.assertIn("--harbor-agent-timeout-sec 43200", result.stdout)
            self.assertIn("--asset-workers 4", result.stdout)
            self.assertIn("--stream-papers", result.stdout)
            self.assertIn("--paper paper-a --paper paper-b", result.stdout)
            self.assertIn("incremental Harbor export: enabled", result.stdout)

    def test_code_dev_collection_uses_git_head_and_official_file_ranking(self) -> None:
        module = self.load_judge_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture@example.test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
            (root / ".gitignore").write_text(
                "results/\ncheckpoints/\nlogits/\n",
                encoding="utf-8",
            )
            (root / "README.md").write_text("core readme", encoding="utf-8")
            (root / "src").mkdir()
            source = "def method():\n    return 'complete implementation, not clipped'\n"
            (root / "src" / "method.py").write_text(source, encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts" / "run_all.sh").write_text("#!/bin/sh\npython src/method.py\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            for directory in ("results", "checkpoints", "logits"):
                (root / directory).mkdir()
                for index in range(100):
                    (root / directory / f"artifact-{index:03d}.json").write_text(
                        '{"ignored": true}\n', encoding="utf-8"
                    )

            collected = module.collect_submission(root, committed_only=True)
            paths = [row["path"] for row in collected["files"]]
            self.assertIn("README.md", paths)
            self.assertIn("src/method.py", paths)
            self.assertIn("scripts/run_all.sh", paths)
            self.assertFalse(any(path.startswith("results/") for path in paths))
            self.assertFalse(any(path.startswith("checkpoints/") for path in paths))
            self.assertFalse(any(path.startswith("logits/") for path in paths))

            leaf = {
                "id": "method",
                "requirements": "Implement the paper's method.",
                "task_category": "Code Development",
            }
            with patch.dict(
                os.environ,
                {"PAPERBENCH_JUDGE_MOCK_FILE_SELECTION": "src/method.py\nscripts/run_all.sh\nREADME.md"},
            ):
                relevant = module.prepare_relevant_submission(
                    collected,
                    leaf=leaf,
                    paper={"paper_md": "paper"},
                    addendum="(NO ADDENDUM GIVEN)",
                    reproduce_log="",
                    context_window_tokens=400000,
                )
            self.assertEqual(
                [row["path"] for row in relevant["files"]],
                ["src/method.py", "scripts/run_all.sh", "README.md"],
            )
            self.assertIn(source, relevant["text"])

    def test_selected_file_content_uses_context_budget_not_global_file_cap(self) -> None:
        module = self.load_judge_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "core.py").write_text("x = 1\n" * 1000, encoding="utf-8")
            snapshot = {
                "root": str(root),
                "files": [{"path": "core.py", "bytes": (root / "core.py").stat().st_size}],
            }
            rows, text = module.read_selected_files(
                snapshot, ["core.py"], max_tokens=100
            )
            self.assertEqual([row["path"] for row in rows], ["core.py"])
            self.assertLessEqual(module.token_count(text), 100)
            self.assertIn("<FILE:core.py>", text)

    def test_reproduction_log_uses_official_per_file_read_limit(self) -> None:
        module = self.load_judge_module()
        with tempfile.TemporaryDirectory() as temporary:
            reproduction = Path(temporary)
            executed = reproduction / "executed" / "workspace" / "submission"
            executed.mkdir(parents=True)
            content = "x" * 250_000
            (executed / "reproduce.log").write_text(content, encoding="utf-8")
            (executed / "reproduce.log.creation_time").write_text(
                "123\n", encoding="utf-8"
            )
            summary = module.reproduction_summary(reproduction)
            self.assertEqual(
                summary["reproduce_log"],
                content[: module.OFFICIAL_FILE_READ_LIMIT],
            )

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

    def test_judge_records_requested_and_upstream_reported_models(self) -> None:
        module = self.load_judge_module()
        completions = iter(
            [
                {
                    "content": "# Expectations\nX\n# Reality\nY\n# Score\n1",
                    "requested_model": "gpt-5.5",
                    "reported_model": "routed-model-a",
                    "response_id": "judge-response",
                },
                {
                    "content": json.dumps(
                        {"valid_score": True, "score": 1, "explanation": "met"}
                    ),
                    "requested_model": "gpt-5.5",
                    "reported_model": "routed-model-b",
                    "response_id": "parser-response",
                },
            ]
        )
        with patch.object(module, "call_llm_completion", side_effect=lambda _messages: next(completions)):
            parsed = module.call_llm([{"role": "user", "content": "grade"}], leaf_id="leaf")
        row = parsed["leaves"][0]
        self.assertEqual(row["judge_requested_model"], "gpt-5.5")
        self.assertEqual(row["judge_reported_model"], "routed-model-a")
        self.assertEqual(row["parser_reported_model"], "routed-model-b")

    def test_leaf_context_matches_official_prior_node_order(self) -> None:
        module = self.load_judge_module()
        contexts = {
            leaf["id"]: requirements
            for leaf, requirements in module.rubric_leaf_contexts(valid_tree())
        }
        self.assertEqual(
            contexts["main-trend"],
            [
                "The scoped core contribution has been reproduced.",
                "The scoped method has been implemented and evaluated.",
                "The implementation contains the method defined in Section 3.",
                "The main experiment from Table 1 is executed by reproduce.sh.",
            ],
        )
        self.assertEqual(
            contexts["machine-readable-results"],
            [
                "The scoped core contribution has been reproduced.",
                "The scoped method has been implemented and evaluated.",
                "The reproduction interface is usable.",
                "The root reproduce.sh runs the scoped workflow.",
            ],
        )
        capped = {
            leaf["id"]: requirements
            for leaf, requirements in module.filtered_leaf_contexts(
                valid_tree(), code_only=False, max_prior_nodes=2
            )
        }
        self.assertEqual(
            capped["main-trend"],
            [
                "The implementation contains the method defined in Section 3.",
                "The main experiment from Table 1 is executed by reproduce.sh.",
            ],
        )

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
            reproduction={"reproduce_log": "must not appear"},
            code_only=True,
        )
        self.assertIn("You are judging a candidate's attempt to reproduce a research paper.", prompt)
        self.assertIn("Does the code in the submission contain a correct implementation of this?", prompt)
        self.assertIn("# Expectations", prompt)
        self.assertIn("# Reality", prompt)
        self.assertIn("# Score", prompt)
        self.assertIn("source code", prompt)
        self.assertNotIn("<reproduce.sh>", prompt)
        self.assertNotIn("<reproduce.log>", prompt)
        self.assertNotIn("must not appear", prompt)

    def test_judge_uses_one_request_per_leaf_and_isolates_failures(self) -> None:
        module = self.load_judge_module()
        rubric = valid_tree()
        requested: list[str] = []
        original_call_llm = module.call_llm

        requirements = {
            leaf["id"]: leaf["requirements"]
            for leaf in module.filtered_leaves(rubric, code_only=False)
        }

        def fake_call_llm(messages: list[dict[str, str]], *, leaf_id: str = "") -> dict:
            requested.append(leaf_id)
            prompt = "\n".join(message["content"] for message in messages)
            self.assertIn(requirements[leaf_id], prompt)
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
                executed_submission={
                    "files": [
                        {
                            "path": "results.csv",
                            "touched_by_reproduction": True,
                        }
                    ],
                    "text": "results",
                },
                reproduction={"reproduce_log": "ran"},
                code_only=False,
                max_workers=3,
                context_window_tokens=400000,
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

            workspace = root / "workspace"
            paper = workspace / "paper"
            submission = workspace / "submission"
            paper.mkdir(parents=True)
            submission.mkdir()
            (paper / "paper.pdf").write_bytes(b"%PDF-1.4\n")
            (paper / "paper.md").write_text("# Fixture\n", encoding="utf-8")
            (paper / "addendum.md").write_text("# Scope\n", encoding="utf-8")
            (paper / "blacklist.txt").write_text(
                "https://github.com/authors/official-code\n", encoding="utf-8"
            )
            (submission / "README.md").write_text("# Reproduction\n", encoding="utf-8")
            (submission / "reproduce.sh").write_text(
                "#!/bin/bash\nprintf 'stdout-line\\n'\nprintf 'stderr-line\\n' >&2\n"
                "mkdir -p results\nprintf 'ok\\n' > results/metrics.txt\n",
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
                "HARBOR_PAPER_DIR": str(paper),
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
            clean_submission = root / "logs-valid" / "reproduction" / "clean" / "workspace" / "submission"
            executed_submission = root / "logs-valid" / "reproduction" / "executed" / "workspace" / "submission"
            self.assertFalse((clean_submission / "untracked-secret.txt").exists())
            self.assertFalse((executed_submission / "untracked-secret.txt").exists())
            self.assertTrue((executed_submission / "results" / "metrics.txt").is_file())
            self.assertTrue((executed_submission / "reproduce.log.creation_time").is_file())
            self.assertEqual(
                (executed_submission / "reproduce.log").read_text(encoding="utf-8"),
                "stdout-line\nstderr-line\n",
            )
            judge_details = load_json(root / "logs-valid" / "paperbench_judge_details.json")
            generated = {
                row["path"]: row
                for row in judge_details["executed_submission_files"]
            }
            self.assertTrue(generated["results/metrics.txt"]["touched_by_reproduction"])
            self.assertEqual(load_json(root / "logs-valid" / "reward.json")["score"], 1.0)

            trajectory = root / "trajectory.json"
            dump(
                trajectory,
                {
                    "schema_version": "ATIF-v1.7",
                    "steps": [
                        {
                            "step_id": 7,
                            "tool_calls": [
                                {
                                    "tool_call_id": "call-1",
                                    "function_name": "Bash",
                                    "arguments": {
                                        "command": "git clone https://github.com/authors/official-code"
                                    },
                                }
                            ],
                        }
                    ],
                },
            )
            env["HARBOR_AGENT_TRAJECTORY_PATH"] = str(trajectory)
            env["HARBOR_LOGS_DIR"] = str(root / "logs-monitor")
            verifier = subprocess.run(
                ["bash", str(VERIFIER_TEMPLATE)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verifier.returncode, 0, verifier.stdout + verifier.stderr)
            monitor = load_json(root / "logs-monitor" / "monitor.json")
            self.assertTrue(monitor["flagged"])
            self.assertEqual(len(monitor["violations"]), 1)
            self.assertEqual(load_json(root / "logs-monitor" / "reward.json")["score"], 0.0)
            self.assertEqual(
                (root / "logs-monitor" / "reproduction" / "exit_code.txt")
                .read_text(encoding="utf-8")
                .strip(),
                "monitor_disqualified",
            )

            (submission / "README.md").write_text("uncommitted tracked change\n", encoding="utf-8")
            env.pop("HARBOR_AGENT_TRAJECTORY_PATH")
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
                (root / "logs-dirty" / "reproduction" / "executed" / "workspace" / "submission").exists()
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
Concrete committed source code, execution evidence, and reproduced outputs are required.

# Clarifications
Use the standard PaperBench reproduction contract.

# Out of scope
Experiments introduced only in the appendix are out of scope.
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
                    "2500",
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
            self.assertEqual(instruction, expected_harbor_instruction("code-dev"))
            self.assertIn("The code will not be executed during grading.", instruction)
            self.assertNotIn("for a maximum runtime of 7 days", instruction)
            self.assertNotIn("reproduce.sh", instruction)
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
            self.assertIn('description = "Reproduce core methods and experiments from Code Dev Fixture"', task_toml)
            self.assertIn("code_only = true", task_toml)
            self.assertIn('rubric_mode = "code-dev"', task_toml)
            self.assertIn(
                'code_dev_derivation = "official-code-development-prune-v1"',
                task_toml,
            )
            self.assertIn('gpu_tier = "H200"', task_toml)
            self.assertIn("gpu_count = 1", task_toml)
            self.assertEqual(task_toml.count('gpu_types = ["H200"]'), 2)
            self.assertIn("[agent]\ntimeout_sec = 43200", task_toml)
            self.assertIn("[verifier]\ntimeout_sec = 2500", task_toml)

            workspace = root / "code-dev-workspace"
            shutil.copytree(harbor_task / "environment" / "paper", workspace / "paper")
            submission = workspace / "submission"
            submission.mkdir()
            (submission / "README.md").write_text("# Code implementation\n", encoding="utf-8")
            (submission / "method.py").write_text("def module_a(value): return value\n", encoding="utf-8")
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
            self.assertFalse(preflight["reproduce_sh_exists"])
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
                    / "workspace"
                    / "submission"
                    / "REPRODUCE_WAS_RUN"
                ).exists()
            )

            missing_entrypoint_logs = root / "code-dev-missing-entrypoint-logs"
            env["HARBOR_LOGS_DIR"] = str(missing_entrypoint_logs)
            verifier = subprocess.run(
                ["bash", str(harbor_task / "tests" / "test.sh")],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verifier.returncode, 0, verifier.stdout + verifier.stderr)
            missing_preflight = load_json(missing_entrypoint_logs / "preflight.json")
            self.assertTrue(missing_preflight["code_only"])
            self.assertFalse(missing_preflight["reproduce_sh_exists"])
            self.assertFalse(missing_preflight["reproduce_sh_tracked"])
            self.assertTrue(missing_preflight["submission_valid"])

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
                "![Selected result](assets/figure.png)\n\n"
                "![Uncurated scrape](assets/unused.png)\n\n"
                + "Evidence sentence.\n" * 100
            )
            (source / "paper.md").write_text(markdown, encoding="utf-8")
            (source / "assets").mkdir()
            (source / "assets" / "figure.png").write_bytes(b"png")
            (source / "assets" / "unused.png").write_bytes(b"unused")
            # Local source modes must not leak into the reusable paper package.
            (source / "paper.pdf").chmod(0o600)
            (source / "paper.md").chmod(0o600)
            (source / "assets" / "figure.png").chmod(0o600)
            paper_list = {
                "collection_id": "fixture",
                "papers": [
                    {
                        "id": "example-paper",
                        "title": "Example Paper",
                        "pdf_path": "source/paper.pdf",
                        "markdown_path": "source/paper.md",
                        "assets_path": "source/assets",
                        "asset_files": ["figure.png"],
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
            self.assertEqual((paper_dir / "paper.pdf").stat().st_mode & 0o777, 0o644)
            self.assertEqual((paper_dir / "paper.md").stat().st_mode & 0o777, 0o644)
            self.assertEqual(
                (paper_dir / "assets" / "figure.png").stat().st_mode & 0o777,
                0o644,
            )
            self.assertFalse((paper_dir / "assets" / "unused.png").exists())
            curated_markdown = (paper_dir / "paper.md").read_text(encoding="utf-8")
            self.assertIn("assets/figure.png", curated_markdown)
            self.assertNotIn("assets/unused.png", curated_markdown)
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
            self.assertTrue(
                (harbor_task / "environment" / "paper" / "assets" / "figure.png").is_file()
            )
            self.assertFalse(
                (harbor_task / "environment" / "paper" / "assets" / "unused.png").exists()
            )
            rendered_paper = (
                harbor_task / "environment" / "paper" / "paper.md"
            ).read_text(encoding="utf-8")
            self.assertIn("assets/figure.png", rendered_paper)
            self.assertNotIn("assets/unused.png", rendered_paper)
            self.assertTrue((harbor_task / "tests" / "paper" / "paper.md").is_file())
            self.assertFalse((harbor_task / "tests" / "paper" / "assets").exists())
            instruction = (harbor_task / "instruction.md").read_text(encoding="utf-8")
            self.assertEqual(instruction, expected_harbor_instruction())
            self.assertIn("/workspace/paper", instruction)
            self.assertIn("/workspace/submission", instruction)
            self.assertIn("for a maximum runtime of 7 days", instruction)
            self.assertIn("spent 12 hours on this task", instruction)
            self.assertIn("NVIDIA H200 GPU", instruction)
            self.assertNotIn("NVIDIA A10 GPU", instruction)
            task_toml = (harbor_task / "task.toml").read_text(encoding="utf-8")
            self.assertNotIn("LLM_API_KEY", task_toml)
            self.assertNotIn("LLM_BASE_URL", task_toml)
            self.assertIn(
                'artifacts = ["/workspace/submission", "/logs/agent/trajectory.json"]',
                task_toml,
            )
            self.assertIn('workdir = "/workspace"', task_toml)
            self.assertIn('PAPERBENCH_REPRODUCTION_TIMEOUT_SEC = "604800"', task_toml)
            self.assertIn('PAPERBENCH_JUDGE_TIMEOUT_SEC = "600"', task_toml)
            self.assertIn('PAPERBENCH_JUDGE_MAX_WORKERS = "100"', task_toml)
            self.assertIn('PAPERBENCH_JUDGE_CONTEXT_WINDOW_TOKENS = "400000"', task_toml)
            self.assertIn("[agent]\ntimeout_sec = 43200", task_toml)
            self.assertIn("[verifier]\ntimeout_sec = 609000", task_toml)
            self.assertIn("cpus = 4\nmemory_mb = 16384\nstorage_mb = 51200", task_toml)
            self.assertIn("cpus = 8\nmemory_mb = 32768\nstorage_mb = 51200", task_toml)
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
            self.assertIn("return post(base_payload)", judge)
            self.assertIn("OFFICIAL_FILE_RANKING_PROMPT", judge)
            self.assertIn("OFFICIAL_GRADING_PROMPT", judge)
            self.assertIn("parse_official_judge_response", judge)
            self.assertNotIn("RESULT_MARKERS", judge)
            self.assertNotIn("max_chars_per_file", judge)
            test_script = (harbor_task / "tests" / "test.sh").read_text(encoding="utf-8")
            self.assertIn('WORKSPACE_DIR="${HARBOR_WORKSPACE_DIR:-/workspace}"', test_script)
            self.assertIn(
                'PAPER_DIR="${HARBOR_PAPER_DIR:-${TESTS_DIR}/paper}"',
                test_script,
            )
            self.assertIn('SUBMISSION_DIR="${WORKSPACE_DIR}/submission"', test_script)
            self.assertIn(
                'CLEAN_SUBMISSION_DIR="${REPRO_DIR}/clean/workspace/submission"',
                test_script,
            )
            self.assertIn(
                'EXECUTED_SUBMISSION_DIR="${REPRO_DIR}/executed/workspace/submission"',
                test_script,
            )
            self.assertIn("MAX_COMMITTED_BYTES = 1_000_000_000", test_script)
            self.assertIn('git -C "${CLEAN_SUBMISSION_DIR}" clean -fd', test_script)
            self.assertIn('--submission-dir "${CLEAN_SUBMISSION_DIR}"', test_script)
            self.assertIn('--submission-validation "${LOGS_DIR}/preflight.json"', test_script)
            self.assertIn('--monitor-result "${LOGS_DIR}/monitor.json"', test_script)
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

            # Replacing a publication whose new review has no judge addendum
            # must not retain an older judge-only instruction.
            (paper_dir / "judge.addendum.md").write_text(
                "stale judge guidance\n", encoding="utf-8"
            )
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
                    "--replace",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse((paper_dir / "judge.addendum.md").exists())
            self.assertIsNone(
                load_json(authoring / "human_approval.json")["judge_addendum_sha256"]
            )

    def test_semantic_review_is_audited_but_does_not_block_harbor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper_id = "blocked-paper"
            authoring = root / "design" / paper_id / "rubric_authoring"
            dump(authoring / "rubric.draft.json", valid_tree())
            (authoring / "addendum.draft.md").write_text(
                "# Scope\n\nCore experiment only.\n", encoding="utf-8"
            )
            dump(authoring / "authoring_provenance.json", {"rubric_mode": "regular"})
            dump(
                authoring / "quality_review.json",
                {
                    "blocking_issues": [{"issue": "duplicate scoring"}],
                    "unresolved_questions": [],
                },
            )
            dump(authoring / "unresolved_questions.json", [])

            (authoring / "authoring_provenance.json").unlink()
            with self.assertRaisesRegex(FileNotFoundError, "no complete rubric/addendum pair"):
                select_authored_bundle(
                    root,
                    paper_id,
                    rubric_mode="regular",
                    require_approved=False,
                )
            dump(authoring / "authoring_provenance.json", {"rubric_mode": "regular"})

            rubric_path, addendum_path, status, mode = select_authored_bundle(
                root,
                paper_id,
                rubric_mode="regular",
                require_approved=False,
            )
            self.assertEqual(rubric_path, authoring / "rubric.draft.json")
            self.assertEqual(addendum_path, authoring / "addendum.draft.md")
            self.assertEqual(status, "authoring-draft")
            self.assertEqual(mode, "regular")

            dump(
                authoring / "quality_review.json",
                {"blocking_issues": [], "unresolved_questions": []},
            )
            dump(
                authoring / "unresolved_questions.json",
                [{"question": "missing evaluator detail", "blocking": True}],
            )
            select_authored_bundle(
                root,
                paper_id,
                rubric_mode="regular",
                require_approved=False,
            )

            dump(
                authoring / "quality_review.json",
                {
                    "blocking_issues": [],
                    "unresolved_questions": [
                        {"question": "review still needs evaluator evidence", "blocking": True}
                    ],
                },
            )
            dump(authoring / "unresolved_questions.json", [])
            select_authored_bundle(
                root,
                paper_id,
                rubric_mode="regular",
                require_approved=False,
            )

            nonblocking = {
                "question": "run a human gold calibration before publication",
                "blocking": False,
            }
            dump(
                authoring / "quality_review.json",
                {"blocking_issues": [], "unresolved_questions": [nonblocking]},
            )
            dump(authoring / "unresolved_questions.json", [nonblocking])
            rubric_path, addendum_path, status, mode = select_authored_bundle(
                root,
                paper_id,
                rubric_mode="regular",
                require_approved=False,
            )
            self.assertEqual(rubric_path, authoring / "rubric.draft.json")
            self.assertEqual(addendum_path, authoring / "addendum.draft.md")
            self.assertEqual(status, "authoring-draft")
            self.assertEqual(mode, "regular")


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
