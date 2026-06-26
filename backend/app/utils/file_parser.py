"""
Text chunking utility.
"""

from typing import List


def split_text_into_chunks(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50
) -> List[str]:
    """
    Split text into overlapping chunks.

    Args:
        text: source text
        chunk_size: characters per chunk
        overlap: overlap in characters between consecutive chunks

    Returns:
        list of text chunks
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        # try to split on a sentence boundary
        if end < len(text):
            # find the nearest sentence terminator
            for sep in ['。', '！', '？', '.\n', '!\n', '?\n', '\n\n', '. ', '! ', '? ']:
                last_sep = text[start:end].rfind(sep)
                if last_sep != -1 and last_sep > chunk_size * 0.3:
                    end = start + last_sep + len(sep)
                    break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        # next chunk starts at the overlap position
        start = end - overlap if end < len(text) else len(text)

    return chunks
