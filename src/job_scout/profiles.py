"""Local candidate-profile and CV document handling for Demo v2."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

MAX_CV_BYTES = 15 * 1024 * 1024
MAX_CV_PAGES = 12
MIN_EXTRACTED_TEXT_CHARS = 80
ALLOWED_CV_SUFFIXES = {".pdf"}


class CvExtractionError(ValueError):
    """The uploaded CV cannot be read safely on this local machine."""


@dataclass(frozen=True)
class ExtractedCv:
    text: str
    method: str
    page_count: int
    pages: tuple[str, ...] = ()


def safe_filename(name: str) -> str:
    basename = Path(name or "cv.pdf").name
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip(".-")
    return normalized or "cv.pdf"


def validate_cv_upload(filename: str, content: bytes) -> None:
    if Path(filename).suffix.casefold() not in ALLOWED_CV_SUFFIXES:
        raise CvExtractionError("Dodaj CV jako plik PDF.")
    if not content:
        raise CvExtractionError("Przesłany plik jest pusty.")
    if len(content) > MAX_CV_BYTES:
        raise CvExtractionError("CV przekracza bezpieczny limit 15 MB.")


def store_cv_bytes(storage_root: Path, profile_id: str, filename: str, content: bytes) -> Path:
    validate_cv_upload(filename, content)
    target_dir = storage_root / profile_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}-{safe_filename(filename)}"
    target.write_bytes(content)
    return target


def extract_cv_pdf(
    path: Path,
    *,
    pdftoppm: str = "pdftoppm",
    tesseract: str = "tesseract",
    languages: str = "eng+pol",
    runner=subprocess.run,
) -> ExtractedCv:
    """Extract embedded text first; invoke local OCR only for scanned PDFs."""
    try:
        reader = PdfReader(path)
    except Exception as exc:  # pypdf exposes several implementation-specific exceptions.
        raise CvExtractionError(f"Nie można odczytać pliku PDF: {exc}") from exc
    page_count = len(reader.pages)
    if not 1 <= page_count <= MAX_CV_PAGES:
        raise CvExtractionError(f"CV musi mieć od 1 do {MAX_CV_PAGES} stron.")
    embedded_pages = tuple((page.extract_text() or "").strip() for page in reader.pages)
    embedded_text = "\n\n".join(embedded_pages).strip()
    if len(re.sub(r"\s+", "", embedded_text)) >= MIN_EXTRACTED_TEXT_CHARS:
        return ExtractedCv(
            text=embedded_text,
            method="pdf_text",
            page_count=page_count,
            pages=embedded_pages,
        )

    if not shutil.which(pdftoppm):
        raise CvExtractionError("Brakuje pdftoppm potrzebnego do OCR PDF.")
    if not shutil.which(tesseract):
        raise CvExtractionError("Brakuje Tesseract potrzebnego do OCR PDF.")
    with tempfile.TemporaryDirectory(prefix="job-scout-cv-ocr-") as directory:
        prefix = Path(directory) / "page"
        try:
            runner(
                [pdftoppm, "-png", "-r", "200", str(path), str(prefix)],
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CvExtractionError(f"Nie udało się wyrenderować PDF do OCR: {exc}") from exc
        images = sorted(Path(directory).glob("page-*.png"))
        if len(images) != page_count:
            raise CvExtractionError("OCR PDF nie utworzył kompletu stron.")
        pages: list[str] = []
        for image in images:
            try:
                response = runner(
                    [tesseract, str(image), "stdout", "-l", languages],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise CvExtractionError(f"OCR strony CV nie powiódł się: {exc}") from exc
            pages.append(response.stdout.strip())
    text = "\n\n".join(pages).strip()
    if len(re.sub(r"\s+", "", text)) < MIN_EXTRACTED_TEXT_CHARS:
        raise CvExtractionError("OCR nie odczytał wystarczającej ilości tekstu z CV.")
    return ExtractedCv(text=text, method="ocr", page_count=page_count, pages=tuple(pages))


def file_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
