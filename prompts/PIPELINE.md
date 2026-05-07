# Knowflow Prompt 流程契约

本文档定义 7 个 prompt 的输入/输出 schema 和流水线连接关系。

---

## 流水线总览

```
原始笔记
    │
    ▼
┌─────────────────┐
│  clean-source   │  笔记 → 结构化元数据
└────────┬────────┘
         │ Note[]
         ▼
┌─────────────────┐
│  split-topics   │  大 topic → 子 topic 分组
└────────┬────────┘
         │ TopicGroup[]
         ▼
┌─────────────────┐
│ reconcile-index │  索引治理（merge/split/rename）
└────────┬────────┘
         │ Index
         ├──────────────────┬──────────────────┬──────────────────┐
         ▼                  ▼                  ▼                  ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│   fuse-post     │ │ explain-concept │ │ generate-title  │ │  draft-research │
│  多笔记→正文     │ │  概念 hub 页面   │ │  标题 + slug    │ │   研究草稿       │
└─────────────────┘ └─────────────────┘ └─────────────────┘ └─────────────────┘
```

---

## 数据结构定义

### Note（clean-source 输出）

```json
{
  "keep": true,
  "type": "knowledge",
  "title": "Git rebase 合并 commit",
  "topic": "git",
  "tags": ["git", "rebase", "commit"],
  "summary": "使用 git rebase -i 可以合并多个 commit...",
  "entities": [
    {"name": "git", "type": "tool", "summary": "版本控制工具", "aliases": []}
  ],
  "comparisons": []
}
```

| 字段 | 类型 | 必须 | 说明 |
|---|---|---|---|
| keep | boolean | 是 | 是否保留 |
| type | string | 是 | knowledge / fragment / memo / trash |
| title | string | 是 | 短标题 |
| topic | string | 是 | 领域概念名，kebab-case |
| tags | string[] | 是 | 3-6 个 |
| summary | string | 是 | 1-2 句话 |
| entities | object[] | 是 | 0-8 个 |
| comparisons | object[] | 是 | 0-3 个 |

### TopicGroup（split-topics 输出）

```json
{
  "children": [
    {"topic": "git-branch", "files": ["a.md", "b.md"]},
    {"topic": "git-commit", "files": ["c.md"]}
  ]
}
```

| 字段 | 类型 | 必须 | 说明 |
|---|---|---|---|
| children | object[] | 是 | 子 topic 列表，2-6 个 |
| children[].topic | string | 是 | 子 topic 名，kebab-case |
| children[].files | string[] | 是 | 至少 1 个文件 |

### IndexOp（reconcile-index 输出）

```json
{
  "merge": [{"target": "git", "sources": ["git-cli"]}],
  "split": [{"source": "linux", "targets": ["linux-network", "linux-storage"]}],
  "rename": [{"from": "docker-tools", "to": "docker"}]
}
```

| 字段 | 类型 | 必须 | 说明 |
|---|---|---|---|
| merge | object[] | 是 | 合并操作 |
| merge[].target | string | 是 | 保留的 topic |
| merge[].sources | string[] | 是 | 被合并的 topic |
| split | object[] | 是 | 拆分操作 |
| split[].source | string | 是 | 被拆分的 topic |
| split[].targets | string[] | 是 | 拆出的子 topic |
| rename | object[] | 是 | 重命名操作 |
| rename[].from | string | 是 | 原名 |
| rename[].to | string | 是 | 新名 |

### TitleSlug（generate-title 输出）

```json
{"title": "Git 核心概念与日常工作流", "slug": "git-core-concepts"}
```

| 字段 | 类型 | 必须 | 说明 |
|---|---|---|---|
| title | string | 是 | 8-25 字 |
| slug | string | 是 | 小写 kebab-case，3-8 词 |

---

## Prompt 详细契约

### 1. clean-source

**角色**: 将原始笔记转化为结构化元数据

**输入**:

| 变量 | 类型 | 必须 | 来源 |
|---|---|---|---|
| `{content}` | string | 是 | 原始笔记文本 |

**输出**: `Note` JSON 对象

**下游依赖**: `topic` 字段被 split-topics / reconcile-index / explain-concept / generate-title / draft-research 使用

---

### 2. split-topics

**角色**: 将过大的 topic 拆分为子 topic

**输入**:

| 变量 | 类型 | 必须 | 来源 |
|---|---|---|---|
| `{topic}` | string | 是 | 来自 clean-source 的 topic 或人工指定 |
| `{files}` | string[] | 是 | 该 topic 下的文件列表 |

**输出**: `TopicGroup` JSON 对象

