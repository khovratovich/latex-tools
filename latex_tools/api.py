"""
api.py — Tool 3: LatexDocument — the logical query API.

All reads go through this class.  Agents should never need to open a raw
.tex file; every interaction is via typed methods that return plain dicts.

Write operations (update_element, insert_after, add_element) modify the
source .tex file using the stored byte offsets, then call refresh() to
rebuild the index.

Usage:
    doc = LatexDocument.load("paper.tex")
    doc.list_elements(type="lemma")
    doc.get_element("eq:op")
    doc.get_context("lem:convergence", before=2, after=2)
    doc.search_elements("memory", type="equation")
    doc.update_element("eq:op", r"S := (\\pc, \\regs, \\mem)")
    doc.validate()
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from latex_tools.indexer import build_index, index_project, load_index, save_index
from latex_tools.parser import latex_to_text, THEOREM_LIKE


# ---------------------------------------------------------------------------
# LatexDocument
# ---------------------------------------------------------------------------

class LatexDocument:
    """
    In-memory representation of a LaTeX project, backed by a .latex-index.json.

    All methods return plain Python dicts / lists — fully JSON-serializable.
    """

    def __init__(self, index: dict) -> None:
        self._index = index
        self._rebuild_lookup()

    # -----------------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "LatexDocument":
        """
        Load a LatexDocument from either:
          - A root .tex file  → index is built (and saved) on the fly
          - A .latex-index.json file → loaded directly
          - A directory       → looks for .latex-index.json inside it
        """
        path = Path(path)
        if path.suffix == ".json" or (path.is_file() and path.name == ".latex-index.json"):
            return cls(load_index(path))
        if path.is_dir():
            idx_file = path / ".latex-index.json"
            if idx_file.exists():
                return cls(load_index(idx_file))
            # Find the root .tex (the one with \begin{document})
            root = _find_root_tex(path)
            if root is None:
                raise FileNotFoundError(f"No root .tex file found in {path}")
            return cls(index_project(root))
        # Assume it's a .tex file
        return cls(index_project(path))

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _rebuild_lookup(self) -> None:
        """Build fast lookup structures from the current index."""
        self._by_id: dict[str, dict] = {}
        self._by_label: dict[str, dict] = {}
        for elem in self._index.get("elements", []):
            self._by_id[elem["id"]] = elem
            if elem.get("label"):
                self._by_label[elem["label"]] = elem

    def _resolve(self, id_or_label: str) -> dict | None:
        return self._by_id.get(id_or_label) or self._by_label.get(id_or_label)

    def _require(self, id_or_label: str) -> dict:
        elem = self._resolve(id_or_label)
        if elem is None:
            raise KeyError(f"Element '{id_or_label}' not found in the index")
        return elem

    def _fetch_source(self, elem: dict) -> str:
        """Read the full LaTeX source block for *elem* directly from its source file."""
        path = Path(elem["source_file"])
        source = path.read_text(encoding="utf-8", errors="replace")
        return source[elem["byte_start"]:elem["byte_end"]]

    # -----------------------------------------------------------------------
    # Read operations
    # -----------------------------------------------------------------------

    def list_elements(
        self,
        *,
        type: str | None = None,
        section: str | None = None,
        subsection: str | None = None,
        labeled_only: bool = False,
    ) -> list[dict]:
        """
        List all indexed elements, optionally filtered.

        Returns a summary list (id, type, label, title, section, line_start).
        """
        results = []
        for elem in self._index.get("elements", []):
            if type and elem.get("type") != type:
                continue
            if section and not _icontains(elem.get("section") or "", section):
                continue
            if subsection and not _icontains(elem.get("subsection") or "", subsection):
                continue
            if labeled_only and not elem.get("label"):
                continue
            results.append(_summary(elem))
        return results

    def get_element(self, id_or_label: str) -> dict:
        """
        Return full element dict for *id_or_label*.

        If the element's ``content_truncated`` flag is set, ``latex`` and
        ``content`` are fetched from the source file on demand so the full
        text is always returned.

        Raises KeyError if not found.
        """
        elem = dict(self._require(id_or_label))
        if elem.get("content_truncated"):
            full_latex = self._fetch_source(elem)
            elem["latex"] = full_latex
            elem["content"] = latex_to_text(full_latex)
            elem["content_truncated"] = False
        return elem

    def get_latex(self, id_or_label: str) -> str:
        """
        Return the complete LaTeX source for *id_or_label*, always reading
        from the source file via the stored byte range.  Never truncated.
        """
        return self._fetch_source(self._require(id_or_label))

    def get_context(
        self,
        id_or_label: str,
        *,
        before: int = 2,
        after: int = 2,
    ) -> dict:
        """
        Return the target element plus *before* elements before it and
        *after* elements after it (in document order).

        Returns:
            {
              "target": {...},
              "before": [...],
              "after": [...]
            }
        """
        target = self._require(id_or_label)
        elements = self._index.get("elements", [])
        idx = next((i for i, e in enumerate(elements) if e["id"] == target["id"]), None)
        if idx is None:
            return {"target": target, "before": [], "after": []}
        return {
            "target": dict(target),
            "before": [_summary(e) for e in elements[max(0, idx - before) : idx]],
            "after": [_summary(e) for e in elements[idx + 1 : idx + 1 + after]],
        }

    def search_elements(
        self,
        query: str,
        *,
        type: str | None = None,
        section: str | None = None,
        case_sensitive: bool = False,
    ) -> list[dict]:
        """
        Full-text search over element content (plain-text and LaTeX).

        Returns matched elements as summary dicts with a 'snippet' field.
        """
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(re.escape(query), flags)
        except re.error:
            pattern = re.compile(query, flags)

        results = []
        for elem in self._index.get("elements", []):
            if type and elem.get("type") != type:
                continue
            if section and not _icontains(elem.get("section") or "", section):
                continue
            content = elem.get("content", "") + " " + elem.get("latex", "")
            m = pattern.search(content)
            if m:
                summary = _summary(elem)
                # Extract snippet around match
                start = max(0, m.start() - 60)
                end = min(len(content), m.end() + 60)
                summary["snippet"] = "..." + content[start:end] + "..."
                results.append(summary)
        return results

    def search_latex(
        self,
        query: str,
        *,
        type: str | None = None,
        case_sensitive: bool = False,
    ) -> list[str]:
        """
        Search all elements' **full** LaTeX source for *query* (substring or
        regex).  Unlike ``search_elements()``, this always checks the complete
        source text even for large blocks where the index only stores a
        truncated preview — those are read from disk, grouped per source file
        so each file is opened at most once.

        Returns element IDs in document order.
        """
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            needle = re.compile(re.escape(query), flags)
        except re.error:
            needle = re.compile(query, flags)

        cached: list[dict] = []
        by_file: dict[str, list[dict]] = {}

        for elem in self._index.get("elements", []):
            if type and elem.get("type") != type:
                continue
            if elem.get("content_truncated"):
                by_file.setdefault(elem["source_file"], []).append(elem)
            else:
                cached.append(elem)

        hits: list[str] = []

        for elem in cached:
            if needle.search(elem.get("latex", "")):
                hits.append(elem["id"])

        for file_path, file_elems in by_file.items():
            try:
                source = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for elem in file_elems:
                if needle.search(source[elem["byte_start"]:elem["byte_end"]]):
                    hits.append(elem["id"])

        order = {e["id"]: i for i, e in enumerate(self._index.get("elements", []))}
        hits.sort(key=lambda eid: order.get(eid, 0))
        return hits

    def get_section(self, name: str) -> list[dict]:
        """
        Return all elements whose section title contains *name* (case-insensitive).
        """
        return [
            _summary(e)
            for e in self._index.get("elements", [])
            if _icontains(e.get("section") or "", name)
        ]

    def get_proof(self, theorem_id: str) -> dict | None:
        """
        Return the proof element associated with a theorem/lemma/proposition.

        Looks up `proof_id` on the theorem element first, then falls back to
        searching for a proof element whose `statement_id` matches.
        """
        thm = self._resolve(theorem_id)
        if thm is None:
            raise KeyError(f"Element '{theorem_id}' not found")

        # Direct proof_id link
        if thm.get("proof_id"):
            return self._by_id.get(thm["proof_id"])

        # Fallback: search proof elements that reference this theorem
        tid = thm["id"]
        for elem in self._index.get("elements", []):
            if elem.get("type") == "proof":
                if elem.get("statement_id") in (tid, thm.get("label")):
                    return dict(elem)
        return None

    def get_references(self, id_or_label: str) -> list[dict]:
        """
        Return all elements that contain a \\ref{} to *id_or_label*.
        """
        target = self._require(id_or_label)
        target_label = target.get("label") or target["id"]
        ref_map = self._index.get("ref_to_elements", {})
        element_ids = ref_map.get(target_label, [])
        return [_summary(self._by_id[eid]) for eid in element_ids if eid in self._by_id]

    def get_toc(self) -> list[dict]:
        """Return the table of contents."""
        return list(self._index.get("toc", []))

    def validate(self) -> dict:
        """
        Validate the document and return a report dict:
            {
              "undefined_refs": [...],
              "placeholder_cites": [...],
              "theorems_without_proofs": [...],
              "unlabeled_theorems": [...],
              "warnings": [...]
            }
        """
        all_labels: set[str] = {
            e["label"] for e in self._index.get("elements", []) if e.get("label")
        }

        undefined_refs: list[dict] = []
        placeholder_cites: list[dict] = []

        _ref_re = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref)\{([^}]+)\}")
        _cite_re = re.compile(r"\\cite[a-zA-Z]*\{([^}]+)\}")

        for elem in self._index.get("elements", []):
            latex_body = elem.get("latex", "")
            for m in _ref_re.finditer(latex_body):
                for key in m.group(1).split(","):
                    key = key.strip()
                    if key and key not in all_labels:
                        undefined_refs.append({
                            "ref": key,
                            "in_element": elem["id"],
                            "file": elem.get("source_file"),
                            "line": elem.get("line_start"),
                        })
            for m in _cite_re.finditer(latex_body):
                for key in m.group(1).split(","):
                    key = key.strip()
                    if key in ("?", "", "TODO", "todo", "FIXME"):
                        placeholder_cites.append({
                            "cite_key": key,
                            "in_element": elem["id"],
                            "file": elem.get("source_file"),
                            "line": elem.get("line_start"),
                        })

        theorems_without_proofs = [
            _summary(e)
            for e in self._index.get("elements", [])
            if e.get("type") in THEOREM_LIKE
            and not e.get("proof_id")
        ]

        unlabeled_theorems = [
            _summary(e)
            for e in self._index.get("elements", [])
            if e.get("type") in THEOREM_LIKE
            and not e.get("label")
        ]

        return {
            "undefined_refs": undefined_refs,
            "placeholder_cites": placeholder_cites,
            "theorems_without_proofs": theorems_without_proofs,
            "unlabeled_theorems": unlabeled_theorems,
            "warnings": [],
        }

    # -----------------------------------------------------------------------
    # Write operations
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Tree navigation
    # -----------------------------------------------------------------------

    def get_children(self, id_or_label: str) -> list[dict]:
        """Return direct child elements of *id_or_label* in document order."""
        elem = self._require(id_or_label)
        return [
            _summary(self._by_id[cid])
            for cid in elem.get("children", [])
            if cid in self._by_id
        ]

    def get_parent(self, id_or_label: str) -> dict | None:
        """Return the parent element, or None if *id_or_label* is top-level."""
        elem = self._require(id_or_label)
        pid = elem.get("parent_id")
        if pid and pid in self._by_id:
            return dict(self._by_id[pid])
        return None

    def get_subtree(
        self,
        id_or_label: str,
        *,
        max_depth: int = 5,
        include_content: bool = False,
    ) -> dict:
        """
        Return a nested dict representing *id_or_label* and all its
        descendants up to *max_depth* levels deep.

        Each node has the element's summary fields plus a ``children`` list
        of nested nodes.  Set *include_content* to True to include the
        ``content`` and ``latex`` fields (omitted by default for brevity).
        """
        elem = self._require(id_or_label)
        return self._node(elem, max_depth, include_content)

    def _node(self, elem: dict, depth: int, include_content: bool) -> dict:
        node = _summary(elem)
        if include_content:
            if elem.get("content_truncated"):
                full_latex = self._fetch_source(elem)
                node["content"] = latex_to_text(full_latex)
                node["latex"] = full_latex
            else:
                node["content"] = elem.get("content", "")
                node["latex"] = elem.get("latex", "")
        if depth > 0:
            node["children"] = [
                self._node(self._by_id[cid], depth - 1, include_content)
                for cid in elem.get("children", [])
                if cid in self._by_id
            ]
        else:
            node["children"] = [f"... ({len(elem.get('children', []))} children)"]
        return node

    def update_element(self, id_or_label: str, replacement_latex: str) -> dict:
        """
        Replace the *body* of an element with *replacement_latex*.

        The `\\begin{X}` and `\\end{X}` delimiters are preserved; only the
        body between them is replaced.  The source .tex file is modified
        in-place (a .bak is written first) and the index is refreshed.

        Returns the updated element dict.
        """
        elem = self._require(id_or_label)
        file_path = Path(elem["source_file"])
        source = file_path.read_text(encoding="utf-8", errors="replace")

        byte_start = elem["byte_start"]
        byte_end = elem["byte_end"]
        old_block = source[byte_start:byte_end]

        # Find body start and end within the block
        elem_type = elem.get("type", "")
        env_name = _type_to_env(elem_type, old_block)
        if env_name:
            begin_tag = f"\\begin{{{env_name}}}"
            end_tag = f"\\end{{{env_name}}}"
            inner_start = old_block.find(begin_tag)
            inner_end = old_block.rfind(end_tag)
            if inner_start != -1 and inner_end != -1:
                inner_start_abs = byte_start + inner_start + len(begin_tag)
                inner_end_abs = byte_start + inner_end
                # Preserve any optional args and \label immediately after \begin{X}
                header = old_block[inner_start + len(begin_tag) : inner_end]
                # Keep everything up to first non-arg, non-label content
                preamble_end = _body_preamble_end(header)
                preamble = header[:preamble_end]
                new_block = (
                    source[:inner_start_abs]
                    + preamble
                    + "\n"
                    + replacement_latex
                    + "\n"
                    + source[inner_end_abs:]
                )
                _write_with_backup(file_path, new_block)
                self.refresh()
                return self.get_element(id_or_label)

        # Fallback: replace entire block
        new_source = source[:byte_start] + replacement_latex + source[byte_end:]
        _write_with_backup(file_path, new_source)
        self.refresh()
        return self.get_element(id_or_label)

    def insert_after(self, id_or_label: str, new_latex: str) -> dict:
        """
        Insert *new_latex* immediately after the element identified by
        *id_or_label* in the source .tex file.

        Returns the element after which the insertion was made.
        """
        elem = self._require(id_or_label)
        file_path = Path(elem["source_file"])
        source = file_path.read_text(encoding="utf-8", errors="replace")
        byte_end = elem["byte_end"]
        new_source = source[:byte_end] + "\n\n" + new_latex + "\n" + source[byte_end:]
        _write_with_backup(file_path, new_source)
        self.refresh()
        return dict(elem)

    def add_element(
        self,
        section: str,
        type: str,
        content: str,
        *,
        label: str | None = None,
        title: str | None = None,
    ) -> str:
        """
        Append a new environment to the end of *section*.

        *type* should be a LaTeX environment name (e.g. "lemma", "equation").
        *content* is the raw LaTeX body.
        *label* is the optional \\label value; one is auto-generated if not given.
        *title* is the optional theorem title [...].

        Returns the ID of the newly created element.
        """
        # Find the last element in the requested section to determine insertion file/pos
        section_elems = [
            e for e in self._index.get("elements", [])
            if _icontains(e.get("section") or "", section)
        ]
        if not section_elems:
            raise ValueError(f"Section '{section}' not found in index")

        last_elem = section_elems[-1]
        file_path = Path(last_elem["source_file"])
        source = file_path.read_text(encoding="utf-8", errors="replace")
        byte_end = last_elem["byte_end"]

        # Build new environment
        env_name = type
        label_line = f"\\label{{{label}}}" if label else ""
        title_part = f"[{title}]" if title else ""
        new_env = (
            f"\n\n\\begin{{{env_name}}}{title_part}{label_line}\n"
            f"{content}\n"
            f"\\end{{{env_name}}}\n"
        )

        new_source = source[:byte_end] + new_env + source[byte_end:]
        _write_with_backup(file_path, new_source)
        self.refresh()

        # Return the ID of the newly created element
        if label:
            return label
        # find last element of this type in section
        candidates = [
            e for e in self._index.get("elements", [])
            if e.get("type") == type and _icontains(e.get("section") or "", section)
        ]
        return candidates[-1]["id"] if candidates else "unknown"

    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the index from the current .tex source files and reload."""
        root = Path(self._index["root"])
        self._index = build_index(root)
        save_index(self._index)
        self._rebuild_lookup()

    @property
    def document_name(self) -> str:
        return self._index.get("document", "")

    @property
    def root_path(self) -> Path:
        return Path(self._index["root"])

    def __repr__(self) -> str:
        n = len(self._index.get("elements", []))
        return f"LatexDocument('{self.document_name}', {n} elements)"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _summary(elem: dict) -> dict:
    """Return a lightweight summary dict for list/search results."""
    return {
        "id": elem["id"],
        "type": elem.get("type"),
        "label": elem.get("label"),
        "title": elem.get("title"),
        "section": elem.get("section"),
        "subsection": elem.get("subsection"),
        "line_start": elem.get("line_start"),
        "source_file": elem.get("source_file"),
        "content_preview": (elem.get("content") or "")[:120],
    }


