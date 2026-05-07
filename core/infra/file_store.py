import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Union


logger = logging.getLogger(__name__)


def iter_raw_source_paths(raw_dir: Union[str, Path]) -> List[Path]:
    """枚举 raw_dir 下所有 source 路径（含 index.md 的目录 + 无 index.md 的 .md 目录）。"""
    raw_root = Path(raw_dir)
    indexed_dirs: set[Path] = set()
    result: List[Path] = []

    for md_path in sorted(raw_root.rglob("index.md"), key=lambda p: p.as_posix()):
        if md_path.is_file():
            indexed_dirs.add(md_path.parent)
            result.append(md_path)

    for md_path in sorted(raw_root.rglob("*.md"), key=lambda p: p.as_posix()):
        parent = md_path.parent
        if parent.is_dir() and parent not in indexed_dirs:
            indexed_dirs.add(parent)
            result.append(parent)

    return result


# 文件存储层：封装文本/JSON 的读写，统一处理异常与编码
class FileStore:
    def read_text(self, path: Union[str, Path]) -> str:
        """读取文本文件，失败时抛出可追踪异常。"""
        p = Path(path)
        try:
            logger.debug("读取文本文件: %s", p)
            return p.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            logger.error("文件不存在: %s", p)
            raise
        except PermissionError as exc:
            logger.error("无权限读取文件: %s", p)
            raise
        except OSError as exc:
            logger.error("读取文本文件失败: %s, %s", p, exc)
            raise

    def load_json(self, path: Union[str, Path]) -> Dict[str, Any]:
        """读取 JSON 文件；文件不存在、无权限或格式错误返回空字典。"""
        p = Path(path)
        if not p.exists():
            logger.debug("JSON 文件不存在，返回空对象: %s", p)
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("JSON 文件在读取时消失: %s", p)
            return {}
        except PermissionError:
            logger.error("无权限读取 JSON 文件: %s", p)
            return {}
        except json.JSONDecodeError as exc:
            logger.error("JSON 格式错误: %s, %s", p, exc)
            return {}
        except OSError as exc:
            logger.error("读取 JSON 文件失败: %s, %s", p, exc)
            return {}

    def save_text(self, path: Union[str, Path], content: str) -> None:
        """写入文本文件，自动补齐父目录。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(content, encoding="utf-8")
            logger.debug("文本已写入: %s", p)
        except PermissionError:
            logger.error("无权限写入文本文件: %s", p)
            raise
        except OSError as exc:
            logger.error("写入文本文件失败: %s, %s", p, exc)
            raise

    def append_text(self, path: Union[str, Path], content: str) -> None:
        """追加写入文本文件，自动补齐父目录。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
            logger.debug("文本已追加: %s", p)
        except PermissionError:
            logger.error("无权限追加文本文件: %s", p)
            raise
        except OSError as exc:
            logger.error("追加文本文件失败: %s, %s", p, exc)
            raise

    def save_json(self, path: Union[str, Path], data: Dict[str, Any]) -> None:
        """写入 JSON 文件，自动补齐父目录。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with p.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug("JSON 已写入: %s", p)
        except PermissionError:
            logger.error("无权限写入 JSON 文件: %s", p)
            raise
        except OSError as exc:
            logger.error("写入 JSON 文件失败: %s, %s", p, exc)
            raise
