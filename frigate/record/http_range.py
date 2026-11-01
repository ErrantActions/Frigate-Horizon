"""Single byte ranges for completed immutable recording windows."""

import re
from email.utils import parsedate_to_datetime


class UnsatisfiableRange(ValueError):
    """A requested single range cannot be served."""


def media_range(
    size: int, value: str | None, if_range: str | None, etag: str, modified: int
) -> tuple[int, int, int]:
    """Return status and inclusive bounds, honoring strong If-Range validators."""
    if if_range:
        matches = if_range == etag
        if not matches and not if_range.startswith(('"', "W/")):
            try:
                matches = parsedate_to_datetime(if_range).timestamp() >= modified
            except (ValueError, TypeError, OverflowError):
                matches = False
        if not matches:
            value = None
    if value is None:
        return 200, 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or not any(match.groups()) or size <= 0:
        raise UnsatisfiableRange
    first, last = match.groups()
    try:
        if not first:
            length = int(last)
            if length <= 0:
                raise UnsatisfiableRange
            return 206, max(0, size - length), size - 1
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    except ValueError:
        raise UnsatisfiableRange from None
    if start >= size or end < start:
        raise UnsatisfiableRange
    return 206, start, end
