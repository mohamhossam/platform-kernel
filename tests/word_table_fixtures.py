"""Synthetic WordprocessingML packages for extraction and publication contracts."""

import io
import zipfile
from xml.sax.saxutils import escape

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def word_paragraph(text: str, *, heading: bool = False) -> str:
    style = '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if heading else ""
    return f"<w:p>{style}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def word_cell(text: str, properties: str = "", extra: str = "") -> str:
    return f"<w:tc><w:tcPr>{properties}</w:tcPr>{word_paragraph(text)}{extra}</w:tc>"


def word_row(*cells: str, properties: str = "") -> str:
    return f"<w:tr><w:trPr>{properties}</w:trPr>{''.join(cells)}</w:tr>"


def word_table(*rows: str, columns: int = 3) -> str:
    return (
        "<w:tbl><w:tblGrid>"
        + '<w:gridCol w:w="1000"/>' * columns
        + "</w:tblGrid>"
        + "".join(rows)
        + "</w:tbl>"
    )


def word_document(body: str, parts: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{W}" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<w:body>{body}</w:body></w:document>",
        )
        for name, content in (parts or {}).items():
            archive.writestr(name, content)
    return output.getvalue()


def reviewed_word_table_document() -> bytes:
    nested = word_table(word_row(word_cell("Private nested rule")), columns=1)
    return word_document(
        word_paragraph("Private heading", heading=True)
        + word_table(
            word_row(
                word_cell("Private header"),
                word_cell("Private category"),
                word_cell("Private terms"),
            ),
            word_row(
                word_cell("Private anchor", '<w:vMerge w:val="restart"/>'),
                word_cell("Internal"),
                word_cell("Internal"),
            ),
            word_row(
                word_cell("", "<w:vMerge/>"),
                word_cell("XGPON التغطية مطلوبة. " * 40),
                word_cell("Public note", extra=nested),
            ),
        )
        + word_table(word_row(word_cell("Different table context")), columns=1)
    )


def reviewed_word_prose_document() -> bytes:
    return word_document(
        word_paragraph("Private heading", heading=True)
        + word_paragraph("Private paragraph XGPON التغطية مطلوبة.")
        + word_paragraph("Private list").replace(
            "<w:p>", '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/></w:pPr>', 1
        )
        + word_paragraph("Private appendix", heading=True)
        + word_paragraph("Never publish.")
    )
