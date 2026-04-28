import logging
import re
from pathlib import Path
from typing import Any, Dict, List


logger = logging.getLogger(__name__)


# 清洗服务：把 raw/index.md 结构化为 metadata，并更新索引
class CleanerService:
    _SENSITIVE_PATTERNS = [
        (re.compile(r"[\w.+'-]+@[\w.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
        (re.compile(r"1[3-9]\\d{9}"), "[PHONE]"),
        (re.compile(r"sk-[A-Za-z0-9]{16,}"), "[API_KEY]"),
        (re.compile(r"[a-zA-Z0-9_\-]{20,}"), "[TOKEN]"),
    ]

    def __init__(self, config, index_repository, llm_client, prompt_registry, file_store, parser):
        self.config = config
        self.index_repository = index_repository
        self.llm_client = llm_client
        self.prompt_registry = prompt_registry
        self.file_store = file_store
        self.parser = parser

    def _is_research_draft(self, md_path: Path) -> bool:
        try:
            return md_path.resolve().is_relative_to(Path(self.config.research_drafts_dir).resolve())
        except AttributeError:
            try:
                md_path.resolve().relative_to(Path(self.config.research_drafts_dir).resolve())
                return True
            except ValueError:
                return False

    def _desensitize(self, content: str) -> str:
        """可选脱敏：默认关闭，打开后用于 raw 文本隐私防护。"""
        if not self.config.raw_desensitize:
            return content

        sanitized = content
        for pattern, replacement in self._SENSITIVE_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    def _analyze(self, content: str):
        payload = {"content": self._desensitize(content)}
        res = self.llm_client.run_chain(self.prompt_registry.clean_prompt, payload)
        return self.parser.safe_json(res)

    @staticmethod
    def _normalize_keep(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, bool):
            return value

        normalized = str(value).strip().lower()
        if normalized in {"0", "false", "f", "no", "n", "off"}:
            return False
        if normalized in {"1", "true", "t", "yes", "y", "on"}:
            return True
        return True

    @staticmethod
    def _normalize_str(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _normalize_tags(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []

        tags: List[str] = []
        for item in value:
            tag = str(item or "").strip()
            if tag and tag not in tags:
                tags.append(tag)
        return tags[:10]

    def _normalize_entities(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []

        entities: List[Dict[str, Any]] = []
        seen = set()

        for item in value:
            if isinstance(item, str):
                name = self._normalize_str(item)
                entity_type = "unknown"
                summary = ""
                aliases: List[str] = []
            elif isinstance(item, dict):
                name = self._normalize_str(item.get("name") or item.get("title"))
                entity_type = self._normalize_str(item.get("type") or item.get("entity_type")).lower() or "unknown"
                summary = self._normalize_str(item.get("summary"))
                aliases = self._normalize_tags(item.get("aliases"))
            else:
                continue

            if not name:
                continue

            key = name.lower()
            if key in seen:
                continue
            seen.add(key)

            cleaned_aliases = [alias for alias in aliases if alias.lower() != key]
            entities.append(
                {
                    "name": name,
                    "type": entity_type,
                    "summary": summary,
                    "aliases": cleaned_aliases[:6],
                }
            )

        return entities[:8]

    def _normalize_comparisons(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []

        comparisons: List[Dict[str, Any]] = []
        seen = set()

        for item in value:
            if not isinstance(item, dict):
                continue

            left = self._normalize_str(item.get("left") or item.get("source") or item.get("a"))
            right = self._normalize_str(item.get("right") or item.get("target") or item.get("b"))
            aspect = self._normalize_str(item.get("aspect") or item.get("dimension")) or "general"
            relation = self._normalize_str(item.get("relation")).lower() or "compare"
            summary = self._normalize_str(item.get("summary"))

            if not left or not right or left.lower() == right.lower():
                continue

            pair_key = tuple(sorted([left.lower(), right.lower()])) + (aspect.lower(),)
            if pair_key in seen:
                continue
            seen.add(pair_key)

            comparisons.append(
                {
                    "left": left,
                    "right": right,
                    "aspect": aspect,
                    "relation": relation,
                    "summary": summary,
                }
            )

        return comparisons[:8]

    def _normalize_meta(self, meta: Any) -> Dict[str, Any]:
        if not isinstance(meta, dict):
            return {}

        normalized = dict(meta)
        normalized["keep"] = self._normalize_keep(meta.get("keep"))
        normalized["type"] = self._normalize_str(meta.get("type")) or "unknown"
        normalized["title"] = self._normalize_str(meta.get("title"))
        normalized["topic"] = self._normalize_str(meta.get("topic"))
        normalized["tags"] = self._normalize_tags(meta.get("tags"))
        normalized["summary"] = self._normalize_str(meta.get("summary"))
        normalized["entities"] = self._normalize_entities(meta.get("entities"))
        normalized["comparisons"] = self._normalize_comparisons(meta.get("comparisons"))
        return normalized

    def _load_existing_meta(self, raw_path: Path) -> Dict[str, Any]:
        meta = self._normalize_meta(self.file_store.load_json(self.config.meta_path_for(raw_path)))
        if meta:
            if not meta.get("title"):
                meta["title"] = raw_path.parent.name
            return meta
        return {}

    def _missing_reusable_fields(self, meta: Dict[str, Any]) -> List[str]:
        if not isinstance(meta, dict) or not meta:
            return ["meta"]
        if meta.get("keep") is False:
            return []

        missing: List[str] = []
        for field in ("topic", "summary"):
            if not str(meta.get(field) or "").strip():
                missing.append(field)
        return missing

    def _clean_one(self, md_path: Path) -> None:
        if not md_path.is_file():
            logger.warning("clean 跳过不存在的 index.md: %s", md_path)
            return
        if self._is_research_draft(md_path):
            logger.info("skip generated research draft during clean: %s", md_path)
            return

        meta_path = self.config.meta_path_for(md_path)
        rel_path = md_path.relative_to(self.config.raw_dir).as_posix()

        if self.config.clean_skip_existing:
            meta = self._load_existing_meta(md_path)
            if meta:
                missing_fields = self._missing_reusable_fields(meta)
                if meta.get("keep") is False:
                    logger.info("已有 meta keep=false，跳过: %s", rel_path)
                    return
                if not missing_fields:
                    self.file_store.save_json(meta_path, meta)
                    self.index_repository.update_index(meta, rel_path)
                    logger.info("skip clean (existing meta): %s", rel_path)
                    return

                logger.info(
                    "已有 meta 不完整，重新分析: %s missing=%s",
                    rel_path,
                    ",".join(missing_fields),
                )

        try:
            content = self.file_store.read_text(md_path)
        except Exception as exc:
            logger.error("读取 %s 失败: %s", md_path, exc)
            return

        try:
            meta = self._normalize_meta(self._analyze(content))
        except Exception as exc:
            logger.error("分析 %s 失败: %s", md_path, exc)
            return
        if not meta:
            logger.warning("index.md 解析失败，已跳过: %s", md_path)
            return
        if not meta.get("title"):
            meta["title"] = md_path.parent.name
        if meta.get("keep") is False:
            logger.info("标记为不保留，跳过: %s", md_path)
            return

        missing_fields = self._missing_reusable_fields(meta)
        if missing_fields:
            logger.warning("新生成 meta 仍不完整: %s missing=%s", rel_path, ",".join(missing_fields))

        try:
            self.file_store.save_json(meta_path, meta)
        except Exception as exc:
            logger.error("保存 meta 失败 %s: %s", meta_path, exc)
            return

        self.index_repository.update_index(meta, rel_path)
        logger.info("cleaned: %s", meta.get("title"))

    def clean(self) -> None:
        logger = logging.getLogger(__name__)
        logger.info("cleaning...")
        raw_root = Path(self.config.raw_dir)
        self.index_repository.reset_index()

        for md_path in sorted(raw_root.rglob("index.md"), key=lambda p: p.as_posix()):
            self._clean_one(md_path)

    def clean_paths(self, raw_paths: List[Any]) -> None:
        logger.info("cleaning selected paths...")

        seen = set()
        for raw_path in raw_paths:
            candidate = Path(str(raw_path).strip())
            if not candidate.is_absolute():
                candidate = self.config.raw_dir / candidate
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            self._clean_one(candidate)
