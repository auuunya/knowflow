import json
from typing import Any, Dict, Optional


class SafeParser:
    @staticmethod
    def safe_json(text: Any) -> Optional[Dict[str, Any]]:
        """尽量将 LLM 文本转成字典，失败时返回 None。"""
        if not isinstance(text, str):
            return None

        try:
            return json.loads(text)
        except Exception:
            # 兼容 LLM 额外说明文本/代码块中的 JSON 片段
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                return None
