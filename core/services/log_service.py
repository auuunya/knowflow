import logging
from datetime import datetime
from pathlib import Path
from typing import Dict

from core.config import AppConfig
from core.infra.file_store import FileStore, iter_raw_source_paths


logger = logging.getLogger(__name__)


class LogService:
    def __init__(self, config: AppConfig, file_store: FileStore):
        self.config = config
        self.file_store = file_store

    def _count_pages(self, section_dir: Path) -> int:
        if not section_dir.exists():
            return 0
        return sum(
            1
            for path in section_dir.rglob("*.md")
            if path.is_file() and path.name != "_index.md" and not path.name.startswith(".")
        )

    def _count_raw_sources(self) -> int:
        raw_root = Path(self.config.raw_dir)
        count = 0
        for path in iter_raw_source_paths(raw_root):
            rel_dir = path.parent.relative_to(raw_root).as_posix() if path.is_file() else path.relative_to(raw_root).as_posix()
            if not rel_dir.startswith("research"):
                count += 1
        return count

    def _collect_stats(self) -> Dict[str, int]:
        return {
            "raw_sources": self._count_raw_sources(),
            "posts": self._count_pages(Path(self.config.posts_dir)),
            "concepts": self._count_pages(Path(self.config.concepts_dir)),
            "sources": self._count_pages(Path(self.config.sources_dir)),
        }

    def append_run(self, command: str) -> None:
        stats = self._collect_stats()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = (
            f"- {timestamp}：`{command}` 完成；"
            f" raw=`{stats['raw_sources']}`，"
            f" posts=`{stats['posts']}`，"
            f" wiki-concepts=`{stats['concepts']}`，"
            f" wiki-sources=`{stats['sources']}`。\n"
        )

        path = Path(self.config.log_path)
        if path.exists():
            content = self.file_store.read_text(path)
        else:
            content = "# knowflow log\n\n这是内部构建日志，不在 Hugo 内容层公开。\n"

        if not content.endswith("\n"):
            content += "\n"
        if not content.endswith("\n\n"):
            content += "\n"
        content += entry
        self.file_store.save_text(path, content)
        logger.info("log updated: %s", path)
