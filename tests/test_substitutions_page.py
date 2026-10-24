"""The substitution editor page: open to fetch, and nothing else is opened with it."""
from __future__ import annotations

import base64
import hashlib
import re

from conftest import AUTH


def test_the_editor_page_needs_no_key(client):
    r = client.get("/substitutions")
    assert r.status_code == 200
    assert "Word substitutions" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_the_page_holds_no_data_and_is_not_cached(client):
    r = client.get("/substitutions")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    # No rules, no key: the page is the same for everyone.
    assert AUTH["Authorization"].split()[-1] not in r.text


def test_the_csp_allows_only_its_own_script_and_same_origin_calls(client):
    r = client.get("/substitutions")
    csp = r.headers["content-security-policy"]
    script = re.search(r"<script>(.*?)</script>", r.text, re.DOTALL).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # Nothing external is loaded by the page itself.
    assert not re.search(r"""(src|href)=["']https?:""", r.text)


def test_only_the_exact_path_is_open(client):
    for path in ("/substitutions/", "/substitutions/x", "/substitutions.html",
                 "/static/substitutions.html", "/Substitutions"):
        assert client.get(path).status_code == 401, path


def test_a_forged_host_does_not_open_other_paths(client):
    r = client.get("/api/settings/server", headers={"Host": "x/substitutions?"})
    assert r.status_code == 401


def test_the_api_behind_the_page_is_still_gated(client):
    assert client.get("/api/settings/server").status_code == 401
    assert client.put("/api/settings/server", json={"substitutions": {}}).status_code == 401
    wrong = client.put("/api/settings/server", json={"substitutions": {}},
                       headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert client.get("/api/settings/server", headers=AUTH).status_code == 200
