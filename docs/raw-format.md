# Raw Note Format

`knowflow` keeps the raw-note contract intentionally small.

## Core Rule

Every note lives in its own directory and the main note file must be named `index.md`.

Example:

```text
RAW_DIR/
└── articles/
    └── systems/
        └── knowledge-compilers/
            ├── index.md
            └── diagram.png
```

`knowflow` recursively scans `RAW_DIR/**/index.md`.

## What Can Go In `index.md`

Free-form Markdown is enough. You do not need a strict frontmatter schema.

Recommended content:

- a clear title
- short summary or framing paragraph
- definitions, steps, comparisons, examples, or references
- enough context for an LLM to infer topic and summary

Minimal example:

```md
# Knowledge Compilers

A knowledge compiler turns rough notes into reusable pages.

## Why it matters

- raw notes are easy to capture but hard to query
- structure makes later retrieval and synthesis easier

## Core pipeline

1. ingest raw notes
2. extract metadata
3. group by topic
4. build wiki pages
5. publish selected content

## Comparison

A search-only note archive stores text well, but a knowledge compiler tries to govern and connect it.
```

## Attachments

Files next to `index.md` are treated as source assets.

Current behavior:

- assets next to a raw note may be copied into the generated `wiki/sources/<slug>/` bundle
- this is useful for images, diagrams, PDFs, and other note-local artifacts

It is not a full asset pipeline. If your public posts depend on additional publishing behavior, handle that in your site layer.

## Reserved Paths

`RAW_DIR/research/` is reserved for generated research drafts and queue notes.

Notes under that subtree are not treated like normal hand-authored raw sources during clean/query flows.

## Suggested Directory Strategy

Use category folders that match your own capture habits. For example:

```text
RAW_DIR/
├── articles/
├── papers/
├── repos/
└── research/
```

Nested grouping is fine as long as each note directory ends with an `index.md`.
