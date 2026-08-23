"""Markdown/paragraph-aware chunking with sentence fallback and overlap."""
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SENTENCE_RE = re.compile(r"[^。！？.!?]+(?:[。！？.!?]+|$)", re.S)
_BOUNDARY_RE = re.compile(r"[。！？.!?]\s*")


@dataclass(frozen=True)
class _Section:
    path: str
    content: str
    start: int


class StructuredChunker:
    """Prefer semantic boundaries and use fixed windows only as a final fallback."""

    def __init__(self, chunk_size: int = 500, overlap: int = 80):
        self.chunk_size = max(100, int(chunk_size))
        self.overlap = min(max(0, int(overlap)), self.chunk_size // 2)

    def chunk_document(self, document: Dict[str, Any]) -> List[Dict[str, Any]]:
        title = str(document.get("title") or "Untitled").strip()
        content = str(document.get("content") or "")
        source = str(document.get("source") or self._logical_source(title)).strip()
        raw_chunks: List[Tuple[str, str, int]] = []
        for section in self._sections(content, title):
            for chunk in self._chunk_section(section.content):
                normalized = self._normalize(chunk)
                if normalized:
                    raw_chunks.append((section.path, normalized, section.start))

        total = len(raw_chunks)
        chunks: List[Dict[str, Any]] = []
        for index, (section_path, chunk, start) in enumerate(raw_chunks):
            digest_input = f"{source}\n{section_path}\n{index}\n{chunk[:160]}"
            chunk_id = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]
            chunks.append({
                "chunk_id": chunk_id,
                "source": source,
                "title": title,
                "section_path": section_path,
                "chunk_index": index,
                "total_chunks": total,
                "start_position": start,
                "content": chunk,
            })
        return chunks

    def _sections(self, text: str, default_title: str) -> List[_Section]:
        headings: List[str] = []
        sections: List[_Section] = []
        buffer: List[str] = []
        section_start = 0
        position = 0

        def flush() -> None:
            content = "\n".join(buffer).strip()
            if content:
                sections.append(_Section(" / ".join(headings) or default_title, content, section_start))

        for line in text.splitlines(keepends=True):
            clean_line = line.rstrip("\r\n")
            match = _HEADING_RE.match(clean_line)
            if match:
                flush()
                buffer = []
                level = len(match.group(1))
                heading = match.group(2).strip()
                headings = headings[: level - 1]
                while len(headings) < level - 1:
                    headings.append(default_title)
                headings.append(heading)
                section_start = position + len(line)
            else:
                if not buffer:
                    section_start = position
                buffer.append(clean_line)
            position += len(line)
        flush()
        return sections or ([_Section(default_title, text.strip(), 0)] if text.strip() else [])

    def _chunk_section(self, text: str) -> List[str]:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
        units: List[str] = []
        for paragraph in paragraphs:
            if len(paragraph) <= self.chunk_size:
                units.append(paragraph)
            else:
                units.extend(self._split_long_paragraph(paragraph))
        return self._pack(units)

    def _split_long_paragraph(self, paragraph: str) -> List[str]:
        sentences = [match.group(0).strip() for match in _SENTENCE_RE.finditer(paragraph) if match.group(0).strip()]
        parts: List[str] = []
        for sentence in sentences or [paragraph]:
            if len(sentence) <= self.chunk_size:
                parts.append(sentence)
            else:
                step = max(1, self.chunk_size - self.overlap)
                parts.extend(
                    sentence[index:index + self.chunk_size]
                    for index in range(0, len(sentence), step)
                    if sentence[index:index + self.chunk_size].strip()
                )
        return parts

    def _pack(self, units: Sequence[str]) -> List[str]:
        chunks: List[str] = []
        current = ""
        for unit in units:
            separator = "\n\n" if current else ""
            if current and len(current) + len(separator) + len(unit) > self.chunk_size:
                chunks.append(current.strip())
                overlap = self._overlap_tail(current)
                available = self.chunk_size - len(unit) - (2 if overlap else 0)
                if available <= 0:
                    overlap = ""
                elif available < len(overlap):
                    overlap = overlap[-max(0, available):]
                current = f"{overlap}\n\n{unit}".strip() if overlap else unit
            else:
                current = f"{current}{separator}{unit}" if current else unit
        if current.strip():
            chunks.append(current.strip())
        return chunks

    def _overlap_tail(self, text: str) -> str:
        if not self.overlap or len(text) <= self.overlap:
            return "" if not self.overlap else text
        window = text[-min(len(text), self.overlap * 2):]
        candidates = [match.end() for match in _BOUNDARY_RE.finditer(window)]
        for boundary in candidates:
            suffix = window[boundary:].strip()
            if self.overlap // 2 <= len(suffix) <= self.overlap:
                return suffix
        return text[-self.overlap:].strip()

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[ \t]+", " ", text).strip()

    @staticmethod
    def _logical_source(title: str) -> str:
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
        return f"logical://{digest}/{title}"
