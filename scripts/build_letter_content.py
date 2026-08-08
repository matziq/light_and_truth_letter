"""Build semantic HTML and search text for the in-site letter reader."""

from __future__ import annotations

import argparse
import html
import io
import json
import re
from pathlib import Path

import pymupdf
from PIL import Image
from pypdf import PdfReader
from pymupdf4llm.helpers import document_layout


URL_PATTERN = re.compile(r"https?://[^\s<>]+")
LOWERCASE_START = re.compile(r"^[a-z]")
HYPHENATED_STEMS = {
    "anti",
    "book",
    "byu",
    "down",
    "evidence",
    "faith",
    "first",
    "full",
    "god",
    "half",
    "high",
    "home",
    "latter",
    "life",
    "like",
    "long",
    "middle",
    "non",
    "old",
    "one",
    "peep",
    "post",
    "pre",
    "present",
    "pro",
    "right",
    "same",
    "self",
    "short",
    "time",
    "well",
}


def plain_text(box) -> str:
    return "".join(
        span.get("text", "")
        for line in (box.textlines or [])
        for span in line.get("spans", [])
    ).strip()


def clean_cell(value: str) -> str:
    value = re.sub(r"([A-Za-z])-\n(?=[a-z])", r"\1", value or "")
    return re.sub(r"\s+", " ", value).strip()


def linkify(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in URL_PATTERN.finditer(value):
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,;:)":
            trailing = url[-1] + trailing
            url = url[:-1]
        pieces.append(html.escape(value[cursor : match.start()]))
        pieces.append(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener">{html.escape(url)}</a>{html.escape(trailing)}'
        )
        cursor = match.end()
    pieces.append(html.escape(value[cursor:]))
    return "".join(pieces)


def styled_tokens(box) -> list[dict]:
    lines: list[list[dict]] = []
    for line in box.textlines or []:
        line_tokens = []
        for span in line.get("spans", []):
            text = span.get("text", "").replace("\u00ad", "")
            if not text:
                continue
            font = span.get("font", "").lower()
            flags = int(span.get("flags", 0))
            line_tokens.append(
                {
                    "text": text,
                    "bold": bool(flags & 16) or "bold" in font,
                    "italic": bool(flags & 2) or "italic" in font,
                    "sup": bool(flags & 1),
                }
            )
        if line_tokens:
            lines.append(line_tokens)

    tokens: list[dict] = []
    for line_index, line in enumerate(lines):
        if line_index:
            previous = tokens[-1]
            first_text = next(
                (token["text"].lstrip() for token in line if token["text"].strip()),
                "",
            )
            if previous["text"].rstrip().endswith("-") and LOWERCASE_START.match(
                first_text
            ):
                previous_text = previous["text"].rstrip()
                stem_match = re.search(r"([A-Za-z]+)-$", previous_text)
                stem = stem_match.group(1).lower() if stem_match else ""
                if stem not in HYPHENATED_STEMS and "http" not in previous_text:
                    previous["text"] = previous_text[:-1]
            elif not previous["text"].endswith((" ", "\n")):
                previous["text"] += " "
        tokens.extend(line)

    merged: list[dict] = []
    for token in tokens:
        if merged and all(
            merged[-1][key] == token[key] for key in ("bold", "italic", "sup")
        ):
            merged[-1]["text"] += token["text"]
        else:
            merged.append(token.copy())
    return merged


def render_tokens(box) -> str:
    output = []
    for token in styled_tokens(box):
        value = linkify(token["text"])
        if token["bold"]:
            value = f"<strong>{value}</strong>"
        if token["italic"]:
            value = f"<em>{value}</em>"
        if token["sup"]:
            value = f"<sup>{value}</sup>"
        output.append(value)
    return "".join(output).strip()


