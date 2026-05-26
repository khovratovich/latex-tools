"""
annotator.py — Tool 1: enrich .tex files with stable addresses.

What it does per file:
  • Theorem-like envs without \\label  → insert \\label{TYPE:auto.SLUG.N}
  • Equation-like envs without \\label → insert \\label{eq:auto.SLUG.N}
    (starred variants are skipped — unnumbered by design)
  • \\section / \\subsection / \\subsubsection without \\label
    → append \\label{sec:auto.SLUG} on the same line
  • Unknown/custom environments without markers
    → wrap with %<ai:block id="TYPE:auto.SLUG.N"> / %</ai:block>
  • Prose paragraphs between environments
    → wrap with %<ai:block id="para:auto.SLUG.N" type="paragraph">

All operations are idempotent: already-labeled/marked elements are skipped.
A .tex.bak backup is written before any in-place modification.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import NamedTuple

from latex_tools.parser import (
    EQUATION_LIKE,
    THEOREM_LIKE,
    PROOF_LIKE,
    FLOAT_LIKE,
    OPAQUE_LIKE,
    VERBATIM_LIKE,
    TIKZ_LIKE,
    _is_macro_file,
    build_line_map,
    byte_to_line,
    find_ai_blocks,
    find_display_math,
    find_document_body,
    find_environments,
    find_paragraphs_between,
    find_sections,
    resolve_includes,
    section_at_pos,
    section_command_span,
    slugify,
    strip_comments,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Environments for which we insert a \label{} (standard LaTeX environments)
_LABELABLE = THEOREM_LIKE | PROOF_LIKE | FLOAT_LIKE

# Environments we annotate with AI block comments (custom/non-standard)
_AI_BLOCK_CUSTOM = set()  # populated dynamically: anything not in known sets

# Environments we skip entirely (opaque content, verbatim, tikz)
_SKIP_LABEL = VERBATIM_LIKE | TIKZ_LIKE | OPAQUE_LIKE | {"document", "instruction"}

_LABEL_RE = re.compile(r"\\label\{")
_AI_BLOCK_RE = re.compile(r"%\s*<ai:block\s")
_AI_BLOCK_END_RE = re.compile(r"%\s*</ai:block>")

# For detecting if a section already has a \label on the same or next line
_SECTION_LABEL_NEARBY_RE = re.compile(r"\\label\{[^}]+\}")


class _Insertion(NamedTuple):
    """A pending text insertion at a byte offset."""
    pos: int           # byte position in original source where text is inserted
    text: str          # text to insert
    priority: int = 1  # sort tiebreaker at same pos: lower = processed first = appears last
                       # opens: 0, default: 1, section-close: 10+level (inner > outer)


# ---------------------------------------------------------------------------
# Label & ID generation
# ---------------------------------------------------------------------------

_ENV_PREFIX: dict[str, str] = {
    "theorem": "thm",
    "lemma": "lem",
    "proposition": "prop",
    "corollary": "cor",
    "definition": "def",
    "assumption": "asm",
    "example": "ex",
    "remark": "rem",
    "claim": "clm",
    "conjecture": "conj",
    "notation": "not",
    "observation": "obs",
    "proof": "proof",
    "figure": "fig",
    "figure*": "fig",
    "table": "tbl",
    "table*": "tbl",
}
_DEFAULT_PREFIX = "env"


def _env_prefix(name: str) -> str:
    return _ENV_PREFIX.get(name, _DEFAULT_PREFIX)


def _eq_is_starred(name: str) -> bool:
    return name.endswith("*")


def _make_label(prefix: str, section_slug: str, counter: int) -> str:
    return f"{prefix}:auto.{section_slug}.{counter}"


# ---------------------------------------------------------------------------
# Per-file annotation
# ---------------------------------------------------------------------------

def annotate_file(
    path: Path,
    *,
    section_slug_override: str | None = None,
    global_counters: dict[str, int] | None = None,
    dry_run: bool = False,
) -> tuple[int, dict[str, int]]:
    """
    Annotate a single .tex file in-place (with .tex.bak backup).

    Returns (insertions_made, updated_counters).
    *global_counters* maps counter keys (e.g. "eq:auto.introduction") to the
    last-used counter value; updated in-place and also returned.
    """
    if global_counters is None:
        global_counters = {}

    source = path.read_text(encoding="utf-8", errors="replace")
    stripped = strip_comments(source)
    line_map = build_line_map(source)

    sections = find_sections(source, stripped=stripped)
    envs = find_environments(source, stripped=stripped)

    doc_start, doc_end = find_document_body(source)
    display_math = find_display_math(source, stripped=stripped)
    # Combine $$...$$ / \[..\] with \begin{align}..\end{align} etc.
    # so that sub-environments inside any math context are all skipped.
    math_ranges = display_math + [
        (e["pos"], e["end"]) for e in envs if e["name"] in EQUATION_LIKE
    ]

    # Collect pending insertions; apply in reverse order to preserve offsets
    insertions: list[_Insertion] = []

    # -----------------------------------------------------------------------
    # 0. Wrap section / subsection / subsubsection with AI block markers
    # -----------------------------------------------------------------------
    _SEC_BLOCK_CMDS = {"section", "subsection", "subsubsection"}
    _SEC_CMD_LEVEL = {"section": 2, "subsection": 3, "subsubsection": 4}

    # Process all section commands within the document body.
    secs_to_wrap = [
        s for s in sections
        if s["command"] in _SEC_BLOCK_CMDS
        and s["pos"] >= doc_start
    ]
    secs_to_wrap.sort(key=lambda s: s["pos"])

    # Per-slug counter for starred-section IDs (reset per file, matches indexer
    # position-based lookup so cross-file counter sync is not required).
    _star_counters: dict[str, int] = {}

    def _sec_ai_id(s: dict) -> str:
        """Derive the AI block ID for a section (matches the indexer's ID)."""
        if s["label"]:
            return s["label"]
        slug = slugify(s["title"]) or slugify(s["command"])
        if s["starred"]:
            _star_counters[slug] = _star_counters.get(slug, 0) + 1
            return f"sec:star.{slug}.{_star_counters[slug]}"
        return f"sec:{slug}"

    for i, sec in enumerate(secs_to_wrap):
        # Idempotency: skip if already wrapped
        pre = source[max(0, sec["pos"] - 80) : sec["pos"]]
        if _AI_BLOCK_RE.search(pre):
            continue

        sec_id = _sec_ai_id(sec)
        sec_type = sec["command"]
        lvl = _SEC_CMD_LEVEL[sec_type]

        # Close position: start of next section at same or higher level, or doc_end
        close = doc_end
        for j in range(i + 1, len(secs_to_wrap)):
            nxt = secs_to_wrap[j]
            if _SEC_CMD_LEVEL[nxt["command"]] <= lvl:
                close = nxt["pos"]
                break

        open_marker = f'%<ai:block id="{sec_id}" type="{sec_type}">\n'
        close_marker = '%</ai:block>\n'

        # priority: opens=0 (appear last), closes=10+level (innermost=highest=appears first)
        insertions.append(_Insertion(sec["pos"], open_marker, 0))
        insertions.append(_Insertion(close, close_marker, 10 + lvl))

    # -----------------------------------------------------------------------
    # 1. Annotate section headings without \label
    # -----------------------------------------------------------------------
    for sec in sections:
        if sec["pos"] < doc_start:
            continue
        if sec["label"]:
            continue  # already labeled
        if sec["starred"]:
            continue  # unnumbered sections don't need labels
        cmd = sec["command"]
        if cmd in ("paragraph", "subparagraph"):
            continue  # \paragraph{} is not a float; skip label insertion

        # Find end of \section{...} macro to append \label there
        # The macro ends right after the closing } of the title
        # We need to find it in the original source
        # sec["pos"] points to the \ of the command
        m = re.match(
            r"\\(?:part|chapter|section|subsection|subsubsection)\*?\{[^}]*\}",
            source[sec["pos"]:],
        )
        if not m:
            continue
        insert_pos = sec["pos"] + m.end()
        slug = slugify(sec["title"]) or slugify(cmd)
        label_id = f"sec:{slug}"
        # Check if label already exists anywhere nearby (next 80 chars)
        nearby = source[insert_pos : insert_pos + 80]
        if _SECTION_LABEL_NEARBY_RE.search(nearby):
            continue
        insertions.append(_Insertion(insert_pos, f"\\label{{{label_id}}}"))

    # -----------------------------------------------------------------------
    # 2. Annotate environments
    # -----------------------------------------------------------------------
    for env in envs:
        name = env["name"]
        label = env["label"]
        pos = env["pos"]
        content = env["content"]

        if pos < doc_start or pos > doc_end:
            continue

        # Skip environments nested inside any math context ($$, \[, align, multline, …)
        if any(dm_s <= pos < dm_e for dm_s, dm_e in math_ranges):
            continue

        sec_title, _ = section_at_pos(sections, pos)
        sec_slug = slugify(sec_title) if sec_title else section_slug_override or "doc"

        # ----------------------------------------------------------------
        # Skip already-labeled or AI-blocked envs
        # ----------------------------------------------------------------
        if label or _AI_BLOCK_RE.search(source[max(0, pos - 120) : pos]):
            continue

        # ----------------------------------------------------------------
        # Environments to insert \label into
        # ----------------------------------------------------------------
        if name in _LABELABLE:
            prefix = _env_prefix(name)
            counter_key = f"{prefix}:auto.{sec_slug}"
            global_counters[counter_key] = global_counters.get(counter_key, 0) + 1
            label_id = _make_label(prefix, sec_slug, global_counters[counter_key])

            # Insert \label right after \begin{X} (or optional args)
            # Find end of \begin{name}[...]{...} in original source
            begin_end = _find_begin_end(source, pos, name)
            insertions.append(_Insertion(begin_end, f"\\label{{{label_id}}}"))
            continue

        # ----------------------------------------------------------------
        # Equation-like: insert \label unless starred
        # ----------------------------------------------------------------
        if name in EQUATION_LIKE:
            if _eq_is_starred(name):
                continue  # unnumbered by design
            prefix = "eq"
            counter_key = f"{prefix}:auto.{sec_slug}"
            global_counters[counter_key] = global_counters.get(counter_key, 0) + 1
            label_id = _make_label(prefix, sec_slug, global_counters[counter_key])
            begin_end = _find_begin_end(source, pos, name)
            # For align/gather, insert as a comment to not break alignment
            if name in {"align", "align*", "gather", "gather*", "alignat", "alignat*"}:
                # \label goes on its own line inside the env (before \end)
                end_tag = f"\\end{{{name}}}"
                end_pos = source.rfind(end_tag, pos, env["end"])
                if end_pos > 0:
                    insertions.append(_Insertion(end_pos, f"\\label{{{label_id}}}\n"))
            else:
                insertions.append(_Insertion(begin_end, f"\\label{{{label_id}}}"))
            continue

        # ----------------------------------------------------------------
        # Skip opaque / verbatim / tikz
        # ----------------------------------------------------------------
        if name in (_SKIP_LABEL | VERBATIM_LIKE | TIKZ_LIKE | OPAQUE_LIKE):
            continue

        # ----------------------------------------------------------------
        # Unknown/custom environments → AI block marker
        # ----------------------------------------------------------------
        prefix = name.rstrip("*")
        counter_key = f"{prefix}:auto.{sec_slug}"
        global_counters[counter_key] = global_counters.get(counter_key, 0) + 1
        block_id = _make_label(prefix, sec_slug, global_counters[counter_key])

        # Insert open marker before \begin{X} and close marker after \end{X}
        open_marker = f"%<ai:block id=\"{block_id}\" type=\"{prefix}\">\n"
        close_marker = f"%</ai:block>\n"
        insertions.append(_Insertion(env["end"], close_marker))     # close first (higher pos)
        insertions.append(_Insertion(pos, open_marker))             # open second (lower pos)

    # -----------------------------------------------------------------------
    # 2b. Wrap display math ($$...$$  and  \[...\]) with AI block markers
    # -----------------------------------------------------------------------
    eq_ranges = [(e["pos"], e["end"]) for e in envs if e["name"] in EQUATION_LIKE]
    for dm_start, dm_end in display_math:
        if dm_start < doc_start or dm_start > doc_end:
            continue
        # Idempotency: skip if already wrapped
        if _AI_BLOCK_RE.search(source[max(0, dm_start - 120) : dm_start]):
            continue
        # Skip if $$...$$ somehow appears inside a \begin{align} (shouldn't happen, but safe)
        if any(eq_s <= dm_start < eq_e for eq_s, eq_e in eq_ranges):
            continue
        sec_title, _ = section_at_pos(sections, dm_start)
        sec_slug = slugify(sec_title) if sec_title else section_slug_override or "doc"
        counter_key = f"eq:auto.{sec_slug}"
        global_counters[counter_key] = global_counters.get(counter_key, 0) + 1
        block_id = f"eq:auto.{sec_slug}.{global_counters[counter_key]}"
        open_marker = f'%<ai:block id="{block_id}" type="display-math">\n'
        close_marker = '\n%</ai:block>\n'
        insertions.append(_Insertion(dm_end, close_marker))
        insertions.append(_Insertion(dm_start, open_marker))
    # -----------------------------------------------------------------------
    excluded: list[tuple[int, int]] = []
    for env in envs:
        if env["pos"] >= doc_start:
            excluded.append((env["pos"], env["end"]))
    for sec in sections:
        if sec["pos"] >= doc_start:
            excluded.append(section_command_span(source, sec))
    # Exclude display math regions and AI blocks that are content (not containers).
    # Section AI blocks are large containers — do NOT exclude them or paragraph
    # finding inside sections would be suppressed.
    excluded.extend(display_math)
    _SEC_CONTAINER_TYPES = {"section", "subsection", "subsubsection"}
    for blk in find_ai_blocks(source):
        if blk.get("type") not in _SEC_CONTAINER_TYPES:
            excluded.append((blk["pos"], blk["end"]))

    for para in find_paragraphs_between(source, excluded, doc_start, doc_end):
        para_pos = para["pos"]
        sec_title, _ = section_at_pos(sections, para_pos)
        sec_slug = slugify(sec_title) if sec_title else section_slug_override or "doc"
        counter_key = f"para:auto.{sec_slug}"
        global_counters[counter_key] = global_counters.get(counter_key, 0) + 1
        block_id = f"para:auto.{sec_slug}.{global_counters[counter_key]}"
        open_marker = f'%<ai:block id="{block_id}" type="paragraph">\n'
        close_marker = '\n%</ai:block>\n'
        # Close at higher position first so sort puts it before open
        insertions.append(_Insertion(para["end"], close_marker))
        insertions.append(_Insertion(para["pos"], open_marker))

    # -----------------------------------------------------------------------
    # Apply insertions (reverse position order; at equal pos, lower priority first
    # so that higher-priority items are applied last and appear first in output)
    # -----------------------------------------------------------------------
    insertions.sort(key=lambda ins: (-ins.pos, ins.priority))

    if not insertions:
        return 0, global_counters

    if dry_run:
        return len(insertions), global_counters

    # Write backup
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)

    # Apply insertions
    chars = list(source)
    for ins in insertions:
        chars.insert(ins.pos, ins.text)

    path.write_text("".join(chars), encoding="utf-8")
    return len(insertions), global_counters


