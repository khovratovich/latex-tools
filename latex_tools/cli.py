"""
cli.py — Command-line interface for latex-tools.

Commands:
  latex-tools annotate <root.tex>
  latex-tools index    <root.tex>
  latex-tools serve    <root.tex>
  latex-tools get      <root.tex> <id>
  latex-tools list     <root.tex> [--type TYPE] [--section SECTION]
  latex-tools search   <root.tex> <query> [--type TYPE]
  latex-tools context  <root.tex> <id> [--before N] [--after N]
  latex-tools validate <root.tex>
  latex-tools toc      <root.tex>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _json_out(obj: object) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def cmd_annotate(args: argparse.Namespace) -> None:
    from latex_tools.annotator import annotate_project
    summary = annotate_project(args.root_tex, dry_run=args.dry_run)
    total = sum(summary.values())
    for f, count in summary.items():
        if count:
            print(f"  {count:3d} insertions  {f}")
    print(f"\nTotal: {total} insertions across {len(summary)} file(s)")
    if args.dry_run:
        print("(dry run — no files were modified)")


def cmd_index(args: argparse.Namespace) -> None:
    from latex_tools.indexer import index_project
    index = index_project(args.root_tex)
    n = len(index["elements"])
    out_path = Path(index["root"]).parent / ".latex-index.json"
    print(f"Indexed {n} elements → {out_path}")


def cmd_serve(args: argparse.Namespace) -> None:
    from latex_tools.mcp_server import create_server
    server = create_server(args.root_tex)
    print(f"Starting MCP server for {args.root_tex} …", file=sys.stderr)
    server.run()


def cmd_get(args: argparse.Namespace) -> None:
    from latex_tools.api import LatexDocument
    doc = LatexDocument.load(args.root_tex)
    try:
        _json_out(doc.get_element(args.id))
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    from latex_tools.api import LatexDocument
    doc = LatexDocument.load(args.root_tex)
    results = doc.list_elements(
        type=args.type,
        section=args.section,
        labeled_only=args.labeled_only,
    )
    _json_out(results)


def cmd_search(args: argparse.Namespace) -> None:
    from latex_tools.api import LatexDocument
    doc = LatexDocument.load(args.root_tex)
    results = doc.search_elements(args.query, type=args.type, section=args.section)
    _json_out(results)


def cmd_context(args: argparse.Namespace) -> None:
    from latex_tools.api import LatexDocument
    doc = LatexDocument.load(args.root_tex)
    try:
        _json_out(doc.get_context(args.id, before=args.before, after=args.after))
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_validate(args: argparse.Namespace) -> None:
    from latex_tools.api import LatexDocument
    doc = LatexDocument.load(args.root_tex)
    report = doc.validate()
    _json_out(report)
    # Exit non-zero if there are issues
    issues = (
        len(report["undefined_refs"])
        + len(report["placeholder_cites"])
    )
    if issues:
        sys.exit(1)


def cmd_toc(args: argparse.Namespace) -> None:
    from latex_tools.api import LatexDocument
    doc = LatexDocument.load(args.root_tex)
    _json_out(doc.get_toc())


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latex-tools",
        description="Agent-friendly LaTeX tooling: annotate, index, and query LaTeX projects.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # annotate
    p = sub.add_parser("annotate", help="Add labels and AI markers to .tex files")
    p.add_argument("root_tex", type=Path)
    p.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    p.set_defaults(func=cmd_annotate)

    # index
    p = sub.add_parser("index", help="Build .latex-index.json")
    p.add_argument("root_tex", type=Path)
    p.set_defaults(func=cmd_index)

    # serve
    p = sub.add_parser("serve", help="Start MCP server")
    p.add_argument("root_tex", type=Path)
    p.set_defaults(func=cmd_serve)

    # get
    p = sub.add_parser("get", help="Get a single element by ID or label")
    p.add_argument("root_tex", type=Path)
    p.add_argument("id", help="Element ID or \\label value")
    p.set_defaults(func=cmd_get)

    # list
    p = sub.add_parser("list", help="List elements")
    p.add_argument("root_tex", type=Path)
    p.add_argument("--type", default=None, help="Filter by element type (e.g. theorem, equation)")
    p.add_argument("--section", default=None, help="Filter by section title (substring match)")
    p.add_argument("--labeled-only", action="store_true", help="Only elements with explicit labels")
    p.set_defaults(func=cmd_list)

    # search
    p = sub.add_parser("search", help="Full-text search across elements")
    p.add_argument("root_tex", type=Path)
    p.add_argument("query")
    p.add_argument("--type", default=None)
    p.add_argument("--section", default=None)
    p.set_defaults(func=cmd_search)

    # context
    p = sub.add_parser("context", help="Get an element with surrounding context")
    p.add_argument("root_tex", type=Path)
    p.add_argument("id")
    p.add_argument("--before", type=int, default=2)
    p.add_argument("--after", type=int, default=2)
    p.set_defaults(func=cmd_context)

    # validate
    p = sub.add_parser("validate", help="Check for broken refs, placeholder cites, etc.")
    p.add_argument("root_tex", type=Path)
    p.set_defaults(func=cmd_validate)

    # toc
    p = sub.add_parser("toc", help="Print table of contents")
    p.add_argument("root_tex", type=Path)
    p.set_defaults(func=cmd_toc)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
