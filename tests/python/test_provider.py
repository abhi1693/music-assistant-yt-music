"""Unit tests for the YouTube Music provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# Imports below are resolved against the stubs registered in conftest.py.
from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
)
from music_assistant_models.streamdetails import StreamDetails

import ytmusic as ytm


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_module_constants_present():
    assert ytm.YTM_DOMAIN == "https://music.youtube.com"
    assert ytm.VARIOUS_ARTISTS_YTM_ID == "UCUTXlgdcKU5vfzFqHOWIvkA"
    assert ytm.DEFAULT_STREAM_URL_EXPIRATION == 3600


def test_base_features_are_anonymous_safe():
    assert ProviderFeature.SEARCH in ytm.BASE_FEATURES
    assert ProviderFeature.BROWSE in ytm.BASE_FEATURES
    assert ProviderFeature.ARTIST_ALBUMS in ytm.BASE_FEATURES
    assert ProviderFeature.ARTIST_TOPTRACKS in ytm.BASE_FEATURES
    assert ProviderFeature.SIMILAR_TRACKS in ytm.BASE_FEATURES


def test_authenticated_features_separate_from_base():
    overlap = ytm.BASE_FEATURES & ytm.AUTHENTICATED_FEATURES
    assert overlap == set(), "library/auth features must not double-up with base set"
    assert ProviderFeature.LIBRARY_TRACKS in ytm.AUTHENTICATED_FEATURES
    assert ProviderFeature.RECOMMENDATIONS in ytm.AUTHENTICATED_FEATURES


def test_auth_constants():
    assert ytm.AUTH_TYPE_NONE == "none"
    assert ytm.AUTH_TYPE_COOKIE == "cookie"
    assert ytm.CONF_AUTH_TYPE == "auth_type"
    assert ytm.CONF_COOKIE == "cookie_header"


# ---------------------------------------------------------------------------
# _yt_playlist_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("playlist_id", "expected"),
    [
        ("VLPLxxx123", "https://www.youtube.com/playlist?list=PLxxx123"),
        ("PLxxx123", "https://www.youtube.com/playlist?list=PLxxx123"),
        ("OLAK5uy_abc", "https://www.youtube.com/playlist?list=OLAK5uy_abc"),
        ("VLOLAK5uy_abc", "https://www.youtube.com/playlist?list=OLAK5uy_abc"),
    ],
)
def test_yt_playlist_url_strips_vl_prefix(playlist_id, expected):
    assert ytm.YoutubeMusicProvider._yt_playlist_url(playlist_id) == expected


# ---------------------------------------------------------------------------
# Cookie / auth header building
# ---------------------------------------------------------------------------


def _forbid_open(monkeypatch):
    """Fail the test if anything tries to open a file.

    Auth headers are built in memory so several provider instances can hold
    different credentials at once (issue #40). Writing them to disk is the
    exact regression these tests guard against.
    """
    monkeypatch.setattr(
        "builtins.open",
        lambda *a, **kw: pytest.fail("auth headers must not be written to disk"),
    )


def test_build_auth_headers_rejects_cookie_without_secure_3papisid(provider, monkeypatch):
    _forbid_open(monkeypatch)
    with pytest.raises(ValueError, match="__Secure-3PAPISID"):
        provider._build_auth_headers("SID=abc; HSID=def")


def test_build_auth_headers_rejects_cookie_with_no_extractable_sapisid(provider, monkeypatch):
    # __Secure-3PAPISID present in the string but only as a substring,
    # never as its own `name=value` pair.
    _forbid_open(monkeypatch)
    with pytest.raises(ValueError, match="SAPISID"):
        provider._build_auth_headers("note=__Secure-3PAPISID-mention; SID=abc")


def test_build_auth_headers_extracts_sapisid_when_present(provider, monkeypatch):
    _forbid_open(monkeypatch)
    cookie = "SAPISID=mySapisid; __Secure-3PAPISID=otherValue; SID=foo"
    headers = provider._build_auth_headers(cookie)

    assert isinstance(headers, dict)
    assert headers["cookie"] == cookie
    assert headers["origin"] == ytm.YTM_DOMAIN
    assert headers["x-origin"] == ytm.YTM_DOMAIN
    # Authorization is SAPISIDHASH <ts>_<sha1(<ts> <sapisid> <origin>)>
    assert headers["authorization"].startswith("SAPISIDHASH ")
    ts_str, hash_str = headers["authorization"][len("SAPISIDHASH "):].split("_")
    assert ts_str.isdigit()
    assert int(ts_str) <= int(time.time()) + 5
    assert len(hash_str) == 40  # sha1 hex digest


def test_build_auth_headers_falls_back_to_secure_3papisid_when_sapisid_missing(
    provider, monkeypatch
):
    _forbid_open(monkeypatch)
    cookie = "__Secure-3PAPISID=fallbackValue; SID=foo"
    headers = provider._build_auth_headers(cookie)
    # The hash uses the extracted SAPISID — we can't see the secret, but we can
    # confirm the same input produces a stable-shape header.
    assert headers["authorization"].startswith("SAPISIDHASH ")


def test_build_auth_headers_json_serializable(provider, monkeypatch):
    """ytmusicapi copies the dict into a CaseInsensitiveDict; keep it plain."""
    _forbid_open(monkeypatch)
    headers = provider._build_auth_headers("__Secure-3PAPISID=a; SAPISID=b")
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
    json.loads(json.dumps(headers))


def test_build_auth_headers_satisfies_ytmusicapi_browser_contract(provider, monkeypatch):
    """The three keys ytmusicapi actually reads at construction time.

    `determine_auth_type` classifies the session as BROWSER only when an
    `authorization` header contains "SAPISIDHASH"; `sapisid_from_cookie` needs
    `__Secure-3PAPISID` in `cookie`; and one of `origin` / `x-origin` is read
    for the per-request hash. Drop any of them and auth degrades silently.
    """
    _forbid_open(monkeypatch)
    headers = provider._build_auth_headers("__Secure-3PAPISID=a; SAPISID=b")
    assert "SAPISIDHASH" in headers["authorization"]
    assert "__Secure-3PAPISID" in headers["cookie"]
    assert headers.get("origin") or headers.get("x-origin")


# ---------------------------------------------------------------------------
# Multi-instance isolation (issue #40)
# ---------------------------------------------------------------------------


def _make_provider(instance_id):
    """Build a second provider instance the way the `provider` fixture does."""
    instance = ytm.YoutubeMusicProvider(mass=None, manifest=None, config=None)
    instance.instance_id = instance_id
    instance._ytmusic = None
    instance._authenticated = False
    return instance


def test_two_instances_build_independent_auth_headers(monkeypatch):
    """Two accounts must never end up sharing a credentials object."""
    _forbid_open(monkeypatch)
    alice = _make_provider("inst_alice")
    bob = _make_provider("inst_bob")

    alice_cookie = "__Secure-3PAPISID=alice; SAPISID=alice2"
    bob_cookie = "__Secure-3PAPISID=bob; SAPISID=bob2"
    alice_headers = alice._build_auth_headers(alice_cookie)
    bob_headers = bob._build_auth_headers(bob_cookie)

    assert alice_headers is not bob_headers
    assert alice_headers["cookie"] == alice_cookie
    assert bob_headers["cookie"] == bob_cookie
    # Mutating one must not reach the other.
    alice_headers["cookie"] = "tampered"
    assert bob_headers["cookie"] == bob_cookie


def test_build_auth_headers_defaults_to_account_index_zero(provider, monkeypatch):
    _forbid_open(monkeypatch)
    headers = provider._build_auth_headers("__Secure-3PAPISID=a; SAPISID=b")
    assert headers["x-goog-authuser"] == "0"


def test_build_auth_headers_honors_the_account_index(provider, monkeypatch):
    """A browser signed in to several Google accounts sends one shared cookie.

    X-Goog-AuthUser is the only thing that says which of those accounts a
    request resolves to, so two instances must be able to differ here.
    """
    _forbid_open(monkeypatch)
    headers = provider._build_auth_headers("__Secure-3PAPISID=a; SAPISID=b", 2)
    assert headers["x-goog-authuser"] == "2"


def test_two_instances_can_target_different_accounts_of_one_cookie(monkeypatch):
    _forbid_open(monkeypatch)
    shared_cookie = "__Secure-3PAPISID=shared; SAPISID=shared2"
    first = _make_provider("inst_first")._build_auth_headers(shared_cookie, 0)
    second = _make_provider("inst_second")._build_auth_headers(shared_cookie, 1)

    assert first["cookie"] == second["cookie"]  # same browser session
    assert first["x-goog-authuser"] == "0"
    assert second["x-goog-authuser"] == "1"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 0),
        (0, 0),
        (1, 1),
        ("2", 2),
        ("", 0),
        ("not_a_number", 0),
        (-1, 0),
    ],
)
def test_configured_auth_user_coerces_safely(provider, configured, expected):
    provider.config = _StubConfig({ytm.CONF_AUTH_USER: configured})
    assert provider._configured_auth_user() == expected


def test_build_auth_headers_returns_a_fresh_dict_each_call(provider, monkeypatch):
    _forbid_open(monkeypatch)
    cookie = "__Secure-3PAPISID=a; SAPISID=b"
    first = provider._build_auth_headers(cookie)
    second = provider._build_auth_headers(cookie)
    assert first is not second


def test_instance_name_postfix_is_never_none_or_empty():
    """MA formats this property directly, so None renders as a literal "[None]".

    `Provider.default_name` builds a numeric fallback into a local variable
    and then interpolates `self.instance_name_postfix` instead, so the
    fallback never reaches the name. The postfix also lands in library data,
    since playlist owners fall back to the provider name.
    """
    instance = _make_provider("ytmusic--abcdef123456")
    instance.config = _StubConfig({})
    postfix = instance.instance_name_postfix
    assert postfix
    assert postfix is not None
    assert "None" not in postfix


def test_instance_name_postfix_differs_between_instances():
    first = _make_provider("ytmusic--aaaaaaaa1111")
    second = _make_provider("ytmusic--bbbbbbbb2222")
    first.config = _StubConfig({})
    second.config = _StubConfig({})
    assert first.instance_name_postfix != second.instance_name_postfix


def test_instance_name_postfix_prefers_the_brand_account():
    instance = _make_provider("ytmusic--abcdef123456")
    instance.config = _StubConfig({ytm.CONF_BRAND_ACCOUNT: "112233445566"})
    assert instance.instance_name_postfix == "112233445566"


def test_instance_name_postfix_uses_the_account_index_when_set():
    instance = _make_provider("ytmusic--abcdef123456")
    instance.config = _StubConfig({ytm.CONF_AUTH_USER: 2})
    assert instance.instance_name_postfix == "account 2"


def test_instance_name_postfix_survives_a_missing_config():
    instance = _make_provider("ytmusic--abcdef123456")
    instance.config = None
    assert instance.instance_name_postfix


def test_library_seen_nonempty_is_not_shared_between_instances():
    """A class-level `= {}` default here would cross-contaminate accounts."""
    assert "_library_seen_nonempty" not in vars(ytm.YoutubeMusicProvider)

    alice = _make_provider("inst_alice")
    bob = _make_provider("inst_bob")
    alice._record_library_count("tracks", 5)

    assert alice._library_seen_nonempty == {"tracks": True}
    # Bob's library is genuinely empty; Alice having tracks must not make
    # Bob look like a partial-auth lapse (issue #10).
    assert bob._record_library_count("tracks", 0) is False


def test_create_ytmusic_client_passes_headers_and_brand_account_through(provider, monkeypatch):
    """The headers dict reaches YTMusic untouched, alongside the brand account."""
    captured = {}

    class _FakeYTMusic:
        def __init__(self, auth=None, user=None):
            captured["auth"] = auth
            captured["user"] = user

    fake_module = MagicMock()
    fake_module.YTMusic = _FakeYTMusic
    monkeypatch.setattr(ytm.importlib, "import_module", lambda name: fake_module)

    headers = {"cookie": "__Secure-3PAPISID=a", "authorization": "SAPISIDHASH x_y"}
    provider._create_ytmusic_client(auth=headers, user="brand123")

    assert captured["auth"] == headers
    assert captured["user"] == "brand123"


def test_create_ytmusic_client_anonymous_passes_no_auth(provider, monkeypatch):
    captured = {}

    class _FakeYTMusic:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            captured["called_with"] = kwargs

    fake_module = MagicMock()
    fake_module.YTMusic = _FakeYTMusic
    monkeypatch.setattr(ytm.importlib, "import_module", lambda name: fake_module)

    provider._create_ytmusic_client()
    assert captured["called_with"] == {}


class _StubConfig:
    """Minimal stand-in for MA's ProviderConfig."""

    def __init__(self, values):
        self._values = values

    def get_value(self, key):
        return self._values.get(key)


def _setup_instance(monkeypatch, instance_id, values):
    """Run handle_async_init against stubs and return the provider.

    Package installation and client construction are stubbed out: neither
    yt-dlp nor ytmusicapi is installed in the test environment.
    """
    instance = _make_provider(instance_id)
    instance.config = _StubConfig(values)
    _forbid_open(monkeypatch)
    created = []

    async def _noop():
        return None

    def _fake_client(auth=None, user=None):
        created.append({"auth": auth, "user": user})
        return MagicMock()

    monkeypatch.setattr(instance, "_install_packages", _noop)
    monkeypatch.setattr(instance, "_purge_legacy_auth_file", _noop)
    monkeypatch.setattr(instance, "_create_ytmusic_client", _fake_client)
    asyncio.run(instance.handle_async_init())
    instance._created_clients = created
    return instance


def test_prefer_quality_false_is_honored(monkeypatch):
    """A configured False must survive; `or True` used to swallow it."""
    instance = _setup_instance(
        monkeypatch, "inst_low", {ytm.CONF_PREFER_AUDIO_QUALITY: False}
    )
    assert instance._prefer_quality is False


def test_prefer_quality_defaults_to_true_when_unset(monkeypatch):
    instance = _setup_instance(monkeypatch, "inst_default", {})
    assert instance._prefer_quality is True


def test_two_instances_keep_independent_quality_settings(monkeypatch):
    """Per-instance config is the point of multi-instance support."""
    high = _setup_instance(
        monkeypatch, "inst_high", {ytm.CONF_PREFER_AUDIO_QUALITY: True}
    )
    low = _setup_instance(
        monkeypatch, "inst_low", {ytm.CONF_PREFER_AUDIO_QUALITY: False}
    )
    assert high._prefer_quality is True
    assert low._prefer_quality is False


def test_anonymous_instance_builds_client_without_auth(monkeypatch):
    instance = _setup_instance(
        monkeypatch, "inst_anon", {ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_NONE}
    )
    assert instance._authenticated is False
    assert instance._created_clients == [{"auth": None, "user": None}]


def test_cookie_instance_passes_headers_dict_not_a_path(monkeypatch):
    """The regression guard for issue #40: no filename ever reaches ytmusicapi."""
    instance = _setup_instance(
        monkeypatch,
        "inst_cookie",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "__Secure-3PAPISID=a; SAPISID=b",
            ytm.CONF_BRAND_ACCOUNT: "brand42",
        },
    )
    call = instance._created_clients[0]
    assert isinstance(call["auth"], dict)
    assert call["user"] == "brand42"
    assert "SAPISIDHASH" in call["auth"]["authorization"]
    assert instance._authenticated is True


