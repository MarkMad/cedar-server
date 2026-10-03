"""Native PDF failures must stay outside the server and release their resources."""
from __future__ import annotations

import base64
import sys
import time

import fitz
import pytest
from conftest import AUTH

from cedar import chunker, pdflayout, pdfsafe
from cedar.config import UPLOAD_DIR


@pytest.fixture
def pdf_file(tmp_path):
    path = tmp_path / "book.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 120), "A readable sentence with several words.")
        doc.set_metadata({"title": "A normal book"})
        doc.set_toc([[1, "First chapter", 1]])
        doc.save(path)
    return path


def test_pdf_worker_preserves_text_toc_and_layout(pdf_file):
    result = chunker.extract_pdf(str(pdf_file), "Fallback")
    assert result.title == "A normal book"
    assert result.num_pages == 1
    assert result.chunks[0].text == "A readable sentence with several words."
    assert result.toc[0].title == "First chapter"
    assert result.pages == [{"page": 1, "start": 0, "count": 1}]
    text = result.chunks[0].text
    rects = pdfsafe.run_pdf("rects", str(pdf_file), page_no=1, text=text)
    assert rects["width"] > 0 and rects["height"] > 0
    assert rects["rects"] and rects["words"]
    hits = pdfsafe.run_pdf("hits", str(pdf_file), page_no=1, sentences=[[0, text]])
    assert hits and hits[0][-1] == 0
    cover = base64.b64decode(pdfsafe.run_pdf("thumbnail", str(pdf_file)))
    assert cover.startswith(b"\xff\xd8")


def test_import_and_reader_work_after_isolation(client, pdf_file):
    response = client.post("/api/documents", headers=AUTH,
                           files={"file": ("book.pdf", pdf_file.read_bytes(), "application/pdf")})
    assert response.status_code == 200, response.text
    did = response.json()["id"]
    try:
        assert client.get(f"/api/documents/{did}/thumb", headers=AUTH).status_code == 200
        rects = client.get(f"/api/documents/{did}/rects/0", headers=AUTH).json()
        assert rects["rects"] and rects["words"]
        x0, y0, x1, y1 = rects["rects"][0]
        hit = client.get(f"/api/documents/{did}/at", headers=AUTH,
                         params={"page": 1, "x": (x0 + x1) / 2, "y": (y0 + y1) / 2})
        assert hit.json() == {"idx": 0}
    finally:
        client.delete(f"/api/documents/{did}", headers=AUTH)


def test_extreme_page_aspect_ratio_has_bounded_thumbnail(tmp_path):
    path = tmp_path / "tall.pdf"
    with fitz.open() as doc:
        doc.new_page(width=10, height=100000)
        doc.save(path)
    jpeg = base64.b64decode(pdfsafe.run_pdf("thumbnail", str(path)))
    pix = fitz.Pixmap(jpeg)
    assert pix.width <= 320 and pix.height <= 641


@pytest.mark.parametrize("operation", ["extract", "thumbnail", "rects", "hits"])
def test_worker_deadline_kills_and_reaps_process(monkeypatch, tmp_path, operation):
    script = tmp_path / "slow_worker.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    processes = []
    temporary_paths = []
    original_popen = pdfsafe.subprocess.Popen

    def record_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(pdfsafe.subprocess, "Popen", record_process)

    def command(request, output):
        temporary_paths.extend([request, output])
        return [sys.executable, str(script)]

    monkeypatch.setattr(pdfsafe, "_worker_command", command)
    monkeypatch.setattr(pdfsafe, "PDF_TIMEOUT", 0.15)
    start = time.monotonic()
    with pytest.raises(pdfsafe.PdfLimitError, match="time limit"):
        pdfsafe.run_pdf(operation, str(tmp_path / "anything.pdf"))
    assert time.monotonic() - start < 5
    assert processes[0].poll() is not None
    assert all(not path.parent.exists() for path in temporary_paths)
    assert pdfsafe._slots.acquire(blocking=False)
    assert pdfsafe._slots.acquire(blocking=False)
    pdfsafe._slots.release()
    pdfsafe._slots.release()


def test_native_worker_crash_is_contained(monkeypatch, tmp_path):
    monkeypatch.setattr(pdfsafe, "_worker_command", lambda *_: [sys.executable, "-c", "import os; os._exit(9)"])
    with pytest.raises(pdfsafe.PdfLimitError, match="resource limits"):
        pdfsafe.run_pdf("extract", str(tmp_path / "bad.pdf"))


def test_saturated_worker_pool_rejects_without_launching(monkeypatch, tmp_path):
    def never_launch(*args):
        pytest.fail("A third worker was launched")

    monkeypatch.setattr(pdfsafe, "_worker_command", never_launch)
    pdfsafe._slots.acquire()
    pdfsafe._slots.acquire()
    try:
        start = time.monotonic()
        with pytest.raises(pdfsafe.PdfLimitError, match="busy"):
            pdfsafe.run_pdf("extract", str(tmp_path / "unused.pdf"))
        assert time.monotonic() - start < 7
    finally:
        pdfsafe._slots.release()
        pdfsafe._slots.release()


