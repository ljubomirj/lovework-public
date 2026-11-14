"""The one cap on a filename component minted from free text.

eCryptfs (the encrypted home on host-a) stores at most **143 bytes** in a
single filename component: measured on this host, a 143-byte name is accepted
and 144 fails with `ENAMETOOLONG` (errno 36). That limit is bytes, not
characters; slugs are ASCII today, but the clip is byte-safe so it still holds
if a caller ever passes non-ASCII.

`MAX_FILENAME_BYTES = 120` leaves room for a `.md`/`.txt` suffix and a short
discriminator while staying well below the hard limit. Structured names whose
parts already bound their total (the date/org/role pack and case schemes) are
≤123 bytes including `-LoveWork-ATA`, still 20 bytes under the limit.
"""
from __future__ import annotations

#: Measured on host-a's ecryptfs home: a 143-byte name is accepted, 144
#: fails with ENAMETOOLONG (errno 36). The limit is bytes, per component.
ECRYPTFS_MAX_COMPONENT_BYTES = 143

MAX_FILENAME_BYTES = 120


def clip_stem(stem: str, *, limit: int = MAX_FILENAME_BYTES,
              strip: str = "._-") -> str:
    """Clip a filename stem to `limit` UTF-8 bytes, never splitting a char."""
    if len(stem.encode("utf-8")) <= limit:
        return stem
    clipped = stem.encode("utf-8")[:limit].decode("utf-8", "ignore")
    return clipped.rstrip(strip)
