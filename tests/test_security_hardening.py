from conftest import AUTH

from cedar import db, ratelimit
from cedar.config import MEDIA_DIR


def test_limiter_fails_closed_at_capacity_without_forgetting_active_peers(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock[0])
    limiter = ratelimit.SlidingWindow(limit=1, window_s=10, max_keys=2)
    assert limiter.allow("first")
    assert limiter.allow("second")
    for i in range(100):
        assert not limiter.allow(f"new-{i}")
    assert len(limiter._log) == 2
    assert not limiter.allow("first")
    clock[0] = 11.0
    assert limiter.allow("new-peer")
    assert len(limiter._log) <= 2


def test_document_thumbnail_is_private_and_requires_key(client, monkeypatch):
    monkeypatch.setattr(db, "document_exists", lambda doc_id: True)
    monkeypatch.setattr(db, "get_document_filename", lambda doc_id: "test.pdf")
    path = MEDIA_DIR / "123456" / "cover.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"thumbnail")
    response = client.get("/api/documents/123456/thumb", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("private,")
    assert client.get("/api/documents/123456/thumb").status_code == 401


def test_catalog_cover_is_private(client, monkeypatch, tmp_path):
    from cedar import catalog
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"cover")
    monkeypatch.setattr(catalog, "cover_path", lambda gid: cover)
    response = client.get("/api/catalog/books/1/cover", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("private,")
