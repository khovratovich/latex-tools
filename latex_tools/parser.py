"""
parser.py — Shared LaTeX parsing utilities.

Uses a regex + stack approach for robustness against malformed or
macro-heavy LaTeX (research papers, custom environments, tikzpictures, etc.).
pylatexenc is imported only for macro-argument extraction where needed.
"""

from __future__ import annotations

import bisect
import re
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Known environment categories
# ---------------------------------------------------------------------------

THEOREM_LIKE = {
    "theorem", "lemma", "proposition", "corollary",
    "definition", "assumption", "example", "remark",
    "claim", "conjecture", "notation", "observation",
}
EQUATION_LIKE = {
    "equation", "equation*",
    "align", "align*",
    "multline", "multline*",
    "gather", "gather*",
    "alignat", "alignat*",
    "flalign", "flalign*",
    "eqnarray", "eqnarray*",
    "subequations",
}
PROOF_LIKE = {"proof"}
FLOAT_LIKE = {"figure", "figure*", "table", "table*"}
LIST_LIKE = {"itemize", "enumerate", "description"}
VERBATIM_LIKE = {"verbatim", "verbatim*", "lstlisting", "minted", "Verbatim"}
TIKZ_LIKE = {"tikzpicture", "tikzcd"}
# Environments we index but treat as opaque boxes
OPAQUE_LIKE = {"tabular", "tabular*", "array", "matrix", "pmatrix",
               "bmatrix", "vmatrix", "Vmatrix", "Bmatrix"}

# Regex for environments that never contain logical sub-structure (skip recursion)
_OPAQUE_ENV_NAMES = VERBATIM_LIKE | TIKZ_LIKE | OPAQUE_LIKE

# Section command names in hierarchy order
SECTION_COMMANDS = [
    "part", "chapter",
    "section", "subsection", "subsubsection",
    "paragraph", "subparagraph",
]
SECTION_LEVEL = {cmd: i for i, cmd in enumerate(SECTION_COMMANDS)}


# ---------------------------------------------------------------------------
# Line-map helpers
# ---------------------------------------------------------------------------

def build_line_map(source: str) -> list[int]:
    """Return list of byte offsets where each line starts (0-indexed lines)."""
    offsets = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            offsets.append(i + 1)
    return offsets


def byte_to_line(line_map: list[int], pos: int) -> int:
    """Convert a byte offset to a 1-based line number."""
    return bisect.bisect_right(line_map, pos)


# ---------------------------------------------------------------------------
# Comment-stripped source (for environment/section finding only)
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"(?<!\\)%[^\n]*")


def strip_comments(source: str) -> str:
    """Remove % ... comments, preserving newlines so line numbers stay valid."""
    def _replace(m: re.Match) -> str:
        return " " * len(m.group())  # keep same length so byte offsets stay valid
    return _COMMENT_RE.sub(_replace, source)


# ---------------------------------------------------------------------------
# Include resolution
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(
    r"\\(?:input|include|subfile)\{([^}]+)\}",
    re.MULTILINE,
)

# Files that only define macros — they produce no indexed elements
_MACRO_ONLY_HINTS = {"macros", "preamble", "defs", "commands", "notation"}


def _is_macro_file(path: Path) -> bool:
    stem = path.stem.lower()
    if stem in _MACRO_ONLY_HINTS:
        return True
    # Heuristic: file has no \begin{document} or \section
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        return r"\begin{document}" not in src and r"\section" not in src
    except OSError:
        return False


def resolve_includes(root_tex: Path) -> list[Path]:
    """
    Return an ordered list of .tex files included by *root_tex*, starting with
    root_tex itself.  Multi-level nesting is handled; cycles are skipped.
    Macro-only files are included (parseable) but callers can check
    `is_macro_file()` if they want to skip indexing them.
    """
    root_tex = Path(root_tex).resolve()
    visited: set[Path] = set()
    order: list[Path] = []

    def _visit(path: Path) -> None:
        if path in visited:
            return
        visited.add(path)
        order.append(path)
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for m in _INCLUDE_RE.finditer(src):
            inc = m.group(1).strip()
            if not inc.endswith(".tex"):
                inc += ".tex"
            child = (path.parent / inc).resolve()
            if child.exists():
                _visit(child)

    _visit(root_tex)
    return order


