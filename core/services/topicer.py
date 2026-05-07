import hashlib
import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from core.config import AppConfig
from core.infra.file_store import FileStore
from core.infra.index_repository import IndexRepository
from core.infra.llm_client import LLMClient
from core.infra.prompt_registry import PromptRegistry

logger = logging.getLogger(__name__)


# 概念页服务：基于 topic 自身内容和跨 topic 关联，生成可发布的 hub 页面
class TopicPageService:
    _TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
    _MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    _PUBLIC_CONCEPT_LINK_RE = re.compile(r"/concepts/([^/?#)\s]+)/?")
    _PUBLIC_SOURCE_LINK_RE = re.compile(r"/sources/([^/?#)\s]+)/?")

    def __init__(
        self,
        config: AppConfig,
        index_repository: IndexRepository,
        llm_client: LLMClient,
        prompt_registry: PromptRegistry,
        file_store: FileStore,
    ):
        self.config = config
        self.index_repository = index_repository
        self.llm_client = llm_client
        self.prompt_registry = prompt_registry
        self.file_store = file_store

    @staticmethod
    def _is_under(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _is_research_draft_path(self, rel_path: str) -> bool:
        raw_path = (Path(self.config.raw_dir) / rel_path).resolve()
        return self._is_under(raw_path, Path(self.config.research_drafts_dir).resolve())

    def _link_keys(self, *values: Any) -> List[str]:
        keys: List[str] = []
        for value in values:
            text = str(value or "").strip().strip("/")
            if not text:
                continue

            raw_path = Path(text)
            for candidate in (text, raw_path.name, raw_path.stem, raw_path.parent.name):
                normalized = str(candidate or "").strip().strip("/")
                if not normalized:
                    continue
                slug = self.config.slugify(normalized, fallback="")
                for key in (normalized, slug):
                    if key and key not in keys:
                        keys.append(key)
        return keys

    def _topic_link_lookup(
        self,
        current_topic: str,
        current_slug: str,
        related_topics: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        lookup = {key: "" for key in self._link_keys(current_topic, current_slug)}
        for item in related_topics:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic") or "").strip()
            slug = str(item.get("slug") or self._slug(topic)).strip()
            if not topic or not slug:
                continue
            target = f"./{slug}.md"
            for key in self._link_keys(topic, slug):
                lookup.setdefault(key, target)
        return lookup

    def _source_link_lookup(self, related_sources: List[Dict[str, Any]]) -> Dict[str, str]:
        lookup: Dict[str, str] = {}
        for item in related_sources:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            slug = str(item.get("slug") or "").strip()
            raw_path = str(item.get("raw_path") or "").strip()
            if not title or not slug:
                continue
            target = f"../sources/{slug}/"
            for key in self._link_keys(title, slug, raw_path):
                lookup.setdefault(key, target)
        return lookup

    def _rewrite_private_links(
        self,
        text: str,
        *,
        current_topic: str,
        current_slug: str,
        related_topics: List[Dict[str, Any]],
        related_sources: List[Dict[str, Any]],
    ) -> str:
        body = text or ""
        body = self._PUBLIC_CONCEPT_LINK_RE.sub(lambda match: f"./{match.group(1)}.md", body)
        body = self._PUBLIC_SOURCE_LINK_RE.sub(lambda match: f"../sources/{match.group(1)}/", body)

        topic_lookup = self._topic_link_lookup(current_topic, current_slug, related_topics)
        source_lookup = self._source_link_lookup(related_sources)

        def replace_link(match: re.Match[str]) -> str:
            label = match.group(1)
            original_target = match.group(2).strip()
            target = original_target.strip("<>").strip()
            if not target:
                return match.group(0)

            if target.startswith("/concepts/"):
                slug = target.split("/concepts/", 1)[1].strip("/")
                resolved = topic_lookup.get(slug, f"./{slug}.md")
                return label if not resolved else f"[{label}]({resolved})"

            if target.startswith("/sources/"):
                slug = target.split("/sources/", 1)[1].strip("/")
                return f"[{label}](../sources/{slug}/)"

            if not (
                target.startswith("./")
                or target.startswith("../")
                or target.endswith(".md")
            ):
                return match.group(0)

            for key in self._link_keys(target, label):
                if key in topic_lookup:
                    resolved = topic_lookup[key]
                    return label if not resolved else f"[{label}]({resolved})"
                if key in source_lookup:
                    return f"[{label}]({source_lookup[key]})"
            return label

        return self._MARKDOWN_LINK_RE.sub(replace_link, body)

    def _ensure_source_navigation(self, text: str, related_sources: List[Dict[str, Any]]) -> str:
        if "../sources/" in (text or ""):
            return text

        lines = []
        for item in related_sources:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            slug = str(item.get("slug") or "").strip()
            if title and slug:
                lines.append(f"- [{title}](../sources/{slug}/)")

        if not lines:
            return text

        block = "\n".join(lines)
        body = (text or "").rstrip()
        if "## 实践入口 / 继续阅读" in body:
            return f"{body}\n{block}\n"
        return f"{body}\n\n## 实践入口 / 继续阅读\n\n{block}\n"

    def _slug(self, topic: str) -> str:
        return self.config.slugify(topic, fallback="topic")

    @staticmethod
    def _append_unique(items: List[str], value: str) -> None:
        if value and value not in items:
            items.append(value)

    def _normalize_list(self, value: Any) -> List[str]:
        items: List[str] = []
        for item in value if isinstance(value, list) else []:
            text = str(item or "").strip()
            if text and text not in items:
                items.append(text)
        return items

    def _normalize_related_topics(self, value: Any) -> List[Dict[str, Any]]:
        related: List[Dict[str, Any]] = []
        seen = set()

        for item in value if isinstance(value, list) else []:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic") or "").strip()
            if not topic or topic in seen:
                continue
            seen.add(topic)

            try:
                score = round(float(item.get("score", 0.0)), 3)
            except (TypeError, ValueError):
                score = 0.0

            normalized = {
                "topic": topic,
                "score": score,
                "relation": str(item.get("relation") or "related").strip() or "related",
                "shared_tags": self._normalize_list(item.get("shared_tags")),
                "shared_entities": self._normalize_list(item.get("shared_entities")),
                "shared_comparisons": self._normalize_list(item.get("shared_comparisons")),
                "reasons": self._normalize_list(item.get("reasons")),
            }

            summary = str(item.get("summary") or "").strip()
            if summary:
                normalized["summary"] = summary

            related.append(normalized)

        return related

    def _tokenize(self, value: Any) -> Set[str]:
        return {item for item in self._TOKEN_RE.findall(str(value or "").lower()) if item}

    @staticmethod
    def _jaccard(left: Set[str], right: Set[str]) -> float:
        if not left or not right:
            return 0.0
        union = left | right
        if not union:
            return 0.0
        return len(left & right) / len(union)

    @staticmethod
    def _sequence_ratio(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        from difflib import SequenceMatcher

        return SequenceMatcher(None, left.lower(), right.lower()).ratio()

    def _load_source_meta(self, rel_path: str) -> Dict[str, Any]:
        meta = self.file_store.load_json(self.config.meta_path_for(rel_path))
        return meta if isinstance(meta, dict) else {}

    def _collect_files(self, data: Dict[str, Any]) -> List[str]:
        files = []
        for item in data.get("files", []):
            if isinstance(item, str) and item and not self._is_research_draft_path(item):
                files.append(item)
        return sorted(set(files), key=lambda item: item)

    def _read_raw_excerpt(self, rel_path: str, limit: int = 1200) -> str:
        raw_path = Path(self.config.raw_dir) / rel_path

        try:
            if raw_path.is_file():
                text = self.file_store.read_text(raw_path)
            elif raw_path.is_dir():
                md_files = sorted(raw_path.glob("*.md"), key=lambda p: p.name)
                if not md_files:
                    return ""
                if len(md_files) == 1:
                    text = self.file_store.read_text(md_files[0])
                else:
                    parts = [self.file_store.read_text(f) for f in md_files]
                    text = "\n\n".join(parts)
            else:
                return ""
        except Exception:
            logger.debug("读取 raw excerpt 失败: %s", raw_path, exc_info=True)
            return ""

        compact = re.sub(r"\s+", " ", text).strip()
        return compact[:limit]

    def _comparison_label(self, data: Dict[str, Any]) -> str:
        left = str(data.get("left") or "").strip()
        right = str(data.get("right") or "").strip()
        aspect = str(data.get("aspect") or "").strip()
        if not left or not right:
            return ""
        core = f"{left} vs {right}"
        return f"{core} ({aspect})" if aspect else core

    def _build_source_entries(self, files: List[str]) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        seen = set()

        for rel_path in files:
            if not isinstance(rel_path, str) or not rel_path.strip():
                continue

            slug = self.config.source_slug_for(rel_path)
            if slug in seen:
                continue
            seen.add(slug)

            rel_dir = Path(rel_path).parent.as_posix()
            meta = self._load_source_meta(rel_path)
            title = str(meta.get("title") or Path(rel_dir).name).strip() or Path(rel_dir).name
            summary = str(meta.get("summary") or "").strip()
            tags = self._normalize_list(meta.get("tags"))
            entity_names: List[str] = []
            for entity in meta.get("entities", []) if isinstance(meta.get("entities"), list) else []:
                if isinstance(entity, dict):
                    self._append_unique(entity_names, str(entity.get("name") or "").strip())

            comparison_labels: List[str] = []
            for comparison in meta.get("comparisons", []) if isinstance(meta.get("comparisons"), list) else []:
                if isinstance(comparison, dict):
                    label = self._comparison_label(comparison)
                    if label:
                        self._append_unique(comparison_labels, label)

            entries.append(
                {
                    "title": title,
                    "slug": slug,
                    "raw_path": rel_path,
                    "summary": summary,
                    "tags": tags,
                    "entities": entity_names,
                    "comparisons": comparison_labels,
                    "excerpt": self._read_raw_excerpt(rel_path),
                }
            )

        return entries

    def _resolve_entity_names(self, data: Dict[str, Any], index_entities: Dict[str, Any], source_entries: List[Dict[str, Any]]) -> List[str]:
        names: List[str] = []

        for key in data.get("entities", []) if isinstance(data.get("entities"), list) else []:
            entity = index_entities.get(key, {}) if isinstance(index_entities, dict) else {}
            name = str(entity.get("name") or key or "").strip()
            if name:
                self._append_unique(names, name)

        for entry in source_entries:
            for name in entry.get("entities", []) if isinstance(entry.get("entities"), list) else []:
                self._append_unique(names, str(name).strip())

        return names

    def _resolve_comparison_labels(
        self,
        data: Dict[str, Any],
        index_comparisons: Dict[str, Any],
        source_entries: List[Dict[str, Any]],
    ) -> List[str]:
        labels: List[str] = []

        for key in data.get("comparisons", []) if isinstance(data.get("comparisons"), list) else []:
            comparison = index_comparisons.get(key, {}) if isinstance(index_comparisons, dict) else {}
            label = self._comparison_label(comparison)
            if label:
                self._append_unique(labels, label)

        for entry in source_entries:
            for label in entry.get("comparisons", []) if isinstance(entry.get("comparisons"), list) else []:
                self._append_unique(labels, str(label).strip())

        return labels

    def _resolve_summary(self, topic: str, data: Dict[str, Any], source_entries: List[Dict[str, Any]]) -> str:
        summary = str(data.get("summary") or "").strip()
        if summary:
            return summary

        candidates = [str(entry.get("summary") or "").strip() for entry in source_entries]
        candidates = [item for item in candidates if item]
        if candidates:
            return max(candidates, key=len)

        return f"{topic} 相关资料正在整理中。"

    def _build_topic_snapshot(self, topic: str, data: Dict[str, Any], index: Dict[str, Any]) -> Dict[str, Any]:
        files = self._collect_files(data)
        source_entries = self._build_source_entries(files)
        index_entities = index.get("entities", {}) if isinstance(index, dict) else {}
        index_comparisons = index.get("comparisons", {}) if isinstance(index, dict) else {}

        tags = self._normalize_list(data.get("tags"))
        for entry in source_entries:
            for tag in entry.get("tags", []) if isinstance(entry.get("tags"), list) else []:
                self._append_unique(tags, str(tag).strip())

        entity_names = self._resolve_entity_names(data, index_entities, source_entries)
        comparison_labels = self._resolve_comparison_labels(data, index_comparisons, source_entries)
        summary = self._resolve_summary(topic, data, source_entries)

        tokens = set(self._tokenize(topic))
        for item in tags + entity_names + comparison_labels:
            tokens.update(self._tokenize(item))

        related_sources = [
            {
                "title": str(entry.get("title") or "").strip(),
                "slug": str(entry.get("slug") or "").strip(),
                "raw_path": str(entry.get("raw_path") or "").strip(),
                "summary": str(entry.get("summary") or "").strip(),
            }
            for entry in source_entries
            if str(entry.get("title") or "").strip()
        ]

        return {
            "topic": topic,
            "slug": self._slug(topic),
            "summary": summary,
            "files": files,
            "tags": tags,
            "entity_names": entity_names,
            "comparison_labels": comparison_labels,
            "source_notes": source_entries,
            "related_sources": related_sources,
            "related_topics_hint": self._normalize_related_topics(data.get("related_topics")),
            "tokens": tokens,
        }

    def _score_snapshots(self, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        left_tags = set(left.get("tags", []))
        right_tags = set(right.get("tags", []))
        left_entities = set(left.get("entity_names", []))
        right_entities = set(right.get("entity_names", []))
        left_comparisons = set(left.get("comparison_labels", []))
        right_comparisons = set(right.get("comparison_labels", []))
        left_tokens = set(left.get("tokens", set()))
        right_tokens = set(right.get("tokens", set()))

        shared_tags = sorted(left_tags & right_tags)
        shared_entities = sorted(left_entities & right_entities)
        shared_comparisons = sorted(left_comparisons & right_comparisons)

        tag_score = self._jaccard(left_tags, right_tags)
        entity_score = self._jaccard(left_entities, right_entities)
        comparison_score = self._jaccard(left_comparisons, right_comparisons)
        token_score = self._jaccard(left_tokens, right_tokens)
        name_score = self._sequence_ratio(str(left.get("slug") or ""), str(right.get("slug") or ""))

        score = (
            tag_score * 0.34
            + entity_score * 0.34
            + comparison_score * 0.12
            + token_score * 0.12
            + name_score * 0.08
        )

        reasons: List[str] = []
        if shared_tags:
            reasons.append(f"shared-tags:{', '.join(shared_tags[:4])}")
        if shared_entities:
            reasons.append(f"shared-entities:{', '.join(shared_entities[:4])}")
        if shared_comparisons:
            reasons.append(f"shared-comparisons:{', '.join(shared_comparisons[:3])}")
        if name_score >= 0.85:
            reasons.append("similar-name")

        if score < 0.22 and not shared_tags and not shared_entities and not shared_comparisons:
            return {}

        relation = "merge-candidate" if score >= 0.7 and (shared_tags or shared_entities or name_score >= 0.9) else "related"
        return {
            "score": round(min(score, 1.0), 3),
            "relation": relation,
            "shared_tags": shared_tags[:6],
            "shared_entities": shared_entities[:6],
            "shared_comparisons": shared_comparisons[:4],
            "reasons": reasons[:6],
        }

    def _merge_related_item(
        self,
        preferred: Dict[str, Any],
        fallback: Dict[str, Any],
        other_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = dict(fallback)
        merged.update({key: value for key, value in preferred.items() if value not in (None, "", [], {})})
        merged["topic"] = str(preferred.get("topic") or fallback.get("topic") or other_snapshot.get("topic") or "").strip()
        merged["summary"] = str(preferred.get("summary") or fallback.get("summary") or other_snapshot.get("summary") or "").strip()
        merged["slug"] = str(other_snapshot.get("slug") or "").strip()
        merged["score"] = round(
            max(float(preferred.get("score", 0.0)), float(fallback.get("score", 0.0))),
            3,
        )

        relation = str(preferred.get("relation") or fallback.get("relation") or "related").strip() or "related"
        if preferred.get("relation") == "merge-candidate" or fallback.get("relation") == "merge-candidate":
            relation = "merge-candidate"
        merged["relation"] = relation

        for field in ("shared_tags", "shared_entities", "shared_comparisons", "reasons"):
            values: List[str] = []
            for item in self._normalize_list(fallback.get(field)) + self._normalize_list(preferred.get(field)):
                self._append_unique(values, item)
            merged[field] = values[:6]

        return merged

    def _build_related_topics_lookup(self, snapshots: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        pairs: Dict[str, Dict[str, Dict[str, Any]]] = {topic: {} for topic in snapshots}
        topic_names = sorted(snapshots.keys())

        for index_left, left_topic in enumerate(topic_names):
            left_snapshot = snapshots[left_topic]
            for right_topic in topic_names[index_left + 1 :]:
                right_snapshot = snapshots[right_topic]
                scored = self._score_snapshots(left_snapshot, right_snapshot)
                if not scored:
                    continue
                pairs[left_topic][right_topic] = self._merge_related_item(
                    {"topic": right_topic, **scored},
                    {},
                    right_snapshot,
                )
                pairs[right_topic][left_topic] = self._merge_related_item(
                    {"topic": left_topic, **scored},
                    {},
                    left_snapshot,
                )

        for topic, snapshot in snapshots.items():
            for hint in snapshot.get("related_topics_hint", []):
                other_topic = str(hint.get("topic") or "").strip()
                if not other_topic or other_topic == topic or other_topic not in snapshots:
                    continue
                other_snapshot = snapshots[other_topic]
                existing = pairs[topic].get(other_topic, {})
                pairs[topic][other_topic] = self._merge_related_item(hint, existing, other_snapshot)

                reverse_existing = pairs[other_topic].get(topic, {})
                reverse_hint = dict(hint)
                reverse_hint["topic"] = topic
                reverse_hint["summary"] = snapshot.get("summary", "")
                pairs[other_topic][topic] = self._merge_related_item(reverse_hint, reverse_existing, snapshot)

        return {
            topic: sorted(
                related.values(),
                key=lambda item: (-float(item.get("score", 0.0)), str(item.get("topic") or "")),
            )[:6]
            for topic, related in pairs.items()
        }

    def _render_content_digest(self, source_notes: List[Dict[str, Any]]) -> str:
        if not source_notes:
            return "暂无来源摘要。"

        blocks: List[str] = []
        for item in source_notes[:8]:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            summary = str(item.get("summary") or "").strip() or "暂无摘要"
            excerpt = str(item.get("excerpt") or "").strip()
            tags = ", ".join(self._normalize_list(item.get("tags"))) or "无"
            entities = ", ".join(self._normalize_list(item.get("entities"))) or "无"
            blocks.append(
                "\n".join(
                    [
                        f"- 标题：{title}",
                        f"  摘要：{summary}",
                        f"  tags：{tags}",
                        f"  entities：{entities}",
                        f"  片段：{excerpt[:420] if excerpt else '无'}",
                    ]
                )
            )

        return "\n\n".join(blocks) if blocks else "暂无来源摘要。"

    def _dump_frontmatter(self, payload: Dict[str, Any]) -> str:
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{rendered}\n---\n\n"

    def _compose_markdown(
        self,
        topic: str,
        snapshot: Dict[str, Any],
        related_topics: List[Dict[str, Any]],
        explain: str,
    ) -> str:
        frontmatter = self._dump_frontmatter(
            {
                "title": topic,
                "date": date.today().isoformat(),
                "type": "concept",
                "draft": False,
                "summary": str(snapshot.get("summary") or "").strip(),
                "topic_slug": str(snapshot.get("slug") or "").strip(),
                "source_count": len(snapshot.get("related_sources", [])),
                "entity_count": len(snapshot.get("entity_names", [])),
                "comparison_count": len(snapshot.get("comparison_labels", [])),
                "tags": snapshot.get("tags", []),
                "entities": snapshot.get("entity_names", []),
                "related_topics": related_topics,
                "related_sources": snapshot.get("related_sources", []),
            }
        )

        body = explain.strip() or "## 概述\n\n该主题暂无可用正文。"
        return frontmatter + body + "\n"

    def _calc_hash(
        self,
        topic: str,
        snapshot: Dict[str, Any],
        related_topics: List[Dict[str, Any]],
        explain: str,
    ) -> str:
        payload = {
            "schema": "concept-hub-v1",
            "topic": topic,
            "snapshot": snapshot,
            "related_topics": related_topics,
            "explain": explain,
        }
        return hashlib.md5(repr(payload).encode("utf-8")).hexdigest()

    def _resolve_output_path(self, topic: str, used_names: Dict[str, int]) -> Path:
        concepts_dir = Path(self.config.concepts_dir)
        base = self._slug(topic)
        index = used_names.setdefault(base, 0)
        used_names[base] = index + 1
        if index > 0:
            return concepts_dir / f"{base}-{index}.md"
        return concepts_dir / f"{base}.md"

    def _build_one(
        self,
        topic: str,
        snapshot: Dict[str, Any],
        related_lookup: Dict[str, List[Dict[str, Any]]],
        topic_map: Dict[str, str],
        cache: Dict[str, str],
        used_names: Dict[str, int],
    ) -> Optional[Tuple[str, str]]:
        related_topics = related_lookup.get(topic, [])

        res = self.llm_client.run_chain(
            self.prompt_registry.explain_prompt,
            {
                "topic": topic,
                "summary": str(snapshot.get("summary") or "").strip(),
                "tags": json.dumps(snapshot.get("tags", []), ensure_ascii=False),
                "entities": json.dumps(snapshot.get("entity_names", []), ensure_ascii=False),
                "comparisons": json.dumps(snapshot.get("comparison_labels", []), ensure_ascii=False),
                "topics": json.dumps(sorted(topic_map.keys()), ensure_ascii=False),
                "topic_map": json.dumps(topic_map, ensure_ascii=False),
                "related_topics": json.dumps(related_topics, ensure_ascii=False),
                "source_notes": json.dumps(snapshot.get("source_notes", [])[:8], ensure_ascii=False),
                "content_digest": self._render_content_digest(snapshot.get("source_notes", [])),
            },
        )
        res = self._rewrite_private_links(
            str(res or "").strip(),
            current_topic=topic,
            current_slug=str(snapshot.get("slug") or "").strip(),
            related_topics=related_topics,
            related_sources=snapshot.get("related_sources", []),
        )
        res = self._ensure_source_navigation(res, snapshot.get("related_sources", []))

        out_path = self._resolve_output_path(topic, used_names)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        content = self._compose_markdown(topic=topic, snapshot=snapshot, related_topics=related_topics, explain=res)
        new_hash = self._calc_hash(topic, snapshot, related_topics, res)

        cache_key = str(out_path)
        if cache.get(cache_key) == new_hash and out_path.exists():
            logger.info("skip topic page (cache hit): %s", topic)
            return cache_key, new_hash

        self.file_store.save_text(out_path, content)
        logger.info("generated topic page: %s", out_path)
        return cache_key, new_hash

    def build_topics(self) -> None:
        concepts_dir = Path(self.config.concepts_dir)
        concepts_dir.mkdir(parents=True, exist_ok=True)
        index = self.index_repository.load_index()
        raw_topics = index.get("topics", {})
        if not isinstance(raw_topics, dict):
            logger.warning("index.topics 非法，跳过 topic 生成")
            return

        cache = self.file_store.load_json(self.config.concepts_cache_path)
        if not isinstance(cache, dict):
            cache = {}

        pairs: List[Tuple[str, Dict[str, Any]]] = []
        seen_topics: Set[str] = set()

        def append_pair(topic: Any, data: Any) -> None:
            if not isinstance(topic, str) or not isinstance(data, dict) or topic in seen_topics:
                return
            seen_topics.add(topic)
            pairs.append((topic, data))

        for topic, data in raw_topics.items():
            append_pair(topic, data)

            children = data.get("children", {}) if isinstance(data, dict) else {}
            if isinstance(children, dict):
                for child_topic, child_data in children.items():
                    append_pair(child_topic, child_data)

        snapshots = {
            topic: self._build_topic_snapshot(topic, data, index)
            for topic, data in pairs
        }
        topic_map = {
            topic: str(snapshot.get("summary") or "").strip()
            for topic, snapshot in snapshots.items()
        }
        related_lookup = self._build_related_topics_lookup(snapshots)

        new_cache: Dict[str, str] = {}
        used_names: Dict[str, int] = {}
        expected_paths: Set[str] = set()

        for topic in [name for name, _ in pairs]:
            try:
                result = self._build_one(topic, snapshots[topic], related_lookup, topic_map, cache, used_names)
                if result:
                    cache_key, new_hash = result
                    new_cache[cache_key] = new_hash
                    expected_paths.add(cache_key)
            except Exception as exc:
                logger.exception("生成 topic 页面失败: %s, %s", topic, exc)

        existing_paths = {
            str(path)
            for path in concepts_dir.glob("*.md")
            if path.name != "_index.md" and not path.name.startswith(".")
        }
        stale_paths = sorted(existing_paths - expected_paths)
        for stale_path in stale_paths:
            try:
                Path(stale_path).unlink(missing_ok=True)
                logger.info("removed stale topic page: %s", stale_path)
            except OSError:
                logger.exception("删除过期 topic 页面失败: %s", stale_path)

        self.file_store.save_json(self.config.concepts_cache_path, new_cache)