def render_table(table: dict) -> str:
    rows = table.get("extract") or []
    if not rows:
        return ""
    column_count = max(len(row) for row in rows)
    body = []
    for row_index, row in enumerate(rows):
        cells = []
        for column_index in range(column_count):
            raw = row[column_index] if column_index < len(row) else ""
            value = clean_cell(raw)
            if row_index == 0:
                cells.append(f"<th scope=\"col\">{html.escape(value)}</th>")
                continue
            if column_index == 0 and "\n" in (raw or ""):
                first, remainder = (raw.split("\n", 1) + [""])[:2]
                first = clean_cell(first)
                remainder = clean_cell(remainder)
                cell_html = f"<strong>{html.escape(first)}</strong>"
                if remainder:
                    cell_html += f"<br>{html.escape(remainder)}"
            else:
                cell_html = html.escape(value)
            cells.append(f"<td>{cell_html}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f'<div class="document-table-wrap" tabindex="0" '
        f'aria-label="Scrollable {column_count}-column table">'
        f'<table class="document-table columns-{column_count}">'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def render_figure(
    pdf_page: pymupdf.Page,
    box,
    page_number: int,
    figure_number: int,
    figure_dir: Path,
) -> str:
    width = box.x1 - box.x0
    height = box.y1 - box.y0
    label = plain_text(box)
    if width < 90 or height < 55:
        return ""

    clip = pymupdf.Rect(
        max(0, box.x0 - 3),
        max(0, box.y0 - 3),
        min(pdf_page.rect.width, box.x1 + 3),
        min(pdf_page.rect.height, box.y1 + 3),
    )
    pixmap = pdf_page.get_pixmap(
        matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=False
    )
    filename = f"page-{page_number:03d}-figure-{figure_number:02d}.webp"
    output_path = figure_dir / filename
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    image.save(output_path, "WEBP", quality=88, method=6)

    alt = (
        re.sub(r"\s+", " ", label).strip()
        if label and len(label) <= 220
        else f"Figure from page {page_number} of the Light and Truth Letter"
    )
    return (
        '<figure class="document-figure">'
        f'<img src="./assets/figures/{filename}" alt="{html.escape(alt, quote=True)}" '
        'loading="lazy" decoding="async">'
        "</figure>"
    )


def block_classes(box, page_number: int, text: str) -> list[str]:
    classes = ["document-block"]
    line_centers = [
        (float(line["bbox"][0]) + float(line["bbox"][2])) / 2
        for line in (box.textlines or [])
        if line.get("bbox")
    ]
    centered = (
        bool(line_centers)
        and sum(abs(center - 306) for center in line_centers) / len(line_centers)
        < 22
        and (box.x1 - box.x0) < 500
    )
    spans = [
        span
        for line in (box.textlines or [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    italic_count = sum(
        bool(int(span.get("flags", 0)) & 2)
        or "italic" in span.get("font", "").lower()
        for span in spans
    )
    italic_ratio = italic_count / len(spans) if spans else 0

    if centered:
        classes.append("is-centered")
    if italic_ratio > 0.55 or (
        text.startswith(("\u201c", '"')) and centered and len(text) > 80
    ):
        classes.append("pull-quote")
    elif (
        text.rstrip().endswith("?")
        and len(text) <= 260
        and re.match(
            r"^(?:Why|How|What|When|Where|Who|Is|Are|Can|Could|Do|Does|Did|"
            r"Would|Should|Has|Have)\b",
            text,
        )
    ):
        classes.append("question-callout")
    if page_number >= 208:
        classes.append("source-note")
    return classes


def render_page(
    layout_page,
    pdf_page: pymupdf.Page,
    page_number: int,
    figure_dir: Path,
) -> str:
    blocks: list[str] = []
    list_items: list[str] = []
    quote_fragments: list[str] = []
    figure_number = 0

    def flush_quotes() -> None:
        if quote_fragments:
            blocks.append(
                '<blockquote class="document-block pull-quote">'
                f'{" ".join(quote_fragments)}</blockquote>'
            )
            quote_fragments.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append(
                f'<ul class="document-list">{"".join(list_items)}</ul>'
            )
            list_items.clear()

    for box in layout_page.boxes:
        kind = box.boxclass
        if kind in {"page-header", "page-footer"}:
            continue
        text = re.sub(r"\s+", " ", plain_text(box)).strip()

        if kind == "list-item":
            flush_quotes()
            item = render_tokens(box)
            item = re.sub(r"^(?:\u2022|\u25cf|\u25aa|-)\s*", "", item)
            list_items.append(f"<li>{item}</li>")
            continue

        flush_list()
        if kind == "text" and text:
            classes = block_classes(box, page_number, text)
            if "pull-quote" in classes:
                quote_fragments.append(render_tokens(box))
                continue
        flush_quotes()

        if kind == "table":
            table_html = render_table(box.table or {})
            if table_html:
                blocks.append(table_html)
        elif kind == "picture":
            figure_number += 1
            figure_html = render_figure(
                pdf_page, box, page_number, figure_number, figure_dir
            )
            if figure_html:
                blocks.append(figure_html)
        elif kind == "section-header":
            font_size = float(box.max_fontsize or 0)
            level = 2 if font_size >= 16 else 3 if font_size >= 13 else 4
            blocks.append(
                f'<h{level} class="document-heading level-{level}">'
                f"{render_tokens(box)}</h{level}>"
            )
        elif text:
            value = render_tokens(box)
            classes = block_classes(box, page_number, text)
            blocks.append(f'<p class="{" ".join(classes)}">{value}</p>')

    flush_list()
    flush_quotes()
    if not blocks:
        return '<p class="empty-state">This page contains no extractable text.</p>'
    return "".join(blocks)


def build(pdf_path: Path, output_path: Path, figure_dir: Path) -> None:
    figure_dir.mkdir(parents=True, exist_ok=True)
    for old_figure in figure_dir.glob("page-*-figure-*.webp"):
        old_figure.unlink()

    print("Reading search text...")
    text_reader = PdfReader(str(pdf_path))
    search_pages = [
        (page.extract_text() or "").strip() for page in text_reader.pages
    ]

    print("Analyzing document layout...")
    parsed = document_layout.parse_document(
        str(pdf_path),
        render_html_tables=True,
        force_text=True,
        show_progress=True,
    )
    pdf = pymupdf.open(pdf_path)
    pages = []
    for page_number, layout_page in enumerate(parsed.pages, start=1):
        page_html = render_page(
            layout_page, pdf[page_number - 1], page_number, figure_dir
        )
        pages.append(
            {
                "page": page_number,
                "text": search_pages[page_number - 1],
                "html": page_html,
            }
        )
        print(f"\rRendering page {page_number}/{len(parsed.pages)}", end="")
    print()

    payload = json.dumps(pages, ensure_ascii=False, separators=(",", ":"))
    output_path.write_text(
        f"window.LETTER_PAGES = {payload};\n", encoding="utf-8"
    )
    print(
        f"Wrote {output_path} ({output_path.stat().st_size:,} bytes) and "
        f"{len(list(figure_dir.glob('*.webp')))} figures."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pdf", type=Path, default=Path("Light_and_Truth_Letter.pdf")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("letter-content.js")
    )
    parser.add_argument(
        "--figures", type=Path, default=Path("assets/figures")
    )
    args = parser.parse_args()
    build(args.pdf, args.output, args.figures)


if __name__ == "__main__":
    main()