def test_two_cookie_instances_do_not_share_credentials(monkeypatch):
    """Different accounts, different headers, no shared file to overwrite."""
    alice = _setup_instance(
        monkeypatch,
        "inst_alice",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "__Secure-3PAPISID=alice; SAPISID=alice2",
        },
    )
    bob = _setup_instance(
        monkeypatch,
        "inst_bob",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "__Secure-3PAPISID=bob; SAPISID=bob2",
            ytm.CONF_BRAND_ACCOUNT: "brand_bob",
        },
    )
    alice_auth = alice._created_clients[0]["auth"]
    bob_auth = bob._created_clients[0]["auth"]

    assert "alice" in alice_auth["cookie"]
    assert "bob" in bob_auth["cookie"]
    assert alice_auth["cookie"] != bob_auth["cookie"]
    assert alice._created_clients[0]["user"] is None
    assert bob._created_clients[0]["user"] == "brand_bob"


def test_configured_account_index_reaches_the_client(monkeypatch):
    """Two household members sharing one browser need this to differ."""
    instance = _setup_instance(
        monkeypatch,
        "inst_second_user",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "__Secure-3PAPISID=a; SAPISID=b",
            ytm.CONF_AUTH_USER: 1,
        },
    )
    assert instance._created_clients[0]["auth"]["x-goog-authuser"] == "1"


def test_cookie_instance_falls_back_to_anonymous_on_bad_cookie(monkeypatch):
    """An unusable cookie must not take the instance down."""
    instance = _setup_instance(
        monkeypatch,
        "inst_bad",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "SID=no_papisid_here",
        },
    )
    assert instance._authenticated is False
    # Second call is the anonymous retry after _build_auth_headers raised.
    assert instance._created_clients[-1] == {"auth": None, "user": None}


# ---------------------------------------------------------------------------
# Legacy auth file cleanup (issue #40)
# ---------------------------------------------------------------------------


def test_legacy_auth_file_constant_is_the_old_hardcoded_path():
    assert ytm.LEGACY_AUTH_FILE == "/data/ytmusic_browser_auth.json"


def test_purge_legacy_auth_file_removes_a_leftover_file(provider, tmp_path, monkeypatch):
    stale = tmp_path / "ytmusic_browser_auth.json"
    stale.write_text('{"cookie": "secret"}', encoding="utf-8")
    monkeypatch.setattr(ytm, "LEGACY_AUTH_FILE", str(stale))

    asyncio.run(provider._purge_legacy_auth_file())

    assert not stale.exists()


def test_purge_legacy_auth_file_is_a_noop_when_absent(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(ytm, "LEGACY_AUTH_FILE", str(tmp_path / "not_here.json"))
    # Must not raise.
    asyncio.run(provider._purge_legacy_auth_file())


def test_purge_legacy_auth_file_survives_an_unremovable_file(provider, monkeypatch):
    """A read-only or missing /data must never block provider setup."""
    monkeypatch.setattr(ytm.os.path, "exists", lambda _p: True)

    def _boom(_path):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(ytm.os, "remove", _boom)
    # Must not raise.
    asyncio.run(provider._purge_legacy_auth_file())


# ---------------------------------------------------------------------------
# _parse_track
# ---------------------------------------------------------------------------


def test_parse_track_minimal(provider):
    track = provider._parse_track(
        {
            "videoId": "abc123",
            "title": "Some Song",
            "artists": [{"id": "UCart", "name": "An Artist"}],
        }
    )
    assert track.item_id == "abc123"
    assert track.name == "Some Song"
    assert track.artists[0].item_id == "UCart"
    assert track.artists[0].name == "An Artist"
    mappings = list(track.provider_mappings)
    assert mappings[0].item_id == "abc123"
    assert mappings[0].provider_domain == "ytmusic"
    assert mappings[0].url == f"{ytm.YTM_DOMAIN}/watch?v=abc123"


def test_parse_track_missing_video_id_raises(provider):
    with pytest.raises(InvalidDataError, match="videoId"):
        provider._parse_track({"title": "no id"})


def test_parse_track_missing_artists_raises(provider):
    with pytest.raises(InvalidDataError, match="artists"):
        provider._parse_track({"videoId": "abc", "title": "x", "artists": []})


def test_parse_track_artist_fallback_when_id_missing(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Song",
            "artists": [{"name": "Solo Singer"}],
        }
    )
    assert track.artists[0].name == "Solo Singer"
    assert track.artists[0].item_id == "unknown_Solo Singer"


def test_parse_track_various_artists_resolves_to_canonical_id(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Compilation Song",
            "artists": [{"name": "Various Artists"}],
        }
    )
    assert track.artists[0].item_id == ytm.VARIOUS_ARTISTS_YTM_ID


def test_parse_track_artist_uses_browse_id_and_artist_keys(provider):
    """A browseId/artist-keyed track artist resolves to a real id, not unknown_*."""
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Song",
            "artists": [{"browseId": "UCart", "artist": "Real Artist"}],
        }
    )
    assert track.artists[0].item_id == "UCart"
    assert track.artists[0].name == "Real Artist"


def test_parse_track_various_artists_via_artist_key(provider):
    """Track artist keyed only as artist='Various Artists' resolves to canonical id."""
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Compilation",
            "artists": [{"artist": "Various Artists"}],
        }
    )
    assert track.artists[0].item_id == ytm.VARIOUS_ARTISTS_YTM_ID
    assert track.artists[0].name == "Various Artists"


def test_parse_track_duration_from_seconds(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
            "duration_seconds": "245",
        }
    )
    assert track.duration == 245


def test_parse_track_duration_from_int(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
            "duration": "180",
        }
    )
    assert track.duration == 180


def test_parse_track_album_mapping(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
            "album": {"id": "MPREb_album", "name": "Album Name"},
        }
    )
    assert track.album.item_id == "MPREb_album"
    assert track.album.name == "Album Name"
    assert track.album.media_type == MediaType.ALBUM


def test_parse_track_track_number_kwarg(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
        },
        track_number=7,
    )
    assert track.track_number == 7


# ---------------------------------------------------------------------------
# _parse_album
# ---------------------------------------------------------------------------


def test_parse_album_basic(provider):
    album = provider._parse_album(
        {
            "browseId": "MPREb_xyz",
            "title": "An Album",
            "artists": [{"id": "UC1", "name": "Artist"}],
            "year": "2023",
            "type": "Album",
        }
    )
    assert album.item_id == "MPREb_xyz"
    assert album.name == "An Album"
    assert album.year == 2023
    assert album.album_type == AlbumType.ALBUM


def test_parse_album_missing_id_raises(provider):
    with pytest.raises(InvalidDataError, match="ID"):
        provider._parse_album({"title": "no id"})


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("Single", AlbumType.SINGLE),
        ("EP", AlbumType.EP),
        ("Album", AlbumType.ALBUM),
        ("", AlbumType.UNKNOWN),
        ("Compilation", AlbumType.UNKNOWN),
    ],
)
def test_parse_album_type_mapping(provider, raw_type, expected):
    album = provider._parse_album(
        {"browseId": "MPREb_x", "title": "A", "type": raw_type}
    )
    assert album.album_type == expected


def test_parse_album_explicit_id_argument_wins(provider):
    album = provider._parse_album(
        {"browseId": "ignored", "title": "A"}, album_id="explicit-id"
    )
    assert album.item_id == "explicit-id"


def test_parse_album_inferred_live(provider):
    album = provider._parse_album(
        {"browseId": "MPREb_live", "title": "Live at the Apollo", "type": "Album"}
    )
    assert album.album_type == AlbumType.LIVE


def test_parse_album_artist_uses_browse_id_and_artist_keys(provider):
    """Album artists from search results are keyed browseId/artist, not id/name."""
    album = provider._parse_album(
        {
            "browseId": "MPREb_x",
            "title": "A",
            "artists": [{"browseId": "UCartist", "artist": "Album Artist"}],
        }
    )
    assert len(album.artists) == 1
    assert album.artists[0].item_id == "UCartist"
    assert album.artists[0].name == "Album Artist"


