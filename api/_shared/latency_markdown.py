"""Convert the Microsoft Docs Azure network-latency markdown into the model's
latency CSV.

Microsoft publishes the inter-region round-trip latency matrix as a markdown
article (``articles/networking/azure-network-latency.md`` in the public
``MicrosoftDocs/azure-docs`` repo), *not* as a downloadable CSV. The data lives
in many markdown tables — one per destination-region tab — each shaped like::

    | Source            | West Central US | West US | West US 2 |
    |---|---|---|---|
    | Australia Central | 167             | 145     | 167       |
    | ...               | ...             | ...     | ...       |

Every table lists the same source regions down the first column but a different
slice of destination regions across the header. Merging all of them yields the
full source × destination matrix. Region names are the human display names
("East US", "Australia Central"), which is exactly what the packaged latency
CSV already uses — so no name mapping is required.

:func:`markdown_to_latency_csv` stitches every ``Source`` table in the document
into a single wide CSV with a ``Source`` column followed by one column per
destination region, matching the format :mod:`dataset_store` validates.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Dict, List, Optional

# A markdown separator cell: ---, :---, ---:, :---: (with 2+ dashes).
_SEP_RE = re.compile(r"^:?-{2,}:?$")


def _split_row(line: str) -> Optional[List[str]]:
    """Split a markdown table row ``| a | b |`` into ``['a', 'b']``.

    Returns None for any line that isn't a pipe-delimited table row."""
    s = line.strip()
    if not s.startswith("|"):
        return None
    return [c.strip() for c in s.strip("|").split("|")]


def _is_separator(cells: Optional[List[str]]) -> bool:
    return bool(cells) and all(_SEP_RE.match(c or "") for c in cells if c != "")


def looks_like_markdown_latency(text: str) -> bool:
    """True if the text contains at least one markdown table whose first header
    cell is 'Source' (the shape of the Azure latency tables)."""
    for line in text.splitlines():
        cells = _split_row(line)
        if cells and len(cells) >= 2 and cells[0].strip().lower() == "source":
            return True
    return False


def markdown_to_latency_csv(text: str) -> str:
    """Parse every ``Source`` latency table in ``text`` and return one merged
    CSV string (``Source`` + one column per destination region).

    Raises ``ValueError`` if no latency tables are found."""
    lines = text.splitlines()
    n = len(lines)
    matrix: Dict[str, Dict[str, str]] = {}
    dest_seen: List[str] = []
    src_seen: List[str] = []

    i = 0
    while i < n:
        header = _split_row(lines[i])
        is_header = (
            header is not None
            and len(header) >= 2
            and header[0].strip().lower() == "source"
        )
        if not is_header:
            i += 1
            continue
        # A latency table's header must be followed by a separator row.
        sep = _split_row(lines[i + 1]) if i + 1 < n else None
        if not _is_separator(sep):
            i += 1
            continue

        dests = [d.strip() for d in header[1:]]
        for d in dests:
            if d and d not in dest_seen:
                dest_seen.append(d)

        j = i + 2
        while j < n:
            row = _split_row(lines[j])
            if not row or len(row) < 2 or _is_separator(row):
                break
            src = row[0].strip()
            if not src:
                break
            cells = matrix.setdefault(src, {})
            if src not in src_seen:
                src_seen.append(src)
            for k, d in enumerate(dests):
                if not d:
                    continue
                val = row[k + 1].strip() if (k + 1) < len(row) else ""
                if val:
                    cells[d] = val
            j += 1
        i = j

    if not matrix or not dest_seen:
        raise ValueError(
            "no 'Source' latency tables were found in the markdown document")

    dests_sorted = sorted(dest_seen)
    srcs_sorted = sorted(src_seen)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Source"] + dests_sorted)
    for s in srcs_sorted:
        w.writerow([s] + [matrix[s].get(d, "") for d in dests_sorted])
    return out.getvalue()