**上游依赖**: topic 通常来自 clean-source 的输出
**下游依赖**: children 被 reconcile-index 用于更新索引结构

---

### 3. reconcile-index

**角色**: 治理索引，合并/拆分/重命名 topic

**输入**:

| 变量 | 类型 | 必须 | 来源 |
|---|---|---|---|
| `{index}` | Index JSON | 是 | 当前知识库索引 |

**输出**: `IndexOp` JSON 对象

**上游依赖**: index 包含了 clean-source 和 split-topics 的结果
**下游依赖**: merge/split/rename 结果影响 fuse-post / explain-concept 的 topics 和 topic_map

---

### 4. fuse-post

**角色**: 将多条笔记融合为一篇 wiki 风格正文

**输入**:

| 变量 | 类型 | 必须 | 来源 |
|---|---|---|---|
| `{index}` | Index JSON | 是 | 当前 topic 的索引条目 |
| `{content}` | Note[] | 是 | 原始笔记集合（keep=true 的） |
| `{topics}` | string[] | 否 | 已有概念列表，用于内链匹配 |
| `{topic_map}` | object | 否 | topic→summary 映射 |

**输出**: Markdown 正文（无 frontmatter）

**上游依赖**:
- `index` 来自 reconcile-index 治理后的索引
- `content` 来自 clean-source 筛选后的笔记（keep=true）

---

### 5. explain-concept

**角色**: 生成概念 hub 页面正文

**输入**:

| 变量 | 类型 | 必须 | 来源 |
|---|---|---|---|
| `{topic}` | string | 是 | 概念名 |
| `{summary}` | string | 是 | 概念摘要 |
| `{tags}` | string[] | 否 | 标签 |
| `{entities}` | object[] | 否 | 相关实体 |
| `{comparisons}` | object[] | 否 | 对比关系 |
| `{topics}` | string[] | 否 | 已有概念列表 |
| `{topic_map}` | object | 否 | topic→summary 映射 |
| `{related_topics}` | string[] | 否 | 相关概念 |
| `{source_notes}` | object[] | 否 | 来源笔记信息 |
| `{content_digest}` | string | 否 | 内容摘要 |

**输出**: Markdown 正文（无 frontmatter）

**上游依赖**: topic/summary/tags/entities/comparisons 均来自 clean-source 的输出

---

### 6. generate-title

**角色**: 为 topic 生成标题和 slug

**输入**:

| 变量 | 类型 | 必须 | 来源 |
|---|---|---|---|
| `{topic}` | string | 是 | 概念名 |
| `{summary}` | string | 是 | 概念摘要 |

**输出**: `TitleSlug` JSON 对象

**上游依赖**: topic 和 summary 来自 clean-source 或 reconcile-index
**下游依赖**: title 和 slug 用于 Hugo 页面文件名和 URL

---

### 7. draft-research

**角色**: 为 topic 生成研究草稿

**输入**:

| 变量 | 类型 | 必须 | 来源 |
|---|---|---|---|
| `{topic}` | string | 是 | 研究主题 |
| `{task_summary}` | string | 否 | 候选任务描述 |
| `{seed_sources}` | string[] | 否 | 已有来源 |
| `{source_excerpts}` | string[] | 否 | 原始摘录 |

**输出**: Markdown 研究草稿（无 frontmatter）

**上游依赖**: topic 来自 reconcile-index 的审计结果

---

## 变量命名规范

| 变量名 | 含义 | 使用 prompt |
|---|---|---|
| `{content}` | 原始笔记文本或笔记集合 | clean-source, fuse-post |
| `{topic}` | 领域概念名（kebab-case） | 全部 7 个 |
| `{summary}` | 概念摘要（1-2 句） | generate-title, explain-concept |
| `{index}` | 索引 JSON 数据 | reconcile-index, fuse-post |
| `{topics}` | 已有概念名列表（string[]） | fuse-post, explain-concept |
| `{topic_map}` | concept→summary 映射（object） | fuse-post, explain-concept |
| `{files}` | 文件路径列表（string[]） | split-topics |

---

## 使用示例：典型流水线

```
1. 收集原始笔记 → 逐条调用 clean-source
2. 按 topic 分组 → 对过大的 topic 调用 split-topics
3. 汇总索引 → 调用 reconcile-index 治理
4. 对每个 topic：
   a. 调用 fuse-post 生成正文
   b. 调用 explain-concept 生成概念 hub
   c. 调用 generate-title 生成标题和 slug
5. 对 reconcile-index 发现的缺口 topic → 调用 draft-research
```