def test_oversized_worker_response_is_rejected_before_read(monkeypatch, tmp_path):
    def command(request, output):
        return [sys.executable, "-c", "import sys; open(sys.argv[1], 'wb').write(b'x' * 1000)", str(output)]

    monkeypatch.setattr(pdfsafe, "PDF_OUTPUT_BYTES", 128)
    monkeypatch.setattr(pdfsafe, "_worker_command", command)
    with pytest.raises(pdfsafe.PdfLimitError, match="too much output"):
        pdfsafe.run_pdf("extract", str(tmp_path / "bad.pdf"))


def test_rejected_import_removes_upload(client):
    before = set(UPLOAD_DIR.iterdir())
    response = client.post("/api/documents", headers=AUTH,
                           files={"file": ("broken.pdf", b"%PDF-hostile", "application/pdf")})
    assert response.status_code == 400
    assert set(UPLOAD_DIR.iterdir()) == before


def test_extraction_page_and_text_limits(pdf_file, monkeypatch):
    monkeypatch.setattr(pdfsafe, "PDF_MAX_PAGES", 0)
    with pytest.raises(pdfsafe.PdfLimitError, match="too many pages"):
        chunker._extract_pdf_local(str(pdf_file), "Fallback")
    monkeypatch.setattr(pdfsafe, "PDF_MAX_PAGES", 2000)
    monkeypatch.setattr(pdfsafe, "PDF_MAX_TEXT", 8)
    with pytest.raises(pdfsafe.PdfLimitError, match="too much text"):
        chunker._extract_pdf_local(str(pdf_file), "Fallback")


def test_layout_word_limit(pdf_file, monkeypatch):
    monkeypatch.setattr(pdflayout, "PDF_MAX_WORDS", 1)
    with pytest.raises(pdfsafe.PdfLimitError, match="too many words"):
        pdflayout._sentence_rects_local(str(pdf_file), 1, "A readable sentence")


def test_failed_layout_preserves_empty_client_contract(monkeypatch):
    def fail(*args, **kwargs):
        raise pdfsafe.PdfLimitError("Deadline")

    monkeypatch.setattr(pdflayout, "run_pdf", fail)
    pdflayout._cache.clear()
    pdflayout._hit_cache.clear()
    assert pdflayout.sentence_rects(999, "broken.pdf", 1, "Sentence") == {
        "page": 1, "width": 0.0, "height": 0.0, "rotation": 0, "rects": [], "words": []}
    assert pdflayout.hit_at(999, "broken.pdf", 1, [(0, "Sentence")], 0, 0) is None


def test_layout_caches_are_bounded(monkeypatch):
    monkeypatch.setattr(pdflayout, "run_pdf", lambda operation, *args, **kwargs:
                        [] if operation == "hits" else {"rects": [], "words": []})
    pdflayout._cache.clear()
    pdflayout._hit_cache.clear()
    for page in range(300):
        pdflayout.sentence_rects(998, "file.pdf", page, "Sentence")
        pdflayout._hit_map(998, "file.pdf", page, [(0, "Sentence")])
    assert len(pdflayout._cache) == 256
    assert len(pdflayout._hit_cache) == 64
    pdflayout._cache.clear()
    pdflayout._hit_cache.clear()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux resource limits")
def test_linux_worker_sets_hard_native_limits(monkeypatch, tmp_path):
    def command(request, output):
        code = ("from cedar.pdfworker import _set_limits; _set_limits(); import resource, json, sys; "
                "limits = {str(k): resource.getrlimit(k) for k in "
                "[resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_FSIZE]}; "
                "open(sys.argv[1], 'w').write(json.dumps({'result': limits}))")
        return [sys.executable, "-c", code, str(output)]

    monkeypatch.setattr(pdfsafe, "_worker_command", command)
    limits = pdfsafe.run_pdf("extract", str(tmp_path / "unused.pdf"))
    import resource
    assert limits[str(resource.RLIMIT_AS)] == [pdfsafe.PDF_MEMORY_BYTES] * 2
    assert limits[str(resource.RLIMIT_CPU)] == [int(pdfsafe.PDF_TIMEOUT)] * 2
    assert limits[str(resource.RLIMIT_FSIZE)] == [pdfsafe.PDF_OUTPUT_BYTES] * 2


@pytest.mark.skipif(sys.platform != "linux", reason="Linux resource limits")
def test_linux_memory_limit_blocks_large_native_allocations(monkeypatch, tmp_path):
    script = tmp_path / "memory_worker.py"
    script.write_text(
        "from cedar.pdfworker import _set_limits\n"
        "from cedar.pdfsafe import PDF_MEMORY_BYTES\n"
        "import json, sys\n"
        "_set_limits()\n"
        "blocked = False\n"
        "try:\n"
        "    data = bytearray(PDF_MEMORY_BYTES * 2)\n"
        "except MemoryError:\n"
        "    blocked = True\n"
        "open(sys.argv[1], 'w').write(json.dumps({'result': blocked}))\n", encoding="utf-8")
    # Execute as code so the project root remains on the child's import path.
    def command(request, output):
        return [sys.executable, "-c", script.read_text(encoding="utf-8"), str(output)]

    monkeypatch.setattr(pdfsafe, "_worker_command", command)
    assert pdfsafe.run_pdf("extract", str(tmp_path / "unused.pdf")) is True
