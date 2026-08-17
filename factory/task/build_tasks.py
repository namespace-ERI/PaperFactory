#!/usr/bin/env python3
"""Build PaperBench paper packages from an already-selected paper list.

The builder intentionally does not inspect or clone an author's official code.
Official repositories are copied only into ``blacklist.txt``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


USER_AGENT = "PaperBench-task-factory/1.0"
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PDF_MAGIC = b"%PDF"
HTML_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "div", "figcaption", "figure",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main",
    "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def json_dump(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def fetch(url: str, destination: Path, *, retries: int = 4) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(retries):
        temporary: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                expected_raw = response.headers.get("Content-Length")
                expected = int(expected_raw) if expected_raw and expected_raw.isdigit() else None
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, delete=False
                ) as tmp_handle:
                    temporary = Path(tmp_handle.name)
                    downloaded = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        tmp_handle.write(chunk)
                        downloaded += len(chunk)
            if expected is not None and downloaded != expected:
                raise OSError(
                    f"truncated response: expected {expected} bytes, got {downloaded}"
                )
            if destination.suffix.lower() == ".pdf":
                with temporary.open("rb") as handle:
                    header = handle.read(4)
                    handle.seek(max(0, temporary.stat().st_size - 4096))
                    trailer = handle.read()
                if header != PDF_MAGIC or b"%%EOF" not in trailer:
                    raise OSError("downloaded PDF is incomplete or missing its EOF marker")
            temporary.replace(destination)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            time.sleep(2**attempt)
    raise RuntimeError(f"failed to download {url}: {last_error}")


def load_paper_list(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        collection = {"collection_id": path.stem}
        papers = raw
    elif isinstance(raw, dict) and isinstance(raw.get("papers"), list):
        collection = {key: value for key, value in raw.items() if key != "papers"}
        papers = raw["papers"]
    else:
        raise ValueError("paper list must be a JSON array or an object containing 'papers'")
    if not all(isinstance(item, dict) for item in papers):
        raise ValueError("each paper-list entry must be a JSON object")
    return collection, papers


def validate_entries(entries: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    ids: list[str] = []
    for index, entry in enumerate(entries, start=1):
        paper_id = entry.get("id")
        title = entry.get("title")
        label = paper_id or f"entry #{index}"
        if not isinstance(paper_id, str) or not ID_RE.fullmatch(paper_id):
            errors.append(f"{label}: id must be non-empty kebab-case")
        else:
            ids.append(paper_id)
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{label}: title is required")
        if not any(entry.get(key) for key in ("pdf_path", "paper_pdf", "pdf_url")):
            errors.append(f"{label}: one of pdf_path, paper_pdf, or pdf_url is required")
        blacklist = entry.get("blacklist")
        if blacklist is not None and not isinstance(blacklist, (str, list)):
            errors.append(f"{label}: blacklist must be a string or list of strings")
    duplicate_ids = sorted({paper_id for paper_id in ids if ids.count(paper_id) > 1})
    if duplicate_ids:
        errors.append(f"duplicate paper ids: {', '.join(duplicate_ids)}")
    if errors:
        raise ValueError("invalid paper list:\n- " + "\n- ".join(errors))


def source_value(entry: dict[str, Any], names: Iterable[str]) -> str | None:
    for name in names:
        value = entry.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def materialize_source(
    *,
    local_value: str | None,
    url_value: str | None,
    destination: Path,
    source_root: Path,
    offline: bool,
) -> str:
    if local_value:
        source = resolve_path(local_value, source_root)
        if not source.is_file():
            raise FileNotFoundError(f"local source does not exist: {source}")
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        return str(source)
    if not url_value:
        raise ValueError(f"no source is available for {destination.name}")
    if offline:
        raise RuntimeError(f"offline mode forbids download: {url_value}")
    fetch(url_value, destination)
    return url_value


def pdf_to_markdown(pdf_path: Path, markdown_path: Path, source: str) -> str:
    executable = shutil.which("pdftotext")
    if executable:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            text_path = Path(handle.name)
        try:
            result = subprocess.run(
                [executable, "-layout", str(pdf_path), str(text_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"pdftotext failed: {result.stderr.strip()}")
            text = text_path.read_text(encoding="utf-8", errors="replace").strip()
        finally:
            text_path.unlink(missing_ok=True)
        conversion = "pdftotext-layout"
    else:
        text = ""
        extractor_errors: list[str] = []
        try:
            import pymupdf

            document = pymupdf.open(str(pdf_path))
            try:
                text = "\n\n\f\n\n".join(
                    page.get_text("text", sort=True) for page in document
                ).strip()
            finally:
                document.close()
            conversion = "pymupdf-text"
        except (ImportError, RuntimeError, ValueError, OSError) as exc:
            extractor_errors.append(f"pymupdf: {exc}")
        if len(text) < 1000:
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(pdf_path))
                pages: list[str] = []
                for page in reader.pages:
                    try:
                        page_text = page.extract_text(extraction_mode="layout")
                    except TypeError:
                        page_text = page.extract_text()
                    pages.append(page_text or "")
                text = "\n\n\f\n\n".join(pages).strip()
                conversion = "pypdf-layout"
            except (ImportError, RuntimeError, ValueError, OSError) as exc:
                extractor_errors.append(f"pypdf: {exc}")
        if len(text) < 1000 and extractor_errors:
            raise RuntimeError(
                "paper markdown was not provided and no PDF text extractor is "
                "available; install factory/requirements.txt or provide "
                "markdown_path/paper_md; extractor errors: "
                + "; ".join(extractor_errors)
            )
    if len(text) < 1000:
        raise RuntimeError("PDF text extraction produced implausibly little text")
    write_text(
        markdown_path,
        "<!-- Layout-preserving text extracted from paper.pdf. "
        f"Authoritative source: {source} -->\n\n```text\n{text}\n```\n",
    )
    return conversion


class PaperHTMLTextParser(HTMLParser):
    """Dependency-free article HTML to readable, heading-preserving Markdown."""

    def __init__(self, *, require_article: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.require_article = require_article
        self.article_depth = 0
        self.skip_depth = 0
        self.parts: list[str] = []

    @property
    def active(self) -> bool:
        return not self.require_article or self.article_depth > 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "article":
            self.article_depth += 1
        if not self.active:
            return
        if tag in {"script", "style", "nav", "noscript"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in HTML_BLOCK_TAGS or tag == "br":
            self.parts.append("\n")
        if re.fullmatch(r"h[1-6]", tag):
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("- ")
        elif tag == "math":
            latex = attributes.get("alttext")
            if latex:
                self.parts.append(f" ${latex} ")
        elif tag == "img":
            alt = attributes.get("alt")
            if alt:
                self.parts.append(f" [{alt}] ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.active and tag in {"script", "style", "nav", "noscript"}:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif self.active and not self.skip_depth and tag in HTML_BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "article" and self.article_depth:
            self.article_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.active and not self.skip_depth:
            self.parts.append(data)


def html_to_markdown(html_path: Path, markdown_path: Path, source: str) -> str:
    raw = html_path.read_text(encoding="utf-8", errors="replace")
    parser = PaperHTMLTextParser(require_article=True)
    parser.feed(raw)
    text = "".join(parser.parts)
    if len(text.strip()) < 1000:
        parser = PaperHTMLTextParser(require_article=False)
        parser.feed(raw)
        text = "".join(parser.parts)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 1000:
        raise RuntimeError("HTML text extraction produced implausibly little text")
    write_text(
        markdown_path,
        "<!-- Generated from a public paper HTML rendering because PDF text "
        f"extraction was unavailable. Authoritative source: paper.pdf; HTML: {source} -->\n\n"
        + text
        + "\n",
    )
    return "paper-html"


def inferred_html_url(entry: dict[str, Any]) -> str | None:
    explicit = source_value(entry, ("html_url",))
    if explicit:
        return explicit
    for key in ("paper_url", "pdf_url"):
        value = source_value(entry, (key,))
        if not value:
            continue
        parsed = urllib.parse.urlparse(value)
        if parsed.netloc.lower() not in {"arxiv.org", "www.arxiv.org"}:
            continue
        match = re.match(r"/(?:abs|pdf)/([^/?#]+)", parsed.path)
        if match:
            arxiv_id = re.sub(r"\.pdf$", "", match.group(1))
            return f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}"
    return None


def copy_assets(entry: dict[str, Any], source_root: Path, destination: Path) -> list[dict[str, str]]:
    destination.mkdir(parents=True, exist_ok=True)
    value = source_value(entry, ("assets_path", "paper_assets"))
    if not value:
        return []
    source = resolve_path(value, source_root)
    if not source.is_dir():
        raise FileNotFoundError(f"assets directory does not exist: {source}")
    records: list[dict[str, str]] = []
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.resolve() != target.resolve():
            shutil.copy2(item, target)
        records.append(
            {
                "source": str(item),
                "local_path": f"assets/{relative.as_posix()}",
                "sha256": sha256(target),
            }
        )
    return records


def blacklist_lines(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("blacklist")
    if raw is None:
        raw = entry.get("official_repo", [])
    if isinstance(raw, str):
        raw = [raw]
    lines = [str(value).strip() for value in raw if str(value).strip()]
    return list(dict.fromkeys(lines))


def page_count(pdf_path: Path) -> int | None:
    executable = shutil.which("pdfinfo")
    if not executable:
        return None
    result = subprocess.run(
        [executable, str(pdf_path)], capture_output=True, text=True, check=False
    )
    match = re.search(r"^Pages:\s+(\d+)$", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def normalized_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    ignored = {
        "pdf_path",
        "paper_pdf",
        "markdown_path",
        "paper_md",
        "assets_path",
        "paper_assets",
        "blacklist",
    }
    return {key: value for key, value in entry.items() if key not in ignored}


def build_one(
    entry: dict[str, Any],
    *,
    output_root: Path,
    source_root: Path,
    offline: bool,
    force: bool,
) -> None:
    paper_id = entry["id"]
    paper_dir = output_root / "paper_sources" / paper_id
    design_dir = output_root / "design" / paper_id
    required = ["config.yaml", "paper.pdf", "paper.md", "blacklist.txt"]
    authoring_files = [
        design_dir / "task_metadata.json",
        design_dir / "source_provenance.json",
    ]
    if (
        not force
        and all((paper_dir / name).is_file() for name in required)
        and (paper_dir / "assets").is_dir()
        and all(path.is_file() for path in authoring_files)
    ):
        print(f"skip {paper_id}: package already exists (use --force to rebuild)")
        return
    paper_dir.mkdir(parents=True, exist_ok=True)
    design_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = paper_dir / "paper.pdf"
    pdf_source = materialize_source(
        local_value=source_value(entry, ("pdf_path", "paper_pdf")),
        url_value=source_value(entry, ("pdf_url",)),
        destination=pdf_path,
        source_root=source_root,
        offline=offline,
    )
    if not pdf_path.read_bytes().startswith(PDF_MAGIC):
        raise ValueError(f"{paper_id}: paper.pdf is not a hydrated PDF")

    markdown_path = paper_dir / "paper.md"
    markdown_local = source_value(entry, ("markdown_path", "paper_md"))
    markdown_url = source_value(entry, ("markdown_url", "md_url"))
    if markdown_local or markdown_url:
        markdown_source = materialize_source(
            local_value=markdown_local,
            url_value=markdown_url,
            destination=markdown_path,
            source_root=source_root,
            offline=offline,
        )
        markdown_conversion = "provided-markdown"
    else:
        markdown_source = pdf_source
        try:
            markdown_conversion = pdf_to_markdown(pdf_path, markdown_path, pdf_source)
        except RuntimeError as pdf_error:
            html_url = inferred_html_url(entry)
            if offline or not html_url:
                raise pdf_error
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
                html_path = Path(handle.name)
            try:
                fetch(html_url, html_path)
                markdown_conversion = html_to_markdown(
                    html_path, markdown_path, html_url
                )
                markdown_source = html_url
            finally:
                html_path.unlink(missing_ok=True)

    if len(markdown_path.read_text(encoding="utf-8", errors="replace")) < 1000:
        raise ValueError(f"{paper_id}: paper.md is implausibly short")

    assets = copy_assets(entry, source_root, paper_dir / "assets")
    write_text(
        paper_dir / "config.yaml",
        f"id: {paper_id}\ntitle: {json.dumps(entry['title'], ensure_ascii=False)}\n",
    )
    lines = blacklist_lines(entry)
    write_text(paper_dir / "blacklist.txt", "\n".join(lines) + ("\n" if lines else ""))

    metadata = {
        **normalized_metadata(entry),
        "agent_visible": False,
        "purpose": "Dataset-authoring metadata; never mount under /home/paper.",
        "authoring_status": "paper-package-built",
    }
    json_dump(design_dir / "task_metadata.json", metadata)
    json_dump(
        design_dir / "source_provenance.json",
        {
            "paper_id": paper_id,
            "pdf_source": pdf_source,
            "markdown_source": markdown_source,
            "paper_pdf_sha256": sha256(pdf_path),
            "paper_pdf_pages": page_count(pdf_path),
            "paper_md_sha256": sha256(markdown_path),
            "paper_md_conversion": markdown_conversion,
            "blacklist": lines,
            "assets": assets,
        },
    )
    print(
        f"built {paper_id}: pages={page_count(pdf_path) or '?'} "
        f"markdown={markdown_path.stat().st_size} bytes assets={len(assets)}"
    )


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-list", type=Path, default=project_root / "manifest.json")
    parser.add_argument("--output-root", type=Path, default=project_root)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="base directory for relative input paths (default: paper-list directory)",
    )
    parser.add_argument("--paper", action="append", dest="paper_ids")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="number of paper packages to download and build concurrently",
    )
    parser.add_argument(
        "--split-name",
        help="also write splits/<name>.txt; defaults to a sanitized collection_id",
    )
    parser.add_argument("--no-split", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    paper_list = args.paper_list.resolve()
    collection, entries = load_paper_list(paper_list)
    validate_entries(entries)
    selected = set(args.paper_ids or [])
    known = {entry["id"] for entry in entries}
    if selected - known:
        raise SystemExit(f"unknown paper ids: {', '.join(sorted(selected - known))}")
    chosen = [entry for entry in entries if not selected or entry["id"] in selected]
    source_root = (args.source_root or paper_list.parent).resolve()
    output_root = args.output_root.resolve()
    def build(entry: dict[str, Any]) -> None:
        build_one(
            entry,
            output_root=output_root,
            source_root=source_root,
            offline=args.offline,
            force=args.force,
        )

    if args.workers == 1 or len(chosen) <= 1:
        for entry in chosen:
            build(entry)
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(args.workers, len(chosen))
        ) as executor:
            futures = [executor.submit(build, entry) for entry in chosen]
            for future in concurrent.futures.as_completed(futures):
                future.result()

    if not args.no_split:
        raw_name = args.split_name or str(collection.get("collection_id", paper_list.stem))
        split_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_name).strip("-")
        if not split_name:
            raise ValueError("split name is empty after sanitization")
        write_text(
            output_root / "splits" / f"{split_name}.txt",
            "\n".join(entry["id"] for entry in entries) + "\n",
        )
        print(f"wrote split {split_name}: {len(entries)} papers")


if __name__ == "__main__":
    main()
