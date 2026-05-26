"""
mcp_server.py — MCP server exposing the latex-tools API.

Every LatexDocument method is registered as an MCP tool with a JSON schema.
The server holds one LatexDocument in memory across calls (stateful).

Start with:
    latex-tools serve path/to/paper.tex
or directly:
    python -m latex_tools.mcp_server path/to/paper.tex
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from latex_tools.api import LatexDocument
from latex_tools.annotator import annotate_project
from latex_tools.indexer import index_project


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

def create_server(root_tex: str | Path) -> FastMCP:
    """
    Build and return a FastMCP server with all latex-tools tools registered.
    The LatexDocument is loaded once and kept in server state.
    """
    root_tex = Path(root_tex)
    doc = LatexDocument.load(root_tex)

    mcp = FastMCP(
        name="latex-tools",
        instructions=(
            "Tools for working with a LaTeX document project. "
            "All interactions are logical (by element ID or label), "
            "not by raw line number. "
            f"Current document: {root_tex.name}"
        ),
    )

    # -----------------------------------------------------------------------
    # Project-level tools
    # -----------------------------------------------------------------------

    @mcp.tool()
    def annotate_latex_project(dry_run: bool = False) -> dict:
        """
        Add stable \\label{} markers and AI block comments to all .tex files
        in the project.  Idempotent — already-labeled elements are skipped.
        A .tex.bak backup is written before any modifications.

        Returns a summary of insertions made per file.
        """
        summary = annotate_project(root_tex, dry_run=dry_run)
        doc.refresh()
        return summary

    @mcp.tool()
    def index_latex_project() -> dict:
        """
        (Re)build the .latex-index.json from the current .tex source files.
        Returns high-level stats about the indexed elements.
        """
        index = index_project(root_tex)
        doc._index = index
        doc._rebuild_lookup()
        n = len(index["elements"])
        types: dict[str, int] = {}
        for e in index["elements"]:
            types[e["type"]] = types.get(e["type"], 0) + 1
        return {"total_elements": n, "by_type": types, "files": len(set(
            e["source_file"] for e in index["elements"]
        ))}

    # -----------------------------------------------------------------------
    # Read tools
    # -----------------------------------------------------------------------

    @mcp.tool()
    def list_elements(
        type: str | None = None,
        section: str | None = None,
        subsection: str | None = None,
        labeled_only: bool = False,
    ) -> list[dict]:
        """
        List all indexed elements, optionally filtered.

        - type: filter by element type (e.g. "theorem", "lemma", "equation",
          "definition", "proof", "figure", "section")
        - section: filter by section title (substring, case-insensitive)
        - subsection: filter by subsection title
        - labeled_only: only return elements that have an explicit \\label{}

        Returns a list of summary dicts with id, type, label, section, line_start.
        """
        return doc.list_elements(
            type=type, section=section,
            subsection=subsection, labeled_only=labeled_only,
        )

    @mcp.tool()
    def get_element(id_or_label: str) -> dict:
        """
        Get full details of a single element by its ID or \\label value.

        Returns the complete element dict including latex body, line range,
        source file, proof_id (if applicable), and cross-references.

        Raises an error if the element is not found.
        """
        return doc.get_element(id_or_label)

    @mcp.tool()
    def get_context(id_or_label: str, before: int = 2, after: int = 2) -> dict:
        """
        Get an element together with the N elements before and after it
        in document order.

        Useful for understanding the narrative context around an equation,
        theorem, or paragraph.

        Returns: { "target": {...}, "before": [...], "after": [...] }
        """
        return doc.get_context(id_or_label, before=before, after=after)

    @mcp.tool()
    def search_elements(
        query: str,
        type: str | None = None,
        section: str | None = None,
    ) -> list[dict]:
        """
        Full-text search over element content (both plain text and raw LaTeX).

        Returns matched elements as summary dicts, each with a 'snippet'
        field showing the matched text in context.
        """
        return doc.search_elements(query, type=type, section=section)

    @mcp.tool()
    def get_section(name: str) -> list[dict]:
        """
        Return all elements whose section title contains *name*.
        Useful for getting everything in, e.g., the "Security" section.
        """
        return doc.get_section(name)

    @mcp.tool()
    def get_proof(theorem_id: str) -> dict | None:
        """
        Return the proof element associated with a theorem, lemma, or proposition.

        *theorem_id* can be the element ID or \\label value of the theorem.
        Returns None if no proof is linked.
        """
        return doc.get_proof(theorem_id)

    @mcp.tool()
    def get_references(id_or_label: str) -> list[dict]:
        """
        Return all elements that contain a \\ref{} pointing to *id_or_label*.

        Useful for finding where a theorem, equation, or figure is cited.
        """
        return doc.get_references(id_or_label)

    @mcp.tool()
    def get_toc() -> list[dict]:
        """
        Return the full table of contents as a list of section entries,
        each with command, title, label, level, line, and source file.
        """
        return doc.get_toc()

    @mcp.tool()
    def validate() -> dict:
        """
        Validate the document and return a report with:
        - undefined_refs: \\ref{X} where X has no \\label
        - placeholder_cites: \\cite{?} or \\cite{TODO}
        - theorems_without_proofs: theorem-like elements with no linked proof
        - unlabeled_theorems: theorem-like elements lacking a \\label
        """
        return doc.validate()

    # -----------------------------------------------------------------------
    # Write tools
    # -----------------------------------------------------------------------

    @mcp.tool()
    def update_element(id_or_label: str, replacement_latex: str) -> dict:
        """
        Replace the body of an element with new LaTeX.

        The \\begin{X} / \\end{X} delimiters and \\label are preserved.
        Only the content between them is replaced.

        The source .tex file is modified in-place (a .bak backup is created).
        The index is automatically refreshed after the change.

        Returns the updated element dict.
        """
        return doc.update_element(id_or_label, replacement_latex)

    @mcp.tool()
    def insert_after(id_or_label: str, new_latex: str) -> dict:
        """
        Insert raw LaTeX immediately after an element in the source file.

        Use this to add a new paragraph, equation, or remark after an
        existing element without disturbing surrounding content.

        The index is automatically refreshed after the insertion.
        Returns the element after which the text was inserted.
        """
        return doc.insert_after(id_or_label, new_latex)

    @mcp.tool()
    def add_element(
        section: str,
        type: str,
        content: str,
        label: str | None = None,
        title: str | None = None,
    ) -> str:
        """
        Add a new environment to the end of *section*.

        - section: section title (substring match)
        - type: LaTeX environment name (e.g. "lemma", "definition", "equation")
        - content: the raw LaTeX body of the new environment
        - label: optional \\label value; auto-generated if omitted
        - title: optional theorem title [...] (e.g. "Main Lemma")

        Returns the ID of the newly created element.
        """
        return doc.add_element(section, type, content, label=label, title=title)

    @mcp.tool()
    def refresh_index() -> dict:
        """
        Rebuild the index from the current .tex source files and reload.

        Call this if .tex files were modified outside these tools.
        Returns element count by type.
        """
        doc.refresh()
        types: dict[str, int] = {}
        for e in doc._index.get("elements", []):
            types[e["type"]] = types.get(e["type"], 0) + 1
        return {"total": len(doc._index.get("elements", [])), "by_type": types}

    return mcp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m latex_tools.mcp_server <root.tex>", file=sys.stderr)
        sys.exit(1)
    root_tex = Path(sys.argv[1])
    server = create_server(root_tex)
    server.run()


if __name__ == "__main__":
    main()
