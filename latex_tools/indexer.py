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
    find_ai_blocks,
    find_display_math,
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
# Tree builder — assigns parent_id / children to every element
# ---------------------------------------------------------------------------

_SECTION_TYPES = {"part", "chapter", "section", "subsection", "subsubsection"}
_SECTION_LEVEL = {
    "part": 0, "chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4,
}


def _build_element_tree(elements: list[dict]) -> None:
    """
    Compute ``parent_id`` and ``children`` for every element in-place.

    When the annotator has run, section/subsection/subsubsection elements have
    proper byte ranges (from their AI block markers) and nest naturally via
    byte-range containment, just like every other block.

    Fallback: sections that still have ``byte_end == byte_start`` (annotator not
    run) get a computed effective end equal to the start of the next sibling-or-
    higher section in the same file, so the tree is still meaningful.
    """
    if not elements:
        return

    for e in elements:
        e["parent_id"] = None
        e["children"] = []

    by_id: dict[str, dict] = {e["id"]: e for e in elements}

    from collections import defaultdict

    # ------------------------------------------------------------------
    # Fallback effective ends for point-like section elements
    # (byte_end == byte_start means no AI block wrapping available).
    # ------------------------------------------------------------------
    sec_fallback_end: dict[str, int] = {}
    secs_by_file: dict[str, list[dict]] = defaultdict(list)
    for e in elements:
        if e["type"] in _SECTION_TYPES and e.get("byte_end", 0) == e.get("byte_start", 0):
            secs_by_file[e.get("source_file", "")].append(e)

    for file_path, file_secs in secs_by_file.items():
        file_secs_sorted = sorted(file_secs, key=lambda s: s["byte_start"])
        file_max = max(
            (e.get("byte_end", 0) for e in elements if e.get("source_file") == file_path),
            default=0,
        ) + 1
        for i, sec in enumerate(file_secs_sorted):
            lvl = _SECTION_LEVEL.get(sec["type"], 9)
            end = file_max
            for j in range(i + 1, len(file_secs_sorted)):
                nxt = file_secs_sorted[j]
                if _SECTION_LEVEL.get(nxt["type"], 9) <= lvl:
                    end = nxt["byte_start"]
                    break
            sec_fallback_end[sec["id"]] = end

    def _byte_end(e: dict) -> int:
        be = e.get("byte_end", e.get("byte_start", 0))
        bs = e.get("byte_start", 0)
        if be == bs and e["id"] in sec_fallback_end:
            return sec_fallback_end[e["id"]]
        return be

    # ------------------------------------------------------------------
    # Single byte-range containment sweep.
    # Sort: group by file first; within a file, outer containers
    # (larger byte ranges) sort before their children.
    # ------------------------------------------------------------------
    sorted_elems = sorted(
        elements,
        key=lambda e: (
            e.get("source_file", ""),
            e.get("byte_start", 0),
            -(_byte_end(e) - e.get("byte_start", 0)),
        ),
    )

    stack: list[tuple[int, str, str]] = []  # (byte_end, source_file, element_id)

    for elem in sorted_elems:
        bs = elem.get("byte_start", 0)
        be = _byte_end(elem)
        file_ = elem.get("source_file", "")

        # Pop containers that ended or belong to a different file
        while stack and (stack[-1][0] <= bs or stack[-1][1] != file_):
            stack.pop()

        if stack:
            parent_id = stack[-1][2]
            if parent_id != elem["id"]:
                elem["parent_id"] = parent_id
                by_id[parent_id]["children"].append(elem["id"])

        if be > bs:
            stack.append((be, file_, elem["id"]))

    # ------------------------------------------------------------------
    # Post-processing: deduplicate children lists and sort by byte_start.
    # ------------------------------------------------------------------
    for e in elements:
        seen: set[str] = set()
        unique: list[str] = []
        for cid in e["children"]:
            if cid not in seen:
                seen.add(cid)
                unique.append(cid)
        unique.sort(key=lambda cid: by_id[cid].get("byte_start", 0) if cid in by_id else 0)
        e["children"] = unique


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
        display_math = find_display_math(source, stripped=stripped)
        # Combine $$...$$ / \[..\] with \begin{align}..\end{align} etc.
        math_ranges = display_math + [
            (e["pos"], e["end"]) for e in envs if e["name"] in EQUATION_LIKE
        ]

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
                "content_truncated": False,
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
        # Index AI block markers (from annotator output) — stack-based, nesting-aware
        # -----------------------------------------------------------------------
        # Section types: when the annotator wraps a section with an AI block we update
        # the existing section element's byte range instead of creating a duplicate.
        _SEC_BLOCK_TYPES = {"section", "subsection", "subsubsection"}
        # Build a lookup for section elements indexed for this file
        sec_elem_by_id: dict[str, dict] = {
            e["id"]: e
            for e in elements
            if e.get("source_file") == str(file_path) and e["type"] in _SEC_BLOCK_TYPES
        }

        ai_block_ranges: list[tuple[int, int]] = []  # track for env dedup below
        for blk in find_ai_blocks(source):
            pos = blk["pos"]
            if pos < doc_start or pos > doc_end:
                continue
            block_content = blk["content"]
            sec_title, subsec_title = section_at_pos(sections, pos)

            # If this AI block wraps a section heading, update the existing section
            # element's byte range rather than creating a second element.
            if blk["type"] in _SEC_BLOCK_TYPES:
                existing = sec_elem_by_id.get(blk["id"])
                if existing is None:
                    # Position-based fallback: the annotator may use a different ID
                    # format (e.g. sec:star.slug.n for starred sections). Find the
                    # section element whose \section command falls inside this block.
                    for _se in sec_elem_by_id.values():
                        if pos <= _se["byte_start"] <= blk["end"]:
                            existing = _se
                            break
                if existing is not None:
                    old_id = existing["id"]
                    existing["id"] = blk["id"]  # adopt AI block ID
                    existing["byte_start"] = pos
                    existing["byte_end"] = blk["end"]
                    existing["line_start"] = byte_to_line(line_map, pos)
                    existing["line_end"] = byte_to_line(line_map, blk["end"] - 1)
                    existing["content"] = latex_to_text(block_content)[:2000]
                    existing["latex"] = block_content[:4000]
                    existing["content_truncated"] = len(block_content) > 4000
                    cross_refs[blk["id"]] = _extract_refs(block_content)
                    ai_block_ranges.append((pos, blk["end"]))
                    if old_id != blk["id"]:
                        sec_elem_by_id[blk["id"]] = existing
                        sec_elem_by_id.pop(old_id, None)
                        cross_refs.pop(old_id, None)
                        if label_to_id.get(existing.get("label")) == old_id:
                            label_to_id[existing["label"]] = blk["id"]
                    continue
                # Ultimate fallback: annotator block has no matching section element

            elem = {
                "id": blk["id"],
                "type": blk["type"],
                "label": None,
                "title": None,
                "section": sec_title,
                "subsection": subsec_title,
                "numbered": False,
                "content": latex_to_text(block_content)[:2000],
                "latex": block_content[:4000],
                "content_truncated": len(block_content) > 4000,
                "source_file": str(file_path),
                "line_start": byte_to_line(line_map, pos),
                "line_end": byte_to_line(line_map, blk["end"] - 1),
                "byte_start": pos,
                "byte_end": blk["end"],
            }
            elements.append(elem)
            cross_refs[blk["id"]] = _extract_refs(block_content)
            ai_block_ranges.append((pos, blk["end"]))

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

            # Skip environments nested inside any math context.
            # Use dm_s < pos (strict) so equation-like envs don't exclude themselves
            # (their own entry in math_ranges has dm_s == pos).
            if any(dm_s < pos < dm_e for dm_s, dm_e in math_ranges):
                continue

            # Skip environments already covered by their own AI block element
            # (annotator wraps theorems/proofs/figures/sections — avoid duplicates).
            # EQUATION_LIKE envs are exempt: annotator only adds \label, no AI block,
            # so they fall inside section AI blocks but must still be indexed here.
            if name not in EQUATION_LIKE and any(ab_s <= pos < ab_e for ab_s, ab_e in ai_block_ranges):
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
                "content_truncated": len(body_latex) > 4000,
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

        # -----------------------------------------------------------------------
        # Index display math ($$...$$ and \[...\]) as equation elements
        # -----------------------------------------------------------------------
        for dm_start, dm_end in display_math:
            if dm_start < doc_start or dm_start > doc_end:
                continue
            # Skip if already covered by an indexed element
            if any(
                e["byte_start"] <= dm_start < e["byte_end"]
                for e in elements
                if e.get("source_file") == str(file_path)
            ):
                continue
            sec_title, subsec_title = section_at_pos(sections, dm_start)
            sec_slug = slugify(sec_title) if sec_title else "doc"
            dm_id = _next_id("eq", sec_slug)
            dm_content = source[dm_start:dm_end]
            dm_elem: dict = {
                "id": dm_id,
                "type": "equation",
                "label": None,
                "title": None,
                "section": sec_title,
                "subsection": subsec_title,
                "numbered": False,
                "content": latex_to_text(dm_content)[:2000],
                "latex": dm_content[:4000],
                "content_truncated": len(dm_content) > 4000,
                "source_file": str(file_path),
                "line_start": byte_to_line(line_map, dm_start),
                "line_end": byte_to_line(line_map, dm_end - 1),
                "byte_start": dm_start,
                "byte_end": dm_end,
            }
            elements.append(dm_elem)
            cross_refs[dm_id] = _extract_refs(dm_content)

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
        # Exclude display math blocks so paragraphs don't straddle them
        excluded_ranges.extend(display_math)
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
                "content_truncated": len(para["content"]) > 4000,
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

    # -----------------------------------------------------------------------
    # Build parent_id / children tree
    # -----------------------------------------------------------------------
    _build_element_tree(elements)

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
