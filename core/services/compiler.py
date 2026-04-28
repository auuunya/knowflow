import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class CompilerService:
    _NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
    _FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?", re.S)
    _CONCEPT_LINK_RE = re.compile(r"/concepts/([^/?#]+)/?")

    def __init__(self, config, index_repository, llm_client, prompt_registry, file_store, parser):
        self.config = config
        self.index_repository = index_repository
        self.llm_client = llm_client
        self.prompt_registry = prompt_registry
        self.file_store = file_store
        self.parser = parser
        self._cache_lock = threading.Lock()

    def _slug(self, value: str) -> str:
        return self.config.slugify(value, fallback="post")

    def _source_slug(self, rel_path: str) -> str:
        return self.config.source_slug_for(rel_path)

    def _is_research_draft_rel_path(self, rel_path: Any) -> bool:
        candidate = Path(str(rel_path or "").strip())
        return bool(candidate.parts[:1] == ("research",))

    def _comparison_output_name(self, key: str) -> str:
        normalized = self._slug(key)[:96] or "comparison"
        digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
        return f"{normalized}-{digest}.json"

    def _load_source_meta(self, rel_path: str) -> Dict[str, Any]:
        meta = self.file_store.load_json(self.config.meta_path_for(rel_path))
        return meta if isinstance(meta, dict) else {}

    def _collect_related_sources(self, files: List[str]) -> List[Dict[str, str]]:
        related_sources: List[Dict[str, str]] = []
        seen: Set[str] = set()

        for rel_path in files:
            if not isinstance(rel_path, str) or not rel_path.strip():
                continue
            if self._is_research_draft_rel_path(rel_path):
                continue

            slug = self._source_slug(rel_path)
            if slug in seen:
                continue
            seen.add(slug)

            rel_dir = Path(rel_path).parent
            meta = self._load_source_meta(rel_path)
            title = str(meta.get("title") or rel_dir.name).strip() or rel_dir.name
            related_sources.append(
                {
                    "title": title,
                    "slug": slug,
                }
            )

        return related_sources

    def _render_related_sources(self, related_sources: List[Dict[str, str]]) -> str:
        return ""

    def _calc_hash(
        self,
        topic: str,
        contents: List[str],
        title: str,
        slug: str,
        concepts: List[str],
        sources: List[str],
        related_sources: List[Dict[str, str]],
        body: str,
    ) -> str:
        payload = {
            "schema": "public-post-v2",
            "topic": topic,
            "title": title,
            "slug": slug,
            "concepts": concepts,
            "sources": sources,
            "contents": contents,
            "related_sources": related_sources,
            "body": body,
        }
        return hashlib.md5(repr(payload).encode("utf-8")).hexdigest()

    def _collect_contents(self, data: Dict[str, Any]) -> List[str]:
        contents: List[str] = []
        for item in data.get("files", []):
            if not isinstance(item, str):
                continue
            if self._is_research_draft_rel_path(item):
                continue
            path = Path(self.config.raw_dir) / item
            if not path.is_file():
                continue
            try:
                contents.append(self.file_store.read_text(path))
            except Exception as exc:
                logger.error("读取编译源文件失败: %s, %s", path, exc)
        return contents

    def _iter_topics(self, index: Dict[str, Any]):
        """扁平化主题索引（展开 split 后的 children 结构）。"""
        topics = index.get("topics", {})
        if not isinstance(topics, dict):
            return []

        pairs = []
        for topic, data in topics.items():
            if not isinstance(topic, str) or not isinstance(data, dict):
                continue
            pairs.append((topic, data))

            children = data.get("children", {})
            if isinstance(children, dict):
                for child_topic, child_data in children.items():
                    if isinstance(child_topic, str) and isinstance(child_data, dict):
                        pairs.append((child_topic, child_data))

        return pairs

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        pattern = re.compile(r"^\s*---\s*\n.*?\n---\s*\n?", re.S)
        return re.sub(pattern, "", text, count=1).strip()

    def _extract_mentioned_concepts(self, text: str) -> List[str]:
        body = self._strip_frontmatter(text or "")
        seen: Set[str] = set()
        mentions: List[str] = []
        for match in self._CONCEPT_LINK_RE.findall(body):
            slug = self._slug(match)
            if slug and slug not in seen:
                seen.add(slug)
                mentions.append(slug)
        return mentions

    @staticmethod
    def _coerce_title(value: str) -> str:
        normalized = (value or "").strip().replace("\r", " ").replace("\n", " ")
        return normalized or "知识文章"

    def _normalize_title(self, value: Any, fallback: str, topic: str) -> str:
        if isinstance(value, str) and value.strip():
            return self._coerce_title(value)

        normalized = self._coerce_title(fallback)
        logger.info("title fallback topic=%s fallback=%s raw=%r", topic, normalized, value)
        return normalized

    def _build_concepts(self, topic: str) -> List[str]:
        concept = self._slug(topic)
        return [concept] if concept else []

    def _resolve_output_path(self, slug: str, used_names: Dict[str, int]) -> Tuple[Path, str]:
        """确保同一 slug 不冲突，按顺序追加 -1/-2 编号。"""
        if not slug:
            slug = "post"

        with self._cache_lock:
            index = used_names.get(slug, 0)
            used_names[slug] = index + 1

        effective_slug = slug if index == 0 else f"{slug}-{index}"
        if index > 0:
            return Path(self.config.posts_dir) / f"{effective_slug}.md", effective_slug
        return Path(self.config.posts_dir) / f"{effective_slug}.md", effective_slug

    def _build_frontmatter(
        self,
        title: str,
        slug: str,
        date_text: str,
        concepts: List[str],
    ) -> str:
        normalized_title = self._coerce_title(title)
        concept_block = "concepts: []\n" if not concepts else "concepts:\n" + "\n".join(f"  - {item}" for item in concepts) + "\n"
        return (
            "---\n"
            f"title: {json.dumps(normalized_title, ensure_ascii=False)}\n"
            f"date: {date_text}\n"
            f"slug: {json.dumps(slug, ensure_ascii=False)}\n"
            "type: post\n"
            "draft: false\n"
            f"{concept_block}"
            "---\n"
        )

    def _parse_json(self, text: str) -> Dict[str, Any]:
        parsed = self.parser.safe_json(text)
        return parsed if isinstance(parsed, dict) else {}

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

    def _cache_key_to_path(self, key: str) -> Optional[Path]:
        if not isinstance(key, str):
            return None

        candidate = Path(key)
        if candidate.is_absolute():
            return candidate if candidate.exists() else None

        candidates = [
            Path(self.config.knowledge_base_dir) / candidate,
            Path(self.config.posts_dir) / candidate,
        ]

        for p in candidates:
            if p.exists():
                return p

        return None

    def _collect_cache_paths(self, cache: Dict[str, Any]) -> Set[str]:
        if not isinstance(cache, dict):
            return set()

        paths: Set[str] = set()
        for key in cache.keys():
            path = self._cache_key_to_path(key)
            if path is not None and path.is_file():
                paths.add(str(path))
        return paths

    def _prune_cache_entries(self, cache: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
        if not isinstance(cache, dict):
            return {}

        pruned: Dict[str, Any] = {}
        removed: List[str] = []

        for key, value in cache.items():
            if not isinstance(key, str):
                continue

            candidate = Path(key)
            if candidate.is_absolute():
                resolved = candidate
            else:
                resolved = base_path.parent / key
                if not resolved.exists():
                    resolved = self.config.knowledge_base_dir / key

            if resolved.exists():
                pruned[str(resolved)] = value
            else:
                removed.append(key)

        if removed:
            logger.info("清理无效的编译缓存项: %s", removed)

        return pruned

    def _validate_cache_consistency(self, compile_cache: Dict[str, str], concepts_cache: Dict[str, str]) -> None:
        posts_dir = Path(self.config.posts_dir)
        concepts_dir = Path(self.config.concepts_dir)

        on_disk_posts = {
            str(p)
            for p in posts_dir.glob("*.md")
            if p.name != "_index.md" and not p.name.startswith(".")
        }
        on_disk_concepts = {
            str(p)
            for p in concepts_dir.glob("*.md")
            if p.name != "_index.md" and not p.name.startswith(".")
        }

        cached_posts = self._collect_cache_paths(compile_cache)
        cached_concepts = self._collect_cache_paths(concepts_cache)

        missing_posts = sorted(on_disk_posts - cached_posts)
        missing_concepts = sorted(on_disk_concepts - cached_concepts)

        if missing_posts:
            logger.warning("compile_cache 缺失文章条目: %d 个，示例: %s", len(missing_posts), missing_posts[:20])
        if missing_concepts:
            logger.warning("concept_cache 缺失概念页条目: %d 个，示例: %s", len(missing_concepts), missing_concepts[:20])

        logger.info(
            "cache 对齐检查: compile=%d/%d, concept=%d/%d",
            len(cached_posts),
            len(on_disk_posts),
            len(cached_concepts),
            len(on_disk_concepts),
        )

    def _materialize_structured_indexes(self, index: Dict[str, Any]) -> None:
        entities_dir = Path(self.config.entities_dir)
        comparisons_dir = Path(self.config.comparisons_dir)
        entities_dir.mkdir(parents=True, exist_ok=True)
        comparisons_dir.mkdir(parents=True, exist_ok=True)

        entities = index.get("entities", {}) if isinstance(index, dict) else {}
        comparisons = index.get("comparisons", {}) if isinstance(index, dict) else {}

        expected_entity_paths: Set[str] = set()
        if isinstance(entities, dict):
            for key, data in sorted(entities.items()):
                if not isinstance(key, str) or not isinstance(data, dict):
                    continue
                out_path = entities_dir / f"{self._slug(key)}.json"
                payload = {
                    "key": key,
                    "name": str(data.get("name") or "").strip(),
                    "type": str(data.get("type") or "unknown").strip() or "unknown",
                    "summary": str(data.get("summary") or "").strip(),
                    "aliases": list(data.get("aliases", [])) if isinstance(data.get("aliases"), list) else [],
                    "topics": list(data.get("topics", [])) if isinstance(data.get("topics"), list) else [],
                    "files": list(data.get("files", [])) if isinstance(data.get("files"), list) else [],
                }
                self.file_store.save_json(out_path, payload)
                expected_entity_paths.add(str(out_path))

        expected_comparison_paths: Set[str] = set()
        if isinstance(comparisons, dict):
            for key, data in sorted(comparisons.items()):
                if not isinstance(key, str) or not isinstance(data, dict):
                    continue
                out_path = comparisons_dir / self._comparison_output_name(key)
                payload = {
                    "key": key,
                    "left": str(data.get("left") or "").strip(),
                    "right": str(data.get("right") or "").strip(),
                    "aspect": str(data.get("aspect") or "general").strip() or "general",
                    "relation": str(data.get("relation") or "compare").strip().lower() or "compare",
                    "summary": str(data.get("summary") or "").strip(),
                    "topics": list(data.get("topics", [])) if isinstance(data.get("topics"), list) else [],
                    "files": list(data.get("files", [])) if isinstance(data.get("files"), list) else [],
                }
                self.file_store.save_json(out_path, payload)
                expected_comparison_paths.add(str(out_path))

        existing_entity_paths = {str(path) for path in entities_dir.glob("*.json") if path.is_file()}
        stale_entities = sorted(existing_entity_paths - expected_entity_paths)
        for stale_path in stale_entities:
            try:
                Path(stale_path).unlink()
                logger.info("removed stale entity materialization: %s", stale_path)
            except OSError:
                logger.exception("删除过期 entity 文件失败: %s", stale_path)

        existing_comparison_paths = {str(path) for path in comparisons_dir.glob("*.json") if path.is_file()}
        stale_comparisons = sorted(existing_comparison_paths - expected_comparison_paths)
        for stale_path in stale_comparisons:
            try:
                Path(stale_path).unlink()
                logger.info("removed stale comparison materialization: %s", stale_path)
            except OSError:
                logger.exception("删除过期 comparison 文件失败: %s", stale_path)

    def _build_private_knowledge_graph(self) -> None:
        index = self.index_repository.load_index()
        topics = index.get("topics", {}) if isinstance(index, dict) else {}
        self._materialize_structured_indexes(index)

        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, str]] = []
        seen_nodes: Set[str] = set()
        seen_edges: Set[str] = set()
        concept_nodes: Dict[str, str] = {}
        source_nodes_by_slug: Dict[str, str] = {}
        source_nodes_by_path: Dict[str, str] = {}
        topic_source_slugs: Dict[str, List[str]] = {}
        entity_nodes: Dict[str, str] = {}
        comparison_nodes: Dict[str, str] = {}

        def add_node(
            node_id: str,
            label: str,
            group: str,
            size_hint: int,
            url: str = "",
            summary: str = "",
            group_label: str = "",
        ) -> None:
            if not node_id or node_id in seen_nodes:
                return
            seen_nodes.add(node_id)
            node = {
                "id": node_id,
                "name": label,
                "size": size_hint,
                "group": group,
                "url": url,
                "summary": summary,
            }
            if group_label:
                node["group_label"] = group_label
            nodes.append(node)

        def add_edge(src: str, dst: str, relation: str = "related") -> None:
            if not src or not dst or src == dst:
                return
            edge_key = f"{src}->{dst}:{relation}"
            if edge_key in seen_edges:
                return
            seen_edges.add(edge_key)
            edges.append({"source": src, "target": dst, "relation": relation})

        def ensure_concept_node(topic_name: str, size_hint: int = 18) -> Optional[str]:
            if not isinstance(topic_name, str) or not topic_name.strip():
                return None
            topic_slug = self._slug(topic_name)
            node_id = concept_nodes.get(topic_slug)
            if node_id:
                return node_id
            node_id = f"concept:{topic_slug}"
            concept_nodes[topic_slug] = node_id
            add_node(node_id, topic_name, "concept", size_hint, f"/concepts/{topic_slug}/")
            return node_id

        def ensure_entity_node(entity_key: str, entity_name: str, entity_type: str = "unknown", size_hint: int = 10) -> Optional[str]:
            normalized_key = self._slug(entity_key or entity_name)
            label = str(entity_name or normalized_key).strip()
            if not normalized_key or not label:
                return None
            node_id = entity_nodes.get(normalized_key)
            if node_id:
                return node_id
            node_id = f"entity:{normalized_key}"
            entity_nodes[normalized_key] = node_id
            rendered_label = f"{label}" if entity_type in {"", "unknown"} else f"{label}"
            add_node(node_id, rendered_label, "entity", size_hint)
            return node_id

        def ensure_comparison_node(comparison_key: str, left: str, right: str, aspect: str, size_hint: int = 9) -> Optional[str]:
            normalized_key = self._slug(comparison_key)
            if not normalized_key:
                return None
            node_id = comparison_nodes.get(normalized_key)
            if node_id:
                return node_id
            label_core = f"{left} vs {right}".strip()
            label = f"{label_core} ({aspect})" if aspect and aspect != "general" else label_core
            node_id = f"comparison:{normalized_key}"
            comparison_nodes[normalized_key] = node_id
            add_node(node_id, label or normalized_key, "comparison", size_hint)
            return node_id

        if isinstance(topics, dict):
            for topic, data in topics.items():
                if not isinstance(topic, str) or not isinstance(data, dict):
                    continue

                files = [
                    f
                    for f in data.get("files", [])
                    if isinstance(f, str) and not self._is_research_draft_rel_path(f)
                ]
                topic_slug = self._slug(topic)
                topic_source_slugs.setdefault(topic_slug, [])
                topic_node_id = ensure_concept_node(topic, 18 + min(len(files), 12))

                for raw_file in files:
                    source_meta = self._load_source_meta(raw_file)
                    source_label = str(source_meta.get("title") or Path(raw_file).parent.name or Path(raw_file).stem).strip()
                    source_summary = str(source_meta.get("summary") or "").strip()
                    source_node_id = f"source:{raw_file}"
                    source_slug = self._source_slug(raw_file)
                    if source_slug not in topic_source_slugs[topic_slug]:
                        topic_source_slugs[topic_slug].append(source_slug)
                    add_node(
                        source_node_id,
                        source_label,
                        "source",
                        12,
                        "",
                        source_summary,
                    )
                    source_nodes_by_slug[source_slug] = source_node_id
                    source_nodes_by_path[raw_file] = source_node_id
                    if topic_node_id:
                        add_edge(source_node_id, topic_node_id, "source")

                children = data.get("children", {})
                if isinstance(children, dict):
                    for child_topic, child_data in children.items():
                        if not isinstance(child_topic, str) or not isinstance(child_data, dict):
                            continue
                        child_files = [
                            f
                            for f in child_data.get("files", [])
                            if isinstance(f, str) and not self._is_research_draft_rel_path(f)
                        ]
                        child_slug = self._slug(child_topic)
                        topic_source_slugs.setdefault(child_slug, [])
                        for raw_file in child_files:
                            source_slug = self._source_slug(raw_file)
                            if source_slug not in topic_source_slugs[child_slug]:
                                topic_source_slugs[child_slug].append(source_slug)
                        child_node_id = ensure_concept_node(child_topic, 16 + min(len(child_files), 10))
                        if topic_node_id and child_node_id:
                            add_edge(topic_node_id, child_node_id, "contains")

        entities = index.get("entities", {}) if isinstance(index, dict) else {}
        if isinstance(entities, dict):
            for entity_key, entity_data in entities.items():
                if not isinstance(entity_key, str) or not isinstance(entity_data, dict):
                    continue
                entity_name = str(entity_data.get("name") or entity_key).strip() or entity_key
                entity_type = str(entity_data.get("type") or "unknown").strip().lower() or "unknown"
                topics_for_entity = entity_data.get("topics", [])
                files_for_entity = entity_data.get("files", [])
                size_hint = 10 + min(
                    (len(topics_for_entity) if isinstance(topics_for_entity, list) else 0)
                    + (len(files_for_entity) if isinstance(files_for_entity, list) else 0),
                    6,
                )
                entity_node_id = ensure_entity_node(entity_key, entity_name, entity_type, size_hint)
                if not entity_node_id:
                    continue

                if isinstance(topics_for_entity, list):
                    for topic_name in topics_for_entity:
                        if not isinstance(topic_name, str):
                            continue
                        topic_node_id = ensure_concept_node(topic_name, 18)
                        if topic_node_id:
                            add_edge(topic_node_id, entity_node_id, "entity")

                if isinstance(files_for_entity, list):
                    for raw_file in files_for_entity:
                        if not isinstance(raw_file, str) or self._is_research_draft_rel_path(raw_file):
                            continue
                        source_node_id = source_nodes_by_path.get(raw_file)
                        if source_node_id:
                            add_edge(source_node_id, entity_node_id, "entity")

        comparisons = index.get("comparisons", {}) if isinstance(index, dict) else {}
        if isinstance(comparisons, dict):
            for comparison_key, comparison_data in comparisons.items():
                if not isinstance(comparison_key, str) or not isinstance(comparison_data, dict):
                    continue
                left = str(comparison_data.get("left") or "").strip()
                right = str(comparison_data.get("right") or "").strip()
                if not left or not right:
                    continue
                aspect = str(comparison_data.get("aspect") or "general").strip() or "general"
                relation = str(comparison_data.get("relation") or "compare").strip().lower() or "compare"
                topics_for_comparison = comparison_data.get("topics", [])
                files_for_comparison = comparison_data.get("files", [])

                comparison_node_id = ensure_comparison_node(comparison_key, left, right, aspect, 9)
                left_entity_id = ensure_entity_node(left, left, "unknown", 10)
                right_entity_id = ensure_entity_node(right, right, "unknown", 10)

                if comparison_node_id and left_entity_id:
                    add_edge(comparison_node_id, left_entity_id, "left")
                if comparison_node_id and right_entity_id:
                    add_edge(comparison_node_id, right_entity_id, "right")
                if left_entity_id and right_entity_id:
                    add_edge(left_entity_id, right_entity_id, relation)

                if isinstance(topics_for_comparison, list):
                    for topic_name in topics_for_comparison:
                        if not isinstance(topic_name, str):
                            continue
                        topic_node_id = ensure_concept_node(topic_name, 18)
                        if topic_node_id and comparison_node_id:
                            add_edge(topic_node_id, comparison_node_id, "comparison")

                if isinstance(files_for_comparison, list):
                    for raw_file in files_for_comparison:
                        if not isinstance(raw_file, str) or self._is_research_draft_rel_path(raw_file):
                            continue
                        source_node_id = source_nodes_by_path.get(raw_file)
                        if source_node_id and comparison_node_id:
                            add_edge(source_node_id, comparison_node_id, "comparison")

        for rule in index.get("merge", []) if isinstance(index, dict) else []:
            if not isinstance(rule, dict):
                continue
            target = ensure_concept_node(str(rule.get("target", "")), 18)
            for source in rule.get("sources", []):
                source_node = ensure_concept_node(str(source), 15)
                if source_node and target:
                    add_edge(source_node, target, "merge")

        for rule in index.get("rename", []) if isinstance(index, dict) else []:
            if not isinstance(rule, dict):
                continue
            src = ensure_concept_node(str(rule.get("from", "")), 15)
            dst = ensure_concept_node(str(rule.get("to", "")), 18)
            if src and dst:
                add_edge(src, dst, "rename")

        for rule in index.get("split", []) if isinstance(index, dict) else []:
            if not isinstance(rule, dict):
                continue
            src = ensure_concept_node(str(rule.get("source", "")), 18)
            for target in rule.get("targets", []):
                dst = ensure_concept_node(str(target), 15)
                if src and dst:
                    add_edge(src, dst, "split")

        posts_dir = Path(self.config.posts_dir)
        for post_path in sorted(posts_dir.glob("*.md")):
            if post_path.name.startswith("."):
                continue

            try:
                post_text = self.file_store.read_text(post_path)
                meta = self._parse_frontmatter(post_text)
            except Exception as exc:
                logger.error("读取文章 frontmatter 失败: %s, %s", post_path, exc)
                continue

            title = str(meta.get("title") or post_path.stem)
            slug = self._slug(str(meta.get("slug") or post_path.stem))
            concepts = meta.get("concepts", [])
            if not isinstance(concepts, list):
                concepts = []

            matched_concepts: List[str] = []
            matched_concept_slugs: List[str] = []
            for concept in concepts:
                if not isinstance(concept, str):
                    continue
                concept_slug = self._slug(concept)
                topic_node_id = concept_nodes.get(concept_slug)
                if topic_node_id and topic_node_id not in matched_concepts:
                    matched_concepts.append(topic_node_id)
                    matched_concept_slugs.append(concept_slug)

            if not matched_concepts:
                fallback_node = concept_nodes.get(slug)
                if fallback_node:
                    matched_concepts.append(fallback_node)
                    matched_concept_slugs.append(slug)

            if not matched_concepts:
                continue

            post_node_id = f"post:{slug}"
            add_node(post_node_id, title, "post", 14 + min(len(matched_concepts), 6), f"/posts/{slug}/")
            for concept_node_id in matched_concepts:
                add_edge(post_node_id, concept_node_id, "explains")
            mentioned_concepts = self._extract_mentioned_concepts(post_text)
            for mentioned_slug in mentioned_concepts:
                concept_node_id = concept_nodes.get(self._slug(mentioned_slug))
                if concept_node_id and concept_node_id not in matched_concepts:
                    add_edge(post_node_id, concept_node_id, "mentions")
            for concept_slug in matched_concept_slugs:
                for source_slug in topic_source_slugs.get(concept_slug, []):
                    source_node_id = source_nodes_by_slug.get(source_slug)
                    if source_node_id:
                        add_edge(post_node_id, source_node_id, "compiled-from")

        graph_path = Path(self.config.private_knowledge_graph_path)
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_store.save_json(
            graph_path,
            {
                "nodes": nodes,
                "edges": edges,
            },
        )

    def _build_public_knowledge_graph(self) -> None:
        index = self.index_repository.load_index()
        topic_pairs = self._iter_topics(index)

        topic_lookup: Dict[str, Dict[str, Any]] = {}
        for topic, data in topic_pairs:
            if not isinstance(topic, str) or not isinstance(data, dict):
                continue
            slug = self._slug(topic)
            topic_lookup[slug] = {
                "title": topic,
                "summary": str(data.get("summary") or "").strip(),
                "related_topics": data.get("related_topics", []),
            }

        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, str]] = []
        seen_nodes: Set[str] = set()
        seen_edges: Set[str] = set()
        topic_post_counts: Dict[str, int] = {}
        topic_node_ids: Dict[str, str] = {}
        referenced_topics: Set[str] = set()

        def add_node(
            node_id: str,
            label: str,
            group: str,
            size_hint: int,
            url: str = "",
            summary: str = "",
            group_label: str = "",
        ) -> None:
            if not node_id or node_id in seen_nodes:
                return
            seen_nodes.add(node_id)
            node = {
                "id": node_id,
                "name": label,
                "size": size_hint,
                "group": group,
                "url": url,
                "summary": summary,
            }
            if group_label:
                node["group_label"] = group_label
            nodes.append(node)

        def add_edge(src: str, dst: str, relation: str) -> None:
            if not src or not dst or src == dst:
                return
            edge_key = f"{src}->{dst}:{relation}"
            if edge_key in seen_edges:
                return
            seen_edges.add(edge_key)
            edges.append({"source": src, "target": dst, "relation": relation})

        def ensure_topic_node(topic_slug: str) -> Optional[str]:
            if not topic_slug:
                return None
            node_id = topic_node_ids.get(topic_slug)
            if node_id:
                return node_id
            topic_info = topic_lookup.get(topic_slug, {})
            node_id = f"concept:{topic_slug}"
            topic_node_ids[topic_slug] = node_id
            add_node(
                node_id,
                str(topic_info.get("title") or topic_slug),
                "concept",
                20,
                "",
                str(topic_info.get("summary") or "").strip(),
                "Topic",
            )
            return node_id

        posts_dir = Path(self.config.posts_dir)
        for post_path in sorted(posts_dir.glob("*.md")):
            if post_path.name.startswith("."):
                continue

            try:
                post_text = self.file_store.read_text(post_path)
                meta = self._parse_frontmatter(post_text)
            except Exception as exc:
                logger.error("读取 public graph 文章失败: %s, %s", post_path, exc)
                continue

            title = str(meta.get("title") or post_path.stem).strip() or post_path.stem
            slug = self._slug(str(meta.get("slug") or post_path.stem))
            concepts = meta.get("concepts", [])
            if not isinstance(concepts, list):
                concepts = []

            concept_slugs = [self._slug(item) for item in concepts if isinstance(item, str) and str(item).strip()]
            concept_slugs = [item for item in concept_slugs if item]
            if not concept_slugs:
                continue

            post_node_id = f"post:{slug}"
            add_node(post_node_id, title, "post", 14 + min(len(concept_slugs), 6), f"/posts/{slug}/")

            for concept_slug in concept_slugs:
                referenced_topics.add(concept_slug)
                topic_post_counts[concept_slug] = topic_post_counts.get(concept_slug, 0) + 1
                topic_node_id = ensure_topic_node(concept_slug)
                if topic_node_id:
                    add_edge(post_node_id, topic_node_id, "explains")

        for topic_slug in sorted(referenced_topics):
            topic_info = topic_lookup.get(topic_slug, {})
            topic_node_id = ensure_topic_node(topic_slug)
            if not topic_node_id:
                continue

            for node in nodes:
                if node.get("id") == topic_node_id:
                    node["size"] = 18 + min(topic_post_counts.get(topic_slug, 0), 8)
                    break

            related_topics = topic_info.get("related_topics", [])
            for item in related_topics if isinstance(related_topics, list) else []:
                if not isinstance(item, dict):
                    continue
                related_slug = self._slug(str(item.get("topic") or "").strip())
                if not related_slug or related_slug not in referenced_topics:
                    continue
                related_node_id = ensure_topic_node(related_slug)
                if related_node_id:
                    relation = str(item.get("relation") or "related").strip() or "related"
                    add_edge(topic_node_id, related_node_id, relation)

        graph_path = Path(self.config.public_knowledge_graph_path)
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_store.save_json(
            graph_path,
            {
                "nodes": nodes,
                "edges": edges,
            },
        )

    def build_private_graph(self) -> None:
        self._build_private_knowledge_graph()
        logger.info("private knowledge graph 已更新: %s", self.config.private_knowledge_graph_path)

    def build_public_graph(self) -> None:
        self._build_public_knowledge_graph()
        logger.info("public knowledge graph 已更新: %s", self.config.public_knowledge_graph_path)

    def build_graph(self) -> None:
        self._build_private_knowledge_graph()
        self._build_public_knowledge_graph()
        logger.info(
            "knowledge graph 已更新: public=%s private=%s",
            self.config.public_knowledge_graph_path,
            self.config.private_knowledge_graph_path,
        )

    def _compile_topic(
        self,
        topic: str,
        data: Dict[str, Any],
        cache: Dict[str, str],
        used_names: Dict[str, int],
    ) -> Optional[Tuple[str, str]]:
        contents = self._collect_contents(data)
        if not contents:
            logger.warning("topic 无可编译文件: %s", topic)
            return None
        source_files = [item for item in data.get("files", []) if isinstance(item, str)]
        related_sources = self._collect_related_sources(source_files)
        concepts = self._build_concepts(topic)
        source_slugs = [item["slug"] for item in related_sources if isinstance(item, dict) and str(item.get("slug") or "").strip()]

        index = self.index_repository.load_index()
        topics_index = index.get("topics", {}) if isinstance(index, dict) else {}
        content = "\n\n---\n\n".join(contents)

        merged = self.llm_client.run_chain(
            self.prompt_registry.fusion_prompt,
            {
                "index": json.dumps(data, ensure_ascii=False),
                "content": content,
                "topics": list(topics_index.keys()),
                "topic_map": {
                    t: topics_index[t].get("summary", "")
                    for t in topics_index
                    if isinstance(topics_index.get(t), dict)
                },
            },
        )

        title_data = self._parse_json(
            self.llm_client.run_chain(
                self.prompt_registry.title_prompt,
                {
                    "topic": topic,
                    "summary": str(data.get("summary", "")),
                },
            )
        )

        title = self._normalize_title(title_data.get("title"), topic, topic)
        slug = self._slug(self._normalize_title(title_data.get("slug"), topic, topic))

        logger.info(
            "frontmatter 解析结果: topic=%s title=%s slug=%s concepts=%s sources=%s",
            topic,
            title,
            slug,
            ", ".join(concepts),
            ", ".join(source_slugs),
        )

        body = merged
        if body.strip().startswith("---"):
            body = self._strip_frontmatter(body)
        body = body.strip()

        out_path, effective_slug = self._resolve_output_path(slug, used_names)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        old_path = Path(self.config.posts_dir) / f"{self._slug(topic)}.md"
        if old_path != out_path and old_path.exists():
            try:
                old_path.unlink()
            except Exception:
                logger.debug("清理旧文件失败: %s", old_path, exc_info=True)

        frontmatter = self._build_frontmatter(
            title=title,
            slug=effective_slug,
            date_text=date.today().isoformat(),
            concepts=concepts,
        )

        new_hash = self._calc_hash(
            topic,
            contents,
            title,
            effective_slug,
            concepts,
            source_slugs,
            related_sources,
            body.strip(),
        )

        cache_key = str(out_path)
        cache_value = cache.get(cache_key)
        if cache_value is None:
            cache_value = cache.get(topic)

        if cache_value == new_hash and out_path.exists():
            logger.info("skip compile (cache hit): %s", topic)
            return cache_key, new_hash

        out_path.write_text(f"{frontmatter}\n{body.strip()}\n", encoding="utf-8")
        logger.info("compiled: %s -> %s", topic, out_path)
        return cache_key, new_hash

    def compile(self) -> None:
        logger.info("compiling (incremental + parallel)...")
        index = self.index_repository.load_index()
        posts_dir = Path(self.config.posts_dir)
        posts_dir.mkdir(parents=True, exist_ok=True)

        cache = self.file_store.load_json(self.config.cache_path)
        if not isinstance(cache, dict):
            cache = {}

        futures = []
        new_cache: Dict[str, str] = {}
        expected_paths: Set[str] = set()
        used_names: Dict[str, int] = {}
        failed_topics = 0

        workers = max(1, int(getattr(self.config, "compiler_workers", 4)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for topic, data in self._iter_topics(index):
                futures.append(executor.submit(self._compile_topic, topic, data, cache, used_names))

            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception:
                    logger.exception("compile topic 失败")
                    failed_topics += 1
                    continue

                if result:
                    cache_key, new_hash = result
                    with self._cache_lock:
                        new_cache[cache_key] = new_hash
                        expected_paths.add(cache_key)

        if failed_topics == 0:
            existing_paths = {
                str(path)
                for path in posts_dir.glob("*.md")
                if path.name != "_index.md" and not path.name.startswith(".")
            }
            stale_paths = sorted(existing_paths - expected_paths)
            for stale_path in stale_paths:
                try:
                    Path(stale_path).unlink()
                    logger.info("removed stale post: %s", stale_path)
                except OSError:
                    logger.exception("删除过期 post 失败: %s", stale_path)
        else:
            logger.warning("本次 compile 存在失败 topic，跳过 stale post 清理: failed=%d", failed_topics)

        concepts_cache = self.file_store.load_json(self.config.concepts_cache_path)
        if not isinstance(concepts_cache, dict):
            concepts_cache = {}

        new_cache = self._prune_cache_entries(new_cache, posts_dir)
        self._validate_cache_consistency(new_cache, concepts_cache)
        self.file_store.save_json(self.config.cache_path, new_cache)

        try:
            self.build_graph()
        except Exception:
            logger.exception("knowledge graph 生成失败")
