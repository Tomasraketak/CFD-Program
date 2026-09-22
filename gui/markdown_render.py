"""Turn the assistant's Markdown into HTML the chat transcript can show.

Models answer in Markdown -- tables of results, bold figures, bullet lists --
and the transcript used to print it verbatim, so a table of forces arrived as
rows of pipes and dashes. This converts the subset models actually use into
Qt rich text, with tables drawn as tables.

Everything is HTML-escaped before any markup is added: the text comes from a
remote model and must never be able to inject markup of its own.
"""

from __future__ import annotations

import html
import re

from gui.theme import ACCENT, BORDER, SURFACE_RAISED, TEXT, TEXT_MUTED

_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
# Something that reads as a number, so table cells holding one line up right.
_NUMERIC = re.compile(r"^[−\-+]?\s*[\d.,\s]+\s*[%°a-zA-Z/²³·]*$")


def _inline(text: str) -> str:
    """Escape, then apply bold, italic and inline code."""
    escaped = html.escape(text)
    escaped = re.sub(
        r"`([^`]+)`",
        r'<code style="background-color:#222836;">\1</code>',
        escaped,
    )
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", escaped)
    return escaped


def _cells(line: str) -> list[str]:
    """The cells of one Markdown table row."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _table(rows: list[str]) -> str:
    """A Markdown table as an HTML table with a header row."""
    header = _cells(rows[0])
    body = [_cells(row) for row in rows[2:]]
    width = len(header)
    head_html = "".join(
        f'<th style="background-color:{SURFACE_RAISED};color:{ACCENT};'
        f'padding:5px 10px;text-align:left;">{_inline(cell)}</th>'
        for cell in header
    )
    body_html = []
    for index, row in enumerate(body):
        row = (row + [""] * width)[:width]
        shade = f"background-color:{SURFACE_RAISED};" if index % 2 else ""
        cells = []
        for column, cell in enumerate(row):
            plain = re.sub(r"[*_`]", "", cell)
            align = "right" if column and _NUMERIC.match(plain or "x") else "left"
            cells.append(
                f'<td style="{shade}padding:4px 10px;text-align:{align};'
                f'color:{TEXT};">{_inline(cell)}</td>'
            )
        body_html.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f'<table border="1" cellspacing="0" cellpadding="5" '
        f'style="border-color:{BORDER};border-collapse:collapse;margin:6px 0;">'
        f"<tr>{head_html}</tr>{''.join(body_html)}</table>"
    )


def markdown_to_html(text: str) -> str:
    """Convert a model's Markdown reply into Qt rich text."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None
    in_code = False
    code: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{'<br>'.join(_inline(line) for line in paragraph)}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    index = 0
    while index < len(lines):
        line = lines[index]

        if line.strip().startswith("```"):
            if in_code:
                out.append(
                    f'<pre style="background-color:{SURFACE_RAISED};padding:6px;">'
                    f"{html.escape(chr(10).join(code))}</pre>"
                )
                code.clear()
                in_code = False
            else:
                flush_paragraph()
                close_list()
                in_code = True
            index += 1
            continue
        if in_code:
            code.append(line)
            index += 1
            continue

        # A table: a row of pipes followed by a separator row.
        if (
            "|" in line
            and index + 1 < len(lines)
            and _TABLE_SEPARATOR.match(lines[index + 1])
        ):
            flush_paragraph()
            close_list()
            rows = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(lines[index])
                index += 1
            out.append(_table(rows))
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            size = {1: 17, 2: 15, 3: 14}.get(level, 13)
            out.append(
                f'<p style="margin-top:10px;font-size:{size}px;font-weight:700;'
                f'color:{ACCENT};">{_inline(heading.group(2))}</p>'
            )
            index += 1
            continue

        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            flush_paragraph()
            close_list()
            out.append(f'<hr style="color:{BORDER};">')
            index += 1
            continue

        bullet = _BULLET.match(line)
        numbered = _NUMBERED.match(line)
        if bullet or numbered:
            flush_paragraph()
            kind = "ul" if bullet else "ol"
            if list_kind != kind:
                close_list()
                out.append(f"<{kind}>")
                list_kind = kind
            item = (bullet or numbered).group(1)
            out.append(f"<li>{_inline(item)}</li>")
            index += 1
            continue

        if not line.strip():
            flush_paragraph()
            close_list()
            index += 1
            continue

        close_list()
        paragraph.append(line)
        index += 1

    if in_code and code:
        out.append(f"<pre>{html.escape(chr(10).join(code))}</pre>")
    flush_paragraph()
    close_list()
    return "".join(out) or f'<p style="color:{TEXT_MUTED};">(no answer)</p>'
