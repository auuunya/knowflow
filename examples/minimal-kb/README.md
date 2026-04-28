# Minimal Knowledge Base

This directory is the smallest runnable `knowflow` fixture included in the repository.

It is useful for:

- validating your local setup
- understanding the expected raw-note directory contract
- checking whether a change broke core path resolution

## Layout

```text
examples/minimal-kb/
└── _raw/
    └── articles/
        └── systems/
            └── knowledge-compilers/
                └── index.md
```

Generated `content/`, `data/`, and `wiki/` directories are intentionally ignored here so you can run builds without dirtying the repository.

## Run It

From the repository root:

```bash
cp .env.example .env
python3 knowflow.py build
python3 knowflow.py lint
python3 knowflow.py build audit
```

The repository-level `.env.example` already points `KNOWLEDGE_BASE_DIR` at this example.

When you move to a real knowledge base, update `.env` and point `KNOWLEDGE_BASE_DIR` at your own repository.
