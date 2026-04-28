import logging
import os
from functools import lru_cache
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate

logger = logging.getLogger(__name__)


# 提示词注册器：加载并缓存 PromptTemplate（按路径缓存，避免重复 I/O）
class PromptRegistry:
    PROMPT_FILES = {
        "clean_prompt": "clean-source.md",
        "fusion_prompt": "fuse-post.md",
        "policy_prompt": "reconcile-index.md",
        "split_prompt": "split-topics.md",
        "explain_prompt": "explain-concept.md",
        "title_prompt": "generate-title.md",
        "research_draft_prompt": "draft-research.md",
    }

    def __init__(self, config):
        self.config = config
        for attr, filename in self.PROMPT_FILES.items():
            setattr(self, attr, self._load_prompt(str(Path(self.config.prompt_template) / filename)))

    @staticmethod
    @lru_cache(maxsize=16)
    def _load_prompt(path: str) -> ChatPromptTemplate:
        """从磁盘加载并缓存 prompt 模板，避免重复读取。"""
        logger.debug("加载 prompt 模板: %s", path)
        with open(path, encoding="utf-8") as f:
            template = f.read()
        return ChatPromptTemplate.from_template(template)
