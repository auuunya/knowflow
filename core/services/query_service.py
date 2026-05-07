import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.infra.file_store import iter_raw_source_paths


class QueryService:
    _FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?", re.S)
    _TOKEN_RE = re.compile(r"[^\s,;，；]+")

    def __init__(self, config, file_store):
        self.config = config
        self.file_store = file_store

    def _parse_frontmatter(self, text: str) -> Dict[str, Any]:
        match = self._FRONTMATTER_RE.match(text or "")
        if not match:
            return {}

        frontmatter: Dict[str, Any] = {}
        current_list_key: Optional[str] = None

        for raw_line in match.group(1).splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("- ") and current_list_key:
                value = stripped[2:].strip().strip('"').strip("'")
                if value:
                    frontmatter.setdefault(current_list_key, []).append(value)
                continue

            if ":" not in line:
                current_list_key = None
                continue

            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                current_list_key = None
                continue

            if not value:
                frontmatter[key] = []
                current_list_key = key
                continue

            current_list_key = None
            frontmatter[key] = value.strip('"').strip("'")

        return frontmatter

    def _strip_frontmatter(self, text: str) -> str:
        return self._FRONTMATTER_RE.sub("", text or "", count=1).strip()

    @staticmethod
    def _normalize(text: Any) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip().lower()

    def _tokens(self, query: str) -> List[str]:
        tokens = [self._normalize(item.group(0)) for item in self._TOKEN_RE.finditer(query or "")]
        return [item for item in tokens if item]

    def _score_field(self, value: str, terms: List[str], weight: int) -> int:
        haystack = self._normalize(value)
        if not haystack:
            return 0
        return sum(weight for term in terms if term and term in haystack)

    def _excerpt(self, text: str, terms: List[str], limit: int = 100) -> str:
        body = re.sub(r"\s+", " ", text or "").strip()
        if not body:
            return ""

        lowered = body.lower()
        hit_positions = [lowered.find(term) for term in terms if term and lowered.find(term) >= 0]
        if not hit_positions:
            return body[:limit] + ("..." if len(body) > limit else "")

        start = max(0, min(hit_positions) - 24)
        end = min(len(body), start + limit)
        snippet = body[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(body):
            snippet += "..."
        return snippet

    def _score_item(self, item: Dict[str, Any], terms: List[str], phrase: str) -> Optional[Dict[str, Any]]:
        title = str(item.get("title") or "")
        summary = str(item.get("summary") or "")
        body = str(item.get("body") or "")
        meta_text = " ".join(str(part or "") for part in item.get("meta_fields", []))

        score = 0
        score += self._score_field(title, terms, 8)
        score += self._score_field(summary, terms, 5)
        score += self._score_field(meta_text, terms, 3)
        score += self._score_field(body, terms, 1)

        phrase_norm = self._normalize(phrase)
        if phrase_norm:
            if phrase_norm in self._normalize(title):
                score += 8
            if phrase_norm in self._normalize(summary):
                score += 5
            if phrase_norm in self._normalize(meta_text):
                score += 3
            if phrase_norm in self._normalize(body):
                score += 2

        corpus = " ".join([title, summary, meta_text, body]).lower()
        matched_terms = [term for term in terms if term in corpus]
        if not matched_terms:
            return None

        score += len(set(matched_terms)) * 2
        item["score"] = score
        item["snippet"] = self._excerpt(summary or body, terms)
        return item

    def _load_markdown_dir(self, section_dir: Path, kind: str, public_url_prefix: str = "") -> List[Dict[str, Any]]:
        if not section_dir.exists():
            return []

        items: List[Dict[str, Any]] = []
        for path in sorted(section_dir.rglob("*.md")):
            if path.name == "_index.md" or path.name.startswith("."):
                continue
            text = self.file_store.read_text(path)
            meta = self._parse_frontmatter(text)
            body = self._strip_frontmatter(text)
            slug = str(meta.get("slug") or path.stem).strip() or path.stem
            items.append(
                {
                    "kind": kind,
                    "title": str(meta.get("title") or path.stem).strip() or path.stem,
                    "path": str(path.relative_to(self.config.knowledge_base_dir)),
                    "url": f"{public_url_prefix}{slug}/" if public_url_prefix else "",
                    "summary": body[:240],
                    "body": body,
                    "meta_fields": [
                        slug,
                        str(meta.get("topic") or ""),
                        str(meta.get("source_kind") or ""),
                        str(meta.get("raw_type") or ""),
                        " ".join(meta.get("concepts", [])) if isinstance(meta.get("concepts"), list) else "",
                        " ".join(meta.get("sources", [])) if isinstance(meta.get("sources"), list) else "",
                    ],
                }
            )
        return items

    def _load_json_dir(self, base_dir: Path, kind: str, title_fields: List[str], meta_fields: List[str]) -> List[Dict[str, Any]]:
        if not base_dir.exists():
            return []

        items: List[Dict[str, Any]] = []
        for path in sorted(base_dir.glob("*.json")):
            if not path.is_file():
                continue
            data = self.file_store.load_json(path)
            if not isinstance(data, dict) or not data:
                continue

            title = " / ".join(str(data.get(field) or "").strip() for field in title_fields if str(data.get(field) or "").strip())
            items.append(
                {
                    "kind": kind,
                    "title": title or path.stem,
                    "path": str(path.relative_to(self.config.knowledge_base_dir)),
                    "url": "",
                    "summary": str(data.get("summary") or "").strip(),
                    "body": "",
                    "meta_fields": [str(data.get(field) or "") for field in meta_fields] + [path.stem],
                }
            )
        return items

    def _load_index_topics(self) -> List[Dict[str, Any]]:
        index = self.file_store.load_json(self.config.index_path)
        topics = index.get("topics", {}) if isinstance(index, dict) else {}
        if not isinstance(topics, dict):
            return []

        items: List[Dict[str, Any]] = []
        for topic, data in sorted(topics.items()):
            if not isinstance(topic, str) or not isinstance(data, dict):
                continue
            items.append(
                {
                    "kind": "topic",
                    "title": topic,
                    "path": str(Path(self.config.index_path).relative_to(self.config.knowledge_base_dir)),
                    "url": "",
                    "summary": str(data.get("summary") or "").strip(),
                    "body": "",
                    "meta_fields": [
                        " ".join(data.get("files", [])) if isinstance(data.get("files"), list) else "",
                        " ".join(data.get("entities", [])) if isinstance(data.get("entities"), list) else "",
                        " ".join(data.get("comparisons", [])) if isinstance(data.get("comparisons"), list) else "",
                    ],
                }
            )
        return items

    def _load_raw_sources(self) -> List[Dict[str, Any]]:
        raw_root = Path(self.config.raw_dir)
        if not raw_root.exists():
            return []

        items: List[Dict[str, Any]] = []
        for path in iter_raw_source_paths(raw_root):
            source_dir = path.parent if path.is_file() else path
            rel_dir = str(source_dir.relative_to(raw_root))
            if rel_dir.startswith("research"):
                continue
            if path.is_file():
                text = self.file_store.read_text(path)
            else:
                md_files = sorted(path.glob("*.md"), key=lambda p: p.name)
                if not md_files:
                    continue
                if len(md_files) == 1:
                    text = self.file_store.read_text(md_files[0])
                else:
                    parts = [self.file_store.read_text(f) for f in md_files]
                    text = "\n\n".join(parts)
            rel_path = str(source_dir.relative_to(self.config.knowledge_base_dir))
            items.append(
                {
                    "kind": "raw",
                    "title": source_dir.name,
                    "path": rel_path,
                    "url": "",
                    "summary": text[:240],
                    "body": text,
                    "meta_fields": [rel_dir],
                }
            )
        return items

    def query(self, query: str, limit: int = 12) -> List[Dict[str, Any]]:
        phrase = str(query or "").strip()
        terms = self._tokens(phrase)
        if not terms:
            return []

        corpus: List[Dict[str, Any]] = []
        corpus.extend(self._load_markdown_dir(Path(self.config.posts_dir), "post", "/posts/"))
        corpus.extend(self._load_markdown_dir(Path(self.config.concepts_dir), "concept"))
        corpus.extend(self._load_markdown_dir(Path(self.config.sources_dir), "source"))
        corpus.extend(
            self._load_json_dir(
                Path(self.config.entities_dir),
                "entity",
                ["name"],
                ["type", "summary", "topics", "aliases"],
            )
        )
        corpus.extend(
            self._load_json_dir(
                Path(self.config.comparisons_dir),
                "comparison",
                ["left", "right", "aspect"],
                ["relation", "summary", "topics"],
            )
        )
        corpus.extend(self._load_index_topics())
        corpus.extend(self._load_raw_sources())

        hits: List[Dict[str, Any]] = []
        for item in corpus:
            scored = self._score_item(dict(item), terms, phrase)
            if scored:
                hits.append(scored)

        kind_order = {"post": 0, "concept": 1, "source": 2, "entity": 3, "comparison": 4, "topic": 5, "raw": 6}
        hits.sort(key=lambda item: (-int(item.get("score", 0)), kind_order.get(str(item.get("kind")), 9), str(item.get("title", ""))))
        return hits[:limit]

    def print_query(self, query: str, limit: int = 12) -> int:
        hits = self.query(query, limit=limit)
        phrase = str(query or "").strip()

        print(f"query: {phrase}")
        if not hits:
            print("no matches")
            return 1

        for index, item in enumerate(hits, start=1):
            kind = str(item.get("kind") or "item")
            title = str(item.get("title") or "").strip()
            path = str(item.get("path") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            score = int(item.get("score") or 0)

            print(f"{index}. [{kind}] {title} (score={score})")
            if url:
                print(f"   url: {url}")
            if path:
                print(f"   path: {path}")
            if snippet:
                print(f"   snippet: {snippet}")

        return 0
