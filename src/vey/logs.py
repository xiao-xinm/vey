from __future__ import annotations

from dataclasses import dataclass

from vey.security import redact


@dataclass
class LogPage:
    text: str
    remaining: list[str]
    pending: list[str]
    truncated: bool
    reason: str | None


def page_logs(
    records: list[str],
    requested: int = 100,
    max_chars: int = 10000,
    pending: list[str] | None = None,
) -> LogPage:
    """Consume latest records, preserving chronological order within a page.

    A long record is continued from its beginning before earlier records are read.
    Input must already be redacted before it is persisted in a cursor.
    """
    count = max(1, min(requested, 500))
    earlier = list(records) if pending else list(records[:-count])
    selected = pending if pending else records[-count:]
    parts: list[str] = []
    size = 0
    for index, record in enumerate(selected):
        prefix = "\n" if parts else ""
        room = max_chars - size - len(prefix)
        if room <= 0:
            # Continuation order is explicit: unread selected content first, then older data.
            return LogPage("\n".join(parts), earlier, selected[index:], True, "characters")
        piece = record[:room]
        parts.append(piece)
        size += len(prefix) + len(piece)
        if len(piece) < len(record):
            return LogPage(
                "\n".join(parts),
                earlier,
                [record[len(piece) :]] + selected[index + 1 :],
                True,
                "characters",
            )
    hard_limit = requested > 500 or (count == 500 and bool(earlier))
    return LogPage("\n".join(parts), earlier, [], hard_limit, "lines" if hard_limit else None)


def sanitize_records(records: list[str]) -> list[str]:
    # Redact multi-line credentials before splitting back into logical records.
    return redact("\n".join(records)).splitlines()
