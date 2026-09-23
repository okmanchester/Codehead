"""Small, UTF-8-aware output limits shared by tools and their runtime."""

TRUNCATION_MARKER = "\n... [output truncated] ...\n"


def truncate_text(text: str, max_bytes: int) -> str:
    """Keep the beginning and end of text within a UTF-8 byte budget."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")

    marker = TRUNCATION_MARKER.encode("utf-8")
    if max_bytes < len(marker) + 2:
        raise ValueError("max_bytes is too small for a truncation marker")
    remaining = max_bytes - len(marker)
    head = encoded[: (remaining + 1) // 2].decode("utf-8", errors="ignore")
    tail = encoded[-(remaining // 2) :].decode("utf-8", errors="ignore")
    return head + TRUNCATION_MARKER + tail
