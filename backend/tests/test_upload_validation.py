"""Issue #9 — magic-byte upload content-type validation.

Covers the issue's acceptance criteria:
  - A `.pdf` upload whose content is not a PDF is rejected with 400.
  - PyMuPDF (FileParser.extract_text) is never invoked on a file whose magic
    bytes do not say application/pdf.
  - Both rejection cases are covered.

These tests require the system `libmagic` library (Debian `libmagic1` in the
container, Homebrew `libmagic` on macOS dev hosts). `import magic` at the top
of app/api/graph.py fails loudly without it — that is intentional: the upload
security control must not silently no-op.

MIME assertions use the `text/*` band-pass for non-PDF formats rather than an
exact string, because libmagic versions disagree on the exact subtype.
"""
import io
from types import SimpleNamespace
from unittest.mock import patch

from werkzeug.datastructures import FileStorage

from app.api.graph import validate_upload_content_type

# A minimal but real PDF header — enough for libmagic to classify as PDF.
PDF_BYTES = (
    b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
)
# PNG magic header — binary, definitely not text/* or application/pdf.
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
# Plain ASCII text — classified text/* by every libmagic version.
TEXT_BYTES = (
    b"This is a plain text document used for upload validation tests.\n"
    b"It contains several lines of readable ASCII content so libmagic\n"
    b"classifies it as a text type rather than binary data.\n"
)


def _fs(data: bytes, filename: str) -> FileStorage:
    return FileStorage(stream=io.BytesIO(data), filename=filename)


def test_pdf_extension_with_pdf_bytes_accepted():
    ok, mime = validate_upload_content_type(_fs(PDF_BYTES, "doc.pdf"), "pdf")
    assert ok is True
    assert mime == "application/pdf"


def test_pdf_extension_with_text_bytes_rejected():
    # The headline attack: evil text disguised as a .pdf.
    ok, mime = validate_upload_content_type(_fs(TEXT_BYTES, "evil.pdf"), "pdf")
    assert ok is False
    assert mime != "application/pdf"


def test_txt_extension_with_binary_bytes_rejected():
    ok, mime = validate_upload_content_type(_fs(PNG_BYTES, "evil.txt"), "txt")
    assert ok is False
    assert not mime.startswith("text/")


def test_text_extension_with_pdf_bytes_rejected():
    # The inverse mismatch: real PDF content disguised under a text extension.
    # The text band-pass must still reject it (application/pdf is not text/*).
    for ext in ("txt", "md", "markdown"):
        ok, mime = validate_upload_content_type(_fs(PDF_BYTES, f"evil.{ext}"), ext)
        assert ok is False, f"{ext}: detected {mime}"
        assert mime == "application/pdf"


def test_text_extensions_accept_plain_text():
    # All four non-binary extensions in Config.ALLOWED_EXTENSIONS, including
    # the `.markdown` one the issue's example omitted.
    for ext in ("txt", "md", "markdown"):
        ok, mime = validate_upload_content_type(_fs(TEXT_BYTES, f"doc.{ext}"), ext)
        assert ok is True, f"{ext}: detected {mime}"
        assert mime.startswith("text/")


def test_stream_rewound_after_validation():
    # The full file must remain readable for the subsequent save.
    fs = _fs(PDF_BYTES, "doc.pdf")
    validate_upload_content_type(fs, "pdf")
    assert fs.stream.read() == PDF_BYTES


def test_endpoint_rejects_mismatch_without_invoking_pymupdf(client):
    """AC item 3: a .pdf with non-PDF content is rejected with 400 and
    FileParser.extract_text (→ fitz.open) is never called."""
    fake_project = SimpleNamespace(project_id="proj_test_upload", files=[])
    with patch(
        "app.api.graph.ProjectManager.create_project", return_value=fake_project
    ) as create, patch(
        "app.api.graph.ProjectManager.delete_project"
    ) as delete, patch(
        "app.api.graph.FileParser.extract_text"
    ) as extract:
        resp = client.post(
            "/api/graph/ontology/generate",
            data={
                "simulation_requirement": "test requirement",
                "project_name": "upload-validation-test",
                "files": (io.BytesIO(TEXT_BYTES), "evil.pdf"),
            },
            content_type="multipart/form-data",
        )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"] == "文件内容与扩展名不匹配"
    extract.assert_not_called()
    delete.assert_called_once_with("proj_test_upload")
    create.assert_called_once()
