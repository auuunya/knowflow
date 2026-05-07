import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from core.infra.file_store import iter_raw_source_paths


logger = logging.getLogger(__name__)


class AuditService:
    _FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?", re.S)

    def __init__(self, config, index_repository, file_store):
        self.config = config
        self.index_repository = index_repository
        self.file_store = file_store

    def _iter_topics(self, index: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any], bool]]:
        topics = index.get("topics", {})
        if not isinstance(topics, dict):
            return []

        pairs: List[Tuple[str, Dict[str, Any], bool]] = []
        for topic, data in topics.items():
            if not isinstance(topic, str) or not isinstance(data, dict):
                continue
            pairs.append((topic, data, False))

            children = data.get("children", {})
            if isinstance(children, dict):
                for child_topic, child_data in children.items():
                    if isinstance(child_topic, str) and isinstance(child_data, dict):
                        pairs.append((child_topic, child_data, True))
        return pairs

    def _parse_frontmatter(self, text: str) -> Dict[str, Any]:
        match = self._FRONTMATTER_RE.match(text or "")
        if not match:
            return {}

        frontmatter: Dict[str, Any] = {}
        current_list_key: Optional[str] = None

        for raw_line in match.group(1).splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("- ") and current_list_key:
                value = stripped[2:].strip().strip('"').strip("'")
                if value:
                    frontmatter.setdefault(current_list_key, []).append(value)
                continue

            if ":" not in line:
                current_list_key = None
                continue

            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                current_list_key = None
                continue

            if not value:
                frontmatter[key] = []
                current_list_key = key
                continue

            current_list_key = None
            frontmatter[key] = value.strip('"').strip("'")

        return frontmatter

    def _strip_frontmatter(self, text: str) -> str:
        return self._FRONTMATTER_RE.sub("", text or "", count=1).strip()

    def _slug(self, value: Any) -> str:
        return self.config.slugify(value, fallback="item")

    def _topic_source_lookup(self, index: Dict[str, Any]) -> Dict[str, List[str]]:
        lookup: Dict[str, List[str]] = {}
        for topic, data, _ in self._iter_topics(index):
            if not isinstance(topic, str) or not isinstance(data, dict):
                continue
            topic_slug = self._slug(topic)
            lookup.setdefault(topic_slug, [])
            for raw_file in data.get("files", []) if isinstance(data.get("files"), list) else []:
                if not isinstance(raw_file, str) or Path(raw_file).parts[:1] == ("research",):
                    continue
                source_slug = self.config.source_slug_for(raw_file)
                if source_slug not in lookup[topic_slug]:
                    lookup[topic_slug].append(source_slug)
        return lookup

    def _list_markdown_files(self, section_dir: Path) -> List[Path]:
        if not section_dir.exists():
            return []
        return sorted(
            path
            for path in section_dir.rglob("*.md")
            if path.is_file() and path.name != "_index.md" and not path.name.startswith(".")
        )

    def _load_posts(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for path in self._list_markdown_files(Path(self.config.posts_dir)):
            text = self.file_store.read_text(path)
            meta = self._parse_frontmatter(text)
            body = self._strip_frontmatter(text)
            slug = str(meta.get("slug") or path.stem).strip() or path.stem
            concepts = meta.get("concepts", [])
            sources = meta.get("sources", [])
            if not isinstance(concepts, list):
                concepts = []
            if not isinstance(sources, list):
                sources = []
            items.append(
                {
                    "title": str(meta.get("title") or path.stem).strip() or path.stem,
                    "slug": slug,
                    "concepts": [str(item).strip() for item in concepts if str(item).strip()],
                    "sources": [str(item).strip() for item in sources if str(item).strip()],
                    "path": path,
                    "body_chars": len(body),
                }
            )
        return items

    def _load_concepts(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for path in self._list_markdown_files(Path(self.config.concepts_dir)):
            text = self.file_store.read_text(path)
            meta = self._parse_frontmatter(text)
            items.append(
                {
                    "title": str(meta.get("title") or path.stem).strip() or path.stem,
                    "slug": path.stem,
                    "path": path,
                }
            )
        return items

    def _load_sources(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for path in self._list_markdown_files(Path(self.config.sources_dir)):
            text = self.file_store.read_text(path)
            meta = self._parse_frontmatter(text)
            items.append(
                {
                    "title": str(meta.get("title") or path.stem).strip() or path.stem,
                    "slug": str(meta.get("slug") or path.stem).strip() or path.stem,
                    "topic": str(meta.get("topic") or "").strip(),
                    "source_kind": str(meta.get("source_kind") or "").strip(),
                    "raw_type": str(meta.get("raw_type") or "").strip(),
                    "raw_path": str(meta.get("raw_path") or "").strip(),
                    "path": path,
                }
            )
        return items

    def _count_raw_docs(self) -> int:
        raw_root = Path(self.config.raw_dir)
        count = 0
        for path in iter_raw_source_paths(raw_root):
            rel_dir = path.parent.relative_to(raw_root).as_posix() if path.is_file() else path.relative_to(raw_root).as_posix()
            if not self._is_generated_research_dir(rel_dir):
                count += 1
        return count

    def _count_metadata_docs(self) -> int:
        metadata_root = Path(self.config.metadata_dir)
        return sum(
            1
            for path in metadata_root.rglob("meta.json")
            if path.is_file() and not self._is_generated_research_dir(path.relative_to(metadata_root).parent.as_posix())
        )

    def _count_files(self, folder: Path, pattern: str = "*.json") -> int:
        if not folder.exists():
            return 0
        return sum(1 for path in folder.rglob(pattern) if path.is_file())

    @staticmethod
    def _is_generated_research_dir(rel_dir: str) -> bool:
        parts = Path(rel_dir).parts
        return bool(parts) and parts[0] == "research"

    def _collect_raw_meta_coverage(self) -> Dict[str, List[str]]:
        raw_root = Path(self.config.raw_dir)
        metadata_root = Path(self.config.metadata_dir)

        raw_docs = []
        for path in iter_raw_source_paths(raw_root):
            rel_dir = path.parent.relative_to(raw_root).as_posix() if path.is_file() else path.relative_to(raw_root).as_posix()
            if not self._is_generated_research_dir(rel_dir):
                raw_docs.append(rel_dir)
        raw_docs.sort()
        meta_docs = sorted(
            path.relative_to(metadata_root).parent.as_posix()
            for path in metadata_root.rglob("meta.json")
            if path.is_file() and not self._is_generated_research_dir(path.relative_to(metadata_root).parent.as_posix())
        )

        raw_set = set(raw_docs)
        meta_set = set(meta_docs)
        return {
            "raw_without_meta": sorted(raw_set - meta_set),
            "meta_without_raw": sorted(meta_set - raw_set),
        }

    def _collect_graph_stats(self, graph: Dict[str, Any]) -> Dict[str, Any]:
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        edges = graph.get("edges", []) if isinstance(graph, dict) else []
        relation_counts = Counter()
        for edge in edges:
            if isinstance(edge, dict):
                relation = str(edge.get("relation") or "related").strip() or "related"
                relation_counts[relation] += 1
        return {
            "nodes": len(nodes) if isinstance(nodes, list) else 0,
            "edges": len(edges) if isinstance(edges, list) else 0,
            "relations": dict(sorted(relation_counts.items())),
        }

    def _collect_topic_stats(self, index: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for topic, data, is_child in self._iter_topics(index):
            files = [item for item in data.get("files", []) if isinstance(item, str)]
            children = data.get("children", {})
            rows.append(
                {
                    "topic": topic,
                    "slug": self.config.slugify(topic, fallback="concept"),
                    "summary": str(data.get("summary") or "").strip(),
                    "files": files,
                    "file_count": len(files),
                    "child_count": len(children) if isinstance(children, dict) else 0,
                    "entities": list(data.get("entities", [])) if isinstance(data.get("entities"), list) else [],
                    "comparisons": list(data.get("comparisons", [])) if isinstance(data.get("comparisons"), list) else [],
                    "is_child": is_child,
                }
            )
        return rows

    @staticmethod
    def _status(ok: bool, degraded: bool = False) -> str:
        if ok:
            return "ready"
        if degraded:
            return "partial"
        return "needs-work"

    def _format_post_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": item["title"],
            "slug": item["slug"],
            "url": f"/posts/{item['slug']}/",
            "path": str(item["path"].relative_to(self.config.knowledge_base_dir)),
        }

    def _format_concept_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": item["title"],
            "slug": item["slug"],
            "url": "",
            "path": str(item["path"].relative_to(self.config.knowledge_base_dir)),
        }

    def _format_source_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": item["title"],
            "slug": item["slug"],
            "url": "",
            "path": str(item["path"].relative_to(self.config.knowledge_base_dir)),
            "topic": item["topic"],
            "raw_path": item["raw_path"],
        }

    def _build_findings(
        self,
        index: Dict[str, Any],
        posts: List[Dict[str, Any]],
        concepts: List[Dict[str, Any]],
        sources: List[Dict[str, Any]],
        coverage: Dict[str, List[str]],
    ) -> Dict[str, Any]:
        concept_pages_by_slug = {item["slug"]: item for item in concepts}
        source_pages_by_slug = {item["slug"]: item for item in sources}
        topic_source_lookup = self._topic_source_lookup(index)
        index_topic_slugs = {
            self.config.slugify(topic, fallback="concept")
            for topic, _, _ in self._iter_topics(index)
        }

        post_concept_slugs: Set[str] = set()
        post_source_slugs: Set[str] = set()
        posts_missing_concepts: List[Dict[str, Any]] = []
        posts_missing_sources: List[Dict[str, Any]] = []

        for post in posts:
            if post["concepts"]:
                post_concept_slugs.update(post["concepts"])
            else:
                posts_missing_concepts.append(self._format_post_item(post))

            resolved_sources: List[str] = []
            for concept_slug in post["concepts"]:
                for source_slug in topic_source_lookup.get(self._slug(concept_slug), []):
                    if source_slug not in resolved_sources:
                        resolved_sources.append(source_slug)

            if resolved_sources:
                post_source_slugs.update(resolved_sources)
            else:
                posts_missing_sources.append(self._format_post_item(post))

        concept_refs_without_pages = sorted(
            slug
            for slug in post_concept_slugs
            if slug not in concept_pages_by_slug and slug not in index_topic_slugs
        )
        source_refs_without_pages = sorted(slug for slug in post_source_slugs if slug not in source_pages_by_slug)

        concept_pages_without_posts = [
            self._format_concept_item(item)
            for slug, item in sorted(concept_pages_by_slug.items())
            if slug not in post_concept_slugs
        ]
        unused_sources = [
            self._format_source_item(item)
            for slug, item in sorted(source_pages_by_slug.items())
            if slug not in post_source_slugs
        ]

        topic_rows = self._collect_topic_stats(index)
        oversized_topics = [
            {
                "title": row["topic"],
                "slug": row["slug"],
                "file_count": row["file_count"],
                "files": row["files"],
                "url": "",
            }
            for row in topic_rows
            if not row["is_child"] and row["file_count"] >= 6 and row["child_count"] == 0
        ]
        topics_without_summary = [
            {
                "title": row["topic"],
                "slug": row["slug"],
                "url": "",
            }
            for row in topic_rows
            if not row["summary"]
        ]
        single_source_dense_topics = [
            {
                "title": row["topic"],
                "slug": row["slug"],
                "file_count": row["file_count"],
                "entities": len(row["entities"]),
                "comparisons": len(row["comparisons"]),
                "files": row["files"],
                "url": "",
            }
            for row in topic_rows
            if row["file_count"] <= 1 and (len(row["entities"]) >= 4 or len(row["comparisons"]) >= 2)
        ]

        return {
            "raw_without_meta": coverage["raw_without_meta"],
            "meta_without_raw": coverage["meta_without_raw"],
            "posts_missing_concepts": posts_missing_concepts,
            "posts_missing_sources": posts_missing_sources,
            "concept_refs_without_pages": [
                {"slug": slug, "url": ""} for slug in concept_refs_without_pages
            ],
            "source_refs_without_pages": [
                {"slug": slug, "url": ""} for slug in source_refs_without_pages
            ],
            "concept_pages_without_posts": concept_pages_without_posts,
            "unused_sources": unused_sources,
            "oversized_topics": oversized_topics,
            "topics_without_summary": topics_without_summary,
            "single_source_dense_topics": single_source_dense_topics,
        }

    def _build_research_queue(self, findings: Dict[str, Any], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
        queue: List[Dict[str, Any]] = []

        for item in findings["raw_without_meta"][:20]:
            raw_path = f"{item}/index.md"
            topic = Path(item).name
            queue.append(
                {
                    "priority": "high",
                    "kind": "metadata-gap",
                    "title": f"为 raw 文档补 metadata：{item}",
                    "reason": "raw 已存在，但 metadata 尚未生成或未同步到新位置。",
                    "raw_path": raw_path,
                    "raw_paths": [raw_path],
                    "topic": topic,
                    "topic_slug": self.config.source_slug_for(raw_path),
                    "draftable": True,
                }
            )

        for item in findings["posts_missing_sources"][:20]:
            queue.append(
                {
                    "priority": "high",
                    "kind": "post-source-link",
                    "title": f"为文章补来源：{item['title']}",
                    "reason": "文章已发布，但缺少显式 sources 关系，图谱可追溯性不足。",
                    "url": item["url"],
                    "draftable": False,
                }
            )

        for item in findings["unused_sources"][:20]:
            topic = str(item.get("topic") or item.get("title") or "").strip()
            raw_path = str(item.get("raw_path") or "").strip()
            queue.append(
                {
                    "priority": "medium",
                    "kind": "source-triage",
                    "title": f"处理未进入 post 的来源：{item['title']}",
                    "reason": "来源页已生成，但未被任何融合文章引用；需要决定合并、拆 topic 或标记不保留。",
                    "url": item["url"],
                    "raw_path": raw_path,
                    "raw_paths": [raw_path] if raw_path else [],
                    "topic": topic,
                    "topic_slug": self.config.source_slug_for(raw_path) if raw_path else self.config.slugify(topic, fallback="source"),
                    "draftable": True,
                }
            )

        for item in findings["oversized_topics"][:20]:
            queue.append(
                {
                    "priority": "medium",
                    "kind": "topic-split",
                    "title": f"拆分大主题：{item['title']}",
                    "reason": f"该主题当前包含 {item['file_count']} 个来源，但还没有 children 结构。",
                    "url": item["url"],
                    "topic": item["title"],
                    "topic_slug": item["slug"],
                    "raw_paths": list(item.get("files", [])),
                    "draftable": True,
                }
            )

        for item in findings["single_source_dense_topics"][:20]:
            queue.append(
                {
                    "priority": "low",
                    "kind": "depth-research",
                    "title": f"补充单来源高密度主题：{item['title']}",
                    "reason": f"当前仅 {item['file_count']} 个来源，但已抽取 {item['entities']} 个实体 / {item['comparisons']} 个对比，值得继续补料。",
                    "url": item["url"],
                    "topic": item["title"],
                    "topic_slug": item["slug"],
                    "raw_paths": list(item.get("files", [])),
                    "draftable": True,
                }
            )

        if findings.get("phase2_materialization_gap"):
            queue.append(
                {
                    "priority": "medium",
                    "kind": "phase2-materialize",
                    "title": "把 entities / comparisons 接入发布层与图谱",
                    "reason": (
                        f"当前已抽取 entities=`{stats['entities']}`、comparisons=`{stats['comparisons']}`，"
                        "但还没有对应的 materialized 输出。"
                    ),
                    "draftable": False,
                }
            )

        if findings.get("graph_mentions_missing"):
            queue.append(
                {
                    "priority": "medium",
                    "kind": "graph-mentions",
                    "title": "为 graph 增补正文 mentions / related 边",
                    "reason": "当前图谱已有 explains 与 compiled-from，但还没有基于正文提及关系的概念连接。",
                    "draftable": False,
                }
            )

        return queue[:50]

    def _render_loop_status(self, loop: Dict[str, Any]) -> str:
        lines = []
        for key in ("ingest", "governance", "linking", "insight", "research"):
            item = loop.get(key, {})
            lines.append(f"- {item.get('label', key)}：`{item.get('status', 'unknown')}`，{item.get('detail', '')}\n")
        return "".join(lines)

    def _render_simple_list(self, items: Iterable[str], empty_text: str) -> str:
        rows = [f"- `{item}`\n" for item in items]
        return "".join(rows) if rows else f"- {empty_text}\n"

    def _render_linked_items(self, items: List[Dict[str, Any]], empty_text: str) -> str:
        if not items:
            return f"- {empty_text}\n"
        rows = []
        for item in items:
            title = str(item.get("title") or item.get("slug") or "").strip()
            url = str(item.get("url") or "").strip()
            if url:
                rows.append(f"- [{title}]({url})\n")
            else:
                rows.append(f"- `{title}`\n")
        return "".join(rows)

    def _render_queue(self, queue: List[Dict[str, Any]]) -> str:
        if not queue:
            return "- 当前没有新增研究候选。\n"

        lines = []
        for item in queue:
            title = str(item.get("title") or "").strip()
            reason = str(item.get("reason") or "").strip()
            priority = str(item.get("priority") or "medium").strip()
            url = str(item.get("url") or "").strip()
            if url:
                lines.append(f"- `{priority}` [{title}]({url})：{reason}\n")
            else:
                lines.append(f"- `{priority}` {title}：{reason}\n")
        return "".join(lines)

    def _build_research_queue_note(self, payload: Dict[str, Any]) -> str:
        queue = payload.get("research_queue", [])
        loop = payload.get("loop", {})
        loop_lines = []
        for key in ("ingest", "governance", "linking", "insight", "research"):
            item = loop.get(key, {})
            loop_lines.append(f"- {item.get('label', key)}：{item.get('status', 'unknown')}，{item.get('detail', '')}\n")

        header = (
            "# Knowflow Research Queue\n\n"
            f"这是一份由 `knowflow audit` 自动回写到 `{Path(self.config.raw_dir).relative_to(self.config.knowledge_base_dir).as_posix()}` 的研究候选清单。\n\n"
            "说明：这个文件名不是 `index.md`，因此不会被 `clean` 当作正常来源再次摄入。\n\n"
            f"- 更新时间：`{payload.get('generated_at', '')}`\n"
            f"- 洞察页：`{Path(self.config.insights_report_path).relative_to(self.config.knowledge_base_dir).as_posix()}`\n\n"
            "## 当前闭环状态\n"
            + "".join(loop_lines)
            + "\n## 待补研究\n"
        )

        if not isinstance(queue, list) or not queue:
            return header + "- 当前没有新增研究候选。\n"

        lines = []
        for item in queue:
            priority = str(item.get("priority") or "medium").strip()
            title = str(item.get("title") or "").strip()
            reason = str(item.get("reason") or "").strip()
            url = str(item.get("url") or "").strip()
            kind = str(item.get("kind") or "").strip()
            line = f"- [{priority}] {title}"
            if kind:
                line += f" (`{kind}`)"
            if reason:
                line += f"：{reason}"
            if url:
                line += f"  参考：`{url}`"
            lines.append(line + "\n")

        return header + "".join(lines)

    def _build_markdown(self, payload: Dict[str, Any]) -> str:
        stats = payload["stats"]
        findings = payload["findings"]
        graph = stats["graph"]
        relation_text = ", ".join(f"{key}={value}" for key, value in graph["relations"].items()) or "none"

        frontmatter = (
            "---\n"
            "title: \"Insights\"\n"
            "layout: \"single\"\n"
            "---\n\n"
        )

        return (
            frontmatter
            + "这个页面由 `knowflow audit` 自动生成，用来回答当前知识库是否已经形成“摄入 -> 治理 -> 链接 -> 洞察 -> 研究”的工作闭环。\n\n"
            + f"- 生成时间：`{payload['generated_at']}`\n"
            + "- 图谱入口：[Graph](/graph/)\n\n"
            + "## 概览\n"
            + f"- RAW 文档：`{stats['raw_sources']}`\n"
            + f"- Metadata：`{stats['metadata_files']}`\n"
            + f"- Topics：`{stats['topics_total']}`（顶层 `{stats['top_level_topics']}` / 子主题 `{stats['child_topics']}`）\n"
            + f"- Posts：`{stats['posts']}`\n"
            + f"- Private Concepts：`{stats['concepts']}`\n"
            + f"- Private Sources：`{stats['sources']}`\n"
            + f"- Entities：`{stats['entities']}`\n"
            + f"- Comparisons：`{stats['comparisons']}`\n"
            + f"- Graph Nodes / Edges：`{graph['nodes']}` / `{graph['edges']}`\n"
            + f"- Graph Relations：`{relation_text}`\n\n"
            + "## 闭环状态\n"
            + self._render_loop_status(payload["loop"])
            + "\n## 高优先级问题\n"
            + "### RAW 已有但 metadata 缺失\n"
            + self._render_simple_list(findings["raw_without_meta"], "没有发现。")
            + "\n### 文章缺少 concepts\n"
            + self._render_linked_items(findings["posts_missing_concepts"], "没有发现。")
            + "\n### 文章缺少 sources\n"
            + self._render_linked_items(findings["posts_missing_sources"], "没有发现。")
            + "\n### frontmatter 引用了不存在的概念页\n"
            + self._render_simple_list((item["slug"] for item in findings["concept_refs_without_pages"]), "没有发现。")
            + "\n### frontmatter 引用了不存在的来源页\n"
            + self._render_simple_list((item["slug"] for item in findings["source_refs_without_pages"]), "没有发现。")
            + "\n## 中优先级问题\n"
            + "### 未进入任何 post 的来源页\n"
            + self._render_linked_items(findings["unused_sources"], "没有发现。")
            + "\n### 没有被 post 使用的概念页\n"
            + self._render_linked_items(findings["concept_pages_without_posts"], "没有发现。")
            + "\n### 仍然偏大的 topic\n"
            + self._render_linked_items(findings["oversized_topics"], "没有发现。")
            + "\n### 缺少 summary 的 topic\n"
            + self._render_linked_items(findings["topics_without_summary"], "没有发现。")
            + "\n## 低优先级提醒\n"
            + "### 单来源但知识密度偏高的主题\n"
            + self._render_linked_items(findings["single_source_dense_topics"], "没有发现。")
            + "\n### entities / comparisons 尚未 materialize\n"
            + (
                (
                    f"- 已抽取 entities=`{stats['entities']}`、comparisons=`{stats['comparisons']}`，"
                    f"但 materialized entities=`{stats['entity_materialized']}`、comparisons=`{stats['comparison_materialized']}`。\n"
                )
                if findings["phase2_materialization_gap"]
                else "- 没有发现。\n"
            )
            + "\n### graph 仍缺少 mentions 边\n"
            + ("- 当前尚未生成正文提及关系（`mentions`）边。\n" if findings["graph_mentions_missing"] else "- 没有发现。\n")
            + "\n## 研究候选\n"
            + self._render_queue(payload["research_queue"])
        )

    def build_report(self) -> Dict[str, Any]:
        index = self.index_repository.load_index()
        posts = self._load_posts()
        concepts = self._load_concepts()
        sources = self._load_sources()
        graph = self.file_store.load_json(self.config.public_knowledge_graph_path)
        coverage = self._collect_raw_meta_coverage()
        graph_stats = self._collect_graph_stats(graph)
        topic_rows = self._collect_topic_stats(index)

        stats = {
            "raw_sources": self._count_raw_docs(),
            "metadata_files": self._count_metadata_docs(),
            "topics_total": len(topic_rows),
            "top_level_topics": sum(1 for row in topic_rows if not row["is_child"]),
            "child_topics": sum(1 for row in topic_rows if row["is_child"]),
            "posts": len(posts),
            "concepts": len(concepts),
            "sources": len(sources),
            "entities": len(index.get("entities", {})) if isinstance(index.get("entities"), dict) else 0,
            "comparisons": len(index.get("comparisons", {})) if isinstance(index.get("comparisons"), dict) else 0,
            "entity_materialized": self._count_files(Path(self.config.entities_dir)),
            "comparison_materialized": self._count_files(Path(self.config.comparisons_dir)),
            "graph": graph_stats,
        }

        findings = self._build_findings(index, posts, concepts, sources, coverage)
        findings["phase2_materialization_gap"] = (
            (stats["entities"] > 0 and stats["entity_materialized"] == 0)
            or (stats["comparisons"] > 0 and stats["comparison_materialized"] == 0)
        )
        findings["graph_mentions_missing"] = False
        research_queue = self._build_research_queue(findings, stats)
        has_governed_topics = stats["topics_total"] > 0
        has_linked_publish_layer = stats["posts"] > 0 and graph_stats["edges"] > 0

        loop = {
            "ingest": {
                "label": "摄入",
                "status": self._status(not findings["raw_without_meta"], degraded=bool(findings["meta_without_raw"])),
                "detail": f"raw=`{stats['raw_sources']}`，metadata=`{stats['metadata_files']}`。",
            },
            "governance": {
                "label": "治理",
                "status": self._status(
                    has_governed_topics and not findings["oversized_topics"] and not findings["topics_without_summary"],
                    degraded=has_governed_topics and bool(findings["single_source_dense_topics"]),
                ),
                "detail": f"topics=`{stats['topics_total']}`，entities=`{stats['entities']}`，comparisons=`{stats['comparisons']}`。",
            },
            "linking": {
                "label": "链接",
                "status": self._status(
                    has_linked_publish_layer
                    and not findings["posts_missing_concepts"]
                    and not findings["posts_missing_sources"]
                    and not findings["concept_refs_without_pages"]
                    and not findings["source_refs_without_pages"]
                    and not findings["phase2_materialization_gap"],
                    degraded=(
                        has_linked_publish_layer and (
                            findings["phase2_materialization_gap"]
                        )
                    ),
                ),
                "detail": (
                    f"graph edges=`{graph_stats['edges']}`，explains=`{graph_stats['relations'].get('explains', 0)}`，"
                    f"related=`{graph_stats['relations'].get('related', 0)}`。"
                ),
            },
            "insight": {
                "label": "洞察",
                "status": "ready",
                "detail": "已有结构化 audit 报告，可直接暴露高优先级缺口与研究候选。",
            },
            "research": {
                "label": "研究",
                "status": "partial",
                "detail": (
                    f"已自动回写 `{len(research_queue)}` 条候选任务到 "
                    f"`{Path(self.config.research_queue_raw_path).relative_to(self.config.knowledge_base_dir).as_posix()}`，"
                    "但深度补料仍未自动写回原始专题笔记。"
                ),
            },
        }

        payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "stats": stats,
            "loop": loop,
            "findings": findings,
            "research_queue": research_queue,
            "research_queue_raw_path": str(Path(self.config.research_queue_raw_path).relative_to(self.config.knowledge_base_dir)),
        }

        self.file_store.save_json(self.config.insights_path, payload)
        self.file_store.save_text(self.config.insights_report_path, self._build_markdown(payload))
        self.file_store.save_text(self.config.research_queue_raw_path, self._build_research_queue_note(payload))
        logger.info("audit report updated: %s", self.config.insights_report_path)
        logger.info("audit json updated: %s", self.config.insights_path)
        logger.info("research queue raw note updated: %s", self.config.research_queue_raw_path)
        return payload