def test_parse_album_artist_various_artists_via_artist_key(provider):
    album = provider._parse_album(
        {
            "browseId": "MPREb_x",
            "title": "A",
            "artists": [{"artist": "Various Artists"}],
        }
    )
    assert len(album.artists) == 1
    assert album.artists[0].item_id == ytm.VARIOUS_ARTISTS_YTM_ID


# ---------------------------------------------------------------------------
# _parse_artist
# ---------------------------------------------------------------------------


def test_parse_artist_basic(provider):
    artist = provider._parse_artist(
        {"channelId": "UCabc", "name": "An Artist"}
    )
    assert artist.item_id == "UCabc"
    assert artist.name == "An Artist"


def test_parse_artist_uses_id_field_when_channelid_missing(provider):
    artist = provider._parse_artist({"id": "UC123", "name": "Other"})
    assert artist.item_id == "UC123"


def test_parse_artist_various_artists_canonical_id(provider):
    artist = provider._parse_artist({"name": "Various Artists"})
    assert artist.item_id == ytm.VARIOUS_ARTISTS_YTM_ID


def test_parse_artist_missing_id_raises(provider):
    with pytest.raises(InvalidDataError, match="ID"):
        provider._parse_artist({"name": "Mystery"})


def test_parse_artist_uses_browse_id_and_artist_keys(provider):
    """Search results (filter='artists') key the id as browseId, name as artist."""
    artist = provider._parse_artist({"browseId": "UCxyz", "artist": "Some Artist"})
    assert artist.item_id == "UCxyz"
    assert artist.name == "Some Artist"


def test_parse_artist_channel_id_takes_precedence_over_browse_id(provider):
    artist = provider._parse_artist(
        {"channelId": "UCchan", "browseId": "UCbrowse", "name": "A"}
    )
    assert artist.item_id == "UCchan"


def test_parse_artist_name_key_preferred_over_artist_key(provider):
    artist = provider._parse_artist(
        {"browseId": "UCx", "name": "Real Name", "artist": "Alt Name"}
    )
    assert artist.name == "Real Name"


def test_parse_artist_various_artists_via_artist_key(provider):
    artist = provider._parse_artist({"artist": "Various Artists"})
    assert artist.item_id == ytm.VARIOUS_ARTISTS_YTM_ID


# ---------------------------------------------------------------------------
# _get_artist_item_mapping
# ---------------------------------------------------------------------------


def test_artist_item_mapping_id_precedence(provider):
    mapping = provider._get_artist_item_mapping(
        {"id": "UCid", "channelId": "UCchan", "browseId": "UCbrowse", "name": "A"}
    )
    assert mapping.media_type == MediaType.ARTIST
    assert mapping.item_id == "UCid"
    assert mapping.name == "A"


def test_artist_item_mapping_browse_id_and_artist_keys(provider):
    mapping = provider._get_artist_item_mapping(
        {"browseId": "UCbrowse", "artist": "Search Artist"}
    )
    assert mapping.item_id == "UCbrowse"
    assert mapping.name == "Search Artist"


def test_artist_item_mapping_various_artists_via_artist_key(provider):
    mapping = provider._get_artist_item_mapping({"artist": "Various Artists"})
    assert mapping.item_id == ytm.VARIOUS_ARTISTS_YTM_ID


def test_artist_item_mapping_empty_name_falls_back_to_unknown(provider):
    """A truthy id with a present-but-empty name/artist must not yield a blank name."""
    assert provider._get_artist_item_mapping(
        {"channelId": "UCx", "artist": ""}
    ).name == "Unknown"
    assert provider._get_artist_item_mapping(
        {"browseId": "UCy", "name": "", "artist": None}
    ).name == "Unknown"


def test_parse_artist_empty_name_falls_back_to_unknown(provider):
    artist = provider._parse_artist({"browseId": "UCz", "name": "", "artist": ""})
    assert artist.name == "Unknown Artist"


# ---------------------------------------------------------------------------
# _parse_playlist
# ---------------------------------------------------------------------------


def test_parse_playlist_id_field(provider):
    playlist = provider._parse_playlist({"id": "PL123", "title": "P"})
    assert playlist.item_id == "PL123"
    assert playlist.is_editable is False


def test_parse_playlist_falls_back_to_browse_id(provider):
    playlist = provider._parse_playlist({"browseId": "VLPL456", "title": "P"})
    assert playlist.item_id == "VLPL456"


def test_parse_playlist_owner_string(provider):
    playlist = provider._parse_playlist(
        {"id": "PL", "title": "P", "author": "Some User"}
    )
    assert playlist.owner == "Some User"


def test_parse_playlist_owner_list_of_dicts(provider):
    playlist = provider._parse_playlist(
        {"id": "PL", "title": "P", "author": [{"name": "First"}, {"name": "Second"}]}
    )
    assert playlist.owner == "First"


def test_parse_playlist_owner_dict(provider):
    playlist = provider._parse_playlist(
        {"id": "PL", "title": "P", "author": {"name": "Channel"}}
    )
    assert playlist.owner == "Channel"


def test_parse_playlist_owner_default_to_provider_name(provider):
    playlist = provider._parse_playlist({"id": "PL", "title": "P"})
    assert playlist.owner == provider.name


# ---------------------------------------------------------------------------
# _parse_thumbnails
# ---------------------------------------------------------------------------


def test_parse_thumbnails_picks_largest_first(provider):
    thumbs = [
        {"url": "https://example/a=w200-h200", "width": 200, "height": 200},
        {"url": "https://example/a=w800-h800", "width": 800, "height": 800},
        {"url": "https://example/a=w400-h400", "width": 400, "height": 400},
    ]
    images = provider._parse_thumbnails(thumbs)
    assert len(images) == 1
    assert "w800" in images[0].path or "w600" in images[0].path
    assert images[0].type == ImageType.THUMB


def test_parse_thumbnails_landscape_for_maxres(provider):
    thumbs = [
        {"url": "https://example/maxresdefault.jpg", "width": 1280, "height": 720},
    ]
    images = provider._parse_thumbnails(thumbs)
    assert images[0].type == ImageType.LANDSCAPE


def test_parse_thumbnails_skips_empty_url(provider):
    thumbs = [{"url": "", "width": 800, "height": 800}]
    images = provider._parse_thumbnails(thumbs)
    assert images == []


def test_parse_thumbnails_skips_low_res_without_size_param(provider):
    thumbs = [{"url": "https://example/raw.jpg", "width": 100, "height": 100}]
    images = provider._parse_thumbnails(thumbs)
    assert images == []


# ---------------------------------------------------------------------------
# _minimal_track
# ---------------------------------------------------------------------------


def test_minimal_track_returns_playable_stub(provider):
    track = provider._minimal_track("vid42")
    assert track.item_id == "vid42"
    assert track.name == "vid42"
    assert track.artists[0].name == "Unknown Artist"
    mapping = next(iter(track.provider_mappings))
    assert mapping.url == f"{ytm.YTM_DOMAIN}/watch?v=vid42"
    assert mapping.audio_format.content_type == ContentType.M4A


# ---------------------------------------------------------------------------
# get_config_entries
# ---------------------------------------------------------------------------


def test_get_config_entries_returns_expected_keys():
    entries = asyncio.run(ytm.get_config_entries(mass=None))
    keys = [e.key for e in entries]
    assert keys == [
        ytm.CONF_AUTH_TYPE,
        ytm.CONF_COOKIE,
        ytm.CONF_BRAND_ACCOUNT,
        ytm.CONF_AUTH_USER,
        ytm.CONF_PREFER_AUDIO_QUALITY,
        ytm.CONF_CACHE_ENABLED,
        ytm.CONF_CACHE_DIRECTORY,
        ytm.CONF_CACHE_STAGING_DIRECTORY,
        ytm.CONF_CACHE_CATALOG_DSN,
        ytm.CONF_ACCOUNT_SYNC_ENABLED,
        ytm.CONF_ACCOUNT_SYNC_INTERVAL,
        ytm.CONF_PREFETCH_ENABLED,
        ytm.CONF_PREFETCH_PLAYLISTS,
        ytm.CONF_PREFETCH_INTERVAL,
        ytm.CONF_PREFETCH_MAX_TRACKS,
        ytm.CONF_PREFETCH_MAX_CACHE_GB,
        ytm.CONF_PREFETCH_PAUSE_PLAYBACK,
        ytm.CONF_PREFETCH_REQUEST_DELAY,
        ytm.CONF_CACHE_UPGRADE_ENABLED,
        ytm.CONF_CACHE_UPGRADE_TARGET_BITRATE,
        ytm.CONF_CACHE_UPGRADE_RECHECK_DAYS,
    ]
    cookie_entry = next(e for e in entries if e.key == ytm.CONF_COOKIE)
    assert cookie_entry.depends_on == ytm.CONF_AUTH_TYPE
    assert cookie_entry.depends_on_value == [ytm.AUTH_TYPE_COOKIE]
    pause_entry = next(
        e for e in entries if e.key == ytm.CONF_PREFETCH_PAUSE_PLAYBACK
    )
    assert pause_entry.default_value is False



def test_cache_miss_uses_http_and_never_writes_foreground_cache(provider, tmp_path):
    """Safe mode keeps cache writes completely outside foreground playback."""

    provider._cache_enabled = True
    provider._cache_directory = str(tmp_path)

    async def stream_format(_item_id):
        return {
            "url": "https://stream.example/audio",
            "ext": "webm",
            "audio_ext": "webm",
            "acodec": "opus",
        }

    provider._get_stream_format = stream_format
    details = asyncio.run(
        provider.get_stream_details("dQw4w9WgXcQ", MediaType.TRACK)
    )

    assert details.stream_type == StreamType.HTTP
    assert details.can_seek is True
    assert details.allow_seek is True
    assert details.data["playback_source"] == "youtube"
    assert list(tmp_path.iterdir()) == []



def test_loaded_provider_registers_native_prefetch_task(provider, tmp_path):
    """Authenticated prefetch uses Music Assistant's scheduled-task controller."""

    values = {
        ytm.CONF_PREFETCH_ENABLED: True,
        ytm.CONF_PREFETCH_INTERVAL: 6,
    }

    class Tasks:
        registered = None

        def register_scheduled_task(self, **kwargs):
            self.registered = kwargs

    tasks = Tasks()
    provider.mass = SimpleNamespace(tasks=tasks)
    provider.config = SimpleNamespace(get_value=lambda key: values.get(key))
    provider._authenticated = True
    provider._cache_enabled = True
    provider._cache_directory = str(tmp_path)
    provider._prefetch_task_id = f"{provider.instance_id}_prefetch"

    asyncio.run(provider.loaded_in_mass())

    assert tasks.registered["task_id"] == f"{provider.instance_id}_prefetch"
    assert tasks.registered["schedule"].every == 6
    assert tasks.registered["handler"] == provider._run_cache_prefetch
    assert tasks.registered["allow_cancel"] is True
    assert tasks.registered["allow_retry"] is True


