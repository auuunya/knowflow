import hashlib
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict


# 应用配置类：统一管理目录、LLM 与运行参数，并从 .env 读取环境变量
class AppConfig:
    _NON_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)
    _MULTI_DASH_RE = re.compile(r"-{2,}")

    def __init__(self):
        # 目录布局：core/ -> 仓库根目录；知识库根目录通过 .env 指定
        self.core_dir = Path(__file__).resolve().parent
        self.repo_dir = self.core_dir.parent

        # 先加载仓库根目录的 .env（不覆盖系统已存在的环境变量）
        self._load_env(self.repo_dir / ".env")

        # 知识库根目录：相对路径统一相对仓库根目录解析
        self.knowledge_base_dir = self._resolve_repo_path(os.getenv("KNOWLEDGE_BASE_DIR", "."))

        # 顶层目录配置：只暴露 root 级目录，其他路径统一按约定推导
        self.raw_dir = self._resolve_knowledge_path(os.getenv("RAW_DIR", "_raw"))
        self.content_dir = self._resolve_knowledge_path(os.getenv("CONTENT_DIR", "content"))
        self.wiki_dir = self._resolve_knowledge_path(os.getenv("WIKI_DIR", "wiki"))
        self.data_dir = self._resolve_knowledge_path(os.getenv("DATA_DIR", "data"))
        self.prompt_template = self._resolve_internal_path(os.getenv("PROMPT_TEMPLATES", "prompts"))

        # 规范化派生路径
        self.posts_dir = (self.content_dir / "posts").resolve()
        self.concepts_dir = (self.wiki_dir / "concepts").resolve()
        self.sources_dir = (self.wiki_dir / "sources").resolve()
        self.insights_report_path = (self.wiki_dir / "insights.md").resolve()
        self.log_path = (self.wiki_dir / "log.md").resolve()

        knowflow_data_dir = (self.data_dir / "knowflow").resolve()
        self.metadata_dir = (knowflow_data_dir / "raw-meta").resolve()
        self.index_path = (knowflow_data_dir / "index.json").resolve()
        self.cache_path = (knowflow_data_dir / "compile-cache.json").resolve()
        self.concepts_cache_path = (knowflow_data_dir / "concepts-cache.json").resolve()
        self.sources_cache_path = (knowflow_data_dir / "sources-cache.json").resolve()
        self.entities_dir = (knowflow_data_dir / "entities").resolve()
        self.comparisons_dir = (knowflow_data_dir / "comparisons").resolve()
        self.insights_path = (knowflow_data_dir / "insights.json").resolve()
        self.research_drafts_manifest_path = (knowflow_data_dir / "research-drafts.json").resolve()

        self.public_knowledge_graph_path = (self.data_dir / "knowledge-graph.public.json").resolve()
        self.private_knowledge_graph_path = (self.data_dir / "knowledge-graph.private.json").resolve()
        self.research_drafts_dir = (self.raw_dir / "research").resolve()
        self.research_queue_raw_path = (self.research_drafts_dir / "knowflow-research-queue.md").resolve()

        # LLM 配置
        self.llm_base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1/")
        self.model_name = os.getenv("MODEL_NAME", "Qwen/Qwen3-14B-AWQ")
        self.llm_api_key = os.getenv("LLM_API_KEY", "")
        self.temperature = self._parse_float(os.getenv("TEMPERATURE"), default=0.0)
        self.llm_timeout_seconds = self._parse_float(os.getenv("LLM_TIMEOUT_SECONDS"), default=180.0)
        self.llm_max_retries = self._parse_int(os.getenv("LLM_MAX_RETRIES"), default=2)

        # 执行行为配置
        self.raw_desensitize = self._parse_bool(os.getenv("RAW_DESENSITIZE"), default=False)
        self.clean_skip_existing = self._parse_bool(os.getenv("CLEAN_SKIP_EXISTING"), default=True)
        self.compiler_workers = self._parse_int(os.getenv("COMPILER_WORKERS"), default=4)
        self.research_draft_max_notes = self._parse_int(os.getenv("RESEARCH_DRAFT_MAX_NOTES"), default=8)
        self.research_draft_overwrite_existing = self._parse_bool(
            os.getenv("RESEARCH_DRAFT_OVERWRITE_EXISTING"),
            default=False,
        )
        self.log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    def _resolve_repo_path(self, value: Any) -> Path:
        """将路径字符串转换为绝对路径：相对路径默认挂到仓库根目录。"""
        p = Path(str(value))
        if not p.is_absolute():
            p = self.repo_dir / p
        return p.resolve()

    def _resolve_knowledge_path(self, value: Any) -> Path:
        """将路径字符串转换为绝对路径：相对路径默认挂到知识库根目录。"""
        p = Path(str(value))
        if not p.is_absolute():
            p = self.knowledge_base_dir / p
        return p.resolve()

    def _resolve_internal_path(self, value: Any) -> Path:
        """将内部资源路径转换为绝对路径：相对路径统一相对仓库根目录解析。"""
        p = Path(str(value))
        if p.is_absolute():
            return p.resolve()
        return (self.repo_dir / p).resolve()

    def _parse_bool(self, value: Any, default: bool = False) -> bool:
        """解析布尔型环境变量字符串。"""
        if value is None:
            return default
        if isinstance(value, bool):
            return value

        normalized = str(value).strip().lower()
        truthy = {"1", "true", "t", "yes", "y", "on"}
        falsy = {"0", "false", "f", "no", "n", "off"}

        if normalized in truthy:
            return True
        if normalized in falsy:
            return False
        return default

    def _parse_int(self, value: Any, default: int = 0) -> int:
        """解析整数型环境变量，失败时返回默认值。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _parse_float(self, value: Any, default: float = 0.0) -> float:
        """解析浮点型环境变量，失败时返回默认值。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def as_dict(self) -> Dict[str, str]:
        """返回可序列化的配置快照，便于日志/调试。"""
        return {
            "repo_dir": str(self.repo_dir),
            "knowledge_base_dir": str(self.knowledge_base_dir),
            "raw_dir": str(self.raw_dir),
            "content_dir": str(self.content_dir),
            "wiki_dir": str(self.wiki_dir),
            "data_dir": str(self.data_dir),
            "metadata_dir": str(self.metadata_dir),
            "posts_dir": str(self.posts_dir),
            "concepts_dir": str(self.concepts_dir),
            "sources_dir": str(self.sources_dir),
            "entities_dir": str(self.entities_dir),
            "comparisons_dir": str(self.comparisons_dir),
            "prompt_template": str(self.prompt_template),
            "index_path": str(self.index_path),
            "cache_path": str(self.cache_path),
            "concepts_cache_path": str(self.concepts_cache_path),
            "sources_cache_path": str(self.sources_cache_path),
            "public_knowledge_graph_path": str(self.public_knowledge_graph_path),
            "private_knowledge_graph_path": str(self.private_knowledge_graph_path),
            "insights_path": str(self.insights_path),
            "insights_report_path": str(self.insights_report_path),
            "research_queue_raw_path": str(self.research_queue_raw_path),
            "research_drafts_dir": str(self.research_drafts_dir),
            "research_drafts_manifest_path": str(self.research_drafts_manifest_path),
            "log_path": str(self.log_path),
            "llm_base_url": self.llm_base_url,
            "model_name": self.model_name,
            "temperature": str(self.temperature),
            "llm_timeout_seconds": str(self.llm_timeout_seconds),
            "llm_max_retries": str(self.llm_max_retries),
            "raw_desensitize": str(self.raw_desensitize),
            "clean_skip_existing": str(self.clean_skip_existing),
            "compiler_workers": str(self.compiler_workers),
            "research_draft_max_notes": str(self.research_draft_max_notes),
            "research_draft_overwrite_existing": str(self.research_draft_overwrite_existing),
            "log_level": self.log_level,
        }

    def raw_rel_path(self, path: Any) -> Path:
        """将 raw 下的绝对路径或相对路径转换为相对 RAW_DIR 的路径。"""
        candidate = Path(str(path))
        if candidate.is_absolute():
            return candidate.resolve().relative_to(self.raw_dir)
        return candidate

    def raw_rel_dir(self, path: Any) -> Path:
        """返回原始文档目录相对路径；输入可为 index.md 或目录本身。"""
        rel_path = self.raw_rel_path(path)
        return rel_path.parent if rel_path.name == "index.md" else rel_path

    def slugify(self, value: Any, fallback: str = "item") -> str:
        """将任意文本转换为稳定、可发布的 slug。"""
        text = unicodedata.normalize("NFKC", str(value or "").strip().lower())
        slug = self._NON_SLUG_RE.sub("-", text)
        slug = slug.replace("_", "-")
        slug = self._MULTI_DASH_RE.sub("-", slug).strip("-")
        return slug or fallback

    def meta_path_for(self, path: Any) -> Path:
        """返回原始文档对应的 metadata 路径。"""
        return self.metadata_dir / self.raw_rel_dir(path) / "meta.json"

    def source_slug_for(self, path: Any) -> str:
        """根据 raw 相对目录生成稳定且低碰撞的 source slug。"""
        rel_dir = self.raw_rel_dir(path).as_posix()
        base = self.slugify(rel_dir, fallback="source")
        digest = hashlib.md5(rel_dir.encode("utf-8")).hexdigest()[:8]
        return f"{base}-{digest}"

    def _load_env(self, env_path: Path) -> None:
        """从 .env 读取 KEY=VALUE，不覆盖已存在的环境变量，支持行内注释。"""
        if not env_path.is_file():
            return

        with env_path.open(encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                # 空行或注释行直接跳过
                if not line or line.startswith("#"):
                    continue

                line = self._strip_env_inline_comment(line)

                if not line or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")

                # 系统环境变量优先生效
                os.environ.setdefault(key, value)

    @staticmethod
    def _strip_env_inline_comment(line: str) -> str:
        """仅移除引号外、且前面带空白的 # 注释，避免截断真实值。"""
        in_single = False
        in_double = False
        previous = ""

        for index, char in enumerate(line):
            if char == "'" and not in_double:
                in_single = not in_single
            elif char == '"' and not in_single:
                in_double = not in_double
            elif char == "#" and not in_single and not in_double:
                if index == 0 or previous.isspace():
                    return line[:index].rstrip()
            previous = char

        return line


# 全局配置单例
CONFIG = AppConfig()
