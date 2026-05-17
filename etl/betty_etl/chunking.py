"""
Betty ETL text chunking.

Splits long text into overlapping chunks suitable for embedding. Uses
character-based sizing (900 chars target, 120 char overlap) with
smart break detection at paragraph/sentence/whitespace boundaries
to avoid splitting mid-word.
"""

from __future__ import annotations
from dataclasses import dataclass
from betty_etl.config import CHUNK


@dataclass
class Chunk:
    """One chunk of text with its index and char-offset metadata."""

    index: int
    content: str
    start_char: int
    end_char: int

    @property
    def char_length(self) -> int:
        return len(self.content)


def find_break_point(text: str, target_end: int, lookback: int = 80) -> int:
    """Find a good place to end a chunk near position target_end."""
    if target_end >= len(text):
        return len(text)

    window_start = max(0, target_end - lookback)
    window = text[window_start:target_end]

    para_idx = window.rfind("\n\n")
    if para_idx != -1:
        return window_start + para_idx + 2

    best_sentence_end = -1
    for i in range(len(window) - 1, 0, -1):
        if window[i] in " \n" and window[i - 1] in ".!?":
            best_sentence_end = window_start + i + 1
            break
    if best_sentence_end != -1:
        return best_sentence_end

    ws_idx = window.rfind(" ")
    if ws_idx != -1:
        return window_start + ws_idx + 1
    nl_idx = window.rfind("\n")
    if nl_idx != -1:
        return window_start + nl_idx + 1

    return target_end


def chunk_text(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    """Split text into overlapping chunks."""
    size = chunk_size if chunk_size is not None else CHUNK.chunk_size
    over = overlap if overlap is not None else CHUNK.chunk_overlap

    if size <= 0:
        raise ValueError("chunk_size must be positive")
    if over < 0 or over >= size:
        raise ValueError("overlap must be >= 0 and < chunk_size")

    text = text.strip()
    if not text:
        return []

    chunks: list[Chunk] = []
    start = 0
    index = 0

    while start < len(text):
        target_end = min(start + size, len(text))
        end = find_break_point(text, target_end)

        if end <= start:
            end = target_end

        content = text[start:end].strip()
        if content:
            chunks.append(Chunk(
                index=index,
                content=content,
                start_char=start,
                end_char=end,
            ))
            index += 1

        if end >= len(text):
            break

        start = end - over
        if start < 0:
            start = 0

    return chunks


def _self_test() -> None:
    """Chunk the Stage 2 test PDF and print a summary."""
    from betty_etl.config import TEST_DATA_DIR
    from betty_etl.extractors.pdf import extract_pdf

    test_pdf = TEST_DATA_DIR / "attention-is-all-you-need.pdf"
    print(f"Extracting and chunking: {test_pdf}")

    doc = extract_pdf(test_pdf)
    chunks = chunk_text(doc.text_for_chunking)

    print(f"  Source chars:    {len(doc.text_for_chunking):,}")
    print(f"  Chunk count:     {len(chunks)}")
    print(f"  Avg chunk size:  {sum(c.char_length for c in chunks) / len(chunks):.0f} chars")
    print(f"  Min chunk size:  {min(c.char_length for c in chunks)} chars")
    print(f"  Max chunk size:  {max(c.char_length for c in chunks)} chars")
    print("  Self-test complete.")


if __name__ == "__main__":
    _self_test()