def test_prefetch_downloads_library_tracks_with_native_progress(provider, tmp_path):
    """The scheduled handler bounds work and reports progress through MA."""

    values = {
        ytm.CONF_PREFETCH_PLAYLISTS: False,
        ytm.CONF_PREFETCH_MAX_TRACKS: 100,
        ytm.CONF_PREFETCH_MAX_CACHE_GB: 50,
        ytm.CONF_PREFETCH_PAUSE_PLAYBACK: True,
        ytm.CONF_PREFETCH_REQUEST_DELAY: 0,
    }

    class Tasks:
        progress = []

        def update_current_task_progress(self, value, text=None):
            self.progress.append((value, text))

    async def library_tracks():
        yield ytm.Track(
            item_id="first-video",
            provider=provider.instance_id,
            name="First",
        )
        yield ytm.Track(
            item_id="second-video",
            provider=provider.instance_id,
            name="Second",
        )

    downloaded = []

    async def prefetch_track(
        video_id,
        cache_size,
        cache_limit,
        pause,
        quality_upgrade,
        cached_bitrate,
    ):
        downloaded.append((video_id, cache_size, cache_limit, pause))
        assert quality_upgrade is False
        assert cached_bitrate is None
        return ("downloaded", 1024, 256)

    provider.mass = SimpleNamespace(tasks=Tasks(), players=[])
    provider.config = SimpleNamespace(get_value=lambda key: values.get(key))
    provider._authenticated = True
    provider._cache_enabled = True
    provider._cache_directory = str(tmp_path)
    provider.get_library_tracks = library_tracks
    provider._prefetch_track = prefetch_track

    asyncio.run(provider._run_cache_prefetch())

    assert [item[0] for item in downloaded] == ["first-video", "second-video"]
    assert downloaded[1][1] == 1024
    assert downloaded[0][2] == 50 * 1024 * 1024 * 1024
    assert downloaded[0][3] is True
    assert provider.mass.tasks.progress[-1] == (
        100,
        "Downloaded 2, upgraded 0, current 0, skipped 0, failed 0",
    )


def test_prefetch_enqueues_durable_catalog_candidates(provider, tmp_path):
    """Inventory enumeration queues work without leasing the full batch."""

    class Catalog:
        enqueued = []
        reconciled = []

        async def enqueue(self, track_ids, priority=100):
            self.enqueued = list(track_ids)

        async def reconcile_cached(self, entries):
            self.reconciled = list(entries)

    async def library_tracks():
        for item_id in ("first-video", "second-video"):
            yield ytm.Track(
                item_id=item_id,
                provider=provider.instance_id,
                name=item_id,
            )

    provider.config = SimpleNamespace(get_value=lambda _key: False)
    provider._cache_enabled = True
    provider._cache_directory = str(tmp_path)
    provider._cache_catalog = Catalog()
    provider.get_library_tracks = library_tracks

    candidates = asyncio.run(provider._prefetch_candidates(100))

    assert candidates == ["first-video", "second-video"]
    assert provider._cache_catalog.enqueued == ["first-video", "second-video"]
    assert provider._catalog_claim_attempts == {}


def test_catalog_reconciliation_requeues_missing_files(provider, tmp_path):
    """A stale PostgreSQL cache hit must become claimable without a DB reset."""

    from ytmusic.catalog import CachedEntry

    existing_id = "existing-video"
    missing_id = "missing-video"
    existing_path = (
        tmp_path / f"{hashlib.sha256(existing_id.encode()).hexdigest()}.webm"
    )
    existing_path.write_bytes(b"audio")

    class Catalog:
        reconciled = []
        requeued = []

        async def list_cached(self):
            return [
                CachedEntry(existing_id, str(existing_path), 138),
                CachedEntry(missing_id, str(tmp_path / "gone.webm"), 138),
            ]

        async def reconcile_cached(self, entries):
            self.reconciled = list(entries)

        async def requeue_missing(self, track_ids):
            self.requeued = list(track_ids)

    provider._cache_directory = str(tmp_path)
    provider._cache_catalog = Catalog()

    count = asyncio.run(provider._reconcile_catalog_files())

    assert count == 1
    assert provider._cache_catalog.requeued == [missing_id]
    assert provider._cache_catalog.reconciled == [
        (existing_id, str(existing_path), 5, "webm")
    ]


def test_requested_cache_miss_receives_demand_priority(provider):
    """A foreground miss is queued ahead of ordinary library inventory."""

    class Catalog:
        calls = []

        async def enqueue(self, track_ids, priority=100):
            self.calls.append((list(track_ids), priority))

    provider._cache_catalog = Catalog()

    asyncio.run(provider._prioritize_cache_track("requested-video"))

    assert provider._cache_catalog.calls == [(["requested-video"], 0)]


def test_catalog_prefetch_claims_only_one_job_at_a_time(provider, tmp_path):
    """Sequential claims leave later jobs eligible for demand reprioritization."""

    from ytmusic.catalog import CacheJob

    class Catalog:
        claims = []
        jobs = [CacheJob("first-video", 1), CacheJob("second-video", 1)]

        async def claim(self, limit):
            self.claims.append(limit)
            return [self.jobs.pop(0)] if self.jobs else []

        async def mark_cached(self, *_args):
            return None

    downloaded = []

    async def prefetch_candidates(_limit):
        return ["first-video", "second-video"]

    async def prefetch_track(video_id, *_args):
        downloaded.append(video_id)
        cache_file = tmp_path / f"{hashlib.sha256(video_id.encode()).hexdigest()}.webm"
        cache_file.write_bytes(b"audio")
        return ("downloaded", 5, 256)

    values = {
        ytm.CONF_PREFETCH_MAX_TRACKS: 100,
        ytm.CONF_PREFETCH_MAX_CACHE_GB: 50,
        ytm.CONF_PREFETCH_PAUSE_PLAYBACK: False,
        ytm.CONF_PREFETCH_REQUEST_DELAY: 0,
    }
    provider.config = SimpleNamespace(get_value=values.get)
    provider.mass = SimpleNamespace(
        players=[],
        tasks=SimpleNamespace(update_current_task_progress=lambda *_args: None),
    )
    provider._authenticated = True
    provider._cache_enabled = True
    provider._cache_directory = str(tmp_path)
    provider._cache_catalog = Catalog()
    provider._prefetch_candidates = prefetch_candidates
    provider._prefetch_track = prefetch_track

    asyncio.run(provider._run_cache_prefetch())

    assert downloaded == ["first-video", "second-video"]
    assert provider._cache_catalog.claims == [1, 1, 1]


def test_catalog_prefetch_progress_never_exceeds_one_hundred(provider, tmp_path):
    """Older durable claims must not overflow progress for a short fresh list."""

    from ytmusic.catalog import CacheJob

    class Catalog:
        jobs = [
            CacheJob("first-video", 1),
            CacheJob("second-video", 1),
            CacheJob("third-video", 1),
        ]

        async def claim(self, _limit):
            return [self.jobs.pop(0)] if self.jobs else []

        async def mark_cached(self, *_args):
            return None

    progress_values = []
    values = {
        ytm.CONF_PREFETCH_MAX_TRACKS: 3,
        ytm.CONF_PREFETCH_MAX_CACHE_GB: 50,
        ytm.CONF_PREFETCH_PAUSE_PLAYBACK: False,
        ytm.CONF_PREFETCH_REQUEST_DELAY: 0,
    }
    provider.config = SimpleNamespace(get_value=values.get)
    provider.mass = SimpleNamespace(
        players=[],
        tasks=SimpleNamespace(
            update_current_task_progress=lambda value, *_args: progress_values.append(
                value
            )
        ),
    )
    provider._authenticated = True
    provider._cache_enabled = True
    provider._cache_directory = str(tmp_path)
    provider._cache_catalog = Catalog()
    provider._prefetch_candidates = lambda _limit: asyncio.sleep(
        0, result=["fresh-only"]
    )

    async def prefetch_track(video_id, *_args):
        cache_file = tmp_path / f"{hashlib.sha256(video_id.encode()).hexdigest()}.webm"
        cache_file.write_bytes(b"audio")
        return ("downloaded", 5, 256)

    provider._prefetch_track = prefetch_track

    asyncio.run(provider._run_cache_prefetch())

    assert progress_values[-1] == 100
    assert all(0 <= value <= 100 for value in progress_values)


def test_prefetch_pauses_without_downloading_during_playback(provider, tmp_path):
    """Foreground players prevent a scheduled prefetch run from using bandwidth."""

    values = {
        ytm.CONF_PREFETCH_PAUSE_PLAYBACK: True,
    }
    player = SimpleNamespace(playback_state=SimpleNamespace(value="playing"))
    tasks = SimpleNamespace(update_current_task_progress=lambda *_args: None)
    provider.mass = SimpleNamespace(tasks=tasks, players=[player])
    provider.config = SimpleNamespace(get_value=lambda key: values.get(key))
    provider._authenticated = True
    provider._cache_enabled = True
    provider._cache_directory = str(tmp_path)

    async def should_not_enumerate(_limit):
        raise AssertionError("active playback must stop before library enumeration")

    provider._prefetch_candidates = should_not_enumerate
    asyncio.run(provider._run_cache_prefetch())


def test_prefetch_continues_during_playback_by_default(provider, tmp_path):
    """An unset gate must not suppress production background downloads."""

    player = SimpleNamespace(playback_state=SimpleNamespace(value="playing"))
    tasks = SimpleNamespace(update_current_task_progress=lambda *_args: None)
    provider.mass = SimpleNamespace(tasks=tasks, players=[player])
    provider.config = SimpleNamespace(get_value=lambda _key: None)
    provider._authenticated = True
    provider._cache_enabled = True
    provider._cache_directory = str(tmp_path)
    enumerated = False

    async def enumerate_candidates(_limit):
        nonlocal enumerated
        enumerated = True
        return []

    provider._prefetch_candidates = enumerate_candidates
    asyncio.run(provider._run_cache_prefetch())

    assert enumerated is True


