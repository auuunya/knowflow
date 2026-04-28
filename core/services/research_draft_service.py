import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import AppConfig
from core.infra.file_store import FileStore
from core.infra.index_repository import IndexRepository
from core.infra.llm_client import LLMClient
from core.infra.prompt_registry import PromptRegistry


logger = logging.getLogger(__name__)


class ResearchDraftService:
    _METADATA_GAP_PREFIX = "为 raw 文档补 metadata："
    _PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
    _MAX_SEED_SOURCES = 3
    _MAX_EXCERPT_CHARS = 1200

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
        self._llm_available: Optional[bool] = None

    def _relative_raw_path(self, raw_path: str) -> str:
        candidate = Path(str(raw_path).strip())
        if candidate.is_absolute():
            candidate = candidate.relative_to(self.config.raw_dir)
        return candidate.as_posix()

    def _normalize_raw_paths(self, item: Dict[str, Any], index: Dict[str, Any]) -> List[str]:
        values: List[str] = []

        raw_paths = item.get("raw_paths", [])
        if isinstance(raw_paths, list):
            for value in raw_paths:
                text = str(value or "").strip()
                if text:
                    values.append(self._relative_raw_path(text))

        raw_path = str(item.get("raw_path") or "").strip()
        if raw_path:
            values.append(self._relative_raw_path(raw_path))

        if not values and str(item.get("kind") or "").strip() == "metadata-gap":
            title = str(item.get("title") or "").strip()
            if title.startswith(self._METADATA_GAP_PREFIX):
                rel_dir = title[len(self._METADATA_GAP_PREFIX) :].strip()
                if rel_dir:
                    values.append(f"{rel_dir}/index.md")

        if not values:
            topic = str(item.get("topic") or "").strip()
            topic_slug = str(item.get("topic_slug") or item.get("slug") or "").strip()
            values.extend(self._lookup_topic_files(index, topic, topic_slug))

        deduped: List[str] = []
        seen = set()
        for value in values:
            if value and value not in seen:
                seen.add(value)
                deduped.append(value)
        return deduped

    def _lookup_topic_files(self, index: Dict[str, Any], topic: str, topic_slug: str) -> List[str]:
        topics = index.get("topics", {})
        if not isinstance(topics, dict):
            return []

        candidates: List[str] = []

        def visit(name: str, data: Dict[str, Any]) -> None:
            if not isinstance(name, str) or not isinstance(data, dict):
                return

            if name == topic or self.config.slugify(name, fallback="concept") == topic_slug:
                files = data.get("files", [])
                if isinstance(files, list):
                    for value in files:
                        text = str(value or "").strip()
                        if text:
                            candidates.append(text)

            children = data.get("children", {})
            if isinstance(children, dict):
                for child_name, child_data in children.items():
                    visit(child_name, child_data)

        for name, data in topics.items():
            visit(name, data)

        deduped: List[str] = []
        seen = set()
        for value in candidates:
            if value not in seen:
                seen.add(value)
                deduped.append(value)
        return deduped

    def _derive_topic(self, item: Dict[str, Any], raw_paths: List[str]) -> str:
        topic = str(item.get("topic") or "").strip()
        if topic:
            return topic

        if raw_paths:
            return Path(raw_paths[0]).parent.name

        title = str(item.get("title") or "").strip()
        if title.startswith(self._METADATA_GAP_PREFIX):
            return Path(title[len(self._METADATA_GAP_PREFIX) :].strip()).name

        return title or "Research Draft"

    def _derive_topic_slug(self, item: Dict[str, Any], topic: str, raw_paths: List[str]) -> str:
        explicit = str(item.get("topic_slug") or item.get("slug") or "").strip()
        if explicit:
            return explicit

        if raw_paths:
            return self.config.source_slug_for(raw_paths[0])

        base = self.config.slugify(topic, fallback="research")
        if base != "research":
            return base

        digest = hashlib.md5(topic.encode("utf-8")).hexdigest()[:8]
        return f"research-{digest}"

    def _normalize_item(self, item: Dict[str, Any], index: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        if item.get("draftable") is False:
            return None

        raw_paths = self._normalize_raw_paths(item, index)
        topic = self._derive_topic(item, raw_paths)
        if not topic:
            return None

        return {
            "kind": str(item.get("kind") or "").strip() or "research",
            "priority": str(item.get("priority") or "medium").strip() or "medium",
            "title": str(item.get("title") or topic).strip() or topic,
            "reason": str(item.get("reason") or "").strip(),
            "url": str(item.get("url") or "").strip(),
            "topic": topic,
            "topic_slug": self._derive_topic_slug(item, topic, raw_paths),
            "raw_paths": raw_paths,
        }

    def _group_items(self, queue: List[Dict[str, Any]], index: Dict[str, Any]) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}

        for item in queue:
            normalized = self._normalize_item(item, index)
            if not normalized:
                continue

            slug = normalized["topic_slug"]
            group = groups.setdefault(
                slug,
                {
                    "topic": normalized["topic"],
                    "topic_slug": slug,
                    "priority": normalized["priority"],
                    "tasks": [],
                    "raw_paths": [],
                },
            )

            if self._PRIORITY_ORDER.get(normalized["priority"], 99) < self._PRIORITY_ORDER.get(group["priority"], 99):
                group["priority"] = normalized["priority"]

            group["tasks"].append(
                {
                    "kind": normalized["kind"],
                    "priority": normalized["priority"],
                    "title": normalized["title"],
                    "reason": normalized["reason"],
                    "url": normalized["url"],
                }
            )

            for raw_path in normalized["raw_paths"]:
                if raw_path not in group["raw_paths"]:
                    group["raw_paths"].append(raw_path)

        return sorted(
            groups.values(),
            key=lambda item: (
                self._PRIORITY_ORDER.get(str(item.get("priority") or "").strip(), 99),
                str(item.get("topic") or "").lower(),
            ),
        )

    def _excerpt_text(self, text: str) -> str:
        compact = "\n".join(line.strip() for line in str(text or "").splitlines() if line.strip())
        return compact[: self._MAX_EXCERPT_CHARS].strip()

    def _collect_seed_sources(self, raw_paths: List[str]) -> List[Dict[str, str]]:
        seeds: List[Dict[str, str]] = []

        for raw_path in raw_paths[: self._MAX_SEED_SOURCES]:
            abs_path = self.config.raw_dir / raw_path
            if not abs_path.is_file():
                continue
            try:
                text = self.file_store.read_text(abs_path)
            except Exception as exc:
                logger.warning("读取 research seed 失败，跳过: %s, %s", abs_path, exc)
                continue

            seeds.append(
                {
                    "path": raw_path,
                    "title": Path(raw_path).parent.name,
                    "excerpt": self._excerpt_text(text),
                }
            )

        return seeds

    def _render_task_summary(self, tasks: List[Dict[str, str]]) -> str:
        lines: List[str] = []
        for item in tasks:
            line = f"- [{item['priority']}] {item['title']} (`{item['kind']}`)"
            if item["reason"]:
                line += f"：{item['reason']}"
            if item["url"]:
                line += f" 参考：`{item['url']}`"
            lines.append(line + "\n")
        return "".join(lines) if lines else "- 暂无候选任务\n"

    def _render_seed_sources(self, seeds: List[Dict[str, str]], raw_paths: List[str]) -> str:
        if seeds:
            return "".join(f"- `{item['path']}`：{item['title']}\n" for item in seeds)
        if raw_paths:
            return "".join(f"- `{raw_path}`\n" for raw_path in raw_paths)
        return "- 暂无可引用来源\n"

    def _render_source_excerpts(self, seeds: List[Dict[str, str]]) -> str:
        if not seeds:
            return "暂无原始摘录，可在后续手工补充。"

        blocks: List[str] = []
        for item in seeds:
            blocks.append(
                f"### {item['title']}\n"
                f"path: `{item['path']}`\n\n"
                f"{item['excerpt'] or '（原文为空，待补充）'}\n"
            )
        return "\n".join(blocks).strip()

    def _fallback_markdown(self, topic: str, tasks: List[Dict[str, str]], seeds: List[Dict[str, str]], raw_paths: List[str]) -> str:
        known_lines = []
        for item in seeds:
            excerpt = item["excerpt"][:220].strip()
            known_lines.append(f"- `{item['path']}`：{excerpt or '原始内容已存在，但还需要进一步整理。'}\n")

        if not known_lines and raw_paths:
            known_lines = [f"- 已有关联原始笔记：`{raw_path}`\n" for raw_path in raw_paths]
        if not known_lines:
            known_lines = ["- 当前还没有可直接引用的原始内容，需后续补料。\n"]

        question_lines = []
        for item in tasks:
            question_lines.append(f"- 待验证：{item['reason'] or item['title']}\n")
        if not question_lines:
            question_lines = ["- 待验证：补齐该主题的定义、边界、关键案例与关联实体。\n"]

        next_steps = [
            "- 先补 1 到 3 份高质量来源，优先选择能定义概念边界的材料。\n",
            "- 把原始材料中的实体、对比关系和关键步骤显式写出来。\n",
            "- 完成后重新运行 `python3 knowflow.py build` 进入下一轮 ingest。\n",
        ]

        return (
            f"# {topic}\n\n"
            "> 这是一份由 knowflow 根据 audit 候选任务自动生成的研究草稿，用于下一轮 ingest。\n\n"
            "## 为什么值得补充\n"
            + self._render_task_summary(tasks)
            + "\n## 当前已知\n"
            + "".join(known_lines)
            + "\n## 待验证问题\n"
            + "".join(question_lines)
            + "\n## 关键概念 / 实体\n"
            + "- 待补充：请在下一轮整理时明确概念定义、重要工具、相关系统与替代方案。\n"
            + "\n## 下一步补料建议\n"
            + "".join(next_steps)
            + "\n## 来源线索\n"
            + self._render_seed_sources(seeds, raw_paths)
        )

    def _generate_markdown(self, group: Dict[str, Any], seeds: List[Dict[str, str]]) -> str:
        if self._llm_available is False:
            return self._fallback_markdown(group["topic"], group["tasks"], seeds, group["raw_paths"])

        try:
            content = self.llm_client.run_chain(
                self.prompt_registry.research_draft_prompt,
                {
                    "topic": group["topic"],
                    "task_summary": self._render_task_summary(group["tasks"]),
                    "seed_sources": self._render_seed_sources(seeds, group["raw_paths"]),
                    "source_excerpts": self._render_source_excerpts(seeds),
                },
            )
            text = str(content or "").strip()
            if text:
                self._llm_available = True
                return text
        except Exception as exc:
            self._llm_available = False
            logger.warning("research draft LLM 生成失败，使用 fallback：%s, %s", group["topic"], exc)

        return self._fallback_markdown(group["topic"], group["tasks"], seeds, group["raw_paths"])

    def _fingerprint(self, group: Dict[str, Any]) -> str:
        payload = {
            "topic": group["topic"],
            "priority": group["priority"],
            "tasks": group["tasks"],
            "raw_paths": group["raw_paths"],
        }
        return hashlib.md5(repr(payload).encode("utf-8")).hexdigest()

    def materialize_from_audit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        queue = payload.get("research_queue", []) if isinstance(payload, dict) else []
        if not isinstance(queue, list):
            queue = []

        index = self.index_repository.load_index()
        groups = self._group_items(queue, index)
        limit = max(0, int(self.config.research_draft_max_notes))
        created: List[Dict[str, str]] = []
        skipped_existing: List[Dict[str, str]] = []
        deferred: List[Dict[str, str]] = []

        for group in groups:
            target_path = self.config.research_drafts_dir / group["topic_slug"] / "index.md"
            rel_target = target_path.relative_to(self.config.knowledge_base_dir).as_posix()

            if limit and len(created) >= limit:
                deferred.append({"topic": group["topic"], "path": rel_target})
                continue

            if target_path.exists() and not self.config.research_draft_overwrite_existing:
                skipped_existing.append({"topic": group["topic"], "path": rel_target})
                logger.info("skip research draft (existing file): %s", target_path)
                continue

            seeds = self._collect_seed_sources(group["raw_paths"])
            content = self._generate_markdown(group, seeds).rstrip() + "\n"
            self.file_store.save_text(target_path, content)
            created.append(
                {
                    "topic": group["topic"],
                    "path": rel_target,
                    "raw_path": target_path.relative_to(self.config.raw_dir).as_posix(),
                    "fingerprint": self._fingerprint(group),
                }
            )
            logger.info("research draft generated: %s", target_path)

        manifest = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "eligible_groups": len(groups),
            "created": created,
            "skipped_existing": skipped_existing,
            "deferred": deferred,
            "overwrite_existing": self.config.research_draft_overwrite_existing,
            "max_notes": limit,
        }
        self.file_store.save_json(self.config.research_drafts_manifest_path, manifest)
        return manifest
