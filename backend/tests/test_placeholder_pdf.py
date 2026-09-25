"""Sample placeholder PDFs carry each cited value at its bbox (so the source viewer's highlight
lands on the value)."""

from __future__ import annotations

import io

from pypdf import PdfReader

from core.seeds import placeholder_pdf, sample_citations


def test_placeholder_prints_annotations_on_their_page():
    pdf = placeholder_pdf(
        "DUP Centar – Zona C2", 3, {2: [([72, 388, 520, 406], "max far: 3.2 (UP 12)")]}
    )
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 3
    assert "max far: 3.2 (UP 12)" in reader.pages[1].extract_text()
    assert "max far" not in reader.pages[0].extract_text()
    assert (
        "DUP Centar - Zona C2 - sample placeholder, page 1 of 3" in reader.pages[0].extract_text()
    )


def test_sample_citations_follow_the_published_values():
    citations = sample_citations()
    # DUP Centar – Zona C2 (document 2) cites UP 12's FAR on page 13 at [72, 388, 520, 406]
    far = [c for c in citations[2][13] if c[0] == [72, 388, 520, 406]]
    assert far and far[0][1].startswith("max far: 3.2")
    assert sum(len(v) for pages in citations.values() for v in pages.values()) >= 30