def test_prefetch_track_atomically_publishes_completed_audio(provider, tmp_path):
    """yt-dlp stages locally before atomically publishing to the cache."""

    payload = b"prefetched audio"
    cache_dir = tmp_path / "cache"
    staging_dir = tmp_path / "staging"
    observed_options = {}

    class YoutubeDL:
        def __init__(self, options):
            observed_options.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def process_ie_result(self, info, download):
            assert download is True
            assert info["url"] == "https://stream.example/audio"
            output = Path(observed_options["outtmpl"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload)
            return info

    async def stream_format(_video_id):
        return {
            "url": "https://stream.example/audio",
            "audio_ext": "webm",
            "format_id": "251",
            "http_headers": {},
        }

    provider.mass = SimpleNamespace(players=[])
    provider._cache_enabled = True
    provider._cache_directory = str(cache_dir)
    provider._cache_staging_directory = str(staging_dir)
    provider._cache_writers = set()
    provider._yt_dlp_module = SimpleNamespace(YoutubeDL=YoutubeDL)
    provider._get_stream_format = stream_format

    result, added_bytes, bitrate = asyncio.run(
        provider._prefetch_track(
            "prefetch-video",
            cache_size=0,
            cache_limit=1024 * 1024,
            pause_during_playback=True,
        )
    )

    assert result == "downloaded"
    assert added_bytes == len(payload)
    assert bitrate is None
    files = list(cache_dir.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".webm"
    assert files[0].read_bytes() == payload
    assert not list(cache_dir.glob("*.part"))
    assert not list(staging_dir.iterdir())
    assert observed_options["continuedl"] is True
    assert observed_options["http_chunk_size"] == 10 * 1024 * 1024
    assert "format" not in observed_options


def test_prefetch_atomically_replaces_lower_quality_cache(provider, tmp_path):
    """A lower-bitrate file stays present until the better file is complete."""

    cache_dir = tmp_path / "cache"
    staging_dir = tmp_path / "staging"
    cache_dir.mkdir()
    video_id = "quality-video"
    old_path = cache_dir / f"{hashlib.sha256(video_id.encode()).hexdigest()}.webm"
    old_path.write_bytes(b"old")
    observed_old_during_download = []

    class YoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def process_ie_result(self, info, download):
            assert download is True
            observed_old_during_download.append(old_path.read_bytes())
            Path(self.options["outtmpl"]).write_bytes(b"better audio")
            return info

    async def stream_format(_video_id):
        return {
            "url": "https://stream.example/better",
            "audio_ext": "m4a",
            "format_id": "141",
            "abr": 257.6,
        }

    provider.mass = SimpleNamespace(players=[])
    provider._cache_directory = str(cache_dir)
    provider._cache_staging_directory = str(staging_dir)
    provider._cache_writers = set()
    provider._yt_dlp_module = SimpleNamespace(YoutubeDL=YoutubeDL)
    provider._get_stream_format = stream_format

    result, size_delta, bitrate = asyncio.run(
        provider._prefetch_track(
            video_id,
            cache_size=3,
            cache_limit=1024 * 1024,
            pause_during_playback=False,
            quality_upgrade=True,
            cached_bitrate=138,
        )
    )

    assert result == "upgraded"
    assert size_delta == len(b"better audio") - len(b"old")
    assert bitrate == 258
    assert observed_old_during_download == [b"old"]
    assert not old_path.exists()
    assert provider._find_cached_path(video_id).endswith(".m4a")
    assert Path(provider._find_cached_path(video_id)).read_bytes() == b"better audio"


def test_quality_probe_keeps_cache_when_accessible_format_is_not_better(
    provider,
    tmp_path,
):
    """A quality check does not redownload or replace an equal/lower format."""

    video_id = "already-best"
    cache_path = tmp_path / f"{hashlib.sha256(video_id.encode()).hexdigest()}.webm"
    cache_path.write_bytes(b"current audio")

    async def stream_format(_video_id):
        return {
            "url": "https://stream.example/current",
            "audio_ext": "webm",
            "format_id": "251",
            "abr": 138,
        }

    provider.mass = SimpleNamespace(players=[])
    provider._cache_directory = str(tmp_path)
    provider._cache_staging_directory = str(tmp_path / "staging")
    provider._cache_writers = set()
    provider._get_stream_format = stream_format

    result = asyncio.run(
        provider._prefetch_track(
            video_id,
            cache_size=len(b"current audio"),
            cache_limit=1024 * 1024,
            pause_during_playback=False,
            quality_upgrade=True,
            cached_bitrate=138,
        )
    )

    assert result == ("current", 0, 138)
    assert cache_path.read_bytes() == b"current audio"
    assert not Path(provider._cache_staging_directory).exists()


def test_prefetch_uses_authenticated_headers_and_resolved_format(provider, tmp_path):
    """The downloader must reuse the provider cookie without re-extracting."""

    observed = {}

    class CookieJar:
        def set_cookie(self, cookie):
            observed.setdefault("cookies", []).append(cookie)

    class YoutubeDL:
        def __init__(self, options):
            observed["options"] = options
            self.cookiejar = CookieJar()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def process_ie_result(self, info, download):
            observed["info"] = info
            Path(observed["options"]["outtmpl"]).write_bytes(b"audio")
            return info

    provider.mass = SimpleNamespace(players=[])
    provider._auth_headers = {
        "cookie": "__Secure-3PAPISID=secret; SAPISID=secret",
        "x-goog-authuser": "2",
        "origin": ytm.YTM_DOMAIN,
        "authorization": "SAPISIDHASH value",
        "content-type": "application/json",
    }
    provider._cache_staging_directory = str(tmp_path)
    provider._yt_dlp_module = SimpleNamespace(YoutubeDL=YoutubeDL)
    stream_format = {
        "url": "https://stream.example/audio",
        "format_id": "251",
        "http_headers": {"Referer": ytm.YTM_DOMAIN},
    }

    provider._download_to_staging(
        "video-id",
        stream_format,
        str(tmp_path / "audio.webm"),
        False,
    )

    assert "Cookie" not in observed["options"]["http_headers"]
    assert observed["options"]["http_headers"]["X-Goog-Authuser"] == "2"
    assert {cookie.name for cookie in observed["cookies"]} == {
        "__Secure-3PAPISID",
        "SAPISID",
    }
    assert all(cookie.domain == ".youtube.com" for cookie in observed["cookies"])
    assert observed["info"]["url"] == "https://stream.example/audio"
    assert observed["info"]["format_id"] == "251"
    assert observed["info"]["http_headers"]["Referer"] == ytm.YTM_DOMAIN


def test_library_tracks_use_deduplicated_saved_liked_and_history_mirror(provider):
    """Completed PostgreSQL snapshots are the durable MA library source."""

    snapshots = {
        "saved_tracks": [
            {"videoId": "saved", "title": "Saved"},
            {"videoId": "duplicate", "title": "Duplicate"},
        ],
        "liked_tracks": [
            {"videoId": "liked", "title": "Liked"},
            {"videoId": "duplicate", "title": "Duplicate"},
        ],
        "history": [{"videoId": "history", "title": "History"}],
    }

    async def mirrored(collection, _owner_id=""):
        return snapshots.get(collection)

    provider._authenticated = True
    provider._mirrored_payloads = mirrored
    provider._parse_track = lambda item: SimpleNamespace(item_id=item["videoId"])
    provider._guard_partial_auth_empty = lambda *_args: asyncio.sleep(0)

    async def collect():
        return [track.item_id async for track in provider.get_library_tracks()]

    assert asyncio.run(collect()) == ["saved", "duplicate", "liked", "history"]


def test_prefetch_resumes_existing_ytdlp_partial(provider, tmp_path):
    """A stable staging filename lets yt-dlp resume after provider restart."""

    cache_dir = tmp_path / "cache"
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    observed = {}

    class YoutubeDL:
        def __init__(self, options):
            observed.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def process_ie_result(self, info, download):
            assert download is True
            assert info["url"] == "https://stream.example/audio"
            output = Path(observed["outtmpl"])
            partial = Path(f"{output}.part")
            assert partial.read_bytes() == b"first half"
            output.write_bytes(partial.read_bytes() + b" second half")
            partial.unlink()
            return info

    async def stream_format(_video_id):
        return {
            "url": "https://stream.example/audio",
            "audio_ext": "webm",
            "format_id": "251",
        }

    provider.mass = SimpleNamespace(players=[])
    provider._cache_enabled = True
    provider._cache_directory = str(cache_dir)
    provider._cache_staging_directory = str(staging_dir)
    provider._cache_writers = set()
    provider._yt_dlp_module = SimpleNamespace(YoutubeDL=YoutubeDL)
    provider._get_stream_format = stream_format
    stage = staging_dir / f"{hashlib.sha256(b'prefetch-video').hexdigest()}.webm"
    Path(f"{stage}.part").write_bytes(b"first half")

    result, added_bytes, bitrate = asyncio.run(
        provider._prefetch_track(
            "prefetch-video",
            cache_size=0,
            cache_limit=1024 * 1024,
            pause_during_playback=False,
        )
    )

    assert result == "downloaded"
    assert added_bytes == len(b"first half second half")
    assert bitrate is None
    assert next(cache_dir.iterdir()).read_bytes() == b"first half second half"



def test_auth_user_entry_is_cookie_only_and_defaults_to_zero():
    entries = asyncio.run(ytm.get_config_entries(mass=None))
    entry = next(e for e in entries if e.key == ytm.CONF_AUTH_USER)
    assert entry.default_value == 0
    assert entry.depends_on == ytm.CONF_AUTH_TYPE
    assert entry.depends_on_value == [ytm.AUTH_TYPE_COOKIE]


# ---------------------------------------------------------------------------
# Async dispatch
# ---------------------------------------------------------------------------


def _make_ytm_search_mock(results):
    mock = MagicMock()
    mock.search = MagicMock(return_value=results)
    return mock


def test_search_artist_dispatches_with_artists_filter(provider):
    captured = {}

    def _search(query, filter, limit):
        captured["filter"] = filter
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(provider.search("foo", [MediaType.ARTIST], limit=3))
    assert captured["filter"] == "artists"


def test_search_track_dispatches_with_songs_filter(provider):
    captured = {}

    def _search(query, filter, limit):
        captured["filter"] = filter
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(provider.search("foo", [MediaType.TRACK], limit=3))
    assert captured["filter"] == "songs"


def test_search_multi_type_runs_filtered_call_per_type(provider):
    """Multi-type search issues one filtered call per type, not one unfiltered
    call. An unfiltered YTM search skews to songs/videos, so artists and
    playlists rarely surface (issue #18)."""
    captured = []

    def _search(query, filter, limit):
        captured.append(filter)
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(
        provider.search(
            "foo",
            [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST],
            limit=3,
        )
    )
    assert captured == ["artists", "albums", "songs", "playlists"]
    assert None not in captured


def test_search_one_failing_filter_does_not_sink_others(provider):
    """A filter that raises is logged and skipped; the rest still return."""

    def _search(query, filter, limit):
        if filter == "artists":
            raise RuntimeError("boom")
        return [
            {
                "resultType": "song",
                "videoId": "vid1",
                "title": "Song",
                "artists": [{"id": "UCart", "name": "A"}],
            }
        ]

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.ARTIST, MediaType.TRACK]))
    assert len(results.artists) == 0
    assert len(results.tracks) == 1


def test_search_parses_returned_items_by_result_type(provider):
    # Each filtered call returns only its own category, as real YTM does. The
    # provider runs one call per type and merges them (issue #18).
    by_filter = {
        "artists": [{"resultType": "artist", "channelId": "UCart", "name": "Some Artist"}],
        "songs": [
            {
                "resultType": "song",
                "videoId": "vid1",
                "title": "Song",
                "artists": [{"id": "UCart", "name": "Some Artist"}],
            }
        ],
        "albums": [
            {
                "resultType": "album",
                "browseId": "MPREb_x",
                "title": "Album",
                "artists": [{"id": "UCart", "name": "Some Artist"}],
                "type": "Album",
            }
        ],
        "playlists": [{"resultType": "playlist", "browseId": "VLPLx", "title": "Playlist"}],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search(
            "foo",
            [MediaType.ARTIST, MediaType.TRACK, MediaType.ALBUM, MediaType.PLAYLIST],
        )
    )
    assert len(results.artists) == 1
    assert len(results.tracks) == 1
    assert len(results.albums) == 1
    assert len(results.playlists) == 1


def test_search_skips_invalid_items(provider):
    """An item missing a required field should be skipped, not crash the search."""
    mock = MagicMock()
    mock.search = MagicMock(
        return_value=[
            # No videoId — should be silently skipped.
            {
                "resultType": "song",
                "title": "broken",
                "artists": [{"id": "UCart", "name": "A"}],
            },
            {
                "resultType": "song",
                "videoId": "good",
                "title": "ok",
                "artists": [{"id": "UCart", "name": "A"}],
            },
        ]
    )
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK]))
    assert len(results.tracks) == 1
    assert results.tracks[0].item_id == "good"