# ---------------------------------------------------------------------------
# Environment finder (stack-based, handles nesting)
# ---------------------------------------------------------------------------

_BEGIN_RE = re.compile(r"\\begin\{([\w@*]+)\}")
_END_RE = re.compile(r"\\end\{([\w@*]+)\}")
_LABEL_RE = re.compile(r"\\label\{([^}]+)\}")
_CAPTION_LABEL_RE = re.compile(r"\\caption\{[^}]*\\label\{([^}]+)\}")


def find_environments(source: str, *, stripped: str | None = None) -> list[dict]:
    """
    Return a list of dicts describing every environment in *source*.

    Each dict has:
        name, label, pos, end, line_start, line_end, content, depth
    where depth=0 means top-level (not nested inside another environment).

    *stripped* is the comment-stripped version of *source* (same byte length).
    If not provided it is computed internally.
    """
    if stripped is None:
        stripped = strip_comments(source)

    line_map = build_line_map(source)

    # Collect all \begin and \end tokens with positions
    tokens: list[tuple[str, str, int, int]] = []  # (kind, name, start, end)
    for m in _BEGIN_RE.finditer(stripped):
        tokens.append(("begin", m.group(1), m.start(), m.end()))
    for m in _END_RE.finditer(stripped):
        tokens.append(("end", m.group(1), m.start(), m.end()))
    tokens.sort(key=lambda t: t[2])

    stack: list[tuple[str, int, int]] = []  # (name, begin_pos, depth_at_open)
    results: list[dict] = []

    for kind, name, start, end in tokens:
        if kind == "begin":
            stack.append((name, start, len(stack)))
        elif kind == "end" and stack:
            # Pop matching open (handle mismatches tolerantly)
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    open_name, open_pos, depth = stack.pop(i)
                    env_content = source[open_pos:end]
                    # Extract label: first try standalone \label, then caption-embedded
                    label_m = _LABEL_RE.search(env_content)
                    label = label_m.group(1) if label_m else None
                    if label is None:
                        cap_m = _CAPTION_LABEL_RE.search(env_content)
                        label = cap_m.group(1) if cap_m else None
                    results.append({
                        "name": name,
                        "label": label,
                        "pos": open_pos,
                        "end": end,
                        "line_start": byte_to_line(line_map, open_pos),
                        "line_end": byte_to_line(line_map, end - 1),
                        "content": env_content,
                        "depth": depth,
                    })
                    break

    return results


def top_level_environments(source: str, *, stripped: str | None = None) -> list[dict]:
    """Return only depth-0 environments (not nested inside another)."""
    return [e for e in find_environments(source, stripped=stripped) if e["depth"] == 0]


# ---------------------------------------------------------------------------
# Section finder
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?"
    r"\{([^}]*)\}",
    re.MULTILINE,
)
_SECTION_LABEL_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?"
    r"\{[^}]*\}\s*\\label\{([^}]+)\}",
    re.MULTILINE,
)


def find_sections(source: str, *, stripped: str | None = None) -> list[dict]:
    """
    Return list of section-like command dicts, each with:
        command, starred, title, label, pos, line, level
    """
    if stripped is None:
        stripped = strip_comments(source)

    line_map = build_line_map(source)

    # Pre-compute inline labels (\section{...}\label{...})
    inline_labels: dict[int, str] = {}
    for m in _SECTION_LABEL_RE.finditer(stripped):
        inline_labels[m.start()] = m.group(2)

    results: list[dict] = []
    # Match in stripped (same byte offsets as source)
    section_re = re.compile(
        r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)(\*)?"
        r"\{([^}]*)\}",
        re.MULTILINE,
    )
    for m in section_re.finditer(stripped):
        cmd = m.group(1)
        starred = m.group(2) == "*" if m.group(2) else False
        title_stripped = m.group(3).strip()
        # Title from original source (preserves macros)
        title_raw = source[m.start(3):m.end(3)].strip()
        label = inline_labels.get(m.start())
        results.append({
            "command": cmd,
            "starred": starred,
            "title": title_raw,
            "label": label,
            "pos": m.start(),
            "line": byte_to_line(line_map, m.start()),
            "level": SECTION_LEVEL.get(cmd, 99),
        })

    return results


# ---------------------------------------------------------------------------
# Section context helper
# ---------------------------------------------------------------------------

