import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import AppConfig


class AppConfigTests(unittest.TestCase):
    def test_paths_resolve_against_knowledge_base_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_dir = Path(temp_dir) / "kb"
            kb_dir.mkdir()

            env = {
                "KNOWLEDGE_BASE_DIR": str(kb_dir),
                "RAW_DIR": "_raw",
                "CONTENT_DIR": "content",
                "WIKI_DIR": "wiki",
                "DATA_DIR": "data",
                "PROMPT_TEMPLATES": "prompts",
            }

            with patch.dict(os.environ, env, clear=False):
                config = AppConfig()

            self.assertEqual(config.knowledge_base_dir, kb_dir.resolve())
            self.assertEqual(config.raw_dir, (kb_dir / "_raw").resolve())
            self.assertEqual(config.content_dir, (kb_dir / "content").resolve())
            self.assertEqual(config.data_dir, (kb_dir / "data").resolve())
            self.assertEqual(config.posts_dir, (kb_dir / "content" / "posts").resolve())
            self.assertEqual(config.concepts_dir, (kb_dir / "wiki/concepts").resolve())
            self.assertEqual(config.sources_dir, (kb_dir / "wiki/sources").resolve())
            self.assertEqual(config.prompt_template, (config.repo_dir / "prompts").resolve())

    def test_meta_path_for_raw_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_dir = Path(temp_dir) / "kb"
            raw_path = kb_dir / "_raw" / "articles" / "systems" / "demo" / "index.md"
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text("# Demo\n", encoding="utf-8")

            env = {
                "KNOWLEDGE_BASE_DIR": str(kb_dir),
                "RAW_DIR": "_raw",
                "DATA_DIR": "data",
            }

            with patch.dict(os.environ, env, clear=False):
                config = AppConfig()

            expected = kb_dir / "data" / "knowflow" / "raw-meta" / "articles" / "systems" / "demo" / "meta.json"
            self.assertEqual(config.meta_path_for(raw_path.resolve()), expected.resolve())


if __name__ == "__main__":
    unittest.main()