# ---------------------------------------------------------------------------
# Search by pasted URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, expected",
    [
        # Single videos / songs
        ("https://music.youtube.com/watch?v=abc123", ("track", "abc123")),
        ("https://www.youtube.com/watch?v=abc123", ("track", "abc123")),
        ("https://m.youtube.com/watch?v=abc123", ("track", "abc123")),
        ("https://youtu.be/abc123", ("track", "abc123")),
        ("https://youtu.be/abc123?si=xyz", ("track", "abc123")),
        # v + list together resolves to the track, not the playlist
        ("https://music.youtube.com/watch?v=abc123&list=PLxyz", ("track", "abc123")),
        ("https://www.youtube.com/watch?v=abc123&t=42s&feature=share", ("track", "abc123")),
        # Playlists
        ("https://music.youtube.com/playlist?list=PLxyz", ("playlist", "PLxyz")),
        ("https://www.youtube.com/playlist?list=PLxyz", ("playlist", "PLxyz")),
        # Bare ?list= with no path still means a playlist
        ("https://www.youtube.com/?list=PLxyz", ("playlist", "PLxyz")),
        # Lenient: missing scheme
        ("youtu.be/abc123", ("track", "abc123")),
        ("music.youtube.com/watch?v=abc123", ("track", "abc123")),
        # Not URLs / not YouTube
        ("just a search query", None),
        ("https://example.com/watch?v=abc123", None),
        ("https://spotify.com/track/abc", None),
        ("", None),
        ("   ", None),
        # YouTube host but no resolvable id
        ("https://music.youtube.com/", None),
        ("https://www.youtube.com/watch", None),
    ],
)
def test_parse_youtube_url(provider, query, expected):
    assert provider._parse_youtube_url(query) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        # MA's search controller does: query.replace("/", " ").replace("'", "")
        # before calling the provider, destroying "://" and path separators.
        # These are the exact forms the provider actually receives.
        ("https:  www.youtube.com watch?v=S33tWZqXhnk", ("track", "S33tWZqXhnk")),
        ("https:  music.youtube.com watch?v=S33tWZqXhnk", ("track", "S33tWZqXhnk")),
        ("https:  m.youtube.com watch?v=S33tWZqXhnk", ("track", "S33tWZqXhnk")),
        # v + list together still resolves to the track
        ("https:  music.youtube.com watch?v=S33tWZqXhnk&list=PLabc", ("track", "S33tWZqXhnk")),
        ("https:  www.youtube.com watch?v=S33tWZqXhnk list=PLabc", ("track", "S33tWZqXhnk")),
        # mangled playlist URL
        ("https:  music.youtube.com playlist?list=PLabcdefghij", ("playlist", "PLabcdefghij")),
        ("https:  www.youtube.com playlist?list=PLabcdefghij", ("playlist", "PLabcdefghij")),
        # mangled youtu.be short link
        ("https:  youtu.be S33tWZqXhnk", ("track", "S33tWZqXhnk")),
        # a youtube host token but no valid id -> not a link
        ("a song called youtube.com is great", None),
        ("youtube.com", None),
        # ordinary text searches must not be hijacked
        ("the youtuber song", None),
        ("watch v in the dark", None),
    ],
)
def test_parse_youtube_url_handles_ma_mangled_query(provider, query, expected):
    """MA strips '/' from the query before calling search(); the parser must
    still recover the id from that sanitized form."""
    assert provider._parse_youtube_url(query) == expected


def test_search_with_ma_mangled_video_url_returns_track(provider):
    """End-to-end: the de-slashed query MA actually delivers still resolves."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={"videoDetails": {"videoId": "S33tWZqXhnk", "title": "x", "author": "a"}}
    )
    mock.search = MagicMock(return_value=[])
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https:  www.youtube.com watch?v=S33tWZqXhnk", [MediaType.TRACK])
    )
    assert results.tracks[0].item_id == "S33tWZqXhnk"


def test_search_with_video_url_returns_track_first(provider):
    """Pasting a watch URL resolves the video to a Track placed first."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "abc123",
                "title": "Pasted Song",
                "lengthSeconds": "180",
                "author": "Some Uploader",
                "thumbnail": {"thumbnails": []},
            }
        }
    )
    mock.search = MagicMock(return_value=[])
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://music.youtube.com/watch?v=abc123", [MediaType.TRACK])
    )
    assert results.tracks[0].item_id == "abc123"
    assert len(results.playlists) == 0


def test_search_with_video_url_runs_name_search_on_title(provider):
    """The other results come from a text search on the resolved video title."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "abc123",
                "title": "Pasted Song",
                "author": "Some Uploader",
            }
        }
    )

    def _search(query, filter=None, limit=5):  # noqa: A002
        # The text search must use the resolved title, never the raw URL.
        assert query == "Pasted Song"
        if filter == "songs":
            return [
                {
                    "resultType": "song",
                    "videoId": "related1",
                    "title": "Related",
                    "artists": [{"id": "UCx", "name": "A"}],
                }
            ]
        return []

    mock.search = MagicMock(side_effect=_search)
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://music.youtube.com/watch?v=abc123", [MediaType.TRACK])
    )
    mock.search.assert_called()
    assert results.tracks[0].item_id == "abc123"  # raw video first
    assert any(t.item_id == "related1" for t in results.tracks)


def test_search_with_video_url_dedupes_raw_video(provider):
    """The pasted video isn't listed twice if it also surfaces in name search."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={"videoDetails": {"videoId": "abc123", "title": "Song", "author": "a"}}
    )
    mock.search = MagicMock(
        return_value=[
            {
                "resultType": "song",
                "videoId": "abc123",
                "title": "Song",
                "artists": [{"id": "UCx", "name": "A"}],
            }
        ]
    )
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://music.youtube.com/watch?v=abc123", [MediaType.TRACK])
    )
    assert [t.item_id for t in results.tracks] == ["abc123"]


def test_search_with_url_ignores_media_types_filter(provider):
    """An explicit link resolves even when its type isn't in media_types."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={"videoDetails": {"videoId": "abc123", "title": "x", "author": "a"}}
    )
    mock.search = MagicMock(return_value=[])
    provider._ytmusic = mock
    # Searching only for ALBUM, but pasting a song link -> still returns the track.
    results = asyncio.run(
        provider.search("https://youtu.be/abc123", [MediaType.ALBUM])
    )
    assert len(results.tracks) == 1
    assert results.tracks[0].item_id == "abc123"


def test_search_with_non_music_video_falls_back_to_minimal_track(provider):
    """A plain youtube.com video whose get_song fails still yields a playable track."""
    mock = MagicMock()
    mock.get_song = MagicMock(side_effect=RuntimeError("not a music catalog item"))
    mock.search = MagicMock(side_effect=AssertionError("text search must not run for a URL"))
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://www.youtube.com/watch?v=randomvid", [MediaType.TRACK])
    )
    assert len(results.tracks) == 1
    assert results.tracks[0].item_id == "randomvid"


def test_search_with_playlist_url_returns_single_playlist(provider):
    """Pasting a playlist URL resolves it to one Playlist via get_playlist."""
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={
            "id": "PLxyz",
            "title": "Pasted Playlist",
            "owner": "Someone",
        }
    )
    mock.search = MagicMock(side_effect=AssertionError("text search must not run for a URL"))
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://music.youtube.com/playlist?list=PLxyz", [MediaType.PLAYLIST])
    )
    assert len(results.playlists) == 1
    assert results.playlists[0].item_id == "PLxyz"
    assert len(results.tracks) == 0


def test_search_with_url_resolution_failure_returns_empty(provider):
    """If URL resolution raises, search returns empty results rather than erroring."""
    mock = MagicMock()
    mock.get_playlist = MagicMock(side_effect=RuntimeError("boom"))
    provider._ytmusic = mock

    # yt-dlp fallback path also fails -> _search_by_url swallows and returns empty.
    async def _boom(_playlist_id):
        raise RuntimeError("boom")

    provider._get_playlist_via_ytdlp = _boom
    results = asyncio.run(
        provider.search("https://music.youtube.com/playlist?list=PLbad", [MediaType.PLAYLIST])
    )
    assert len(results.playlists) == 0
    assert len(results.tracks) == 0


# ---------------------------------------------------------------------------
# Trim timestamps (@start-end)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token, expected",
    [
        ("15", 15),
        ("0:15", 15),
        ("3:42", 222),
        ("1:02:03", 3723),
        ("1m30s", 90),
        ("2h", 7200),
        ("90s", 90),
        ("", None),
        ("   ", None),
        ("abc", None),
        ("1:2:3:4", None),
    ],
)
def test_parse_timestamp(token, expected):
    assert ytm._parse_timestamp(token) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        ("https://youtu.be/abc123 @15-222", ("https://youtu.be/abc123", 15, 222)),
        ("https://youtu.be/abc123 @0:15-3:42", ("https://youtu.be/abc123", 15, 222)),
        ("https://youtu.be/abc123 @15-", ("https://youtu.be/abc123", 15, None)),
        ("https://youtu.be/abc123 @-3:42", ("https://youtu.be/abc123", None, 222)),
        ("https://youtu.be/abc123@15-222", ("https://youtu.be/abc123", 15, 222)),
        # No spec / unparseable spec -> query untouched, no bounds.
        ("https://youtu.be/abc123", ("https://youtu.be/abc123", None, None)),
        ("an email a@b thing", ("an email a@b thing", None, None)),
        # start >= end is nonsensical -> bounds ignored, but the recognized
        # "@start-end" suffix is still stripped so URL resolution stays clean.
        ("https://youtu.be/abc123 @3:42-0:15", ("https://youtu.be/abc123", None, None)),
    ],
)
def test_split_trim_spec(query, expected):
    assert ytm._split_trim_spec(query) == expected


@pytest.mark.parametrize(
    "video_id, start, end, encoded",
    [
        ("abc12345678", None, None, "abc12345678"),
        ("abc12345678", 15, 222, "abc12345678@15-222"),
        ("abc12345678", 15, None, "abc12345678@15-"),
        ("abc12345678", None, 222, "abc12345678@-222"),
    ],
)
def test_encode_split_track_id_roundtrip(video_id, start, end, encoded):
    assert ytm._encode_track_id(video_id, start, end) == encoded
    assert ytm._split_track_id(encoded) == (video_id, start, end)


def test_split_track_id_plain():
    assert ytm._split_track_id("abc12345678") == ("abc12345678", None, None)


def test_get_similar_tracks_strips_trim_suffix(provider):
    """Song radio on a trimmed track must query YTM with the bare video id."""
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(return_value={"tracks": []})
    provider._ytmusic = mock
    asyncio.run(provider.get_similar_tracks("abc12345678@15-222"))
    assert mock.get_watch_playlist.call_args.kwargs["videoId"] == "abc12345678"


def test_parse_youtube_url_encodes_trim(provider):
    """A pasted link with a trim spec resolves to an encoded track id."""
    assert provider._parse_youtube_url("https://youtu.be/abc12345678 @15-222") == (
        "track",
        "abc12345678@15-222",
    )
    # Playlists ignore the trim spec.
    assert provider._parse_youtube_url(
        "https://music.youtube.com/playlist?list=PLabcdefghij @15-222"
    ) == ("playlist", "PLabcdefghij")


def test_get_track_with_trim_encodes_id_and_duration(provider):
    """get_track queries the bare id but returns an encoded, trimmed Track."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "abc12345678",
                "title": "Song",
                "lengthSeconds": "300",
                "author": "a",
            }
        }
    )
    provider._ytmusic = mock
    track = asyncio.run(provider.get_track("abc12345678@15-222"))
    # Bare id used for the API lookup.
    mock.get_song.assert_called_once_with("abc12345678")
    # Encoded id persists on the track and its provider mapping.
    assert track.item_id == "abc12345678@15-222"
    assert all(m.item_id == "abc12345678@15-222" for m in track.provider_mappings)
    # Watch URL still uses the bare id.
    assert all("watch?v=abc12345678" in m.url for m in track.provider_mappings)
    # Duration reflects the trimmed window.
    assert track.duration == 222 - 15


def test_minimal_track_with_trim_keeps_encoded_id(provider):
    track = provider._minimal_track("abc12345678@15-222")
    assert track.item_id == "abc12345678@15-222"
    assert all("watch?v=abc12345678" in m.url for m in track.provider_mappings)
    assert track.name == "abc12345678"


def test_get_stream_details_adds_trim_args(provider):
    async def _fmt(video_id):
        assert video_id == "abc12345678"  # bare id reaches yt-dlp
        return {"url": "https://stream.example/x", "ext": "m4a"}

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678@15-222", MediaType.TRACK))
    assert sd.item_id == "abc12345678@15-222"
    assert sd.extra_input_args == ["-ss", "15", "-t", str(222 - 15)]
    assert sd.duration == 222 - 15