def section_at_pos(sections: list[dict], pos: int) -> tuple[str | None, str | None]:
    """
    Given a list of sections and a byte position, return (section_title, subsection_title)
    of the innermost section containing *pos*.
    """
    current_section: str | None = None
    current_subsection: str | None = None

    for sec in sections:
        if sec["pos"] > pos:
            break
        cmd = sec["command"]
        if cmd == "section":
            current_section = sec["title"]
            current_subsection = None
        elif cmd == "subsection":
            current_subsection = sec["title"]
        elif cmd == "subsubsection":
            pass  # don't track for now

    return current_section, current_subsection


# ---------------------------------------------------------------------------
# Label extraction helpers
# ---------------------------------------------------------------------------

def extract_inline_labels(source: str) -> list[tuple[str, int]]:
    """Return all (label_value, byte_pos) tuples in *source*."""
    return [(m.group(1), m.start()) for m in _LABEL_RE.finditer(source)]


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_MACRO_RE = re.compile(r"\\[a-zA-Z]+\{?")


def slugify(text: str) -> str:
    """Convert a section title to a stable ASCII slug."""
    # Strip LaTeX macros first
    text = _MACRO_RE.sub("", text)
    text = text.lower().strip()
    text = _NON_ALNUM_RE.sub("-", text)
    text = text.strip("-")
    return text or "unknown"


# ---------------------------------------------------------------------------
# Display math detection ($$...$$  and  \[...\])
# ---------------------------------------------------------------------------

# Matches $$..$$ that are not themselves inline $ (two consecutive dollar signs)
_DISPLAY_MATH_RE = re.compile(r"\$\$(.*?)\$\$|\\\[(.*?)\\\]", re.DOTALL)


def find_display_math(source: str, *, stripped: str | None = None) -> list[tuple[int, int]]:
    """
    Return (start, end) byte ranges of ``$$...$$`` and ``\\[...\\]`` display math blocks.
    Uses the comment-stripped source so that ``%`` lines don't confuse matching.
    """
    if stripped is None:
        stripped = strip_comments(source)
    return [(m.start(), m.end()) for m in _DISPLAY_MATH_RE.finditer(stripped)]


# ---------------------------------------------------------------------------
# Paragraph detection — finds ALL text in gaps between known elements
# ---------------------------------------------------------------------------

def section_command_span(source: str, sec: dict) -> tuple[int, int]:
    """
    Return (start, end) byte span of a section command including any inline \\label.
    Pass results into excluded_ranges for find_paragraphs_between.
    """
    m = re.match(
        r"\\(?:part|chapter|section|subsection|subsubsection"
        r"|paragraph|subparagraph)\*?\{[^}]*\}"
        r"(?:\s*\\label\{[^}]*\})?",
        source[sec["pos"]:],
    )
    end = sec["pos"] + (m.end() if m else 80)
    return sec["pos"], end


