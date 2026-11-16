"""Running text helpers (from brreg_parse_bench/text_extract.py): notes across page breaks, text/table order."""

import re

NUM = re.compile(r"(?<![\d,.])-?\d{1,3}(?:[  .]\d{3})+(?![\d])|(?<![\d,.])-?\d+(?![\d])")


def assign_notes(pages: list[list[dict]], kinds: list[str]) -> list[list[dict]]:
    """Give note-less blocks at the top of a notes page the note open at the end of the previous page.

    pages: blocks per page in page order; kinds: page_kind per page. Returns new blocks with 'note' filled in.
    """
    out, open_note = [], None
    for blocks, kind in zip(pages, kinds, strict=True):
        if kind != "noter":
            open_note = None
        page_out, seen_heading = [], False
        for b in blocks:
            if b["note"] is not None:
                seen_heading, open_note = True, b["note"]
            elif not seen_heading and kind == "noter" and open_note is not None:
                b = {**b, "note": open_note, "continued": True}
            page_out.append(b)
        out.append(page_out)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zæøå]", "", s.lower())


def _same_label(placeholder: str, table: dict) -> bool:
    a, b = _norm(placeholder), _norm(table["rows"][0]["label"]) if table["rows"] else ""
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def interleave(blocks: list[dict], tables: list[dict]) -> list[tuple[str, int]]:
    """Order text blocks and a note's tables as printed: [("text", i) | ("table", j)].

    A table placeholder matches the next unplaced table whose first row label it equals (or prefixes); tables the
    text pass didn't mark go right after the last matched one, so the page read's table order is always kept.
    """
    out, nxt = [], 0
    for i, b in enumerate(blocks):
        if b["kind"] != "table":
            out.append(("text", i))
            continue
        hit = next((j for j in range(nxt, len(tables)) if _same_label(b["text"], tables[j])), None)
        if hit is None:
            continue
        out += [("table", j) for j in range(nxt, hit + 1)]
        nxt = hit + 1
    return out + [("table", j) for j in range(nxt, len(tables))]


def text_numbers(text: str) -> list[int]:
    """Integers written in running text, grouped digits joined ('kr 8 608 984' → 8608984); decimals dropped."""
    text = re.sub(r"(\d),\d+", r"\1", text)
    return [int(re.sub(r"[  .]", "", m)) for m in NUM.findall(text)]