def test_get_stream_details_open_ended_start_only(provider):
    async def _fmt(video_id):
        return {"url": "https://stream.example/x", "ext": "m4a"}

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678@15-", MediaType.TRACK))
    assert sd.extra_input_args == ["-ss", "15"]


def test_get_stream_details_no_trim_has_no_args(provider):
    async def _fmt(video_id):
        return {"url": "https://stream.example/x", "ext": "m4a"}

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678", MediaType.TRACK))
    assert sd.extra_input_args == []


def test_get_album_raises_when_not_found(provider):
    mock = MagicMock()
    mock.get_album = MagicMock(return_value=None)
    provider._ytmusic = mock
    with pytest.raises(MediaNotFoundError):
        asyncio.run(provider.get_album("MPREb_missing"))


def test_get_album_tracks_returns_empty_on_none(provider):
    mock = MagicMock()
    mock.get_album = MagicMock(return_value=None)
    provider._ytmusic = mock
    tracks = asyncio.run(provider.get_album_tracks("MPREb_missing"))
    assert tracks == []


def test_get_album_tracks_assigns_track_numbers(provider):
    mock = MagicMock()
    mock.get_album = MagicMock(
        return_value={
            "tracks": [
                {
                    "videoId": "v1",
                    "title": "First",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
                {
                    "videoId": "v2",
                    "title": "Second",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ]
        }
    )
    provider._ytmusic = mock
    tracks = asyncio.run(provider.get_album_tracks("MPREb_x"))
    assert [t.item_id for t in tracks] == ["v1", "v2"]
    assert [t.track_number for t in tracks] == [1, 2]


def test_get_track_falls_back_to_minimal_track_on_failure(provider):
    mock = MagicMock()
    mock.get_song = MagicMock(side_effect=RuntimeError("boom"))
    provider._ytmusic = mock
    track = asyncio.run(provider.get_track("vid_x"))
    assert track.item_id == "vid_x"
    assert track.name == "vid_x"


def test_get_track_normalizes_video_details(provider):
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "vid_y",
                "title": "Some Song",
                "lengthSeconds": "200",
                "author": "Author",
                "thumbnail": {"thumbnails": []},
            }
        }
    )
    provider._ytmusic = mock
    track = asyncio.run(provider.get_track("vid_y"))
    assert track.item_id == "vid_y"
    assert track.name == "Some Song"
    assert track.duration == 200


def test_get_playlist_tracks_uses_ytdlp_when_ytmusicapi_is_partial(provider):
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={
            "trackCount": 3,
            "tracks": [
                {
                    "videoId": "ytm_1",
                    "title": "First",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
                {
                    "videoId": "ytm_2",
                    "title": "Second",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ],
        }
    )
    provider._ytmusic = mock

    async def _fallback(_playlist_id):
        return [
            provider._minimal_track(track_id)
            for track_id in ("ytm_1", "ytm_2", "dlp_3")
        ]

    provider._get_playlist_tracks_via_ytdlp = _fallback

    tracks = asyncio.run(provider.get_playlist_tracks("PLpartial"))

    assert [track.item_id for track in tracks] == ["ytm_1", "ytm_2", "dlp_3"]
    assert [track.name for track in tracks] == ["First", "Second", "dlp_3"]


def test_get_playlist_tracks_keeps_ytmusicapi_when_complete(provider):
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={
            "trackCount": "2 songs",
            "tracks": [
                {
                    "videoId": "ytm_1",
                    "title": "First",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
                {
                    "videoId": "ytm_2",
                    "title": "Second",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ],
        }
    )
    provider._ytmusic = mock
    fallback = MagicMock(side_effect=AssertionError("yt-dlp fallback should not run"))
    provider._get_playlist_tracks_via_ytdlp = fallback

    tracks = asyncio.run(provider.get_playlist_tracks("PLcomplete"))

    assert [track.item_id for track in tracks] == ["ytm_1", "ytm_2"]


def test_get_playlist_tracks_skips_unavailable_tracks(provider):
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={
            "tracks": [
                {
                    "videoId": "gone",
                    "title": "Gone",
                    "artists": [{"id": "UC1", "name": "A"}],
                    "isAvailable": False,
                },
                {
                    "videoId": "available",
                    "title": "Available",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ],
        }
    )
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_playlist_tracks("PLavailable"))

    assert [track.item_id for track in tracks] == ["available"]


def test_get_artist_unknown_prefix_returns_stub(provider):
    artist = asyncio.run(provider.get_artist("unknown_Foo Bar"))
    assert artist.name == "Foo Bar"
    assert artist.item_id == "unknown_Foo Bar"


def test_get_artist_non_channel_id_not_found_without_ytm_call(provider):
    """A non-channel id (e.g. one pulled from track metadata) must not be
    handed to YTM — it would return HTTP 400. We raise MediaNotFoundError
    without ever calling get_artist (issue #18)."""
    from music_assistant_models.errors import MediaNotFoundError

    mock = MagicMock()
    mock.get_artist = MagicMock(side_effect=AssertionError("must not be called"))
    provider._ytmusic = mock
    with pytest.raises(MediaNotFoundError):
        asyncio.run(provider.get_artist("MPLA_not_a_channel"))
    mock.get_artist.assert_not_called()


def test_get_artist_albums_non_channel_id_returns_empty(provider):
    """A non-channel id degrades to an empty album list instead of raising the
    raw HTTP 400 it would previously surface (issue #18)."""
    mock = MagicMock()
    mock.get_artist = MagicMock(side_effect=AssertionError("must not be called"))
    provider._ytmusic = mock
    assert asyncio.run(provider.get_artist_albums("not_a_channel")) == []
    mock.get_artist.assert_not_called()


def test_get_artist_albums_ytm_error_returns_empty(provider):
    """A YTM 400 on a channel-shaped id is caught and degrades to []."""
    mock = MagicMock()
    mock.get_artist = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 400: Bad Request")
    )
    provider._ytmusic = mock
    assert asyncio.run(provider.get_artist_albums("UCbroken")) == []


def test_get_artist_toptracks_non_channel_id_returns_empty(provider):
    """A non-channel id degrades to an empty track list (issue #18)."""
    mock = MagicMock()
    mock.get_artist = MagicMock(side_effect=AssertionError("must not be called"))
    provider._ytmusic = mock
    assert asyncio.run(provider.get_artist_toptracks("not_a_channel")) == []
    mock.get_artist.assert_not_called()


def test_get_artist_toptracks_ytm_error_returns_empty(provider):
    """A YTM 400 on a channel-shaped id is caught and degrades to []."""
    mock = MagicMock()
    mock.get_artist = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 400: Bad Request")
    )
    provider._ytmusic = mock
    assert asyncio.run(provider.get_artist_toptracks("UCbroken")) == []


def test_get_similar_tracks_watch_playlist_error_returns_empty(provider):
    """A ytmusicapi failure (e.g. KeyError 'endpoint') degrades to [] instead of
    propagating and failing the whole play_media command (issue #20)."""
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(side_effect=KeyError("endpoint"))
    provider._ytmusic = mock
    assert asyncio.run(provider.get_similar_tracks("vid_x")) == []
    mock.get_watch_playlist.assert_called_once()


def test_get_similar_tracks_parses_returned_tracks(provider):
    """A normal watch-playlist response still parses its tracks."""
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(
        return_value={
            "tracks": [
                {
                    "videoId": "vid1",
                    "title": "Song One",
                    "artists": [{"id": "UCart", "name": "An Artist"}],
                },
                # Missing videoId — must be skipped, not crash the radio fill.
                {"title": "broken", "artists": [{"id": "UCart", "name": "A"}]},
            ]
        }
    )
    provider._ytmusic = mock
    tracks = asyncio.run(provider.get_similar_tracks("vid_x"))
    assert len(tracks) == 1
    assert tracks[0].item_id == "vid1"


def test_library_methods_no_op_when_not_authenticated(provider):
    """Library generators should yield nothing when auth is off."""
    provider._authenticated = False

    async def _consume(generator):
        return [item async for item in generator]

    assert asyncio.run(_consume(provider.get_library_artists())) == []
    assert asyncio.run(_consume(provider.get_library_albums())) == []
    assert asyncio.run(_consume(provider.get_library_tracks())) == []
    assert asyncio.run(_consume(provider.get_library_playlists())) == []


def test_library_add_remove_short_circuit_when_not_authenticated(provider):
    provider._authenticated = False
    item = MagicMock()
    item.media_type = MediaType.ARTIST
    item.provider_mappings = []
    assert asyncio.run(provider.library_add(item)) is False
    assert asyncio.run(provider.library_remove("UC1", MediaType.ARTIST)) is False


def test_recommendations_empty_when_not_authenticated(provider):
    provider._authenticated = False
    result = asyncio.run(provider.recommendations())
    assert result == []


# ---------------------------------------------------------------------------
# library_add / library_remove — 403 no-op for user-owned items
# ---------------------------------------------------------------------------


def _make_authed_provider_with_rate_failure(provider, error: Exception):
    provider._authenticated = True
    mock = MagicMock()
    mock.rate_playlist = MagicMock(side_effect=error)
    mock.subscribe_artists = MagicMock(side_effect=error)
    mock.unsubscribe_artists = MagicMock(side_effect=error)
    provider._ytmusic = mock
    return mock


def _make_item(media_type, item_id):
    item = MagicMock()
    item.media_type = media_type
    mapping = MagicMock()
    mapping.provider_instance = provider_instance_id_for_tests
    mapping.item_id = item_id
    item.provider_mappings = [mapping]
    return item


provider_instance_id_for_tests = "test_instance"


