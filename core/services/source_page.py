import hashlib
import logging
import os
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.config import AppConfig
from core.infra.file_store import FileStore
from core.infra.index_repository import IndexRepository


logger = logging.getLogger(__name__)


class SourcePageService:
    def __init__(self, config: AppConfig, file_store: FileStore, index_repository: IndexRepository):
        self.config = config
        self.file_store = file_store
        self.index_repository = index_repository

    @staticmethod
    def _is_under(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _is_research_draft_dir(self, source_dir: Path) -> bool:
        return self._is_under(source_dir.resolve(), Path(self.config.research_drafts_dir).resolve())

    def _slug(self, value: str) -> str:
        return self.config.slugify(value, fallback="source")

    def _yaml_escape(self, value: Any) -> str:
        text = str(value or "").replace('"', '\\"').strip()
        return text

    def _strip_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    def _excerpt(self, text: str, limit: int = 360) -> str:
        cleaned = self._strip_text(text)
        if len(cleaned) <= limit:
            return cleaned or "暂无可展示的原文摘录。"
        return cleaned[: limit - 3].rstrip() + "..."

    def _render_entities(self, meta: Dict[str, Any]) -> str:
        entities = meta.get("entities", [])
        if not isinstance(entities, list) or not entities:
            return "- 未抽取到显式实体\n"

        lines: List[str] = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name") or "").strip()
            if not name:
                continue
            entity_type = str(entity.get("type") or "unknown").strip() or "unknown"
            summary = str(entity.get("summary") or "").strip()
            suffix = f"：{summary}" if summary else ""
            lines.append(f"- `{entity_type}` {name}{suffix}\n")

        return "".join(lines) if lines else "- 未抽取到显式实体\n"

    def _render_comparisons(self, meta: Dict[str, Any]) -> str:
        comparisons = meta.get("comparisons", [])
        if not isinstance(comparisons, list) or not comparisons:
            return "- 未抽取到明确对比关系\n"

        lines: List[str] = []
        for item in comparisons:
            if not isinstance(item, dict):
                continue
            left = str(item.get("left") or "").strip()
            right = str(item.get("right") or "").strip()
            if not left or not right:
                continue
            aspect = str(item.get("aspect") or "general").strip() or "general"
            relation = str(item.get("relation") or "compare").strip() or "compare"
            summary = str(item.get("summary") or "").strip()
            suffix = f"：{summary}" if summary else ""
            lines.append(f"- `{aspect}` {left} vs {right} (`{relation}`){suffix}\n")

        return "".join(lines) if lines else "- 未抽取到明确对比关系\n"

    def _collect_assets(self, source_dir: Path) -> List[str]:
        assets: List[str] = []
        for item in sorted(source_dir.iterdir(), key=lambda p: p.name):
            if not item.is_file():
                continue
            if item.name in {"index.md", "meta.json"} or item.name.startswith("."):
                continue
            assets.append(item.name)
        return assets

    def _asset_manifest(self, source_dir: Path, assets: List[str]) -> List[Dict[str, Any]]:
        manifest: List[Dict[str, Any]] = []
        for name in assets:
            path = source_dir / name
            if not path.is_file():
                continue
            stat = path.stat()
            manifest.append(
                {
                    "name": name,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        return manifest

    def _sync_bundle_assets(self, bundle_dir: Path, source_dir: Path, assets: List[str]) -> None:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        expected_names = set(assets)

        for item in sorted(bundle_dir.iterdir(), key=lambda p: p.name):
            if item.name in {"index.md"} or item.name.startswith("."):
                continue
            if item.name not in expected_names:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)

        for name in assets:
            src = source_dir / name
            dst = bundle_dir / name
            if not src.is_file():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def _load_meta(self, raw_path: Path) -> Dict[str, Any]:
        meta = self.file_store.load_json(self.config.meta_path_for(raw_path))
        return meta if isinstance(meta, dict) else {}

    def _topic_link(self, bundle_dir: Path, topic: str, topic_slug: str) -> str:
        if not topic or not topic_slug:
            return "未归类"

        concept_path = Path(self.config.concepts_dir) / f"{topic_slug}.md"
        relative = Path(os.path.relpath(concept_path, start=bundle_dir))
        return f"[{topic}]({relative.as_posix()})"

    def _calc_hash(
        self,
        rel_dir: str,
        meta: Dict[str, Any],
        raw_text: str,
        asset_manifest: List[Dict[str, Any]],
        canonical_topic: str,
    ) -> str:
        payload = {
            "schema": "source-v6",
            "rel_dir": rel_dir,
            "meta": meta,
            "raw_text": raw_text,
            "assets": asset_manifest,
            "canonical_topic": canonical_topic,
        }
        return hashlib.md5(repr(payload).encode("utf-8")).hexdigest()

    def _compose_markdown(
        self,
        bundle_dir: Path,
        title: str,
        slug: str,
        rel_dir: str,
        source_kind: str,
        meta: Dict[str, Any],
        index: Dict[str, Any],
        excerpt: str,
        assets: List[str],
    ) -> str:
        raw_topic = str(meta.get("topic") or "").strip()
        topic = self.index_repository.canonicalize_topic(index, raw_topic) or raw_topic
        topic_slug = self._slug(topic) if topic else ""
        topic_link = self._topic_link(bundle_dir, topic, topic_slug)
        summary = str(meta.get("summary") or "").strip() or "该来源暂无摘要。"
        raw_type = str(meta.get("type") or "unknown").strip() or "unknown"
        raw_path = f"{rel_dir}/index.md"
        assets_block = (
            "- 无额外附件\n"
            if not assets
            else "".join(f"- [{name}](<{name}>)\n" for name in assets)
        )
        frontmatter = (
            "---\n"
            f"title: \"{self._yaml_escape(title)}\"\n"
            f"date: {date.today().isoformat()}\n"
            "type: source\n"
            f"slug: {slug}\n"
            f"source_kind: \"{self._yaml_escape(source_kind)}\"\n"
            f"raw_type: \"{self._yaml_escape(raw_type)}\"\n"
            f"topic: \"{self._yaml_escape(topic)}\"\n"
            f"raw_path: \"{self._yaml_escape(raw_path)}\"\n"
            "draft: false\n"
            "---\n\n"
        )
        return (
            frontmatter
            + f"# {title}\n\n"
            + "## 概述\n"
            + f"{summary}\n\n"
            + "## 来源信息\n"
            + f"- 分类：`{source_kind}`\n"
            + f"- 原始类型：`{raw_type}`\n"
            + f"- 关联概念：{topic_link}\n\n"
            + "## 摘录\n"
            + f"{excerpt}\n\n"
            + "## 抽取到的实体\n"
            + self._render_entities(meta)
            + "\n"
            + "## 抽取到的对比\n"
            + self._render_comparisons(meta)
            + "\n"
            + "## 附件\n"
            + "当前自动同步到发布层的仅是该 Source 页原始目录中的附件。"
            + "如果未来 posts 直接保留 raw 相对路径图片，还需要单独补统一的资产发布链路。\n\n"
            + f"{assets_block}"
        )

    def _iter_source_dirs(self) -> List[Path]:
        raw_root = Path(self.config.raw_dir)
        source_dirs: List[Path] = []
        for raw_path in raw_root.rglob("index.md"):
            if not raw_path.is_file():
                continue
            if self._is_research_draft_dir(raw_path.parent):
                logger.info("skip research draft source page: %s", raw_path.parent)
                continue
            if self.config.meta_path_for(raw_path).is_file():
                source_dirs.append(raw_path.parent)
        return sorted(source_dirs, key=lambda p: p.as_posix())

    def _build_one(self, source_dir: Path, cache: Dict[str, str], index: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
        raw_path = source_dir / "index.md"
        rel_dir = source_dir.relative_to(self.config.raw_dir).as_posix()
        meta_path = self.config.meta_path_for(raw_path)
        meta = self._load_meta(raw_path)
        if not isinstance(meta, dict) or not meta:
            logger.warning("source meta 不可用，已跳过: %s", meta_path)
            return None

        if meta.get("keep") is False:
            logger.info("source keep=false，跳过: %s", rel_dir)
            return None

        try:
            raw_text = self.file_store.read_text(raw_path)
        except Exception as exc:
            logger.error("读取 source 原文失败: %s, %s", raw_path, exc)
            return None

        title = str(meta.get("title") or source_dir.name).strip() or source_dir.name
        slug = self.config.source_slug_for(raw_path)
        source_kind = rel_dir.split("/", 1)[0] if "/" in rel_dir else rel_dir
        bundle_dir = Path(self.config.sources_dir) / slug
        assets = self._collect_assets(source_dir)
        asset_manifest = self._asset_manifest(source_dir, assets)
        excerpt = self._excerpt(raw_text)
        canonical_topic = self.index_repository.canonicalize_topic(index, meta.get("topic")) or str(meta.get("topic") or "").strip()
        content = self._compose_markdown(bundle_dir, title, slug, rel_dir, source_kind, meta, index, excerpt, assets)
        new_hash = self._calc_hash(rel_dir, meta, raw_text, asset_manifest, canonical_topic)
        out_path = bundle_dir / "index.md"
        cache_key = str(out_path)

        if cache.get(cache_key) == new_hash and out_path.exists():
            self._sync_bundle_assets(bundle_dir, source_dir, assets)
            logger.info("skip source page (cache hit): %s", rel_dir)
            return cache_key, new_hash, str(bundle_dir)

        self.file_store.save_text(out_path, content)
        self._sync_bundle_assets(bundle_dir, source_dir, assets)
        logger.info("generated source page: %s", out_path)
        return cache_key, new_hash, str(bundle_dir)

    def build_sources(self) -> None:
        logger.info("building source pages...")
        sources_dir = Path(self.config.sources_dir)
        sources_dir.mkdir(parents=True, exist_ok=True)
        index = self.index_repository.load_index()

        cache = self.file_store.load_json(self.config.sources_cache_path)
        if not isinstance(cache, dict):
            cache = {}

        new_cache: Dict[str, str] = {}
        expected_paths: Set[str] = set()
        expected_bundle_dirs: Set[str] = set()

        for source_dir in self._iter_source_dirs():
            result = self._build_one(source_dir, cache, index)
            if not result:
                continue
            cache_key, new_hash, bundle_dir = result
            new_cache[cache_key] = new_hash
            expected_paths.add(cache_key)
            expected_bundle_dirs.add(bundle_dir)

        existing_paths = {str(path) for path in sources_dir.glob("*/index.md") if not path.parent.name.startswith(".")}
        stale_paths = sorted(existing_paths - expected_paths)
        for stale_path in stale_paths:
            try:
                Path(stale_path).unlink()
                logger.info("removed stale source page: %s", stale_path)
            except OSError:
                logger.exception("删除过期 source 页面失败: %s", stale_path)

        existing_bundle_dirs = {
            str(path)
            for path in sources_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        }
        stale_bundle_dirs = sorted(existing_bundle_dirs - expected_bundle_dirs)
        for stale_bundle_dir in stale_bundle_dirs:
            try:
                shutil.rmtree(stale_bundle_dir)
                logger.info("removed stale source bundle: %s", stale_bundle_dir)
            except OSError:
                logger.exception("删除过期 source bundle 失败: %s", stale_bundle_dir)

        self.file_store.save_json(self.config.sources_cache_path, new_cache)
