import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import AppConfig
from core.infra.file_store import FileStore
from core.services.query_service import QueryService


class QueryServiceTests(unittest.TestCase):
    def _build_config(self, kb_dir: Path) -> AppConfig:
        env = {
            "KNOWLEDGE_BASE_DIR": str(kb_dir),
            "RAW_DIR": "_raw",
            "CONTENT_DIR": "content",
            "WIKI_DIR": "wiki",
            "DATA_DIR": "data",
        }

        with patch.dict(os.environ, env, clear=False):
            return AppConfig()

    def test_query_finds_raw_source_in_knowledge_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_dir = Path(temp_dir) / "kb"
            raw_note = kb_dir / "_raw" / "articles" / "systems" / "compiler-demo" / "index.md"
            raw_note.parent.mkdir(parents=True)
            raw_note.write_text(
                "# Compiler Demo\n\nA knowledge compiler turns rough notes into reusable wiki pages.\n",
                encoding="utf-8",
            )

            config = self._build_config(kb_dir)
            service = QueryService(config, FileStore())

            results = service.query("compiler")

            self.assertTrue(results)
            self.assertEqual(results[0]["kind"], "raw")
            self.assertEqual(results[0]["path"], "_raw/articles/systems/compiler-demo")

    def test_query_skips_generated_research_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_dir = Path(temp_dir) / "kb"

            normal_note = kb_dir / "_raw" / "articles" / "systems" / "queryable" / "index.md"
            normal_note.parent.mkdir(parents=True)
            normal_note.write_text("# Queryable\n\nsearchable note\n", encoding="utf-8")

            generated_note = kb_dir / "_raw" / "research" / "hidden" / "index.md"
            generated_note.parent.mkdir(parents=True)
            generated_note.write_text("# Hidden\n\nshould not be returned by query\n", encoding="utf-8")

            config = self._build_config(kb_dir)
            service = QueryService(config, FileStore())

            results = service.query("hidden")

            self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