def test_library_add_treats_403_on_playlist_as_no_op(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    item = _make_item(MediaType.PLAYLIST, "PL0OwTHSGw5kg_owned")
    assert asyncio.run(provider.library_add(item)) is True


def test_library_add_treats_403_on_album_as_no_op(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    item = _make_item(MediaType.ALBUM, "MPREb_owned")
    assert asyncio.run(provider.library_add(item)) is True


def test_library_add_non_403_error_returns_false(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 500: Internal Server Error.")
    )
    item = _make_item(MediaType.PLAYLIST, "PLsome")
    assert asyncio.run(provider.library_add(item)) is False


def test_library_add_403_on_artist_is_not_swallowed(provider):
    """The 403 no-op only applies to ALBUM/PLAYLIST — artist subscription failure is real."""
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    item = _make_item(MediaType.ARTIST, "UCsome")
    assert asyncio.run(provider.library_add(item)) is False


def test_library_remove_treats_403_on_playlist_as_no_op(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    assert (
        asyncio.run(provider.library_remove("PL0OwTHSGw5kg_owned", MediaType.PLAYLIST))
        is True
    )


def test_library_remove_treats_403_on_album_as_no_op(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    assert asyncio.run(provider.library_remove("MPREb_owned", MediaType.ALBUM)) is True


def test_library_remove_non_403_error_returns_false(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 500: Internal Server Error.")
    )
    assert asyncio.run(provider.library_remove("PLsome", MediaType.PLAYLIST)) is False


def test_library_remove_403_on_artist_is_not_swallowed(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    assert asyncio.run(provider.library_remove("UCsome", MediaType.ARTIST)) is False


# ---------------------------------------------------------------------------
# Cookie sanity warning (issue #6 follow-up)
# ---------------------------------------------------------------------------


import logging as _logging


class _CaptureHandler(_logging.Handler):
    """Logging handler that stores records for later assertion."""

    def __init__(self):
        super().__init__(level=_logging.DEBUG)
        self.records: list[_logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


def _attach_capture(provider):
    handler = _CaptureHandler()
    logger = _logging.getLogger(f"ytmusic_capture_{id(handler)}")
    logger.handlers = [handler]
    logger.setLevel(_logging.DEBUG)
    logger.propagate = False
    provider.logger = logger
    return handler


def test_build_auth_headers_warns_when_recommended_cookies_missing(provider, monkeypatch):
    _forbid_open(monkeypatch)
    handler = _attach_capture(provider)

    # Has the hard requirement but none of the recommended session cookies.
    provider._build_auth_headers("__Secure-3PAPISID=onlythis; SAPISID=foo")

    messages = handler.messages()
    assert any("missing recommended" in m for m in messages), messages
    joined = " ".join(messages)
    assert "__Secure-1PSID" in joined
    assert "__Secure-3PSID" in joined


def test_build_auth_headers_no_warning_when_full_cookie_present(provider, monkeypatch):
    _forbid_open(monkeypatch)
    handler = _attach_capture(provider)

    cookie = (
        "__Secure-3PAPISID=a; SAPISID=b; "
        "__Secure-1PSID=c; __Secure-3PSID=d; HSID=e"
    )
    provider._build_auth_headers(cookie)

    assert not any("missing recommended" in m for m in handler.messages())


def test_build_auth_headers_substring_only_does_not_satisfy_recommendation(provider, monkeypatch):
    """A bare mention like '__Secure-1PSID-other=v' must not count as having that cookie."""
    _forbid_open(monkeypatch)
    handler = _attach_capture(provider)
    # The cookie names parsed are the bit before '=' — make sure we match exactly.
    cookie = "__Secure-3PAPISID=a; __Secure-1PSID-typo=oops; SAPISID=b"
    provider._build_auth_headers(cookie)
    joined = " ".join(handler.messages())
    assert "__Secure-1PSID" in joined  # listed as missing


# ---------------------------------------------------------------------------
# Auth-lapse detection in library calls
# ---------------------------------------------------------------------------


def test_is_auth_lapse_detects_401(provider):
    assert provider._is_auth_lapse(RuntimeError("Server returned HTTP 401: Unauthorized")) is True


def test_is_auth_lapse_detects_unauthorized_text(provider):
    assert provider._is_auth_lapse(RuntimeError("Unauthorized access")) is True


def test_is_auth_lapse_ignores_non_auth_errors(provider):
    assert provider._is_auth_lapse(RuntimeError("Connection reset by peer")) is False
    assert provider._is_auth_lapse(RuntimeError("HTTP 500")) is False
    # 403 alone is intentionally not treated as auth lapse here — it has the
    # separate owned-playlist no-op path. Auth lapses surface as 401.
    assert provider._is_auth_lapse(RuntimeError("HTTP 403: Forbidden")) is False


def test_library_error_warning_includes_refresh_hint_on_auth_lapse(provider):
    handler = _attach_capture(provider)
    provider._auth_lapse_warned = False
    provider._warn_library_error(
        "get_library_songs", RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    joined = " ".join(handler.messages())
    assert "refresh it" in joined.lower() or "cookie" in joined.lower()
    assert provider._auth_lapse_warned is True


def test_library_error_warning_does_not_spam_repeated_auth_errors(provider):
    handler = _attach_capture(provider)
    provider._auth_lapse_warned = False
    err = RuntimeError("Server returned HTTP 401: Unauthorized")
    provider._warn_library_error("get_library_songs", err)
    provider._warn_library_error("get_library_playlists", err)
    provider._warn_library_error("get_library_albums", err)
    warnings = [r for r in handler.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in handler.records]


def test_library_error_warning_uses_generic_message_for_non_auth_errors(provider):
    handler = _attach_capture(provider)
    provider._auth_lapse_warned = False
    provider._warn_library_error(
        "get_library_albums", RuntimeError("Connection timeout")
    )
    warnings = [r for r in handler.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "Connection timeout" in msg
    assert "cookie" not in msg.lower()
    assert provider._auth_lapse_warned is False


def test_get_library_playlists_propagates_auth_lapse_hint(provider):
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_playlists = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    provider._ytmusic = mock

    async def _consume():
        return [item async for item in provider.get_library_playlists()]

    assert asyncio.run(_consume()) == []
    joined = " ".join(handler.messages())
    assert "cookie" in joined.lower()


def test_get_library_albums_propagates_auth_lapse_hint(provider):
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_albums = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    provider._ytmusic = mock

    async def _consume():
        return [item async for item in provider.get_library_albums()]

    assert asyncio.run(_consume()) == []
    joined = " ".join(handler.messages())
    assert "cookie" in joined.lower()


def test_recommendations_propagates_auth_lapse_hint(provider):
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_home = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    provider._ytmusic = mock

    result = asyncio.run(provider.recommendations())
    assert result == []
    joined = " ".join(handler.messages())
    assert "cookie" in joined.lower()


# ---------------------------------------------------------------------------
# Partial-auth empty-library detection (issue #10)
# ---------------------------------------------------------------------------


def test_probe_session_alive_true_when_account_info_has_name(provider):
    mock = MagicMock()
    mock.get_account_info = MagicMock(return_value={"accountName": "Someone"})
    provider._ytmusic = mock
    assert provider._probe_session_alive() is True


def test_probe_session_alive_false_when_account_info_missing_name(provider):
    mock = MagicMock()
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock
    assert provider._probe_session_alive() is False


def test_probe_session_alive_false_on_auth_lapse_error(provider):
    mock = MagicMock()
    mock.get_account_info = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    provider._ytmusic = mock
    assert provider._probe_session_alive() is False


def test_probe_session_alive_none_on_transient_error(provider):
    """A non-auth error must NOT be treated as a definite lapse signal."""
    mock = MagicMock()
    mock.get_account_info = MagicMock(side_effect=RuntimeError("Connection reset"))
    provider._ytmusic = mock
    assert provider._probe_session_alive() is None


def test_probe_session_alive_none_when_method_unavailable(provider):
    """Older ytmusicapi without get_account_info — undetermined, never False."""
    provider._ytmusic = object()  # bare object, no methods
    assert provider._probe_session_alive() is None


def test_probe_session_alive_none_when_ytmusic_unset(provider):
    provider._ytmusic = None
    assert provider._probe_session_alive() is None


def _consume(generator):
    async def _drain():
        return [item async for item in generator]

    return asyncio.run(_drain())


def _track_dict(video_id: str, title: str = "x") -> dict:
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"id": "UC1", "name": "A"}],
    }


def test_first_empty_library_sync_does_not_warn_or_raise(provider):
    """A brand-new account with no liked songs should sync to empty silently."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(return_value=[])
    mock.get_account_info = MagicMock(
        return_value={"accountName": "Should Not Be Called"}
    )
    provider._ytmusic = mock

    result = _consume(provider.get_library_tracks())

    assert result == []
    # Probe must not be invoked on first-ever empty result.
    assert mock.get_account_info.call_count == 0
    warnings = [r for r in handler.records if r.levelname == "WARNING"]
    assert warnings == []


def test_repeated_empty_library_sync_does_not_warn_or_probe(provider):
    """Empty → empty (never populated) must stay silent and never probe."""
    provider._authenticated = True
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(return_value=[])
    mock.get_account_info = MagicMock(return_value={"accountName": "x"})
    provider._ytmusic = mock

    _consume(provider.get_library_tracks())
    _consume(provider.get_library_tracks())

    assert mock.get_account_info.call_count == 0
    assert [r for r in handler.records if r.levelname == "WARNING"] == []


def test_populated_then_empty_triggers_probe_and_raises_on_lapse(provider):
    """Once we've seen items, a later empty sync must probe and raise on lapse."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    # First call returns items, second returns empty (the lapse).
    mock.get_library_songs = MagicMock(side_effect=[[_track_dict("v1")], []])
    mock.get_account_info = MagicMock(return_value={})  # logged-out shape
    provider._ytmusic = mock

    first = _consume(provider.get_library_tracks())
    assert len(first) == 1

    with pytest.raises(RuntimeError, match="partial-auth"):
        _consume(provider.get_library_tracks())

    assert mock.get_account_info.call_count == 1
    joined = " ".join(handler.messages()).lower()
    assert "cookie" in joined  # warning text should hint at cookie refresh


def test_populated_then_empty_does_not_raise_when_probe_alive(provider):
    """Probe confirms session — treat empty as a real empty library, no raise."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(side_effect=[[_track_dict("v1")], []])
    mock.get_account_info = MagicMock(return_value={"accountName": "Someone"})
    provider._ytmusic = mock

    _consume(provider.get_library_tracks())
    # Probe says alive — generator returns empty without raising.
    result = _consume(provider.get_library_tracks())
    assert result == []
    assert mock.get_account_info.call_count == 1
    assert [r for r in handler.records if r.levelname == "WARNING"] == []


def test_populated_then_empty_does_not_raise_on_undetermined_probe(provider):
    """Transient probe error must not raise — that would invent a false alarm."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(side_effect=[[_track_dict("v1")], []])
    mock.get_account_info = MagicMock(side_effect=RuntimeError("Connection timeout"))
    provider._ytmusic = mock

    _consume(provider.get_library_tracks())
    result = _consume(provider.get_library_tracks())
    assert result == []
    assert [r for r in handler.records if r.levelname == "WARNING"] == []


def test_partial_auth_guard_covers_get_library_albums(provider):
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_albums = MagicMock(
        side_effect=[[{"browseId": "MPREb_x", "title": "A"}], []]
    )
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock

    _consume(provider.get_library_albums())
    with pytest.raises(RuntimeError, match="partial-auth"):
        _consume(provider.get_library_albums())
    joined = " ".join(handler.messages()).lower()
    assert "cookie" in joined


def test_partial_auth_guard_covers_get_library_playlists(provider):
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_playlists = MagicMock(
        side_effect=[[{"id": "PL1", "title": "P"}], []]
    )
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock

    _consume(provider.get_library_playlists())
    with pytest.raises(RuntimeError, match="partial-auth"):
        _consume(provider.get_library_playlists())


def test_partial_auth_guard_covers_get_library_artists(provider):
    """Artists generator combines subscriptions + library artists; guard sees total."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_subscriptions = MagicMock(
        side_effect=[[{"channelId": "UC1", "name": "A"}], []]
    )
    mock.get_library_artists = MagicMock(side_effect=[[], []])
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock

    first = _consume(provider.get_library_artists())
    assert len(first) == 1
    with pytest.raises(RuntimeError, match="partial-auth"):
        _consume(provider.get_library_artists())


def test_get_library_artists_parses_browse_id_and_artist_keys(provider):
    """Subscriptions/library artists keyed browseId/artist parse without pre-mapping."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_subscriptions = MagicMock(
        return_value=[{"browseId": "UCsub", "artist": "Subscribed Artist"}]
    )
    mock.get_library_artists = MagicMock(
        return_value=[{"browseId": "UClib", "artist": "Library Artist"}]
    )
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock

    artists = _consume(provider.get_library_artists())
    assert {a.item_id: a.name for a in artists} == {
        "UCsub": "Subscribed Artist",
        "UClib": "Library Artist",
    }


def test_partial_auth_guard_per_category_state_isolated(provider):
    """Having seen tracks must not arm the guard for playlists."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(return_value=[_track_dict("v1")])
    mock.get_library_playlists = MagicMock(return_value=[])
    mock.get_account_info = MagicMock(return_value={})  # would say lapsed if called
    provider._ytmusic = mock

    # Populate tracks state.
    _consume(provider.get_library_tracks())
    # Playlists has never been populated — empty result must not probe.
    result = _consume(provider.get_library_playlists())
    assert result == []
    assert mock.get_account_info.call_count == 0
    assert [r for r in handler.records if r.levelname == "WARNING"] == []