def _find_begin_end(source: str, begin_pos: int, env_name: str) -> int:
    """
    Find the byte offset right after \\begin{env_name}[opt]{arg} ...
    i.e., past the mandatory \\begin{X} token and any immediately following
    optional/required arguments.  Falls back to just after \\begin{X}.
    """
    # Minimal: skip past \begin{name}
    token = f"\\begin{{{env_name}}}"
    end = begin_pos + len(token)
    # Skip optional arguments [...]
    i = end
    while i < len(source) and source[i] in (" ", "\t"):
        i += 1
    while i < len(source) and source[i] == "[":
        depth = 0
        while i < len(source):
            if source[i] == "[":
                depth += 1
            elif source[i] == "]":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
    return i


# ---------------------------------------------------------------------------
# Project-level annotation
# ---------------------------------------------------------------------------

def annotate_project(root_tex: Path | str, *, dry_run: bool = False) -> dict:
    """
    Annotate all .tex files reachable from *root_tex*.

    Returns a summary dict: {file: insertions_count, ...}
    """
    root_tex = Path(root_tex)
    files = resolve_includes(root_tex)
    counters: dict[str, int] = {}
    summary: dict[str, int] = {}

    for f in files:
        if _is_macro_file(f):
            summary[str(f)] = 0
            continue
        count, counters = annotate_file(f, global_counters=counters, dry_run=dry_run)
        summary[str(f)] = count

    return summary
