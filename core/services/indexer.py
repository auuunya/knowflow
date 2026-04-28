import copy
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Set, Tuple


logger = logging.getLogger(__name__)


# 索引策略服务：先补齐全局 merge/关系证据，再调用 LLM 做结构治理建议
class IndexPolicyService:
    _TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
    _NAME_SEP_RE = re.compile(r"[^\w]+", re.UNICODE)

    def __init__(self, index_repository, llm_client, prompt_registry, parser):
        self.index_repository = index_repository
        self.llm_client = llm_client
        self.prompt_registry = prompt_registry
        self.parser = parser

    @staticmethod
    def _normalize_list(value: Any) -> List[str]:
        items: List[str] = []
        for item in value if isinstance(value, list) else []:
            text = str(item or "").strip()
            if text and text not in items:
                items.append(text)
        return items

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
        return SequenceMatcher(None, left.lower(), right.lower()).ratio()

    def _comparison_label(self, data: Dict[str, Any]) -> str:
        left = str(data.get("left") or "").strip()
        right = str(data.get("right") or "").strip()
        aspect = str(data.get("aspect") or "").strip()
        core = f"{left} vs {right}".strip()
        return f"{core} ({aspect})" if core and aspect else core

    def _topic_features(
        self,
        topic: str,
        data: Dict[str, Any],
        entities: Dict[str, Any],
        comparisons: Dict[str, Any],
    ) -> Dict[str, Any]:
        tags = set(self._normalize_list(data.get("tags")))
        files = {
            str(item).strip()
            for item in data.get("files", [])
            if isinstance(item, str) and str(item).strip()
        }

        entity_names: Set[str] = set()
        for key in data.get("entities", []) if isinstance(data.get("entities"), list) else []:
            entity = entities.get(key, {}) if isinstance(entities, dict) else {}
            name = str(entity.get("name") or key or "").strip()
            if name:
                entity_names.add(name)

        comparison_labels: Set[str] = set()
        for key in data.get("comparisons", []) if isinstance(data.get("comparisons"), list) else []:
            comparison = comparisons.get(key, {}) if isinstance(comparisons, dict) else {}
            label = self._comparison_label(comparison)
            if label:
                comparison_labels.add(label)

        tokens = set(self._tokenize(topic))
        for item in tags | entity_names | comparison_labels:
            tokens.update(self._tokenize(item))

        return {
            "topic": topic,
            "summary": str(data.get("summary") or "").strip(),
            "tags": tags,
            "files": files,
            "entity_names": entity_names,
            "comparison_labels": comparison_labels,
            "tokens": tokens,
            "name_similarity_key": self._NAME_SEP_RE.sub("-", topic.lower()).strip("-"),
        }

    def _score_pair(self, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        shared_tags = sorted(left["tags"] & right["tags"])
        shared_entities = sorted(left["entity_names"] & right["entity_names"])
        shared_comparisons = sorted(left["comparison_labels"] & right["comparison_labels"])
        shared_files = sorted(left["files"] & right["files"])

        tag_score = self._jaccard(left["tags"], right["tags"])
        entity_score = self._jaccard(left["entity_names"], right["entity_names"])
        comparison_score = self._jaccard(left["comparison_labels"], right["comparison_labels"])
        token_score = self._jaccard(left["tokens"], right["tokens"])
        name_score = self._sequence_ratio(left["name_similarity_key"], right["name_similarity_key"])

        score = (
            tag_score * 0.34
            + entity_score * 0.34
            + comparison_score * 0.12
            + token_score * 0.12
            + name_score * 0.08
        )

        if shared_files:
            score += 0.08

        if left["name_similarity_key"] and right["name_similarity_key"]:
            left_key = left["name_similarity_key"]
            right_key = right["name_similarity_key"]
            if left_key == right_key:
                score += 0.2
            elif left_key in right_key or right_key in left_key:
                score += 0.12

        reasons: List[str] = []
        if shared_tags:
            reasons.append(f"shared-tags:{', '.join(shared_tags[:4])}")
        if shared_entities:
            reasons.append(f"shared-entities:{', '.join(shared_entities[:4])}")
        if shared_comparisons:
            reasons.append(f"shared-comparisons:{', '.join(shared_comparisons[:3])}")
        if shared_files:
            reasons.append("shared-files")
        if name_score >= 0.9:
            reasons.append("near-identical-name")
        elif name_score >= 0.78:
            reasons.append("similar-name")

        if score < 0.2 and not shared_tags and not shared_entities and not shared_comparisons:
            return {}

        relation = "merge-candidate" if score >= 0.68 and (shared_tags or shared_entities or name_score >= 0.88) else "related"

        return {
            "score": round(min(score, 1.0), 3),
            "relation": relation,
            "shared_tags": shared_tags[:6],
            "shared_entities": shared_entities[:6],
            "shared_comparisons": shared_comparisons[:4],
            "reasons": reasons[:6],
        }

    @staticmethod
    def _target_weight(feature: Dict[str, Any]) -> Tuple[int, int, int, str]:
        return (
            len(feature.get("files", set())),
            len(feature.get("tags", set())) + len(feature.get("entity_names", set())),
            -len(str(feature.get("topic") or "")),
            str(feature.get("topic") or "").lower(),
        )

    def _pick_merge_target(self, left: Dict[str, Any], right: Dict[str, Any]) -> Tuple[str, str]:
        if self._target_weight(left) >= self._target_weight(right):
            return str(left["topic"]), str(right["topic"])
        return str(right["topic"]), str(left["topic"])

    def _enrich_index(self, index: Dict[str, Any]) -> Dict[str, Any]:
        enriched = copy.deepcopy(index)
        topics = enriched.get("topics", {})
        entities = enriched.get("entities", {})
        comparisons = enriched.get("comparisons", {})

        if not isinstance(topics, dict):
            enriched["merge"] = []
            enriched["merge_candidates"] = []
            return enriched

        features_by_topic: Dict[str, Dict[str, Any]] = {}
        for topic, data in topics.items():
            if not isinstance(topic, str) or not isinstance(data, dict):
                continue
            features_by_topic[topic] = self._topic_features(topic, data, entities, comparisons)

        related_map: Dict[str, List[Dict[str, Any]]] = {topic: [] for topic in features_by_topic}
        merge_pairs: List[Dict[str, Any]] = []

        topic_names = sorted(features_by_topic.keys())
        for index_left, left_topic in enumerate(topic_names):
            left_feature = features_by_topic[left_topic]
            for right_topic in topic_names[index_left + 1 :]:
                right_feature = features_by_topic[right_topic]
                scored = self._score_pair(left_feature, right_feature)
                if not scored:
                    continue

                left_related = {
                    "topic": right_topic,
                    "summary": right_feature.get("summary", ""),
                    **scored,
                }
                right_related = {
                    "topic": left_topic,
                    "summary": left_feature.get("summary", ""),
                    **scored,
                }
                related_map[left_topic].append(left_related)
                related_map[right_topic].append(right_related)

                if scored.get("relation") == "merge-candidate":
                    target, source = self._pick_merge_target(left_feature, right_feature)
                    merge_pairs.append(
                        {
                            "target": target,
                            "sources": [source],
                            "score": scored["score"],
                            "shared_tags": scored["shared_tags"],
                            "shared_entities": scored["shared_entities"],
                            "shared_comparisons": scored["shared_comparisons"],
                            "reasons": scored["reasons"],
                        }
                    )

        for topic, data in topics.items():
            if not isinstance(topic, str) or not isinstance(data, dict):
                continue
            data["related_topics"] = sorted(
                related_map.get(topic, []),
                key=lambda item: (-float(item.get("score", 0.0)), str(item.get("topic") or "")),
            )[:8]

        merge_pairs = sorted(
            merge_pairs,
            key=lambda item: (-float(item.get("score", 0.0)), str(item.get("target") or ""), ",".join(item.get("sources", []))),
        )

        enriched["merge_candidates"] = merge_pairs
        return enriched

    def reconcile_index(self) -> None:
        index = self._enrich_index(self.index_repository.load_index())

        try:
            res = self.llm_client.run_chain(
                self.prompt_registry.policy_prompt,
                {"index": json.dumps(index, ensure_ascii=False)},
            )
        except Exception:
            logger.exception("index-fix LLM 调用失败，保留本地关系/merge 推断结果")
            self.index_repository.save_index(index)
            return

        policy = self.parser.safe_json(res)
        if not policy:
            logger.warning("index-fix 解析失败，保留本地关系/merge 推断结果")
            self.index_repository.save_index(self.index_repository.materialize_merges(index))
            return

        if not isinstance(policy, dict):
            logger.warning("index-fix 返回非法结构（非 JSON 对象），保留富化后的 index")
            self.index_repository.save_index(self.index_repository.materialize_merges(index))
            return

        # index-fix 只允许覆写治理建议字段，topic 级结构化证据保留在 enriched index 中。
        new_index = copy.deepcopy(index)
        for key, value in policy.items():
            if key == "topics":
                if isinstance(value, dict):
                    new_index[key] = value
            elif isinstance(value, list):
                new_index[key] = value

        if not isinstance(new_index.get("topics"), dict):
            logger.warning("index-fix 后未检测到可用 topics，保留原有 topics")
            new_index["topics"] = index.get("topics", {}) if isinstance(index.get("topics"), dict) else {}

        self.index_repository.save_index(self.index_repository.materialize_merges(new_index))
