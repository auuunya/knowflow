# knowflow

[中文说明](README.ZH.md)

`knowflow` turns raw note directories into:

- private wiki pages in `wiki/`
- public post content in `content/posts`
- graph, audit, and cache artifacts in `data/`

## Requirements

- Python 3.11+
- an OpenAI-compatible LLM endpoint
- optional: Hugo if you want to render the generated `content/` layer

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Quick Start

```bash
cp .env.example .env
python3 knowflow.py build
python3 knowflow.py lint
python3 knowflow.py query compiler
```

The default `.env.example` points to [examples/minimal-kb](examples/minimal-kb/README.md), so the repository can be tried immediately after clone.

If your knowledge-base repository lives next to this repository, set:

```bash
KNOWLEDGE_BASE_DIR=../my-kb
```

## Minimal Config

`knowflow` reads `.env` from the repository root.

Most users only need:

- `KNOWLEDGE_BASE_DIR`
- `LLM_BASE_URL`
- `MODEL_NAME`
- `LLM_API_KEY`

Optional root overrides:

- `RAW_DIR`
- `CONTENT_DIR`
- `WIKI_DIR`
- `DATA_DIR`

Everything else is derived automatically from those roots.

## Raw Note Contract

`knowflow` recursively scans every `index.md` under `RAW_DIR`.

Example:

```text
RAW_DIR/
└── articles/
    └── systems/
        └── knowledge-compilers/
            └── index.md
```

Generated outputs usually land in:

```text
KNOWLEDGE_BASE_DIR/
├── _raw/
├── content/posts/
├── data/knowflow/
└── wiki/
    ├── concepts/
    └── sources/
```

More details: [docs/raw-format.md](docs/raw-format.md)

## Commands

```bash
python3 knowflow.py build
python3 knowflow.py build private
python3 knowflow.py build public
python3 knowflow.py build audit
python3 knowflow.py lint
python3 knowflow.py query <keyword>
python3 knowflow.py research
```

Available `build` stages:

- `private`
- `public`
- `clean`
- `index`
- `split`
- `topics`
- `sources`
- `compile`
- `audit`
- `graph`
- `log`

`build` runs the full default pipeline and, if needed, immediately ingests newly generated research drafts.

## Privacy

`knowflow` sends raw note content to the configured LLM endpoint during cleaning, topicing, splitting, compiling, and research-draft generation.

Before using it on sensitive notes, verify:

- where `LLM_BASE_URL` points
- whether `RAW_DESENSITIZE=true` is enough for your workflow
- whether you should use a self-hosted model

The current desensitization is best-effort only.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT. See [LICENSE](LICENSE).
