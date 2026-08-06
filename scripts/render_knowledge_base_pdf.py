from __future__ import annotations

import html
import os
import re
import textwrap
from pathlib import Path
from typing import Iterable, List, Sequence

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "EvoAgent知识库.md"
OUTPUT = ROOT / "output" / "pdf" / "EvoAgent知识库.pdf"

PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT = 18 * mm
RIGHT = 18 * mm
TOP = 17 * mm
BOTTOM = 17 * mm
CONTENT_WIDTH = PAGE_WIDTH - LEFT - RIGHT

INK = colors.HexColor("#20242C")
MUTED = colors.HexColor("#69707D")
BLUE = colors.HexColor("#155EEF")
PURPLE = colors.HexColor("#6D28D9")
PALE_BLUE = colors.HexColor("#EDF4FF")
PALE_GREEN = colors.HexColor("#ECFDF3")
PALE_YELLOW = colors.HexColor("#FFF9C4")
CODE_BG = colors.HexColor("#F5F6F8")
LINE = colors.HexColor("#D8DCE3")


def register_fonts() -> None:
    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    pdfmetrics.registerFont(TTFont("MSYH", str(font_dir / "msyh.ttc"), subfontIndex=0))
    pdfmetrics.registerFont(TTFont("MSYH-Bold", str(font_dir / "msyhbd.ttc"), subfontIndex=0))
    pdfmetrics.registerFont(TTFont("SimHei", str(font_dir / "simhei.ttf")))


def styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "TitleCN",
            parent=base["Title"],
            fontName="MSYH-Bold",
            fontSize=24,
            leading=31,
            textColor=INK,
            alignment=TA_LEFT,
            spaceAfter=9 * mm,
        ),
        "h2": ParagraphStyle(
            "H2CN",
            parent=base["Heading1"],
            fontName="MSYH-Bold",
            fontSize=18,
            leading=25,
            textColor=INK,
            spaceBefore=3 * mm,
            spaceAfter=5 * mm,
            keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "H3CN",
            parent=base["Heading2"],
            fontName="MSYH-Bold",
            fontSize=13.5,
            leading=19,
            textColor=BLUE,
            spaceBefore=4.5 * mm,
            spaceAfter=2.5 * mm,
            keepWithNext=True,
        ),
        "h4": ParagraphStyle(
            "H4CN",
            parent=base["Heading3"],
            fontName="MSYH-Bold",
            fontSize=11.5,
            leading=17,
            textColor=INK,
            spaceBefore=3 * mm,
            spaceAfter=2 * mm,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "BodyCN",
            parent=base["BodyText"],
            fontName="MSYH",
            fontSize=9.6,
            leading=16,
            textColor=INK,
            alignment=TA_LEFT,
            wordWrap="CJK",
            spaceAfter=2.2 * mm,
        ),
        "bullet": ParagraphStyle(
            "BulletCN",
            parent=base["BodyText"],
            fontName="MSYH",
            fontSize=9.4,
            leading=15.5,
            leftIndent=5 * mm,
            firstLineIndent=-3 * mm,
            textColor=INK,
            wordWrap="CJK",
            spaceAfter=1.3 * mm,
        ),
        "quote": ParagraphStyle(
            "QuoteCN",
            parent=base["BodyText"],
            fontName="MSYH",
            fontSize=9.6,
            leading=16,
            textColor=INK,
            wordWrap="CJK",
        ),
        "code": ParagraphStyle(
            "CodeCN",
            parent=base["Code"],
            fontName="MSYH",
            fontSize=7.8,
            leading=11.3,
            textColor=colors.HexColor("#343A46"),
            leftIndent=5 * mm,
            rightIndent=5 * mm,
            spaceBefore=1.8 * mm,
            spaceAfter=3 * mm,
        ),
        "table_header": ParagraphStyle(
            "TableHeaderCN",
            parent=base["BodyText"],
            fontName="MSYH-Bold",
            fontSize=8,
            leading=11,
            textColor=INK,
            wordWrap="CJK",
        ),
        "table_body": ParagraphStyle(
            "TableBodyCN",
            parent=base["BodyText"],
            fontName="MSYH",
            fontSize=7.7,
            leading=11,
            textColor=INK,
            wordWrap="CJK",
        ),
        "footer": ParagraphStyle(
            "FooterCN",
            parent=base["BodyText"],
            fontName="MSYH",
            fontSize=7.5,
            leading=9,
            textColor=MUTED,
            alignment=TA_CENTER,
        ),
    }


def inline_markup(value: str) -> str:
    value = html.escape(value.strip())
    value = re.sub(
        r"`([^`]+)`",
        r'<font name="MSYH" color="#6D28D9">\1</font>',
        value,
    )
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", value)
    return value


def paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(inline_markup(text), style)


def callout(text: str, style: ParagraphStyle, background=PALE_YELLOW) -> Table:
    table = Table([[Paragraph(inline_markup(text), style)]], colWidths=[CONTENT_WIDTH])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E7C900")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def wrap_code(lines: Sequence[str], width: int = 92) -> str:
    wrapped: List[str] = []
    for raw in lines:
        expanded = raw.expandtabs(4).rstrip()
        if not expanded:
            wrapped.append("")
            continue
        indent = len(expanded) - len(expanded.lstrip())
        subsequent = " " * min(indent + 2, 16)
        pieces = textwrap.wrap(
            expanded,
            width=width,
            replace_whitespace=False,
            drop_whitespace=False,
            subsequent_indent=subsequent,
            break_long_words=True,
            break_on_hyphens=False,
        )
        wrapped.extend(pieces or [""])
    return "\n".join(wrapped)


