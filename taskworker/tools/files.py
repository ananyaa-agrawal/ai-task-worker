"""Workspace file tools. All paths are confined to the worker's workspace directory."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING

from ..policy import Assessment
from .base import Tool, ToolError, ToolResult, obj

if TYPE_CHECKING:
    from ..runtime import RunContext

MAX_CHARS = 20000
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".html", ".xml", ".yaml", ".yml", ".tsv"}


def _resolve(ctx: "RunContext", path: str) -> Path:
    root = ctx.workspace.resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"'{path}' is outside the workspace.")
    return target


def list_files(args: dict, ctx: "RunContext") -> ToolResult:
    target = _resolve(ctx, args.get("path") or ".")
    if not target.exists():
        raise ToolError(f"'{args.get('path')}' does not exist.")
    if target.is_file():
        return ToolResult(f"{target.relative_to(ctx.workspace.resolve())} ({target.stat().st_size} bytes)")
    entries = []
    for p in sorted(target.rglob("*")):
        if p.is_file() and not any(part.startswith(".") for part in p.parts):
            entries.append(f"{p.relative_to(ctx.workspace.resolve())}  ({p.stat().st_size} bytes)")
    return ToolResult("\n".join(entries[:200]) or "(empty)")


def read_file(args: dict, ctx: "RunContext") -> ToolResult:
    target = _resolve(ctx, args["path"])
    if not target.is_file():
        raise ToolError(f"File '{args['path']}' not found. Use list_files to see what exists.")
    suffix = target.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(str(target))
            pages = [f"--- page {i + 1} ---\n{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages)]
        except Exception as e:  # corrupt or encrypted PDF
            raise ToolError(f"Could not parse PDF: {e}")
        text = "\n".join(pages)
        if not text.strip() or all(not p.split("\n", 1)[-1].strip() for p in pages):
            text += "\n(No extractable text: the PDF may be a scanned image.)"
    elif suffix in TEXT_SUFFIXES or suffix == "":
        text = target.read_text(errors="replace")
        if suffix == ".csv":
            rows = list(csv.reader(io.StringIO(text)))
            text = f"(CSV with {max(len(rows) - 1, 0)} data rows)\n" + text
    else:
        raise ToolError(f"Unsupported file type '{suffix}'.")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n[... truncated, {len(text) - MAX_CHARS} more characters ...]"
    return ToolResult(f"Contents of {args['path']}:\n{text}")


def write_file(args: dict, ctx: "RunContext") -> ToolResult:
    target = _resolve(ctx, args["path"])
    out_root = (ctx.workspace / "outputs").resolve()
    if out_root not in target.parents:
        raise ToolError("You may only write files under outputs/.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(args["content"])
    return ToolResult(f"Wrote {len(args['content'])} characters to {args['path']}.")


def file_tools() -> list[Tool]:
    path = {"type": "string", "description": "Path relative to the workspace root."}
    return [
        Tool("list_files", "List files in the workspace (shared company documents, downloads, outputs).",
             obj({"path": path}, []), list_files),
        Tool("read_file", "Read a workspace file. PDFs are converted to text; CSV/JSON/text are returned as-is.",
             obj({"path": path}), read_file),
        Tool("write_file", "Write a text file under outputs/ (reports, exports for the user).",
             obj({"path": path, "content": {"type": "string"}}), write_file,
             assess=lambda a, c: Assessment("read", f"write workspace file {a['path']}")),
    ]
