# latex-tools

Agent-friendly tooling for LaTeX projects. Provides stable logical addresses for every mathematical object in a document, a structured JSON index, and a query API so agents can navigate a paper by meaning rather than by line number.

## Overview

Three tools work in a pipeline:

```
.tex files  ──►  annotator  ──►  enriched .tex  ──►  indexer  ──►  .latex-index.json  ──►  API / MCP
```

1. **Annotator** — enriches `.tex` files with `\label{}` markers and `%<ai:block>` comment fences, giving every theorem, equation, paragraph, and section a stable ID.
2. **Indexer** — parses the enriched source and builds `.latex-index.json`: a nested element tree with byte/line ranges, cross-references, and full-text content.
3. **API** — `LatexDocument` class and MCP server for logical queries: get an element by ID, traverse the section tree, search LaTeX source, find proofs, validate labels.

## Installation

```bash
pip install -e .
```

Requires Python ≥ 3.10. Key dependencies: `fastmcp`, `pylatexenc`.

## Quickstart

```bash
# 1. Annotate: add \label{} and %<ai:block> markers to all .tex files
latex-tools annotate paper.tex

# 2. Index: build .latex-index.json next to paper.tex
latex-tools index paper.tex

# 3. Serve: start an MCP server for agent access
latex-tools serve paper.tex
```

Or use the Python API directly:

```python
from latex_tools.api import LatexDocument

doc = LatexDocument.load("paper.tex")          # builds index if needed
doc.get_toc()                                  # table of contents
doc.get_element("thm:main")                    # full element by label
doc.get_subtree("sec:proof-arch", max_depth=2) # nested section tree
doc.search_latex(r"\\BaseFold")                # regex over full source
```

---

## Tool 1 — Annotator

`latex-tools annotate <root.tex>` (or `annotate_project(root_tex)`)

Modifies `.tex` files in-place. A `.tex.bak` backup is written before any change. All operations are **idempotent** — already-annotated elements are skipped.

### What it adds

| Target | Action |
|--------|--------|
| `\theorem`, `\lemma`, `\proof`, `\figure`, … without `\label` | Inserts `\label{thm:auto.SLUG.N}` |
| `\begin{align}`, `\begin{equation}`, … without `\label` (non-starred) | Inserts `\label{eq:auto.SLUG.N}` |
| `\section`, `\subsection`, `\subsubsection` without `\label` (non-starred) | Appends `\label{sec:SLUG}` |
| Sections/subsections/subsubsections | Wraps with `%<ai:block id="…" type="section">` … `%</ai:block>` |
| Starred sections (`\section*{…}`) | Wraps with `%<ai:block id="sec:star.SLUG.N" …>` |
| Unknown/custom environments | Wraps with `%<ai:block id="TYPE:auto.SLUG.N" …>` |
| Prose paragraphs between elements | Wraps with `%<ai:block id="para:auto.SLUG.N" type="paragraph">` |
| `$$…$$` and `\[…\]` display math | Wraps with `%<ai:block id="eq:auto.SLUG.N" type="display-math">` |

### AI block format

```latex
%<ai:block id="sec:execution-model" type="section">
\section{Execution Model}\label{sec:execution-model}

%<ai:block id="para:auto.execution-model.1" type="paragraph">
This section describes...

%</ai:block>

%</ai:block>
```

Blocks nest: section blocks contain subsection blocks which contain paragraph/equation blocks.

### ID naming

| Pattern | Meaning |
|---------|---------|
| `sec:execution-model` | Section with explicit `\label{sec:execution-model}` |
| `sec:star.overview.1` | Starred (unnumbered) section, 1st with slug `overview` |
| `thm:auto.security.2` | 2nd theorem in the *security* section, no explicit label |
| `para:auto.proof-arch.5` | 5th paragraph in the *proof-arch* section |
| `eq:auto.segmentation.1` | 1st display equation in *segmentation* |

---

## Tool 2 — Indexer

`latex-tools index <root.tex>` (or `index_project(root_tex)`)

Reads the (annotated) `.tex` source tree and writes `.latex-index.json` next to the root file.

### Index structure

```jsonc
{
  "document": "paper.tex",
  "root": "/abs/path/paper.tex",
  "generated_at": "2026-05-26T…",
  "elements": [ … ],      // flat list, ordered by source position
  "toc": [ … ],           // section headings in order
  "cross_refs": { … },    // element_id → [labels it \ref{}s]
  "ref_to_elements": { … }// label → [element_ids that cite it]
}
```

### Element fields