def code_block(lines: Sequence[str], style: ParagraphStyle) -> Table:
    pre = Preformatted(wrap_code(lines), style)
    table = Table([[pre]], colWidths=[CONTENT_WIDTH])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def split_table_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def table_widths(rows: Sequence[Sequence[str]]) -> List[float]:
    columns = max(len(row) for row in rows)
    maxima = []
    for column in range(columns):
        length = max(
            len(row[column]) if column < len(row) else 0
            for row in rows
        )
        maxima.append(max(6, min(length, 32)))
    total = sum(maxima)
    return [CONTENT_WIDTH * value / total for value in maxima]


def markdown_table(rows: Sequence[Sequence[str]], style_map: dict) -> Table:
    rendered = []
    for row_index, row in enumerate(rows):
        style = style_map["table_header"] if row_index == 0 else style_map["table_body"]
        rendered.append(
            [
                Paragraph(inline_markup(cell), style)
                for cell in row
            ]
        )
    table = Table(rendered, colWidths=table_widths(rows), repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), PALE_BLUE),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("GRID", (0, 0), (-1, -1), 0.45, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFBFC")]),
            ]
        )
    )
    return table


def parse_markdown(lines: Sequence[str], style_map: dict) -> List:
    story: List = []
    index = 0
    major_seen = False
    paragraph_buffer: List[str] = []

    def flush_paragraph() -> None:
        if not paragraph_buffer:
            return
        joined = " ".join(item.strip() for item in paragraph_buffer)
        story.append(paragraph(joined, style_map["body"]))
        paragraph_buffer.clear()

    while index < len(lines):
        line = lines[index].rstrip("\n")
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            index += 1
            block: List[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index].rstrip("\n"))
                index += 1
            index += 1
            story.append(code_block(block, style_map["code"]))
            story.append(Spacer(1, 1.3 * mm))
            continue

        if stripped.startswith("|") and "|" in stripped[1:]:
            flush_paragraph()
            raw_rows: List[List[str]] = []
            while index < len(lines):
                candidate = lines[index].strip()
                if not (candidate.startswith("|") and "|" in candidate[1:]):
                    break
                cells = split_table_row(candidate)
                if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    raw_rows.append(cells)
                index += 1
            if raw_rows:
                story.append(markdown_table(raw_rows, style_map))
                story.append(Spacer(1, 3 * mm))
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            quote_lines = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote_lines.append(lines[index].strip()[1:].strip())
                index += 1
            quote_text = "<br/>".join(inline_markup(item) for item in quote_lines)
            table = Table(
                [[Paragraph(quote_text, style_map["quote"])]],
                colWidths=[CONTENT_WIDTH],
            )
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), PALE_YELLOW),
                        ("BOX", (0, 0), (-1, -1), 0.55, colors.HexColor("#E4C400")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 10),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                        ("TOPPADDING", (0, 0), (-1, -1), 9),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                    ]
                )
            )
            story.append(table)
            story.append(Spacer(1, 3 * mm))
            continue

        if stripped.startswith("# "):
            flush_paragraph()
            story.append(paragraph(stripped[2:], style_map["title"]))
            story.append(
                callout(
                    "本文档按当前代码编写，已实现能力、实验性适配和规划内容会分开说明。",
                    style_map["quote"],
                )
            )
            story.append(Spacer(1, 5 * mm))
            index += 1
            continue

        if stripped.startswith("## "):
            flush_paragraph()
            if major_seen:
                story.append(PageBreak())
            major_seen = True
            story.append(paragraph(stripped[3:], style_map["h2"]))
            story.append(
                HRFlowable(
                    width="100%",
                    thickness=0.7,
                    color=LINE,
                    spaceAfter=3 * mm,
                )
            )
            index += 1
            continue

        if stripped.startswith("### "):
            flush_paragraph()
            story.append(paragraph(stripped[4:], style_map["h3"]))
            index += 1
            continue

        if stripped.startswith("#### "):
            flush_paragraph()
            story.append(paragraph(stripped[5:], style_map["h4"]))
            index += 1
            continue

        if stripped.startswith("- "):
            flush_paragraph()
            story.append(
                Paragraph(
                    "● " + inline_markup(stripped[2:]),
                    style_map["bullet"],
                )
            )
            index += 1
            continue

        numbered = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if numbered:
            flush_paragraph()
            story.append(
                Paragraph(
                    f"{numbered.group(1)}. " + inline_markup(numbered.group(2)),
                    style_map["bullet"],
                )
            )
            index += 1
            continue

        paragraph_buffer.append(line)
        index += 1

    flush_paragraph()
    return story


def footer(canvas, document) -> None:
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.35)
    canvas.line(LEFT, 11 * mm, PAGE_WIDTH - RIGHT, 11 * mm)
    canvas.setFont("MSYH", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(LEFT, 6.6 * mm, "EvoAgent PR Reviewer 知识库")
    canvas.drawRightString(PAGE_WIDTH - RIGHT, 6.6 * mm, str(document.page))
    canvas.restoreState()


def build() -> None:
    register_fonts()
    style_map = styles()
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    story = parse_markdown(lines, style_map)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        leftMargin=LEFT,
        rightMargin=RIGHT,
        topMargin=TOP,
        bottomMargin=BOTTOM,
        title="EvoAgent PR Reviewer 知识库",
        author="AgentProject",
        subject="EvoAgent 项目知识库",
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    print(OUTPUT)


if __name__ == "__main__":
    build()
