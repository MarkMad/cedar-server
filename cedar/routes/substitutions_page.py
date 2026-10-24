"""The word-substitution editor: one static page, served without the key.

A browser cannot put an ``Authorization`` header on a page it is navigating
to, so the page itself has to be reachable without one (main.OPEN_PATHS). It
carries no data and no secrets: the owner pastes the key into it, and it
talks to the gated ``/api/settings/server`` like any other client.

The Content-Security-Policy allows exactly the one inline script, by hash, and
same-origin requests; nothing external can load into a page that holds a key.
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PATH = "/substitutions"

_PAGE = (Path(__file__).resolve().parent.parent / "static" / "substitutions.html").read_text(
    encoding="utf-8")


def _script_hash(page: str) -> str:
    match = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    if match is None:
        raise RuntimeError("substitutions.html has no inline script")
    digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


_CSP = ("default-src 'none'; script-src '" + _script_hash(_PAGE) + "'; "
        "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'")


@router.get(PATH, response_class=HTMLResponse)
def substitutions_page():
    return HTMLResponse(_PAGE, headers={"Content-Security-Policy": _CSP})
