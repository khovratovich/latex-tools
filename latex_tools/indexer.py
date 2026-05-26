"""
indexer.py — Tool 2: parse a LaTeX project and build a structured index.

Output: .latex-index.json (stored next to the root .tex file).

Index format:
{
  "document": "w1-sample.tex",
  "root": "/abs/path/to/w1-sample.tex",
  "generated_at": "ISO8601",
  "elements": [ { ...element... }, ... ],
  "toc": [ { command, title, label, level, line, file }, ... ],
  "cross_refs": { "id": ["eq:op", ...], ... }   # \ref usages per element
}

Element dict keys:
  id, type, label, title, section, subsection, numbered,
  content (plain text approximation), latex (raw LaTeX body),
  source_file, line_start, line_end, byte_start, byte_end,
  proof_id (on theorem-like), statement_id (on proof elements)
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from latex_tools.parser import (
    EQUATION_LIKE,
    FLOAT_LIKE,
    LIST_LIKE,
    OPAQUE_LIKE,
    PROOF_LIKE,
    THEOREM_LIKE,
    TIKZ_LIKE,
    VERBATIM_LIKE,
    _is_macro_file,
    build_line_map,
    byte_to_line,
    find_document_body,
    find_environments,
    find_paragraphs_between,
    find_sections,
    latex_to_text,
    resolve_includes,
    section_at_pos,
    section_command_span,
    slugify,
    strip_comments,
)

# ---------------------------------------------------------------------------
# Environment → element type mapping
# ---------------------------------------------------------------------------

_ENV_TYPE: dict[str, str] = {
    "figure": "figure",
    "figure*": "figure",
    "table": "table",
    "table*": "table",
    "proof": "proof",
    "itemize": "itemize",
    "enumerate": "enumerate",
    "description": "description",
}
for _n in THEOREM_LIKE:
    _ENV_TYPE[_n] = _n  # theorem → theorem, lemma → lemma, etc.
for _n in EQUATION_LIKE:
    _ENV_TYPE[_n] = "equation"

# Environments we index but mark as opaque (no sub-parsing)
_OPAQUE_TYPES = VERBATIM_LIKE | TIKZ_LIKE | OPAQUE_LIKE

# Environments we skip entirely in the index
_SKIP_ENTIRELY = {
    "document", "center", "minipage", "abstract",
    "tikzpicture", "tikzcd",  # already in TIKZ_LIKE but be explicit
}

# Regex patterns
_REF_RE = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref)\{([^}]+)\}")
_CITE_RE = re.compile(r"\\cite[a-zA-Z]*\{([^}]+)\}")
_PROOF_FOR_RE = re.compile(r"\\begin\{proof\}\s*(?:\[([^\]]*)\])?")
_AI_BLOCK_RE = re.compile(
    r"%\s*<ai:block\s+id=\"([^\"]+)\"\s+type=\"([^\"]+)\">(.*?)%\s*</ai:block>",
    re.DOTALL,
)
_SECTION_LABEL_INLINE_RE = re.compile(r"\\label\{([^}]+)\}")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _body(env_content: str, env_name: str) -> str:
    """Extract the text between \\begin{X} and \\end{X}."""
    begin_tag = f"\\begin{{{env_name}}}"
    end_tag = f"\\end{{{env_name}}}"
    s = env_content
    start = s.find(begin_tag)
    if start != -1:
        start += len(begin_tag)
        # skip optional args
        i = start
        while i < len(s) and s[i] in (" ", "\t", "\n"):
            i += 1
        if i < len(s) and s[i] == "[":
            depth = 0
            while i < len(s):
                if s[i] == "[":
                    depth += 1
                elif s[i] == "]":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
        start = i
    else:
        start = 0
    end = s.rfind(end_tag)
    if end == -1:
        end = len(s)
    return s[start:end].strip()


def _extract_refs(text: str) -> list[str]:
    refs = []
    for m in _REF_RE.finditer(text):
        refs.extend(k.strip() for k in m.group(1).split(","))
    return refs


def _extract_cites(text: str) -> list[str]:
    cites = []
    for m in _CITE_RE.finditer(text):
        cites.extend(k.strip() for k in m.group(1).split(","))
    return cites


# ---------------------------------------------------------------------------
# Core indexer
# ---------------------------------------------------------------------------

def build_index(root_tex: Path | str) -> dict:
    """
    Parse the LaTeX project rooted at *root_tex* and return the full index dict.
    """
    root_tex = Path(root_tex).resolve()
    files = resolve_includes(root_tex)

    elements: list[dict] = []
    toc: list[dict] = []
    # cross_refs: element_id → list of \ref targets used inside that element
    cross_refs: dict[str, list[str]] = {}
    # label → element_id (for resolving proof linkage)
    label_to_id: dict[str, str] = {}

    # Used to generate auto-IDs for unlabeled elements
    counters: dict[str, int] = {}

    def _next_id(prefix: str, slug: str) -> str:
        key = f"{prefix}:{slug}"
        counters[key] = counters.get(key, 0) + 1
        return f"{prefix}:auto.{slug}.{counters[key]}"

    # -----------------------------------------------------------------------
    # Section state shared across files
    # -----------------------------------------------------------------------
    # We process files in include-order; sections carry over across files
    # so that "section_at_pos" works within each file independently.
    # The TOC is built from all files in order.

    for file_path in files:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        stripped = strip_comments(source)
        line_map = build_line_map(source)
        doc_start, doc_end = find_document_body(source)
        sections = find_sections(source, stripped=stripped)
        envs = find_environments(source, stripped=stripped)

        # Add sections to TOC
        for sec in sections:
            if sec["pos"] < doc_start:
                continue
            toc_entry = {
                "command": sec["command"],
                "title": sec["title"],
                "label": sec["label"],
                "level": sec["level"],
                "line": sec["line"],
                "file": str(file_path),
            }
            toc.append(toc_entry)
            # Also index sections as elements
            sec_id = sec["label"] or _next_id("sec", slugify(sec["title"]) or "section")
            elem = {
                "id": sec_id,
                "type": sec["command"],
                "label": sec["label"],
                "title": sec["title"],
                "section": sec["title"] if sec["command"] == "section" else None,
                "subsection": sec["title"] if sec["command"] == "subsection" else None,
                "numbered": not sec["starred"],
                "content": sec["title"],
                "latex": sec["title"],
                "source_file": str(file_path),
                "line_start": sec["line"],
                "line_end": sec["line"],
                "byte_start": sec["pos"],
                "byte_end": sec["pos"],
            }
            elements.append(elem)
            if sec["label"]:
                label_to_id[sec["label"]] = sec_id

        # -----------------------------------------------------------------------
        # Index AI block markers (from annotator output)
        # -----------------------------------------------------------------------
        for ai_m in _AI_BLOCK_RE.finditer(source):
            block_id = ai_m.group(1)
            block_type = ai_m.group(2)
            block_content = ai_m.group(3).strip()
            pos = ai_m.start()
            if pos < doc_start or pos > doc_end:
                continue
            sec_title, subsec_title = section_at_pos(sections, pos)
            elem = {
                "id": block_id,
                "type": block_type,
                "label": None,
                "title": None,
                "section": sec_title,
                "subsection": subsec_title,
                "numbered": False,
                "content": latex_to_text(block_content),
                "latex": block_content,
                "source_file": str(file_path),
                "line_start": byte_to_line(line_map, pos),
                "line_end": byte_to_line(line_map, ai_m.end() - 1),
                "byte_start": pos,
                "byte_end": ai_m.end(),
            }
            elements.append(elem)
            cross_refs[block_id] = _extract_refs(block_content)

        # -----------------------------------------------------------------------
        # Index environments
        # -----------------------------------------------------------------------
        for env in envs:
            name = env["name"]
            label = env["label"]
            pos = env["pos"]
            env_end = env["end"]
            content = env["content"]

            if pos < doc_start or pos > doc_end:
                continue

            # Skip opaque rendering environments (index them but don't sub-parse)
            skip = name in _SKIP_ENTIRELY
            if skip:
                continue

            # Determine element type
            elem_type = _ENV_TYPE.get(name)
            if elem_type is None:
                # Unknown environment — index as generic "environment"
                elem_type = "environment"

            sec_title, subsec_title = section_at_pos(sections, pos)
            sec_slug = slugify(sec_title) if sec_title else "doc"

            # Determine stable ID
            if label:
                elem_id = label
            else:
                prefix = {
                    "equation": "eq",
                    "proof": "proof",
                    "figure": "fig",
                    "table": "tbl",
                }.get(elem_type, elem_type[:3] if len(elem_type) >= 3 else elem_type)
                elem_id = _next_id(f"{prefix}", sec_slug)

            body_latex = _body(content, name)
            plain_content = latex_to_text(body_latex)
            numbered = not name.endswith("*")

            elem: dict = {
                "id": elem_id,
                "type": elem_type,
                "label": label,
                "title": None,
                "section": sec_title,
                "subsection": subsec_title,
                "numbered": numbered,
                "content": plain_content[:2000],  # cap for large environments
                "latex": body_latex[:4000],
                "source_file": str(file_path),
                "line_start": env["line_start"],
                "line_end": env["line_end"],
                "byte_start": pos,
                "byte_end": env_end,
            }

            # Extract theorem optional title [...]
            title_m = re.match(r"\\begin\{[^}]+\}\s*(?:\\label\{[^}]+\})?\s*\[([^\]]+)\]", content)
            if title_m:
                elem["title"] = title_m.group(1).strip()

            # Proof linkage: \begin{proof}[Proof of lemma X] or proximity
            if elem_type == "proof":
                proof_of_m = re.match(r"\\begin\{proof\}\s*\[([^\]]+)\]", content)
                if proof_of_m:
                    elem["statement_id"] = proof_of_m.group(1).strip()

            elements.append(elem)
            if label:
                label_to_id[label] = elem_id
            cross_refs[elem_id] = _extract_refs(body_latex)

        # -----------------------------------------------------------------------
        # Index prose paragraphs (text between known elements)
        # -----------------------------------------------------------------------
        # Skip macro-only files (no document body, just \newcommand definitions)
        if _is_macro_file(file_path):
            continue

        excluded_ranges: list[tuple[int, int]] = [
            (e["byte_start"], e["byte_end"])
            for e in elements
            if e.get("source_file") == str(file_path)
            and e["byte_start"] < e["byte_end"]
        ]
        excluded_ranges += [
            section_command_span(source, s)
            for s in sections
            if s["pos"] >= doc_start
        ]
        # Also exclude \input / \include / \subfile command lines
        for inc_m in re.finditer(
            r"\\(?:input|include|subfile|subfileinclude)\s*\{[^}]*\}",
            source,
        ):
            if doc_start <= inc_m.start() < doc_end:
                excluded_ranges.append((inc_m.start(), inc_m.end()))

        for para in find_paragraphs_between(source, excluded_ranges, doc_start, doc_end):
            # Skip if already covered by a known element at this byte position
            already = any(
                e["byte_start"] <= para["pos"] < e["byte_end"]
                for e in elements
                if e.get("source_file") == str(file_path)
            )
            if already:
                continue
            sec_title, subsec_title = section_at_pos(sections, para["pos"])
            sec_slug = slugify(sec_title) if sec_title else "doc"
            para_id = _next_id("para", sec_slug)
            para_elem: dict = {
                "id": para_id,
                "type": "paragraph",
                "label": None,
                "title": None,
                "section": sec_title,
                "subsection": subsec_title,
                "numbered": False,
                "content": latex_to_text(para["content"])[:2000],
                "latex": para["content"][:4000],
                "source_file": str(file_path),
                "line_start": para["line_start"],
                "line_end": para["line_end"],
                "byte_start": para["pos"],
                "byte_end": para["end"],
            }
            elements.append(para_elem)
            cross_refs[para_id] = _extract_refs(para["content"])

    # -----------------------------------------------------------------------
    # For each theorem-like element, find the next proof element
    theorem_elements = [e for e in elements if e["type"] in THEOREM_LIKE]
    proof_elements = [e for e in elements if e["type"] == "proof"]

    # Map proof statement_id (label reference) → actual element ID
    for proof_elem in proof_elements:
        raw_sid = proof_elem.get("statement_id")
        if raw_sid and raw_sid in label_to_id:
            proof_elem["statement_id"] = label_to_id[raw_sid]

    # For each theorem, find the proof that references it (or is adjacent)
    labeled_theorems = {e["label"]: e for e in theorem_elements if e["label"]}
    for proof_elem in proof_elements:
        sid = proof_elem.get("statement_id")
        if sid:
            # Find theorem with this label or ID
            for thm in theorem_elements:
                if thm["label"] == sid or thm["id"] == sid:
                    thm["proof_id"] = proof_elem["id"]
                    break

    # -----------------------------------------------------------------------
    # Build cross_refs index: label → list of element IDs that \ref it
    # -----------------------------------------------------------------------
    ref_to_elements: dict[str, list[str]] = {}
    for elem_id, refs in cross_refs.items():
        for ref in refs:
            ref_to_elements.setdefault(ref, []).append(elem_id)

    return {
        "document": root_tex.name,
        "root": str(root_tex),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elements": elements,
        "toc": toc,
        "cross_refs": cross_refs,
        "ref_to_elements": ref_to_elements,
    }


def save_index(index: dict, output_path: Path | str | None = None) -> Path:
    """
    Save index to JSON.  If *output_path* is None, saves as
    .latex-index.json next to the root .tex file.
    """
    if output_path is None:
        root = Path(index["root"])
        output_path = root.parent / ".latex-index.json"
    output_path = Path(output_path)
    output_path.write_text(
        json.dumps(index, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return output_path


def load_index(path: Path | str) -> dict:
    """Load an index from a .latex-index.json file."""
    path = Path(path)
    if path.is_dir():
        path = path / ".latex-index.json"
    return json.loads(path.read_text(encoding="utf-8"))


def index_project(root_tex: Path | str, *, save: bool = True) -> dict:
    """Build index for *root_tex* and optionally save it.  Returns the index."""
    index = build_index(root_tex)
    if save:
        save_index(index)
    return index
