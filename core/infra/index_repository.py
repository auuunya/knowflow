import copy
import threading
from typing import Any, Dict, List


# 索引仓库：管理知识索引读写，并提供并发安全更新
class IndexRepository:
    def __init__(self, config, file_store):
        self.config = config
        self.file_store = file_store
        self._lock = threading.Lock()

    def _empty_index(self) -> Dict[str, Any]:
        return {
            "schema_version": 3,
            "topics": {},
            "entities": {},
            "comparisons": {},
            "canonical_topics": {},
            "merge": [],
            "merge_candidates": [],
            "split": [],
            "rename": [],
        }

    def _slug(self, value: str) -> str:
        return self.config.slugify(value, fallback="item")

    @staticmethod
    def _append_unique(items: List[str], value: str) -> None:
        if value and value not in items:
            items.append(value)

    def _normalize_str_list(self, value: Any) -> List[str]:
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

            normalized_item: Dict[str, Any] = {
                "topic": topic,
                "score": score,
                "relation": str(item.get("relation") or "related").strip() or "related",
                "shared_tags": self._normalize_str_list(item.get("shared_tags")),
                "shared_entities": self._normalize_str_list(item.get("shared_entities")),
                "shared_comparisons": self._normalize_str_list(item.get("shared_comparisons")),
                "reasons": self._normalize_str_list(item.get("reasons")),
            }

            summary = str(item.get("summary") or "").strip()
            if summary:
                normalized_item["summary"] = summary

            related.append(normalized_item)

        return related

    def _normalize_topic_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(data) if isinstance(data, dict) else {}
        if not isinstance(normalized.get("files"), list):
            normalized["files"] = []
        normalized["tags"] = self._normalize_str_list(normalized.get("tags"))
        normalized["aliases"] = self._normalize_str_list(normalized.get("aliases"))
        normalized["merged_topics"] = self._normalize_str_list(normalized.get("merged_topics"))
        if not isinstance(normalized.get("entities"), list):
            normalized["entities"] = []
        if not isinstance(normalized.get("comparisons"), list):
            normalized["comparisons"] = []
        normalized["related_topics"] = self._normalize_related_topics(normalized.get("related_topics"))
        if "summary" not in normalized:
            normalized["summary"] = ""
        canonical_topic = str(normalized.get("canonical_topic") or "").strip()
        if canonical_topic:
            normalized["canonical_topic"] = canonical_topic
        else:
            normalized.pop("canonical_topic", None)

        children = normalized.get("children")
        if isinstance(children, dict):
            for child_topic, child_data in list(children.items()):
                if not isinstance(child_topic, str) or not isinstance(child_data, dict):
                    continue
                children[child_topic] = self._normalize_topic_data(child_data)
        return normalized

    def _entity_key(self, name: str) -> str:
        return self._slug(name)

    def _comparison_key(self, left: str, right: str, aspect: str) -> str:
        pair = sorted([self._slug(left), self._slug(right)])
        return f"{pair[0]}--{pair[1]}--{self._slug(aspect or 'general')}"

    def _upsert_entity(self, index: Dict[str, Any], entity: Dict[str, Any], topic: str, rel_path: str) -> str:
        entities = index.setdefault("entities", {})
        name = str(entity.get("name") or "").strip()
        key = self._entity_key(name)
        entry = entities.setdefault(
            key,
            {
                "name": name,
                "type": str(entity.get("type") or "unknown").strip() or "unknown",
                "summary": str(entity.get("summary") or "").strip(),
                "aliases": [],
                "topics": [],
                "files": [],
            },
        )

        entity_type = str(entity.get("type") or "").strip().lower()
        if entity_type and entry.get("type") in {"", "unknown"}:
            entry["type"] = entity_type

        summary = str(entity.get("summary") or "").strip()
        if summary and len(summary) > len(str(entry.get("summary") or "")):
            entry["summary"] = summary

        aliases = entity.get("aliases", [])
        if isinstance(aliases, list):
            for alias in aliases:
                alias_text = str(alias or "").strip()
                if alias_text and alias_text != name:
                    self._append_unique(entry.setdefault("aliases", []), alias_text)

        self._append_unique(entry.setdefault("topics", []), topic)
        self._append_unique(entry.setdefault("files", []), rel_path)
        return key

    def _upsert_comparison(self, index: Dict[str, Any], comparison: Dict[str, Any], topic: str, rel_path: str) -> str:
        comparisons = index.setdefault("comparisons", {})
        left = str(comparison.get("left") or "").strip()
        right = str(comparison.get("right") or "").strip()
        aspect = str(comparison.get("aspect") or "general").strip() or "general"
        key = self._comparison_key(left, right, aspect)

        entry = comparisons.setdefault(
            key,
            {
                "left": left,
                "right": right,
                "aspect": aspect,
                "relation": str(comparison.get("relation") or "compare").strip().lower() or "compare",
                "summary": str(comparison.get("summary") or "").strip(),
                "topics": [],
                "files": [],
            },
        )

        relation = str(comparison.get("relation") or "").strip().lower()
        if relation and entry.get("relation") in {"", "compare"}:
            entry["relation"] = relation

        summary = str(comparison.get("summary") or "").strip()
        if summary and len(summary) > len(str(entry.get("summary") or "")):
            entry["summary"] = summary

        self._append_unique(entry.setdefault("topics", []), topic)
        self._append_unique(entry.setdefault("files", []), rel_path)
        return key

    def load_index(self) -> Dict[str, Any]:
        """加载知识索引；若文件缺失或内容异常，返回空索引。"""
        index = self.file_store.load_json(self.config.index_path)
        if not isinstance(index, dict):
            return self._empty_index()

        normalized = dict(index)
        defaults = self._empty_index()
        for key, default in defaults.items():
            value = normalized.get(key)
            if isinstance(default, dict):
                normalized[key] = value if isinstance(value, dict) else {}
            elif isinstance(default, list):
                normalized[key] = value if isinstance(value, list) else []
            else:
                normalized[key] = value if isinstance(value, type(default)) else default

        normalized["topics"] = {
            topic: self._normalize_topic_data(data)
            for topic, data in normalized["topics"].items()
            if isinstance(topic, str) and isinstance(data, dict)
        }
        normalized["canonical_topics"] = {
            str(source).strip(): str(target).strip()
            for source, target in normalized["canonical_topics"].items()
            if isinstance(source, str) and isinstance(target, str) and str(source).strip() and str(target).strip()
        }
        return normalized

    def save_index(self, index: Dict[str, Any]) -> None:
        """将知识索引持久化到 index.json。"""
        self.file_store.save_json(self.config.index_path, index)

    def reset_index(self) -> None:
        """重置索引，确保 clean 能基于当前 raw 目录完整重建。"""
        self.save_index(self._empty_index())

    def update_index(self, meta: Dict[str, Any], rel_path: str) -> None:
        """
        将单篇元数据写入索引。

        Args:
            meta: 单篇 meta.json 的结构化内容
            rel_path: index.md 相对于 raw_dir 的路径
        """
        with self._lock:
            index = self.load_index()
            topic = meta.get("topic")
            summary = meta.get("summary")

            # 无 topic 时不更新索引，避免写入无效分支
            if not topic:
                return

            topics = index.setdefault("topics", {})
            if topic not in topics:
                topics[topic] = {
                    "summary": summary,  # topic 的知识汇总
                    "files": [],  # topic 下关联的 raw 文件列表
                    "tags": [],
                    "aliases": [],
                    "merged_topics": [],
                    "entities": [],
                    "comparisons": [],
                    "related_topics": [],
                }
            topic_data = self._normalize_topic_data(topics[topic])
            if summary and len(str(summary)) > len(str(topic_data.get("summary") or "")):
                topic_data["summary"] = summary
            if rel_path not in topic_data["files"]:
                topic_data["files"].append(rel_path)

            for tag in meta.get("tags", []):
                tag_text = str(tag or "").strip()
                if tag_text:
                    self._append_unique(topic_data["tags"], tag_text)

            for entity in meta.get("entities", []):
                if not isinstance(entity, dict) or not str(entity.get("name") or "").strip():
                    continue
                entity_key = self._upsert_entity(index, entity, topic, rel_path)
                self._append_unique(topic_data["entities"], entity_key)

            for comparison in meta.get("comparisons", []):
                if not isinstance(comparison, dict):
                    continue
                left = str(comparison.get("left") or "").strip()
                right = str(comparison.get("right") or "").strip()
                if not left or not right:
                    continue
                comparison_key = self._upsert_comparison(index, comparison, topic, rel_path)
                self._append_unique(topic_data["comparisons"], comparison_key)

            topics[topic] = topic_data

            self.save_index(index)

    def canonicalize_topic(self, index: Dict[str, Any], topic: Any) -> str:
        current = str(topic or "").strip()
        if not current:
            return ""

        mapping = index.get("canonical_topics", {}) if isinstance(index, dict) else {}
        if not isinstance(mapping, dict):
            return current

        seen = set()
        while current in mapping and current not in seen:
            seen.add(current)
            target = str(mapping.get(current) or "").strip()
            if not target or target == current:
                break
            current = target
        return current

    def _merge_related_topics(
        self,
        target_topic: str,
        target_related: List[Dict[str, Any]],
        source_related: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}

        for item in target_related + source_related:
            if not isinstance(item, dict):
                continue
            related_topic = str(item.get("topic") or "").strip()
            if not related_topic or related_topic == target_topic:
                continue

            current = merged.setdefault(
                related_topic,
                {
                    "topic": related_topic,
                    "score": 0.0,
                    "relation": "related",
                    "shared_tags": [],
                    "shared_entities": [],
                    "shared_comparisons": [],
                    "reasons": [],
                },
            )

            try:
                current["score"] = max(float(current.get("score", 0.0)), float(item.get("score", 0.0)))
            except (TypeError, ValueError):
                pass

            relation = str(item.get("relation") or "").strip()
            if relation == "merge-candidate" or current.get("relation") == "merge-candidate":
                current["relation"] = "merge-candidate"
            elif relation:
                current["relation"] = relation

            for field in ("shared_tags", "shared_entities", "shared_comparisons", "reasons"):
                values = current.setdefault(field, [])
                for value in self._normalize_str_list(item.get(field)):
                    self._append_unique(values, value)

            summary = str(item.get("summary") or "").strip()
            if summary and not current.get("summary"):
                current["summary"] = summary

        return sorted(
            merged.values(),
            key=lambda item: (-float(item.get("score", 0.0)), str(item.get("topic") or "")),
        )[:8]

    def _merge_topic_data(
        self,
        target_topic: str,
        target_data: Dict[str, Any],
        source_topic: str,
        source_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = self._normalize_topic_data(target_data)
        source = self._normalize_topic_data(source_data)

        if len(str(source.get("summary") or "")) > len(str(merged.get("summary") or "")):
            merged["summary"] = str(source.get("summary") or "").strip()

        for field in ("files", "tags", "aliases", "merged_topics", "entities", "comparisons"):
            values = merged.setdefault(field, [])
            for value in self._normalize_str_list(source.get(field)):
                self._append_unique(values, value)

        self._append_unique(merged.setdefault("aliases", []), source_topic)
        for alias in self._normalize_str_list(source.get("aliases")):
            self._append_unique(merged["aliases"], alias)

        self._append_unique(merged.setdefault("merged_topics", []), source_topic)
        for merged_topic in self._normalize_str_list(source.get("merged_topics")):
            self._append_unique(merged["merged_topics"], merged_topic)

        merged["canonical_topic"] = target_topic
        merged["related_topics"] = self._merge_related_topics(
            target_topic,
            merged.get("related_topics", []),
            source.get("related_topics", []),
        )

        children = merged.get("children")
        source_children = source.get("children")
        if isinstance(children, dict) and isinstance(source_children, dict):
            for child_topic, child_data in source_children.items():
                if child_topic not in children and isinstance(child_data, dict):
                    children[child_topic] = child_data
        elif isinstance(source_children, dict) and source_children:
            merged["children"] = source_children

        return merged

    def _canonicalize_related_topics(self, index: Dict[str, Any]) -> None:
        topics = index.get("topics", {})
        if not isinstance(topics, dict):
            return

        for topic, data in topics.items():
            if not isinstance(topic, str) or not isinstance(data, dict):
                continue

            canonical_related: List[Dict[str, Any]] = []
            seen = set()
            for item in data.get("related_topics", []) if isinstance(data.get("related_topics"), list) else []:
                if not isinstance(item, dict):
                    continue
                related_topic = self.canonicalize_topic(index, item.get("topic"))
                if not related_topic or related_topic == topic or related_topic in seen:
                    continue
                seen.add(related_topic)
                normalized_item = dict(item)
                normalized_item["topic"] = related_topic
                canonical_related.append(normalized_item)

            data["related_topics"] = self._merge_related_topics(topic, canonical_related, [])
            data["canonical_topic"] = topic

    def _canonicalize_structured_refs(self, index: Dict[str, Any]) -> None:
        for key in ("entities", "comparisons"):
            lookup = index.get(key, {})
            if not isinstance(lookup, dict):
                continue
            for item in lookup.values():
                if not isinstance(item, dict):
                    continue
                canonical_topics: List[str] = []
                for topic in item.get("topics", []) if isinstance(item.get("topics"), list) else []:
                    canonical = self.canonicalize_topic(index, topic)
                    if canonical and canonical not in canonical_topics:
                        canonical_topics.append(canonical)
                item["topics"] = canonical_topics

    def materialize_merges(self, index: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(index, dict):
            materialized = self._empty_index()
        else:
            materialized = copy.deepcopy(index)
        topics = materialized.get("topics", {})
        if not isinstance(topics, dict):
            materialized["canonical_topics"] = {}
            return materialized

        materialized["topics"] = {
            topic: self._normalize_topic_data(data)
            for topic, data in topics.items()
            if isinstance(topic, str) and isinstance(data, dict)
        }
        topics = materialized["topics"]

        canonical_map: Dict[str, str] = {}

        def resolve(topic: str) -> str:
            current = str(topic or "").strip()
            seen = set()
            while current in canonical_map and current not in seen:
                seen.add(current)
                target = str(canonical_map.get(current) or "").strip()
                if not target or target == current:
                    break
                current = target
            return current

        for rule in materialized.get("merge", []) if isinstance(materialized.get("merge"), list) else []:
            if not isinstance(rule, dict):
                continue

            target = str(rule.get("target") or "").strip()
            source_list = self._normalize_str_list(rule.get("sources"))
            if not target or not source_list:
                continue

            canonical_target = resolve(target)
            if canonical_target not in topics:
                continue

            for source in source_list:
                canonical_source = resolve(source)
                if not canonical_source or canonical_source == canonical_target:
                    canonical_map[source] = canonical_target
                    continue

                source_data = topics.get(canonical_source)
                target_data = topics.get(canonical_target)
                if not isinstance(source_data, dict) or not isinstance(target_data, dict):
                    canonical_map[source] = canonical_target
                    continue

                topics[canonical_target] = self._merge_topic_data(
                    canonical_target,
                    target_data,
                    canonical_source,
                    source_data,
                )
                topics.pop(canonical_source, None)
                canonical_map[canonical_source] = canonical_target
                canonical_map[source] = canonical_target

        canonical_map = {
            source: resolve(target)
            for source, target in canonical_map.items()
            if str(source).strip() and resolve(target)
        }

        compressed_merge: Dict[str, List[str]] = {}
        for source, target in canonical_map.items():
            if not source or not target or source == target:
                continue
            compressed_merge.setdefault(target, [])
            if source not in compressed_merge[target]:
                compressed_merge[target].append(source)

        materialized["canonical_topics"] = canonical_map
        materialized["merge"] = [
            {
                "target": target,
                "sources": sorted(sources),
            }
            for target, sources in sorted(compressed_merge.items())
            if sources
        ]

        self._canonicalize_related_topics(materialized)
        self._canonicalize_structured_refs(materialized)
        return materialized
