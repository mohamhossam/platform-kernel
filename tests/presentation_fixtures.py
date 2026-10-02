"""Small, deterministic OOXML packages; no Office installation or live provider required."""

import io
import zipfile
from xml.sax.saxutils import escape

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def drawing_paragraph(text: str) -> str:
    return f"<a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p>"


def table_cell(text: str, attributes: str = "") -> str:
    return (
        f"<a:tc {attributes}><a:txBody><a:bodyPr/><a:lstStyle/>"
        f"{drawing_paragraph(text)}</a:txBody><a:tcPr/></a:tc>"
    )


def drawing_table(*rows: tuple[str, ...], columns: int = 2) -> str:
    return (
        "<p:graphicFrame><a:graphic><a:graphicData "
        'uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>'
        "<a:tblPr/><a:tblGrid>"
        + '<a:gridCol w="1000"/>' * columns
        + "</a:tblGrid>"
        + "".join('<a:tr h="1000">' + "".join(row) + "</a:tr>" for row in rows)
        + "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def presentation(*slide_contents: str, notes: str = "") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "ppt/presentation.xml",
            f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
            + "".join(f'<p:sldId id="{256 + i}" r:id="r{i}"/>' for i in range(len(slide_contents)))
            + "</p:sldIdLst></p:presentation>",
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="r{i}" Type="{R}/slide" Target="slides/slide{i + 1}.xml"/>'
                for i in range(len(slide_contents))
            )
            + "</Relationships>",
        )
        for i, content in enumerate(slide_contents, 1):
            archive.writestr(
                f"ppt/slides/slide{i}.xml",
                f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
                f"<p:cSld><p:spTree>{content}</p:spTree></p:cSld></p:sld>",
            )
        if notes:
            archive.writestr(
                "ppt/slides/_rels/slide1.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="notes" Type="{R}/notesSlide" '
                'Target="../notesSlides/notesSlide1.xml"/></Relationships>',
            )
            archive.writestr(
                "ppt/notesSlides/notesSlide1.xml",
                f'<p:notes xmlns:p="{P}" xmlns:a="{A}">{drawing_paragraph(notes)}</p:notes>',
            )
    return output.getvalue()


def reviewed_table_presentation() -> bytes:
    """Private text first catches leakage through a content-derived slide heading."""
    return presentation(
        drawing_table(
            (table_cell("Private operations", 'rowSpan="2"'), table_cell("Internal only")),
            (table_cell("", 'vMerge="true"'), table_cell("XGPON التغطية مطلوبة. " * 40)),
        ),
        notes="Private speaker notes",
    )
