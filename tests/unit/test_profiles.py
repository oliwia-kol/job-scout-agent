from pathlib import Path

import pytest

from job_scout import profiles
from job_scout.profiles import CvExtractionError, extract_cv_pdf, store_cv_bytes, validate_cv_upload


def _text_pdf(path: Path) -> None:
    stream = (
        b"BT /F1 18 Tf 72 720 Td "
        b"(Senior AI Engineer with Python and evaluation experience in local model testing) Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(output)


def test_pdf_text_extraction_prefers_embedded_text(monkeypatch, tmp_path):
    class FakePage:
        def extract_text(self):
            return "Senior AI Engineer with Python and local evaluation experience. " * 2

    class FakeReader:
        pages = [FakePage()]

    monkeypatch.setattr(profiles, "PdfReader", lambda _path: FakeReader())
    extracted = extract_cv_pdf(tmp_path / "cv.pdf")
    assert extracted.method == "pdf_text"
    assert "Senior AI Engineer" in extracted.text
    assert extracted.page_count == 1
    assert extracted.pages == (extracted.text,)


def test_upload_validation_is_local_and_rejects_non_pdf(tmp_path):
    with pytest.raises(CvExtractionError, match="PDF"):
        validate_cv_upload("cv.docx", b"not a pdf")
    stored = store_cv_bytes(tmp_path, "profile", "my cv.pdf", b"%PDF-1.4\n")
    assert stored.parent == tmp_path / "profile"
    assert stored.name.endswith("my-cv.pdf")


def test_upload_rejects_oversized_and_corrupted_pdf(monkeypatch, tmp_path):
    with pytest.raises(CvExtractionError, match="15 MB"):
        validate_cv_upload("cv.pdf", b"x" * (profiles.MAX_CV_BYTES + 1))

    def broken_reader(_path):
        raise ValueError("broken xref")

    monkeypatch.setattr(profiles, "PdfReader", broken_reader)
    with pytest.raises(CvExtractionError, match="Nie można odczytać"):
        extract_cv_pdf(tmp_path / "broken.pdf")


def test_pdf_page_limit_is_enforced(monkeypatch, tmp_path):
    class FakeReader:
        pages = [object()] * (profiles.MAX_CV_PAGES + 1)

    monkeypatch.setattr(profiles, "PdfReader", lambda _path: FakeReader())
    with pytest.raises(CvExtractionError, match="od 1 do"):
        extract_cv_pdf(tmp_path / "too-long.pdf")


def test_scanned_pdf_uses_ocr_when_embedded_text_is_missing(monkeypatch, tmp_path):
    class FakePage:
        def extract_text(self):
            return ""

    class FakeReader:
        pages = [FakePage()]

    class Response:
        stdout = "OCR recovered candidate profile facts " * 3

    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        if command[0] == "pdftoppm":
            (tmp_path / "page-1.png").write_bytes(b"png")
            return Response()
        return Response()

    monkeypatch.setattr(profiles, "PdfReader", lambda _path: FakeReader())
    monkeypatch.setattr(profiles.shutil, "which", lambda _name: "/tool")
    monkeypatch.setattr(
        profiles.tempfile, "TemporaryDirectory", lambda **_kwargs: _TempDir(tmp_path)
    )
    extracted = extract_cv_pdf(tmp_path / "scan.pdf", runner=runner)
    assert extracted.method == "ocr"
    assert "OCR recovered" in extracted.text
    assert extracted.pages == (extracted.text,)
    assert any(command[-2:] == ["-l", "eng+pol"] for command in commands)


class _TempDir:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> str:
        return str(self.path)

    def __exit__(self, *_args) -> None:
        return None
