"""Run untrusted PDF parsing in disposable, bounded worker processes.

Linux workers have address-space, CPU and output-file limits. Other platforms
retain process isolation, wall-clock deadlines, output and concurrency limits.
JSON is the only worker protocol; no executable deserialization is used.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PDF_TIMEOUT = 30.0
PDF_MEMORY_BYTES = 768 * 1024 * 1024
PDF_OUTPUT_BYTES = 32 * 1024 * 1024
PDF_MAX_PAGES = 2000
PDF_MAX_TEXT = 16 * 1024 * 1024
PDF_MAX_WORDS = 50000
_slots = threading.BoundedSemaphore(2)


class PdfLimitError(ValueError):
    """A PDF exceeded processing limits or could not be processed safely."""


def _worker_command(request: Path, output: Path) -> list[str]:
    return [sys.executable, "-m", "cedar.pdfworker", str(request), str(output)]


def run_pdf(operation: str, path: str, **arguments):
    """Bound the wait, native parsing and response for one PDF operation."""
    if not _slots.acquire(timeout=5):
        raise PdfLimitError("PDF processing is busy; please try again.")
    try:
        with tempfile.TemporaryDirectory(prefix="cedar-pdf-") as tmp:
            request, output = Path(tmp) / "request.json", Path(tmp) / "result.json"
            request.write_text(json.dumps({"operation": operation, "path": str(Path(path).resolve()),
                                           **arguments}), encoding="utf-8")
            # No pipes: parser diagnostics cannot fill a pipe or grow server RAM.
            with subprocess.Popen(_worker_command(request, output),
                                  cwd=str(Path(__file__).resolve().parent.parent),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL) as process:
                try:
                    process.wait(timeout=PDF_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise PdfLimitError("PDF processing exceeded the time limit.") from None
                if process.returncode != 0:
                    raise PdfLimitError("PDF could not be processed within resource limits.")
            if not output.exists() or output.stat().st_size > PDF_OUTPUT_BYTES:
                raise PdfLimitError("PDF processing produced too much output.")
            with output.open("rb") as response:
                raw = response.read(PDF_OUTPUT_BYTES + 1)
            if len(raw) > PDF_OUTPUT_BYTES:
                raise PdfLimitError("PDF processing produced too much output.")
            result = json.loads(raw)
            if "error" in result:
                raise PdfLimitError(result["error"])
            return result["result"]
    finally:
        _slots.release()
