"""Private PDF worker entry point. Set limits before importing native parsers."""
from __future__ import annotations

import base64
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

from .pdfsafe import PDF_MEMORY_BYTES, PDF_OUTPUT_BYTES, PDF_TIMEOUT, PdfLimitError


def _set_limits() -> None:
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (PDF_MEMORY_BYTES, PDF_MEMORY_BYTES))
        cpu = math.ceil(PDF_TIMEOUT)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (PDF_OUTPUT_BYTES, PDF_OUTPUT_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _thumbnail(path: str) -> str:
    import fitz

    with fitz.open(path) as doc:
        page = doc.load_page(0)
        width, height = page.rect.width, page.rect.height
        if not all(math.isfinite(v) and v > 0 for v in (width, height)):
            raise PdfLimitError("Invalid PDF page dimensions.")
        # Bound both axes: a very tall page must never allocate a huge bitmap.
        zoom = min(320 / width, 640 / height)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
        return base64.b64encode(pix.tobytes("jpeg", jpg_quality=82)).decode("ascii")


def main() -> None:
    _set_limits()
    request_path, output_path = map(Path, sys.argv[1:])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        operation, path = request["operation"], request["path"]
        if operation == "extract":
            from .chunker import _extract_pdf_local
            result = asdict(_extract_pdf_local(path, request["fallback_title"]))
        elif operation == "thumbnail":
            result = _thumbnail(path)
        elif operation == "rects":
            from .pdflayout import _sentence_rects_local
            result = _sentence_rects_local(path, request["page_no"], request["text"])
        elif operation == "hits":
            from .pdflayout import _hit_map_local
            result = _hit_map_local(path, request["page_no"], request["sentences"])
        else:
            raise ValueError("Unknown PDF operation")
        response = {"result": result}
    except Exception:
        # Parser errors may contain source contents or private server paths.
        response = {"error": "PDF could not be processed within resource limits."}
    raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
    if len(raw) > PDF_OUTPUT_BYTES:
        raise PdfLimitError("PDF processing produced too much output.")
    output_path.write_bytes(raw)


if __name__ == "__main__":
    main()
