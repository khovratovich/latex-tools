A good approach is to treat LaTeX as a **structured knowledge source**, not as plain text. The strategy should separate two things:

1. **Parsing LaTeX into logical elements**
2. **Assigning stable addresses to those elements so agents can retrieve, cite, and edit them**

A practical design would be a **LaTeX Logical Element Indexer**.

## Core idea

Convert each `.tex` project into a structured representation like:

```json
{
  "document": "paper.tex",
  "elements": [
    {
      "id": "sec:introduction.p3",
      "type": "paragraph",
      "section": "Introduction",
      "content": "We study...",
      "source_file": "intro.tex",
      "line_start": 42,
      "line_end": 48
    },
    {
      "id": "eq:loss",
      "type": "equation",
      "label": "eq:loss",
      "content": "\\mathcal{L}(\\theta)=...",
      "context": "Defined in Section 2",
      "source_file": "method.tex",
      "line_start": 88,
      "line_end": 91
    },
    {
      "id": "lem:convergence",
      "type": "lemma",
      "label": "lem:convergence",
      "statement": "Let ... Then ...",
      "proof_id": "proof:lem:convergence",
      "source_file": "proofs.tex",
      "line_start": 122,
      "line_end": 137
    }
  ]
}
```

Then agents interact with the document through logical queries such as:

```text
get_element("lem:convergence")
get_equations(section="Optimization")
get_paragraphs(after="thm:main", limit=3)
update_proof("proof:lem:convergence", new_text=...)
```

instead of opening raw files.

## Demarcation strategy

Use a combination of **LaTeX-native structure** and **explicit AI-friendly markers**.

### 1. Use LaTeX labels as primary IDs

For elements that already have labels:

```latex
\begin{lemma}\label{lem:convergence}
...
\end{lemma}

\begin{equation}\label{eq:loss}
...
\end{equation}
```

The agent address is simply:

```text
lem:convergence
eq:loss
```

This is ideal because labels are already meaningful, stable, and used by authors.

### 2. Add optional AI block markers for unlabeled elements

Paragraphs, remarks, informal claims, assumptions, examples, and explanatory text often do not have labels. For those, use lightweight comments:

```latex
%<ai:block id="intro.motivation" type="paragraph">
We study the problem of ...
%</ai:block>
```

or a shorter syntax:

```latex
% @ai-begin id=intro.motivation type=paragraph
We study the problem of ...
% @ai-end
```

These comments do not affect compilation, but they give agents stable handles.

### 3. Infer missing structure automatically

A parser can infer logical elements from standard LaTeX environments:

```latex
\section{Preliminaries}
\begin{definition}
...
\end{definition}

\begin{theorem}
...
\end{theorem}

\begin{proof}
...
\end{proof}
```

Detected types could include:

```text
section
subsection
paragraph
equation
align-block
figure
table
definition
lemma
theorem
proposition
corollary
assumption
example
remark
proof
citation
bibliography-entry
```

For unlabeled inferred elements, generate deterministic IDs:

```text
sec:preliminaries.def.1
sec:preliminaries.paragraph.3
sec:main-result.equation.2
```

But generated IDs are less stable than explicit labels, so important objects should be labeled or marked.

## Recommended tool architecture

The tool can expose a small API to agents:

```text
index_latex_project(path)
list_elements(type?, section?)
get_element(id)
get_context(id, before=2, after=2)
search_elements(query, type?)
update_element(id, replacement)
insert_after(id, new_element)
validate_latex()
```

Internally, it should maintain:

```text
.tex files
   ↓
LaTeX parser
   ↓
AST / logical document tree
   ↓
element index
   ↓
agent API
```

The index should preserve source mappings:

```json
{
  "id": "thm:main",
  "type": "theorem",
  "file": "main.tex",
  "byte_start": 18392,
  "byte_end": 19108,
  "line_start": 412,
  "line_end": 429
}
```

That way, the agent can edit the original `.tex` safely.

## Best demarcation convention

I would use this hybrid convention:

```latex
% @ai id=intro.problem type=paragraph
We consider the problem of ...

\begin{definition}\label{def:admissible}
...
\end{definition}

\begin{lemma}\label{lem:stability}
...
\end{lemma}

% @ai-begin id=discussion.limitations type=paragraph
The result has two limitations...
% @ai-end
```

Rules:

| Element                    | Preferred address                    |
| -------------------------- | ------------------------------------ |
| theorem, lemma, definition | `\label{...}`                        |
| equation                   | `\label{...}`                        |
| section                    | slug from heading or explicit marker |
| paragraph                  | `% @ai id=... type=paragraph`        |
| proof                      | linked to theorem/lemma label        |
| figure/table               | `\label{...}`                        |
| informal text              | AI marker                            |

## Example

LaTeX source:

```latex
\section{Main Result}
\label{sec:main}

% @ai id=main.intuition type=paragraph
The main idea is to compare the discrete trajectory with its continuous limit.

\begin{lemma}\label{lem:trajectory-bound}
For every $t \leq T$, we have
\[
  \|x_t - x^\star\| \leq C e^{-\lambda t}.
\]
\end{lemma}

\begin{proof}
The claim follows by applying Gronwall's inequality.
\end{proof}
```

Agent view:

```json
[
  {
    "id": "sec:main",
    "type": "section",
    "title": "Main Result"
  },
  {
    "id": "main.intuition",
    "type": "paragraph",
    "content": "The main idea is..."
  },
  {
    "id": "lem:trajectory-bound",
    "type": "lemma",
    "statement": "For every t ≤ T...",
    "proof_id": "proof:lem:trajectory-bound"
  },
  {
    "id": "proof:lem:trajectory-bound",
    "type": "proof",
    "parent": "lem:trajectory-bound"
  }
]
```

## Existing tools that can help

For implementation, useful components are:

* **LaTeXML**: converts LaTeX into XML/HTML, good for structural extraction.
* **plasTeX**: Python framework for parsing LaTeX into a document object model.
* **TexSoup**: lightweight Python parser, easier but less complete.
* **tree-sitter-latex**: good for syntax-level parsing and source spans.
* **Pandoc JSON AST**: useful if the LaTeX is simple enough and convertible.

For a robust agent-facing tool, I would prefer:

```text
tree-sitter-latex for exact source spans
+
LaTeXML or plasTeX for semantic structure
+
custom indexer for AI element IDs
```

## The key design principle

Do not make the AI agent “read files.” Make it query a **document graph**:

```text
Section contains paragraphs, lemmas, equations.
Lemma has statement and proof.
Equation is referenced by paragraphs.
Proof depends on lemmas and definitions.
Citation supports a claim.
```

Then the agent can answer questions like:

```text
“Find all lemmas used in the proof of Theorem 2.”
“Rewrite the paragraph before Equation 5.”
“Check whether every theorem has a proof.”
“Summarize all assumptions.”
“Locate equations that are referenced but not labeled.”
```

The best demarcation strategy is therefore:

```text
LaTeX labels for formal objects,
AI comments for informal objects,
automatic parsing for structure,
stable IDs for retrieval,
source spans for editing.
```
