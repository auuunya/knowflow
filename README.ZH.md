# knowflow

[English README](README.md)

`knowflow` 用来把原始笔记目录编译成：

- 私有 wiki 页面：`wiki/`
- 可发布的文章内容：`content/posts`
- 图谱、审计、缓存等数据：`data/`

## 依赖

- Python 3.11+
- 一个 OpenAI-compatible LLM 接口
- 如果你想把 `content/` 渲染成站点，可以额外使用 Hugo

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 快速开始

```bash
cp .env.example .env
python3 knowflow.py build
python3 knowflow.py lint
python3 knowflow.py query compiler
```

默认的 `.env.example` 已经指向 [examples/minimal-kb](examples/minimal-kb/README.md)，所以 clone 下来后可以直接试跑。

如果你的知识库仓库和当前仓库是同级目录，可以这样设置：

```bash
KNOWLEDGE_BASE_DIR=../my-kb
```

## 最小配置

`knowflow` 从仓库根目录的 `.env` 读取配置。

大多数情况下只需要这几个：

- `KNOWLEDGE_BASE_DIR`
- `LLM_BASE_URL`
- `MODEL_NAME`
- `LLM_API_KEY`

如果目录结构特殊，可以额外覆盖这些根目录：

- `RAW_DIR`
- `CONTENT_DIR`
- `WIKI_DIR`
- `DATA_DIR`

其他路径都会自动从这些根目录推导出来。

## 原始笔记约定

`knowflow` 会递归扫描 `RAW_DIR` 下所有名为 `index.md` 的文件。

示例：

```text
RAW_DIR/
└── articles/
    └── systems/
        └── knowledge-compilers/
            └── index.md
```

生成物通常会落到：

```text
KNOWLEDGE_BASE_DIR/
├── _raw/
├── content/posts/
├── data/knowflow/
└── wiki/
    ├── concepts/
    └── sources/
```

详细说明见：[docs/raw-format.md](docs/raw-format.md)

## 常用命令

```bash
python3 knowflow.py build
python3 knowflow.py build private
python3 knowflow.py build public
python3 knowflow.py build audit
python3 knowflow.py lint
python3 knowflow.py query <keyword>
python3 knowflow.py research
```

可用的 `build` stage：

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

`build` 会执行完整默认流程，并在需要时立即摄入新生成的 research drafts。

## 隐私说明

在 `clean`、`topic`、`split`、`compile`、`research draft` 等阶段，`knowflow` 会把原始笔记内容发送到你配置的 LLM 接口。

在处理敏感笔记前，请确认：

- `LLM_BASE_URL` 指向哪里
- `RAW_DESENSITIZE=true` 是否足够
- 是否应该改用自托管模型

当前脱敏只是 best-effort，不应被视为严格的数据防泄漏机制。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 许可证

MIT，见 [LICENSE](LICENSE)。