def _icontains(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


def _type_to_env(elem_type: str, block: str) -> str | None:
    """Guess the environment name from element type and the raw block."""
    # Try to extract from \begin{X} in the block
    m = re.match(r"\\begin\{([\w@*]+)\}", block.strip())
    if m:
        return m.group(1)
    return None


def _body_preamble_end(header: str) -> int:
    """
    Find where preamble (optional args + \\label) ends inside environment header.
    """
    i = 0
    # Skip whitespace
    while i < len(header) and header[i] in (" ", "\t", "\n"):
        i += 1
    # Skip optional args [...]
    while i < len(header) and header[i] == "[":
        depth = 0
        while i < len(header):
            if header[i] == "[":
                depth += 1
            elif header[i] == "]":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
    # Skip \label{...}
    label_m = re.match(r"\s*\\label\{[^}]+\}", header[i:])
    if label_m:
        i += label_m.end()
    return i


def _write_with_backup(path: Path, new_content: str) -> None:
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    path.write_text(new_content, encoding="utf-8")


def _find_root_tex(directory: Path) -> Path | None:
    """Find a .tex file containing \\begin{document} in *directory*."""
    for tex in directory.glob("*.tex"):
        try:
            content = tex.read_text(encoding="utf-8", errors="replace")
            if r"\begin{document}" in content:
                return tex
        except OSError:
            continue
    return None
