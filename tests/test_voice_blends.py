"""Voice blends: what the picker offers, and what the engine is actually asked
for. A blend is one voice averaged from two, so the mix — not the id — is what
reaches Kokoro."""
import asyncio
from contextlib import asynccontextmanager

from conftest import AUTH
from cedar import blends, tts


def test_catalog_shape():
    for voice_id, (mix, name) in blends.BLENDS.items():
        assert name
        assert len(blends.components(mix)) >= 2
        # The id reads like an ordinary voice id, so clients that parse them
        # (and Cedar's own language lookup) keep working.
        assert voice_id.replace("_", "").isalnum()
        assert voice_id[0] in "abefhijpz"
        for part in blends.components(mix):
            assert part in mix


def test_resolve_expands_only_known_blends():
    assert blends.resolve("af_addie_lily") == "af_aoede(1)+bf_lily(1)"
    # A weighted blend keeps its weights.
    assert blends.resolve("af_addie_lily_2") == "af_aoede(2)+bf_lily(1)"
    # Anything else reaches the engine unchanged, including a mix of the
    # caller's own making.
    assert blends.resolve("af_heart") == "af_heart"
    assert blends.resolve("af_aoede+bf_lily") == "af_aoede+bf_lily"
    assert blends.resolve("af_aoede(1)+bf_lily(1)") == "af_aoede(1)+bf_lily(1)"


def test_is_mix():
    assert blends.is_mix("af_aoede+bf_lily")
    assert blends.is_mix("af_aoede(1)+bf_lily(1)")
    assert not blends.is_mix("af_heart")
    assert not blends.is_mix("af_aoede+")
    assert not blends.is_mix("af_aoede+bf lily")


def test_only_blends_the_engine_can_speak_are_offered():
    have = ["af_aoede", "bf_lily", "af_heart"]
    offered = blends.available(have)
    assert "af_addie_lily" in offered
    # af_bella is missing, so that blend is not on offer.
    assert "af_heart_bella" not in offered


def test_voice_list_carries_the_blends_and_their_names(client):
    v = client.get("/api/voices", headers=AUTH).json()
    assert "af_addie_lily" in v["voices"]
    assert v["names"]["af_addie_lily"] == "Addie & Lily"
    # A weighted blend is offered next to the even one.
    assert "af_addie_lily_2" in v["voices"]
    assert v["names"]["af_addie_lily_2"] == "Addie & Lily (2:1)"
    assert "af_heart_bella" in v["names"]
    # The engine's own voices are still listed, unchanged.
    assert "af_heart" in v["voices"] and v["defaults"]["en"]


def test_preview_speaks_a_blend(client):
    # A catalog blend…
    assert client.get("/api/voices/af_addie_lily/preview", headers=AUTH).status_code == 200
    # …and a mix the caller made up.
    assert client.get("/api/voices/af_aoede(1)+bf_lily(1)/preview",
                      headers=AUTH).status_code == 200
    assert client.get("/api/voices/not_a_voice/preview", headers=AUTH).status_code == 400


def test_a_blend_can_be_chosen_like_any_other_voice(client):
    # The account's voice picks are shared state, so put back what was there.
    before = client.get("/api/settings", headers=AUTH).json()["voices"].get("en", "af_heart")
    try:
        r = client.put("/api/settings", headers=AUTH, json={"voice": "af_addie_lily"})
        assert r.status_code == 200
        assert client.get("/api/settings", headers=AUTH).json()["voices"]["en"] == "af_addie_lily"
    finally:
        client.put("/api/settings", headers=AUTH, json={"voice": before})


def test_a_blend_reads_a_document(client):
    before = client.get("/api/settings", headers=AUTH).json()["voices"].get("en", "af_heart")
    # Long enough for the language detector to be sure it is English.
    text = (
        "It was the best of times, it was the worst of times, it was the age of "
        "wisdom, it was the age of foolishness, it was the epoch of belief, it was "
        "the epoch of incredulity, it was the season of Light, it was the season of "
        "Darkness, it was the spring of hope, it was the winter of despair, we had "
        "everything before us, we had nothing before us, we were all going direct "
        "to Heaven, we were all going direct the other way. There were a king with "
        "a large jaw and a queen with a plain face, on the throne of England; there "
        "were a king with a large jaw and a queen with a fair face, on the throne of "
        "France. In both countries it was clearer than crystal to the lords of "
        "the State preserves of loaves and fishes, that things in general were "
        "settled for ever."
    )
    doc = client.post("/api/documents/from_text", headers=AUTH,
                      json={"text": text, "title": "Blend"}).json()["id"]
    try:
        r = client.post(f"/api/documents/{doc}/voice", headers=AUTH,
                        json={"voice": "af_addie_lily"}).json()
        # A blend speaks the language of its first voice, so it is an ordinary
        # pick for that language.
        assert r["scope"] == "language" and r["lang"] == "en"
        assert client.get(f"/api/documents/{doc}", headers=AUTH).json()["voice"] == "af_addie_lily"
    finally:
        client.put("/api/settings", headers=AUTH, json={"voice": before})
        client.delete(f"/api/documents/{doc}", headers=AUTH)


def test_a_blend_reads_a_captured_page(client):
    clip = client.post("/api/live/tts", headers=AUTH,
                       json={"text": "Hello there.", "voice": "af_addie_lily"}).json()
    assert clip["format"] == "mp3"


def test_the_engine_is_asked_for_the_mix(monkeypatch):
    seen = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"audio": "", "timestamps": []}

    class _Client:
        async def post(self, url, json=None, **_kwargs):
            seen.append(json)
            return _Response()

    @asynccontextmanager
    async def _client():
        yield _Client()

    monkeypatch.setattr(tts, "_synthesis_client", _client)
    asyncio.run(tts._ask_kokoro("Hello there.", "af_addie_lily", "mp3"))
    assert seen[0]["voice"] == "af_aoede(1)+bf_lily(1)"


def test_a_blend_and_its_mix_share_one_cache_entry():
    # The cache is keyed by the voice the engine speaks, so the catalog id and
    # the mix behind it are one entry, not two.
    assert tts._key("Hello there.", "af_addie_lily") == \
        tts._key("Hello there.", "af_aoede(1)+bf_lily(1)")
    assert tts._key("Hello there.", "af_addie_lily") != \
        tts._key("Hello there.", "af_heart_bella")
