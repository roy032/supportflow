"""The shop's policy documents, split into sections and searched by word overlap.

Shops have a handful of short policy pages, so this deliberately avoids an embedding
model and a vector store: every `##` section is a chunk, and the chunks sharing the most
words with the customer's message are handed to the model. Swapping in embeddings later
only means replacing `search`.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("supportflow.knowledge")

# Letters and digits in any script, so Bangla words are tokens too.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for", "from", "has", "have",
    "i", "if", "in", "is", "it", "me", "my", "of", "on", "or", "our", "please", "so", "that",
    "the", "this", "to", "was", "we", "what", "when", "where", "will", "with", "you", "your",
}


@dataclass(frozen=True)
class Section:
    source: str
    title: str
    text: str


def _words(text: str) -> set[str]:
    words = set()
    for word in _WORD.findall(text.lower()):
        if word in _STOPWORDS or len(word) < 2:
            continue
        words.add(word)
        # Crude stemming so "refunded"/"refunds" meet "refund".
        for suffix in ("ing", "ed", "es", "s"):
            if len(word) > len(suffix) + 3 and word.endswith(suffix):
                words.add(word[: -len(suffix)])
                break
    return words


class KnowledgeBase:
    def __init__(self, sections: list[Section]) -> None:
        self.sections = sections
        self._index = [_words(f"{s.title} {s.text}") for s in sections]

    @classmethod
    def load(cls, directory: str | Path) -> "KnowledgeBase":
        path = Path(directory)
        sections: list[Section] = []
        if not path.is_dir():
            logger.warning("knowledge directory %s not found; drafts will have no policies", path)
            return cls(sections)
        for file in sorted(path.glob("*.md")):
            if file.name.lower() == "readme.md":
                continue
            sections.extend(split_sections(file.name, file.read_text(encoding="utf-8")))
        logger.info("loaded %s knowledge sections from %s", len(sections), path)
        return cls(sections)

    def search(self, query: str, limit: int = 4) -> list[Section]:
        query_words = _words(query)
        scored = [
            (len(query_words & words), i) for i, words in enumerate(self._index)
        ]
        scored = [(score, i) for score, i in scored if score > 0]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [self.sections[i] for _, i in scored[:limit]]


def split_sections(source: str, markdown: str) -> list[Section]:
    """One Section per `##` heading; text before the first one is ignored."""
    sections = []
    title = None
    lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if title is not None:
                sections.append(Section(source, title, "\n".join(lines).strip()))
            title, lines = line[3:].strip(), []
        elif title is not None:
            lines.append(line)
    if title is not None:
        sections.append(Section(source, title, "\n".join(lines).strip()))
    return [s for s in sections if s.text]