```jsonc
{
  "id": "sec:execution-model",
  "type": "section",
  "label": "sec:execution-model",
  "title": "Execution Model",
  "section": "Execution Model",
  "subsection": null,
  "numbered": true,
  "content": "plain-text summary (up to 2000 chars)",
  "latex": "raw LaTeX (up to 4000 chars)",
  "content_truncated": false,    // true when source exceeds 4000 chars
  "source_file": "/abs/path/ch01.tex",
  "line_start": 1,
  "line_end": 172,
  "byte_start": 0,
  "byte_end": 6743,
  "parent_id": null,             // null for top-level sections
  "children": ["para:auto.…", "sec:program-execution-and-vm-state", …]
}
```

`content_truncated: true` means the index stores only a preview; the full source is read on demand from `source_file[byte_start:byte_end]`. `get_element()` expands this automatically.

### Element types

`section`, `subsection`, `subsubsection`, `paragraph`, `theorem`, `lemma`, `proposition`, `corollary`, `definition`, `assumption`, `example`, `remark`, `claim`, `conjecture`, `notation`, `observation`, `proof`, `figure`, `table`, `equation`, `display-math`, `itemize`, `enumerate`, `center`, `environment`

### Tree structure

Every element has `parent_id` and `children`. Sections contain their subsections, paragraphs, equations, theorem/proof pairs, and figures as a proper nested tree. Point-to queries (get parent, get children, get subtree) traverse this tree.

---

## Tool 3 — API

```python
from latex_tools.api import LatexDocument

doc = LatexDocument.load("paper.tex")   # or pass a .latex-index.json / directory
```

### Read methods

| Method | Description |
|--------|-------------|
| `list_elements(type, section, subsection, labeled_only)` | Filtered list of element summaries |
| `get_element(id_or_label)` | Full element dict; auto-fetches source for large blocks |
| `get_latex(id_or_label)` | Complete raw LaTeX from source file, never truncated |
| `get_context(id_or_label, before=2, after=2)` | Element + N neighbours in document order |
| `search_elements(query, type, section)` | Text search over cached content+latex with snippet |
| `search_latex(query, type, case_sensitive)` | Full-source regex/substring search; reads disk for large blocks; returns IDs in document order |
| `get_section(name)` | All elements in sections matching *name* |
| `get_proof(theorem_id)` | Proof element linked to a theorem/lemma |
| `get_references(id_or_label)` | Elements that `\ref{}` this element |
| `get_toc()` | Table of contents |
| `validate()` | Report: undefined refs, placeholder cites, theorems without proofs |

### Tree navigation

| Method | Description |
|--------|-------------|
| `get_children(id_or_label)` | Direct children in document order |
| `get_parent(id_or_label)` | Parent element, or `None` for top-level |
| `get_subtree(id_or_label, max_depth=5, include_content=False)` | Nested dict of element + all descendants |

### Write methods

| Method | Description |
|--------|-------------|
| `update_element(id_or_label, replacement_latex)` | Replace element body; preserves `\begin`/`\end`/`\label`; auto-refreshes index |
| `insert_after(id_or_label, new_latex)` | Insert raw LaTeX immediately after an element |
| `add_element(section, type, content, label, title)` | Append a new environment to a section |
| `refresh()` | Rebuild index from current source files |

---

## CLI

```
latex-tools annotate <root.tex> [--dry-run]
latex-tools index    <root.tex>
latex-tools serve    <root.tex>
latex-tools get      <root.tex> <id>
latex-tools list     <root.tex> [--type TYPE] [--section SECTION] [--labeled-only]
latex-tools search   <root.tex> <query> [--type TYPE] [--section SECTION]
latex-tools context  <root.tex> <id> [--before N] [--after N]
latex-tools validate <root.tex>
latex-tools toc      <root.tex>
```

`get`, `list`, `search`, `context`, and `toc` print JSON to stdout.
`validate` exits with code 1 if there are undefined refs or placeholder citations.

---

## MCP Server

```bash
latex-tools serve paper.tex
```

Starts a [FastMCP](https://github.com/jlowin/fastmcp) server. All API methods are registered as tools. The document is loaded once and held in memory across calls.

### Available tools

`annotate_latex_project`, `index_latex_project`, `list_elements`, `get_element`, `get_context`, `search_elements`, `get_section`, `get_proof`, `get_references`, `get_toc`, `validate`, `update_element`, `insert_after`, `add_element`, `refresh_index`

---

## Multi-file projects

`latex-tools` follows `\input{}`, `\include{}`, and `\subfile{}` includes starting from the root `.tex` file. Each included file is annotated and indexed independently; elements carry their `source_file` path. The section tree and cross-reference map span all files.

Files containing only `\newcommand` / `\DeclareMathOperator` definitions (macro files) are skipped during annotation and indexing.

---

## File layout after annotation

```
paper/
  paper.tex               root file (annotated)
  paper.tex.bak           backup before annotation
  ch01-intro.tex          chapter (annotated)
  ch01-intro.tex.bak
  macros.tex              skipped (macro-only file)
  .latex-index.json       built by indexer
```
