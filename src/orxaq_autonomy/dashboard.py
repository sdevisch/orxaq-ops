from __future__ import annotations

import os
from pathlib import Path


def _normalize_tail_slice(tail_slice: bytes) -> bytes:
    # Fast path for common LF-only logs to avoid per-line allocation work.
    if b"\r" not in tail_slice:
        return tail_slice
    # Fast path for common CRLF payloads read from Windows logs.
    if b"\r\n" in tail_slice:
        normalized = tail_slice.replace(b"\r\n", b"\n")
        if b"\r" not in normalized:
            return normalized
        tail_slice = normalized
    return b"\n".join(part.rstrip(b"\r") for part in tail_slice.split(b"\n"))


def _rstrip_newline_bytes(payload: bytes) -> bytes:
    # Avoid copying large buffers when there is no trailing newline to trim.
    if not payload or payload[-1] not in (10, 13):
        return payload
    return payload.rstrip(b"\r\n")


def tail_routing_activity(path: Path, lines: int = 40) -> str:
    if lines <= 0 or not path.exists():
        return ""

    chunk_size = 8192
    chunks: list[bytes] = []
    newline_count = 0
    target_newlines = lines
    trailing_newline: bool | None = None

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        while end > 0:
            read_size = min(chunk_size, end)
            end -= read_size
            handle.seek(end, os.SEEK_SET)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            if trailing_newline is None:
                trailing_newline = chunk.endswith((b"\n", b"\r"))
                target_newlines = lines if trailing_newline else max(lines - 1, 0)
            newline_count += chunk.count(b"\n")
            if newline_count >= target_newlines:
                if target_newlines == 0 and newline_count == 0 and end > 0:
                    # For a non-terminated single-line file, keep scanning until BOF.
                    continue
                if trailing_newline and lines == 1 and newline_count == 1 and end > 0:
                    # A lone trailing newline does not guarantee we captured the full line.
                    continue
                break

    collected = b"".join(reversed(chunks))
    if not collected:
        return ""
    collected = _rstrip_newline_bytes(collected)
    if not collected:
        return ""

    start = len(collected)
    remaining = lines
    while remaining > 0:
        idx = collected.rfind(b"\n", 0, start)
        if idx < 0:
            start = 0
            break
        start = idx
        remaining -= 1

    tail_slice = collected[start + 1 :] if remaining == 0 else collected
    normalized = _normalize_tail_slice(tail_slice)
    return normalized.decode("utf-8", errors="replace")
