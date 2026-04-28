import json
import logging
from typing import Any, Dict, List, Optional

from core.infra.index_repository import IndexRepository


logger = logging.getLogger(__name__)


# 拆分服务：当 topic 过大时，调用 LLM 提供分拆建议
class SplitService:
    def __init__(self, index_repository: IndexRepository, llm_client, prompt_registry, parser):
        self.index_repository = index_repository
        self.llm_client = llm_client
        self.prompt_registry = prompt_registry
        self.parser = parser

    @staticmethod
    def _collect_parent_files(data: Dict[str, Any]) -> List[str]:
        files = []
        for item in data.get("files", []):
            if isinstance(item, str) and item.strip():
                files.append(item)
        return files

    @staticmethod
    def _collect_refs_for_files(lookup: Dict[str, Any], file_list: List[str]) -> List[str]:
        refs: List[str] = []
        file_set = set(file_list)
        if not isinstance(lookup, dict):
            return refs

        for key, data in lookup.items():
            if not isinstance(key, str) or not isinstance(data, dict):
                continue
            files = data.get("files", [])
            if not isinstance(files, list):
                continue
            if any(isinstance(item, str) and item in file_set for item in files):
                refs.append(key)
        return refs

    def split_topic(self, topic: str, data: dict):
        parent_files = self._collect_parent_files(data)
        if len(parent_files) < 6:
            return None

        res = self.llm_client.run_chain(
            self.prompt_registry.split_prompt,
            {
                "topic": topic,
                "files": json.dumps(parent_files, ensure_ascii=False),
            },
        )

        result = self.parser.safe_json(res)
        if not isinstance(result, dict):
            logger.warning("split 解析失败: %s", topic)
            return None

        if not isinstance(result.get("children"), list):
            logger.warning("split 返回 children 格式非法: %s", topic)
            return None

        return result

    def apply_split(self, index: dict):
        topics = index.get("topics", {})
        entities = index.get("entities", {})
        comparisons = index.get("comparisons", {})
        new_topics: Dict[str, dict] = {}
        for topic, data in topics.items():
            if not isinstance(topic, str) or not isinstance(data, dict):
                continue

            split = self.split_topic(topic, data)
            if not split:
                new_topics[topic] = data
                continue

            parent_files = self._collect_parent_files(data)
            parent_file_set = set(parent_files)
            children: Dict[str, dict] = {}
            assigned: List[str] = []
            assigned_set = set()

            for child in split.get("children", []):
                if not isinstance(child, dict):
                    logger.warning("split child 类型非法: %s -> %r", topic, child)
                    continue

                child_topic = child.get("topic")
                child_files = child.get("files", [])
                if not isinstance(child_topic, str) or not child_topic.strip():
                    logger.warning("split child topic 非法: %s -> %r", topic, child)
                    continue

                child_file_list: List[str] = []
                for item in child_files if isinstance(child_files, list) else []:
                    if not isinstance(item, str) or not item.strip():
                        continue
                    if item not in parent_file_set:
                        logger.warning("split child 文件不在父 topic 中: %s -> %s", topic, item)
                        continue
                    if item in assigned_set:
                        logger.warning("split child 文件重复分配，跳过后续分配: %s -> %s", topic, item)
                        continue
                    if item not in child_file_list:
                        child_file_list.append(item)

                if not child_file_list:
                    logger.warning("split child 无有效文件，跳过: %s -> %s", topic, child_topic)
                    continue

                assigned.extend(child_file_list)
                assigned_set.update(child_file_list)
                children[child_topic.strip()] = {
                    "files": child_file_list,
                    "tags": data.get("tags", []),
                    "entities": self._collect_refs_for_files(entities, child_file_list),
                    "comparisons": self._collect_refs_for_files(comparisons, child_file_list),
                }

            if not children:
                logger.warning("split 未生成有效 child，保留原 topic: %s", topic)
                new_topics[topic] = data
                continue

            orphan_files = [f for f in parent_file_set if f not in assigned]
            if orphan_files:
                logger.warning("split 后文件未分配到子主题，归并到父 topic: %s -> %s", topic, orphan_files)

            new_topics[topic] = {
                "summary": data.get("summary"),
                "tags": data.get("tags", []),
                "aliases": data.get("aliases", []),
                "merged_topics": data.get("merged_topics", []),
                "canonical_topic": data.get("canonical_topic") or topic,
                "children": children,
                "files": [],
                "entities": data.get("entities", []),
                "comparisons": data.get("comparisons", []),
                "related_topics": data.get("related_topics", []),
            }
            print(f"split: {topic} -> {list(children.keys())}")

        index["topics"] = new_topics
        return index

    def split(self) -> None:
        index = self.index_repository.load_index()
        self.index_repository.save_index(self.apply_split(index))