def find_paragraphs_between(
    source: str,
    excluded_ranges: list[tuple[int, int]],
    doc_start: int = 0,
    doc_end: int = -1,
    min_letters: int = 15,
) -> list[dict]:
    """
    Find ALL prose paragraph blocks in the document body that fall outside
    *excluded_ranges* (environments + section command spans + AI-block spans).

    Uses line-by-line scanning so byte positions are exact.
    Returns list of dicts: {pos, end, line_start, line_end, content}
    """
    if doc_end < 0:
        doc_end = len(source)

    line_map = build_line_map(source)

    # Sort and merge overlapping excluded ranges, clamped to [doc_start, doc_end]
    raw = [
        (max(doc_start, s), min(doc_end, e))
        for s, e in excluded_ranges
        if e > doc_start and s < doc_end
    ]
    raw.sort()
    merged: list[list[int]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # Compute free regions (gaps between excluded ranges)
    free: list[tuple[int, int]] = []
    cursor = doc_start
    for s, e in merged:
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < doc_end:
        free.append((cursor, doc_end))

    results: list[dict] = []
    for region_start, region_end in free:
        region_text = source[region_start:region_end]
        _collect_paragraphs(region_text, region_start, line_map, min_letters, results)

    return results


def _collect_paragraphs(
    text: str,
    base_offset: int,
    line_map: list[int],
    min_letters: int,
    results: list[dict],
) -> None:
    """Split *text* into blank-line-separated paragraphs and append to *results*."""
    lines = text.split("\n")
    para_lines: list[str] = []
    para_start_offset: int | None = None
    offset = 0  # cumulative byte offset within text

    for line in lines:
        line_len = len(line) + 1  # +1 for the \n we split on

        if line.strip():  # non-blank line
            if para_start_offset is None:
                para_start_offset = offset
            para_lines.append(line)
        else:  # blank line — flush current paragraph
            if para_lines and para_start_offset is not None:
                _flush_paragraph(
                    para_lines, base_offset, para_start_offset,
                    offset, line_map, min_letters, results,
                )
                para_lines = []
                para_start_offset = None

        offset += line_len

    # Flush the final paragraph (no trailing blank line)
    if para_lines and para_start_offset is not None:
        _flush_paragraph(
            para_lines, base_offset, para_start_offset,
            offset, line_map, min_letters, results,
        )


def _flush_paragraph(
    lines: list[str],
    base_offset: int,
    para_start: int,
    para_end: int,
    line_map: list[int],
    min_letters: int,
    results: list[dict],
) -> None:
    text = "\n".join(lines).strip()
    if not text:
        return
    letters = sum(1 for c in text if c.isalpha())
    if letters < min_letters:
        return
    abs_start = base_offset + para_start
    abs_end = base_offset + para_end
    results.append({
        "pos": abs_start,
        "end": abs_end,
        "line_start": byte_to_line(line_map, abs_start),
        "line_end": byte_to_line(line_map, max(abs_start, abs_end - 1)),
        "content": text,
    })


# ---------------------------------------------------------------------------
# Document body range (between \begin{document} and \end{document})
# ---------------------------------------------------------------------------

def find_document_body(source: str) -> tuple[int, int]:
    """Return (start, end) byte positions of the document body."""
    begin_m = re.search(r"\\begin\{document\}", source)
    end_m = re.search(r"\\end\{document\}", source)
    start = begin_m.end() if begin_m else 0
    end = end_m.start() if end_m else len(source)
    return start, end


# ---------------------------------------------------------------------------
# AI block marker parser (stack-based — handles nested blocks correctly)
# ---------------------------------------------------------------------------

_AI_OPEN_TAG_RE = re.compile(
    r'%\s*<ai:block\s+id="([^"]+)"\s+type="([^"]+)">'
)
_AI_CLOSE_TAG_RE = re.compile(r'%\s*</ai:block>')


def find_ai_blocks(source: str) -> list[dict]:
    """
    Parse ``%<ai:block id="..." type="...">`` / ``%</ai:block>`` markers
    using a stack so that nested blocks are handled correctly.

    Returns ALL blocks at every nesting depth.
    Each result dict: {id, type, content, pos, end, depth}
    where *pos* is the byte start of the opening tag and *end* is the byte
    end (exclusive) of the closing tag.
    """
    events: list[tuple[str, int, int, str | None, str | None]] = []
    for m in _AI_OPEN_TAG_RE.finditer(source):
        events.append(("open", m.start(), m.end(), m.group(1), m.group(2)))
    for m in _AI_CLOSE_TAG_RE.finditer(source):
        events.append(("close", m.start(), m.end(), None, None))
    events.sort(key=lambda ev: ev[1])

    results: list[dict] = []
    stack: list[tuple[int, int, str, str]] = []  # (open_pos, open_end, id, type)

    for ev_type, pos, tag_end, bid, btype in events:
        if ev_type == "open":
            stack.append((pos, tag_end, bid, btype))  # type: ignore[arg-type]
        elif ev_type == "close" and stack:
            open_pos, open_tag_end, block_id, block_type = stack.pop()
            results.append({
                "id": block_id,
                "type": block_type,
                "content": source[open_tag_end:pos].strip(),
                "pos": open_pos,
                "end": tag_end,
                "depth": len(stack),
            })

    # Sort by position so callers get a stable order
    results.sort(key=lambda r: r["pos"])
    return results


# ---------------------------------------------------------------------------
# Utility: strip LaTeX commands for plain-text content extraction
# ---------------------------------------------------------------------------

_STRIP_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?\{?")
_STRIP_BRACE_RE = re.compile(r"[{}]")


def latex_to_text(latex: str) -> str:
    """Very rough LaTeX → plain text conversion for search/preview purposes."""
    text = _STRIP_CMD_RE.sub(" ", latex)
    text = _STRIP_BRACE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()
