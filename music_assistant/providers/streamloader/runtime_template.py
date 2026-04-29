"""Runtime-ready Music Assistant provider bridge for streamloader.

This module is intentionally defensive about MA import paths so it can be
developed/tested outside MA and dropped into a running MA instance.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote, unquote, unquote_plus

from .provider import StreamloaderMAAdapter, StreamloaderMusicProvider

_LOGGER = logging.getLogger(__name__)


def _import_music_provider_base() -> type:
    for module_name in (
        "music_assistant.server.models.music_provider",
        "music_assistant.models.music_provider",
    ):
        try:
            module = __import__(module_name, fromlist=["MusicProvider"])
            base = getattr(module, "MusicProvider", None)
            if isinstance(base, type):
                return base
        except Exception:
            continue
    return object


def _import_search_results_model():
    try:
        from music_assistant_models.media_items import SearchResults

        return SearchResults
    except Exception:
        return None


def _import_browse_models() -> tuple[Any, Any]:
    try:
        from music_assistant_models.enums import ImageType, MediaType
        from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemImage

        return BrowseFolder, (MediaType, ItemMapping, ImageType, MediaItemImage)
    except Exception:
        return None, None


def _import_stream_details_model():
    for module_name in (
        "music_assistant_models.streamdetails",
        "music_assistant_models.media_items",
    ):
        try:
            module = __import__(module_name, fromlist=["StreamDetails"])
            model = getattr(module, "StreamDetails", None)
            if model is not None:
                return model
        except Exception:
            continue
    return None


MusicProviderBase = _import_music_provider_base()
SearchResultsModel = _import_search_results_model()
StreamDetailsModel = _import_stream_details_model()
BrowseFolderModel, BrowseModelDeps = _import_browse_models()


def _import_provider_unavailable_error() -> type[Exception]:
    try:
        from music_assistant_models.errors import ProviderUnavailableError

        return ProviderUnavailableError
    except Exception:
        return RuntimeError


ProviderUnavailableErrorClass = _import_provider_unavailable_error()


class StreamloaderMAProvider(MusicProviderBase):
    """Music Assistant provider implementation backed by streamloader."""

    @property
    def instance_id(self) -> str:
        value = getattr(self, "_streamloader_instance_id", None)
        if value:
            return str(value)
        cfg = getattr(self, "config", None)
        cfg_value = getattr(cfg, "instance_id", None)
        if cfg_value:
            return str(cfg_value)
        return "streamloader"

    @instance_id.setter
    def instance_id(self, value: Any) -> None:
        self._streamloader_instance_id = str(value or "streamloader")

    @property
    def domain(self) -> str:
        value = getattr(self, "_streamloader_domain", None)
        if value:
            return str(value)
        return "streamloader"

    @domain.setter
    def domain(self, value: Any) -> None:
        self._streamloader_domain = str(value or "streamloader")

    def __init__(self, mass: Any, manifest: Any, config: Any) -> None:
        try:
            super().__init__(mass, manifest, config)  # type: ignore[misc]
        except Exception:
            # Non-MA local runs fall back to plain object semantics.
            pass
        self.mass = mass
        self.manifest = manifest
        self.config = config
        self._provider_domain = str(getattr(manifest, "domain", "streamloader") or "streamloader")
        self.instance_id = getattr(config, "instance_id", "streamloader")
        self.domain = self._provider_domain

        base_url = self._config_value("base_url", "http://streamloader:41422").rstrip("/")
        api_key = self._config_value("api_key", "") or None
        try:
            request_timeout = max(2.0, float(self._config_value("request_timeout_seconds", "25")))
        except Exception:
            request_timeout = 25.0
        try:
            request_retries = max(1, min(8, int(self._config_value("request_retries", "4"))))
        except Exception:
            request_retries = 4
        self._provider = StreamloaderMusicProvider(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=request_timeout,
            request_retries=request_retries,
        )
        self._provider.instance_id = self.instance_id
        self._provider.domain = self._provider_domain
        self._adapter = StreamloaderMAAdapter(self._provider)
        try:
            self._default_search_limit = max(5, int(self._config_value("search_limit", "25")))
        except Exception:
            self._default_search_limit = 25
        self._prefer_library_browse = self._config_value("prefer_library_browse", "true").lower() not in {
            "false",
            "0",
            "no",
        }
        self._auto_download_track_on_play = self._config_bool("auto_download_track_on_play", False)
        self._auto_download_album_on_play = self._config_bool("auto_download_album_on_play", False)
        self._auto_download_album_missing_only = self._config_bool("auto_download_album_missing_only", False)
        self._strict_startup_health_check = self._config_bool("strict_startup_health_check", True)
        self._seen_auto_download_tracks: set[str] = set()
        self._seen_auto_download_albums: set[str] = set()
        self._artist_name_cache: dict[str, str] = {}
        # Lowercase names of library artists, refreshed during get_library_artists.
        # Used by search() to filter out live-provider duplicates of artists
        # already present in the local library.
        self._library_artist_names_lower: set[str] = set()

    @staticmethod
    def _is_unavailable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        needles = (
            "name or service not known",
            "no address associated with hostname",
            "temporary failure in name resolution",
            "connection refused",
            "connection reset",
            "connect timeout",
            "read timeout",
            "timed out",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "network is unreachable",
            "all connection attempts failed",
            "401",
            "403",
            "unauthorized",
            "forbidden",
        )
        return any(needle in text for needle in needles)

    def _raise_unavailable(self, exc: Exception, context: str) -> None:
        if not self._is_unavailable_error(exc):
            return
        try:
            setattr(self, "available", False)
        except Exception:
            pass
        raise ProviderUnavailableErrorClass(f"Streamloader unavailable during {context}: {exc}") from exc

    def _config_value(self, key: str, default: str = "") -> str:
        config = self.config
        value = None
        if hasattr(config, "get_value"):
            try:
                value = config.get_value(key)
            except Exception:
                value = None
        if value is None and isinstance(config, dict):
            value = config.get(key)
        if value is None:
            value = getattr(config, key, default)
        return str(value if value is not None else default)

    def _config_bool(self, key: str, default: bool = False) -> bool:
        value = self._config_value(key, "true" if default else "false").strip().lower()
        return value in {"1", "true", "yes", "on"}

    @staticmethod
    def _library_relative_path(item_id: Any) -> str | None:
        raw = str(item_id or "").strip()
        if not raw:
            return None
        if raw.startswith("library://"):
            return raw.replace("library://", "", 1).strip("/")
        if raw.startswith("library:"):
            encoded = raw.replace("library:", "", 1).strip()
            if not encoded:
                return None
            return unquote(encoded).strip("/")
        # Some MA call paths may pass through plain relative paths for local items.
        if "/" in raw and not raw.startswith(("monochrome://", "http://", "https://")):
            return raw.strip("/")
        return None

    @staticmethod
    def _strip_extension(name: str) -> str:
        value = str(name or "").strip()
        return value.rsplit(".", 1)[0] if "." in value else value

    @staticmethod
    def _artist_sort_key(name: Any) -> str:
        value = str(name or "").strip().lower()
        if value.startswith("the "):
            value = value[4:].strip()
        return value

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            parsed = int(float(value))
            return parsed if parsed > 0 else None
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = int(float(text))
        except Exception:
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _audio_format_hints_from_track(track: Any) -> tuple[int | None, int | None, int | None, int | None]:
        sample_rate = None
        bit_depth = None
        channels = None
        bit_rate = None
        mappings = getattr(track, "provider_mappings", None)
        if not mappings:
            return sample_rate, bit_depth, channels, bit_rate
        try:
            iterable = list(mappings)
        except Exception:
            iterable = []
        for mapping in iterable:
            audio = getattr(mapping, "audio_format", None)
            if audio is None:
                continue
            sample_rate = sample_rate or StreamloaderMAProvider._coerce_positive_int(getattr(audio, "sample_rate", None))
            bit_depth = bit_depth or StreamloaderMAProvider._coerce_positive_int(getattr(audio, "bit_depth", None))
            channels = channels or StreamloaderMAProvider._coerce_positive_int(getattr(audio, "channels", None))
            bit_rate = bit_rate or StreamloaderMAProvider._coerce_positive_int(getattr(audio, "bit_rate", None))
            if sample_rate and channels:
                break
        return sample_rate, bit_depth, channels, bit_rate

    @staticmethod
    def _content_type_from_mime(mime_type: Any) -> Any:
        mime = str(mime_type or "").strip().lower()
        try:
            from music_assistant_models.enums import ContentType
        except Exception:
            return None
        if "flac" in mime:
            return getattr(ContentType, "FLAC", getattr(ContentType, "UNKNOWN", None))
        if "mpeg" in mime or "mp3" in mime:
            return getattr(ContentType, "MP3", getattr(ContentType, "UNKNOWN", None))
        if "aac" in mime:
            return getattr(ContentType, "AAC", getattr(ContentType, "UNKNOWN", None))
        if "ogg" in mime:
            return getattr(ContentType, "OGG", getattr(ContentType, "UNKNOWN", None))
        if "opus" in mime:
            return getattr(ContentType, "OPUS", getattr(ContentType, "UNKNOWN", None))
        if "mp4" in mime or "m4a" in mime:
            for candidate in ("M4A", "AAC"):
                value = getattr(ContentType, candidate, None)
                if value is not None:
                    return value
        return getattr(ContentType, "UNKNOWN", None)

    @staticmethod
    def _infer_stream_mime(mime_type: Any, *, quality: Any = None, item_id: Any = None) -> str:
        mime = str(mime_type or "").strip().lower()
        if mime and mime != "audio/*":
            return mime
        item_text = str(item_id or "").strip().lower()
        if item_text.endswith(".flac"):
            return "audio/flac"
        if item_text.endswith(".mp3"):
            return "audio/mpeg"
        if item_text.endswith(".opus"):
            return "audio/ogg"
        if item_text.endswith(".aac"):
            return "audio/aac"
        if item_text.endswith(".m4a") or item_text.endswith(".mp4"):
            return "audio/mp4"
        quality_text = str(quality or "").strip().lower()
        if "lossless" in quality_text or "flac" in quality_text:
            return "audio/flac"
        if "opus" in quality_text:
            return "audio/ogg"
        if "aac" in quality_text:
            return "audio/aac"
        if quality_text in {"high", "medium", "low", "mp3"}:
            return "audio/mpeg"
        # Conservative default for monochrome pulls and local-library FLAC collections.
        return "audio/flac"

    @staticmethod
    def _local_artist_id(name: str) -> str:
        return f"local-artist-{quote(str(name or '').strip(), safe='')}"

    @staticmethod
    def _local_album_id(path: str) -> str:
        return f"local-album-{quote(str(path or '').strip(), safe='')}"

    @staticmethod
    def _id_tail(value: str) -> str:
        text = str(value or "").strip()
        return text.rsplit("/", 1)[-1].strip() if text else ""

    def _safe_display_name_from_id(self, value: Any, default: str) -> str:
        text = str(value or "").strip()
        if text and not self._looks_like_uri_name(text):
            return text
        tail = self._id_tail(text)
        if tail and not self._looks_like_uri_name(tail):
            return tail
        return str(default or "").strip() or "Unknown Artist"

    @staticmethod
    def _norm_text(value: Any) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _looks_like_uri_name(value: Any) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return False
        return text.startswith(("monochrome://", "http://", "https://")) or "/artist/" in text

    def _stream_context_label(self, *args: Any, **kwargs: Any) -> str:
        candidates: list[str] = []
        for key in ("player_name", "player_id", "queue_item_id", "session_id", "client_id"):
            value = str(kwargs.get(key, "") or "").strip()
            if value:
                candidates.append(value)
        for arg in args:
            if isinstance(arg, (str, int, float)):
                value = str(arg).strip()
                if value and len(value) <= 64:
                    candidates.append(value)
            elif isinstance(arg, dict):
                for key in ("player_name", "player_id", "queue_item_id", "session_id", "client_id"):
                    value = str(arg.get(key, "") or "").strip()
                    if value:
                        candidates.append(value)
            else:
                for key in ("player_name", "player_id", "queue_item_id", "session_id", "client_id"):
                    value = str(getattr(arg, key, "") or "").strip()
                    if value:
                        candidates.append(value)
        deduped: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return " | ".join(deduped[:2])

    async def _resolve_artist_name_from_catalog(self, artist_id: str) -> str:
        decoded_artist_id = self._adapter.decode_provider_id(artist_id)
        if "://artist/" not in decoded_artist_id:
            return ""
        query = self._id_tail(decoded_artist_id)
        if not query:
            return ""
        try:
            payload = await self._provider.search_items(query, limit=max(10, self._default_search_limit))
        except Exception:
            return ""
        artists = payload.get("artists", []) if isinstance(payload, dict) else []
        for artist in artists:
            if not isinstance(artist, dict):
                continue
            candidate_id = self._adapter.decode_provider_id(artist.get("id") or "")
            if not candidate_id:
                continue
            if candidate_id == decoded_artist_id or self._id_tail(candidate_id) == self._id_tail(decoded_artist_id):
                name = str(artist.get("name") or "").strip()
                if name and not self._looks_like_uri_name(name):
                    return name
        # Fallback: some sparse/edge payloads may not return artist rows for this id,
        # but tracks/albums can still carry a usable artist_name.
        name_votes: dict[str, int] = {}
        for key in ("tracks", "albums"):
            rows = payload.get(key, []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                candidate_name = str(row.get("artist_name") or "").strip()
                if not candidate_name or self._looks_like_uri_name(candidate_name):
                    continue
                name_votes[candidate_name] = name_votes.get(candidate_name, 0) + 1
        if name_votes:
            ranked = sorted(name_votes.items(), key=lambda pair: pair[1], reverse=True)
            return ranked[0][0]
        return ""

    async def _resolve_artist_name_from_albums(self, artist_id: str, artist_name: str | None = None) -> str:
        decoded_artist_id = self._adapter.decode_provider_id(artist_id)
        if "://artist/" not in decoded_artist_id:
            return ""
        try:
            albums = await self._provider.get_artist_albums(
                decoded_artist_id,
                artist_name=(str(artist_name or "").strip() or None),
                limit=max(8, self._default_search_limit),
            )
        except Exception:
            return ""
        name_votes: dict[str, int] = {}
        for album in albums:
            if not isinstance(album, dict):
                continue
            candidates: list[str] = []
            direct_name = str(album.get("artist_name") or "").strip()
            if direct_name:
                candidates.append(direct_name)
            artist_obj = album.get("artist")
            if isinstance(artist_obj, dict):
                nested_name = str(artist_obj.get("name") or "").strip()
                if nested_name:
                    candidates.append(nested_name)
            artists_list = album.get("artists")
            if isinstance(artists_list, list):
                for row in artists_list[:3]:
                    if isinstance(row, dict):
                        nested_name = str(row.get("name") or "").strip()
                    else:
                        nested_name = str(getattr(row, "name", "") or "").strip()
                    if nested_name:
                        candidates.append(nested_name)
            for candidate_name in candidates:
                if not candidate_name or self._looks_like_uri_name(candidate_name):
                    continue
                name_votes[candidate_name] = name_votes.get(candidate_name, 0) + 1
        if not name_votes:
            return ""
        ranked = sorted(name_votes.items(), key=lambda pair: pair[1], reverse=True)
        return ranked[0][0]

    async def _resolve_artist_name_from_locked_tracks(self, artist_id: str, artist_name: str | None = None) -> str:
        decoded_artist_id = self._adapter.decode_provider_id(artist_id)
        if "://artist/" not in decoded_artist_id:
            return ""
        query = str(artist_name or "").strip() or self._id_tail(decoded_artist_id)
        if not query:
            return ""
        try:
            payload = await self._provider.search_items(
                query,
                limit=max(10, self._default_search_limit),
                artist_id=decoded_artist_id,
                artist_name=(str(artist_name or "").strip() or None),
            )
        except TypeError:
            # Compatibility fallback if a provider adapter does not accept artist filters.
            try:
                payload = await self._provider.search_items(
                    query,
                    limit=max(10, self._default_search_limit),
                )
            except Exception:
                return ""
        except Exception:
            return ""
        rows = payload.get("tracks", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return ""
        name_votes: dict[str, int] = {}
        target_tail = self._id_tail(decoded_artist_id)
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate_id = self._adapter.decode_provider_id(row.get("artist_id") or "")
            if candidate_id:
                candidate_tail = self._id_tail(candidate_id)
                if target_tail and candidate_tail and candidate_tail != target_tail:
                    continue
            candidate_name = str(row.get("artist_name") or "").strip()
            if not candidate_name or self._looks_like_uri_name(candidate_name):
                continue
            name_votes[candidate_name] = name_votes.get(candidate_name, 0) + 1
        if not name_votes:
            return ""
        ranked = sorted(name_votes.items(), key=lambda pair: pair[1], reverse=True)
        return ranked[0][0]

    def _is_artist_match(self, artist_id: str, artist_name: str, candidate_artist_id: Any, candidate_artist_name: Any) -> bool:
        target_id = self._adapter.decode_provider_id(artist_id)
        target_tail = self._id_tail(target_id)
        candidate_id = self._adapter.decode_provider_id(candidate_artist_id)
        candidate_tail = self._id_tail(candidate_id)
        if target_id and candidate_id and target_id == candidate_id:
            return True
        if target_tail and candidate_tail and target_tail == candidate_tail:
            return True
        target_name = self._norm_text(artist_name)
        candidate_name = self._norm_text(candidate_artist_name)
        return bool(target_name and candidate_name and target_name == candidate_name)

    def _is_artist_name_conflict(self, preferred_name: str, candidate_name: str) -> bool:
        left = self._norm_text(preferred_name)
        right = self._norm_text(candidate_name)
        if not left or not right:
            return False
        if left == right:
            return False
        return True

    @property
    def supported_features(self) -> set[Any]:
        """Advertise only implemented features to avoid MA NotImplemented calls."""
        try:
            from music_assistant_models.enums import ProviderFeature

            features = {ProviderFeature.SEARCH, ProviderFeature.BROWSE}
            for feature_name in (
                "LIBRARY_ARTISTS",
                "LIBRARY_ALBUMS",
                "LIBRARY_TRACKS",
                "ARTIST_ALBUMS",
                "ARTIST_TOPTRACKS",
                "ALBUM_TRACKS",
                "SIMILAR_TRACKS",
            ):
                feature = getattr(ProviderFeature, feature_name, None)
                if feature is not None:
                    features.add(feature)
            return features
        except Exception:
            return {
                "search",
                "browse",
                "library_artists",
                "library_albums",
                "library_tracks",
                "artist_albums",
                "artist_toptracks",
                "album_tracks",
            }

    async def loaded_in_mass(self) -> None:
        if not self._strict_startup_health_check:
            # Avoid blocking provider startup on transient backend availability.
            try:
                setattr(self, "available", True)
            except Exception:
                pass
            return None
        last_error: Exception | None = None
        for attempt in range(1, 7):
            try:
                health = await self._provider.client.health()
                if health.get("status") in {"ok", "degraded"}:
                    try:
                        setattr(self, "available", True)
                    except Exception:
                        pass
                    return
                last_error = RuntimeError(f"Streamloader health check failed: {health}")
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(min(3.0, 0.4 * attempt))
        if self._strict_startup_health_check:
            try:
                setattr(self, "available", False)
            except Exception:
                pass
            raise RuntimeError(f"Streamloader startup health check failed: {last_error}")
        return None

    async def unload_from_mass(self) -> None:
        return None

    async def get_library_artists(self) -> AsyncGenerator[Any, None]:
        try:
            entries = await self._provider.browse_library("")
        except Exception:
            entries = []
        artist_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("entry_type", "")).lower() == "artist"
        ]
        artist_entries.sort(key=lambda item: self._artist_sort_key(item.get("name")))
        # Rebuild the library-names set from scratch so removed artists don't linger.
        self._library_artist_names_lower = {
            str(e.get("name") or "").strip().lower()
            for e in artist_entries
            if str(e.get("name") or "").strip()
        }
        for entry in artist_entries:
            if not isinstance(entry, dict) or str(entry.get("entry_type", "")).lower() != "artist":
                continue
            name = str(entry.get("name") or "").strip()
            path = str(entry.get("path") or "").strip()
            if not name or not path:
                continue
            image_url = await self._resolve_artist_library_art_url(path)
            yield self._adapter._to_ma_object(
                "artist",
                {
                    "item_id": self._local_artist_id(name),
                    "name": name,
                    "media_type": "artist",
                    "image_url": image_url,
                },
            )

    async def _resolve_artist_library_art_url(self, artist_path: str) -> str | None:
        artist_path = str(artist_path or "").strip()
        if not artist_path:
            return None
        try:
            artist_art = await self._provider.get_library_art_url(artist_path, artist_level=True)
            if str(artist_art or "").strip():
                return str(artist_art).strip()
        except Exception:
            pass
        # Fallback: if artist.jpg is missing, use first available album cover.
        try:
            entries = await self._provider.browse_library(artist_path)
        except Exception:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("entry_type", "")).lower() != "album":
                continue
            album_path = str(entry.get("path") or "").strip()
            if not album_path:
                continue
            try:
                album_art = await self._provider.get_library_art_url(album_path, artist_level=False)
                if str(album_art or "").strip():
                    return str(album_art).strip()
            except Exception:
                continue
        return None

    async def get_library_albums(self) -> AsyncGenerator[Any, None]:
        try:
            artists = await self._provider.browse_library("")
        except Exception:
            artists = []
        artists = [
            entry
            for entry in artists
            if isinstance(entry, dict) and str(entry.get("entry_type", "")).lower() == "artist"
        ]
        artists.sort(key=lambda item: self._artist_sort_key(item.get("name")))
        for artist_entry in artists:
            if not isinstance(artist_entry, dict) or str(artist_entry.get("entry_type", "")).lower() != "artist":
                continue
            artist_name = str(artist_entry.get("name") or "").strip()
            artist_path = str(artist_entry.get("path") or "").strip()
            if not artist_name or not artist_path:
                continue
            try:
                albums = await self._provider.browse_library(artist_path)
            except Exception:
                albums = []
            for album_entry in albums:
                if not isinstance(album_entry, dict) or str(album_entry.get("entry_type", "")).lower() != "album":
                    continue
                album_name = str(album_entry.get("name") or "").strip()
                album_path = str(album_entry.get("path") or "").strip()
                if not album_name or not album_path:
                    continue
                image_url = None
                try:
                    image_url = await self._provider.get_library_art_url(album_path, artist_level=False)
                except Exception:
                    image_url = None
                yield self._adapter._to_ma_object(
                    "album",
                    {
                        "item_id": self._local_album_id(album_path),
                        "name": album_name,
                        "media_type": "album",
                        "artist": artist_name,
                        "artist_id": self._local_artist_id(artist_name),
                        "image_url": image_url,
                    },
                )

    async def get_library_tracks(self) -> AsyncGenerator[Any, None]:
        try:
            artists = await self._provider.browse_library("")
        except Exception:
            artists = []
        artists = [
            entry
            for entry in artists
            if isinstance(entry, dict) and str(entry.get("entry_type", "")).lower() == "artist"
        ]
        artists.sort(key=lambda item: self._artist_sort_key(item.get("name")))
        for artist_entry in artists:
            if not isinstance(artist_entry, dict) or str(artist_entry.get("entry_type", "")).lower() != "artist":
                continue
            artist_name = str(artist_entry.get("name") or "").strip()
            artist_path = str(artist_entry.get("path") or "").strip()
            if not artist_name or not artist_path:
                continue
            try:
                albums = await self._provider.browse_library(artist_path)
            except Exception:
                albums = []
            for album_entry in albums:
                if not isinstance(album_entry, dict) or str(album_entry.get("entry_type", "")).lower() != "album":
                    continue
                album_name = str(album_entry.get("name") or "").strip()
                album_path = str(album_entry.get("path") or "").strip()
                if not album_name or not album_path:
                    continue
                try:
                    tracks = await self._provider.browse_library(album_path)
                except Exception:
                    tracks = []
                for track_entry in tracks:
                    if not isinstance(track_entry, dict) or str(track_entry.get("entry_type", "")).lower() != "track":
                        continue
                    track_name = self._strip_extension(str(track_entry.get("name") or "").strip())
                    track_path = str(track_entry.get("path") or "").strip()
                    if not track_name or not track_path:
                        continue
                    yield self._adapter._to_ma_object(
                        "track",
                        {
                            "item_id": f"library:{quote(track_path, safe='')}",
                            "name": track_name,
                            "media_type": "track",
                            "artist": artist_name,
                            "artist_id": self._local_artist_id(artist_name),
                            "album": album_name,
                            "album_id": self._local_album_id(album_path),
                            "duration": self._coerce_positive_int(track_entry.get("duration")),
                        },
                    )

    async def get_library_playlists(self) -> AsyncGenerator[Any, None]:
        if False:
            yield None

    async def get_library_radios(self) -> AsyncGenerator[Any, None]:
        if False:
            yield None

    async def browse(self, path: str) -> list[Any]:
        if not self._prefer_library_browse:
            return []
        item_path = ""
        if isinstance(path, str) and "://" in path:
            item_path = path.split("://", 1)[1]
        elif isinstance(path, str):
            item_path = path
        item_path = str(item_path or "").strip("/")

        # Prefer MA media object browse for richer artwork/cards over plain folder icons.
        path_parts = [part for part in item_path.split("/") if part]
        if len(path_parts) == 0:
            items: list[Any] = []
            async for artist in self.get_library_artists():
                items.append(artist)
            if items:
                return items
        elif len(path_parts) == 1:
            local_artist_id = self._local_artist_id(path_parts[0])
            items = await self.get_artist_albums(local_artist_id)
            if items:
                return items
        else:
            album_path = "/".join(path_parts[:2])
            local_album_id = self._local_album_id(album_path)
            items = await self.get_album_tracks(local_album_id)
            if items:
                return items

        try:
            entries = await self._provider.browse_library(item_path)
        except Exception as exc:
            self._raise_unavailable(exc, "browse")
            return []
        if BrowseFolderModel is None or BrowseModelDeps is None:
            return entries
        MediaType, ItemMapping, ImageType, MediaItemImage = BrowseModelDeps
        mapped: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("entry_type", "")).lower()
            entry_name = str(entry.get("name") or "Unknown")
            entry_path = str(entry.get("path") or "").strip("/")
            if not entry_path:
                continue
            folder_image = None
            if entry_type in {"artist", "album"}:
                try:
                    if entry_type == "artist":
                        art_url = await self._resolve_artist_library_art_url(entry_path)
                    else:
                        art_url = await self._provider.get_library_art_url(
                            entry_path,
                            artist_level=False,
                        )
                    if not art_url:
                        raise ValueError("empty art url")
                    folder_image = MediaItemImage(
                        type=ImageType.THUMB,
                        path=art_url,
                        provider=self.instance_id,
                        # Let MA proxy image fetches (works better across container/local host boundaries).
                        remotely_accessible=False,
                    )
                except Exception:
                    folder_image = None
            if entry_type in {"artist", "album"}:
                mapped.append(
                    BrowseFolderModel(
                        item_id=entry_path,
                        provider=self.instance_id,
                        name=entry_name,
                        path=f"{self.instance_id}://{entry_path}",
                        is_playable=entry_type == "album",
                        image=folder_image,
                    )
                )
            elif entry_type == "track":
                encoded_path = quote(entry_path, safe="")
                name = entry_name
                if "." in name:
                    name = name.rsplit(".", 1)[0]
                path_parts = [part for part in entry_path.split("/") if part]
                artist_name = path_parts[0] if len(path_parts) >= 2 else ""
                album_name = path_parts[1] if len(path_parts) >= 3 else ""
                track_payload = {
                    "item_id": f"library:{encoded_path}",
                    "name": name or entry_name,
                    "media_type": "track",
                    "artist": artist_name,
                    "artist_id": self._adapter._encode_provider_id(f"artist/{artist_name.lower()}") if artist_name else "",
                    "album": album_name,
                    "album_id": self._adapter._encode_provider_id(f"album/{album_name.lower()}") if album_name else "",
                }
                mapped.append(self._adapter._to_ma_object("track", track_payload))
        return mapped

    async def library_add(self, item: Any) -> bool:
        return False

    async def library_remove(self, prov_item_id: str, media_type: Any) -> bool:
        return False

    def _dedupe_search_results(self, mapped: Any) -> Any:
        """Dedup search-result tracks and albums by content identity.

        The same logical recording can surface under multiple upstream IDs
        (federated catalog + local library copy + mix-context-flavoured ID).
        MA renders one row per ID, so without dedup the user sees the same
        track 2-3 times. Prefer ``library:...`` IDs over ``monochrome://``
        over anything else (per streamloader_source_of_truth memory: local
        always wins). Artists are deliberately untouched -- the existing
        library-artist filter at the top of ``search()`` already handles them.
        """

        def _name(item: Any) -> str:
            value = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
            return str(value or "")

        def _first_artist_name(item: Any) -> str:
            artists = item.get("artists") if isinstance(item, dict) else getattr(item, "artists", None)
            if artists:
                first = artists[0]
                value = (
                    first.get("name") if isinstance(first, dict) else getattr(first, "name", "")
                )
                if value:
                    return str(value)
            # Fall back to the flat artist field used by some adapter shapes.
            value = (
                item.get("artist") or item.get("artist_name")
                if isinstance(item, dict)
                else getattr(item, "artist", "") or getattr(item, "artist_name", "")
            )
            return str(value or "")

        def track_key(track: Any) -> tuple[str, str, int]:
            duration = (
                track.get("duration") if isinstance(track, dict) else getattr(track, "duration", 0)
            )
            try:
                duration_bucket = int(duration or 0) // 2
            except (TypeError, ValueError):
                duration_bucket = 0
            return (
                _first_artist_name(track).strip().lower(),
                _name(track).strip().lower(),
                duration_bucket,
            )

        def album_key(album: Any) -> tuple[str, str, int]:
            year = album.get("year") if isinstance(album, dict) else getattr(album, "year", None)
            try:
                year_int = int(year or 0)
            except (TypeError, ValueError):
                year_int = 0
            return (
                _first_artist_name(album).strip().lower(),
                _name(album).strip().lower(),
                year_int,
            )

        def id_priority(item: Any) -> int:
            item_id = (
                item.get("item_id") if isinstance(item, dict) else getattr(item, "item_id", "")
            )
            try:
                decoded = self._adapter.decode_provider_id(str(item_id or ""))
            except Exception:
                decoded = str(item_id or "")
            if decoded.startswith("library:"):
                return 0
            if "monochrome://" in decoded:
                return 1
            return 2

        def dedup(items: list[Any], keyfn: Any) -> list[Any]:
            seen: dict[Any, Any] = {}
            for item in items:
                try:
                    key = keyfn(item)
                except Exception:
                    # Anything we can't key safely passes through under a
                    # unique sentinel so we don't accidentally collapse it.
                    seen[(id(item),)] = item
                    continue
                # Skip empty-key items (no artist + no name) -- treat them as unique.
                if not any(key):
                    seen[(id(item),)] = item
                    continue
                existing = seen.get(key)
                if existing is None or id_priority(item) < id_priority(existing):
                    seen[key] = item
            return list(seen.values())

        if isinstance(mapped, dict):
            return {
                **mapped,
                "tracks": dedup(list(mapped.get("tracks", [])), track_key),
                "albums": dedup(list(mapped.get("albums", [])), album_key),
            }
        # Dataclass / model with .tracks / .albums attributes.
        if hasattr(mapped, "tracks") and hasattr(mapped, "albums"):
            try:
                deduped_tracks = dedup(list(getattr(mapped, "tracks", []) or []), track_key)
                deduped_albums = dedup(list(getattr(mapped, "albums", []) or []), album_key)
                return type(mapped)(
                    tracks=deduped_tracks,
                    albums=deduped_albums,
                    artists=getattr(mapped, "artists", []),
                )
            except Exception:
                return mapped
        return mapped

    async def search(self, search_query: str, media_types: Any = None, limit: int = 25) -> Any:
        try:
            mapped = await self._adapter.mapped_search_for_ma(search_query)
        except Exception as exc:
            self._raise_unavailable(exc, "search")
            mapped = {"tracks": [], "albums": [], "artists": []}
        # Dedup tracks + albums BEFORE artist filtering / limit slicing so the
        # limit returns N unique items, not N raw items containing duplicates.
        # Federated + library + mix-context IDs can all surface the same logical
        # recording; MA renders one row per ID, so duplicates were visible to users.
        try:
            mapped = self._dedupe_search_results(mapped)
        except Exception:
            # Dedup must never break search; fall through with the raw mapping.
            pass
        # Eagerly populate the library names if MA has not yet called
        # get_library_artists since startup -- otherwise the first search after
        # a restart has an empty set and the filter below is a no-op.
        if not self._library_artist_names_lower:
            try:
                top_level = await self._provider.browse_library("")
                self._library_artist_names_lower = {
                    str(e.get("name") or "").strip().lower()
                    for e in top_level
                    if isinstance(e, dict)
                    and str(e.get("entry_type", "")).lower() == "artist"
                    and str(e.get("name") or "").strip()
                }
            except Exception:
                # Filesystem unavailable -- skip filtering rather than fail search.
                pass
        # Drop live-provider artist hits whose name already exists in the library.
        # MA otherwise shows two cards for the same artist -- one local, one upstream --
        # because the two sources carry different provider URIs. We prefer the local one.
        if self._library_artist_names_lower:
            def _artist_name_lower(obj: Any) -> str:
                if isinstance(obj, dict):
                    return str(obj.get("name") or "").strip().lower()
                return str(getattr(obj, "name", "") or "").strip().lower()

            def _artist_id(obj: Any) -> str:
                if isinstance(obj, dict):
                    return str(obj.get("item_id") or "").strip()
                return str(getattr(obj, "item_id", "") or "").strip()

            filtered: list[Any] = []
            for artist in mapped.get("artists", []):
                name_lower = _artist_name_lower(artist)
                decoded_id = self._adapter.decode_provider_id(_artist_id(artist))
                is_local = decoded_id.startswith("local-artist-")
                if is_local or name_lower not in self._library_artist_names_lower:
                    filtered.append(artist)
            mapped["artists"] = filtered
        # Honor MA limit as a best-effort per-media slice.
        if not isinstance(limit, int) or limit <= 0:
            limit = self._default_search_limit
        else:
            limit = min(limit, self._default_search_limit)
        if isinstance(limit, int) and limit > 0:
            mapped = {
                "tracks": list(mapped.get("tracks", []))[:limit],
                "albums": list(mapped.get("albums", []))[:limit],
                "artists": list(mapped.get("artists", []))[:limit],
            }
        # MA versions can be strict about Artist model variants in SearchResults.
        # Normalize artist entries to ItemMapping to maximize cross-version compatibility.
        if BrowseModelDeps is not None:
            MediaType, ItemMapping, _ImageType, _MediaItemImage = BrowseModelDeps
            normalized_artists: list[Any] = []
            for artist in list(mapped.get("artists", [])):
                if isinstance(artist, dict):
                    artist_id = str(artist.get("item_id") or "").strip()
                    artist_name = str(artist.get("name") or artist_id or "Unknown Artist").strip()
                else:
                    artist_id = str(getattr(artist, "item_id", "") or "").strip()
                    artist_name = str(getattr(artist, "name", "") or artist_id or "Unknown Artist").strip()
                if not artist_id:
                    continue
                try:
                    normalized_artists.append(
                        ItemMapping(
                            item_id=artist_id,
                            provider=self.instance_id,
                            name=artist_name,
                            media_type=MediaType.ARTIST,
                        )
                    )
                except Exception:
                    normalized_artists.append(artist)
            mapped["artists"] = normalized_artists
        for artist in list(mapped.get("artists", [])):
            artist_id = ""
            artist_name = ""
            if isinstance(artist, dict):
                artist_id = str(artist.get("item_id") or "").strip()
                artist_name = str(artist.get("name") or "").strip()
            else:
                artist_id = str(getattr(artist, "item_id", "") or "").strip()
                artist_name = str(getattr(artist, "name", "") or "").strip()
            if not artist_id or not artist_name:
                continue
            decoded_id = self._adapter.decode_provider_id(artist_id)
            self._artist_name_cache[artist_id] = artist_name
            if decoded_id:
                self._artist_name_cache[decoded_id] = artist_name
        for track in list(mapped.get("tracks", [])):
            track_artist_id = ""
            track_artist_name = ""
            if isinstance(track, dict):
                track_artist_id = str(track.get("artist_id") or "").strip()
                track_artist_name = str(track.get("artist") or track.get("artist_name") or "").strip()
            else:
                artists = list(getattr(track, "artists", []) or [])
                if artists:
                    first_artist = artists[0]
                    if isinstance(first_artist, dict):
                        track_artist_id = str(first_artist.get("item_id") or "").strip()
                        track_artist_name = str(first_artist.get("name") or "").strip()
                    else:
                        track_artist_id = str(getattr(first_artist, "item_id", "") or "").strip()
                        track_artist_name = str(getattr(first_artist, "name", "") or "").strip()
                if not track_artist_name:
                    track_artist_name = str(getattr(track, "artist_name", "") or "").strip()
            if track_artist_id and track_artist_name and not self._looks_like_uri_name(track_artist_name):
                decoded_id = self._adapter.decode_provider_id(track_artist_id)
                self._artist_name_cache[track_artist_id] = track_artist_name
                if decoded_id:
                    self._artist_name_cache[decoded_id] = track_artist_name
        for album in list(mapped.get("albums", [])):
            album_artist_id = ""
            album_artist_name = ""
            if isinstance(album, dict):
                album_artist_id = str(album.get("artist_id") or "").strip()
                album_artist_name = str(album.get("artist") or album.get("artist_name") or "").strip()
            else:
                artists = list(getattr(album, "artists", []) or [])
                if artists:
                    first_artist = artists[0]
                    if isinstance(first_artist, dict):
                        album_artist_id = str(first_artist.get("item_id") or "").strip()
                        album_artist_name = str(first_artist.get("name") or "").strip()
                    else:
                        album_artist_id = str(getattr(first_artist, "item_id", "") or "").strip()
                        album_artist_name = str(getattr(first_artist, "name", "") or "").strip()
                if not album_artist_name:
                    album_artist_name = str(getattr(album, "artist_name", "") or "").strip()
            if album_artist_id and album_artist_name and not self._looks_like_uri_name(album_artist_name):
                decoded_id = self._adapter.decode_provider_id(album_artist_id)
                self._artist_name_cache[album_artist_id] = album_artist_name
                if decoded_id:
                    self._artist_name_cache[decoded_id] = album_artist_name
        if SearchResultsModel is None:
            return mapped
        try:
            return SearchResultsModel(
                tracks=mapped.get("tracks", []),
                albums=mapped.get("albums", []),
                artists=mapped.get("artists", []),
            )
        except Exception:
            return mapped

    async def get_track(self, item_id: str) -> Any:
        rel = self._library_relative_path(item_id)
        if rel:
            name = rel.rsplit("/", 1)[-1]
            if "." in name:
                name = name.rsplit(".", 1)[0]
            parts = rel.split("/")
            artist_name = parts[0] if len(parts) >= 1 else ""
            album_name = parts[1] if len(parts) >= 2 else ""
            duration: int | None = None
            if len(parts) >= 3:
                album_path = "/".join(parts[:2])
                try:
                    entries = await self._provider.browse_library(album_path)
                except Exception:
                    entries = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if str(entry.get("entry_type") or "").lower() != "track":
                        continue
                    if str(entry.get("path") or "").strip("/") != rel.strip("/"):
                        continue
                    duration = self._coerce_positive_int(entry.get("duration"))
                    break
            payload = {
                "item_id": str(item_id),
                "name": name or "Library Track",
                "media_type": "track",
                "artist": artist_name,
                "artist_id": self._local_artist_id(artist_name) if artist_name else "",
                "album": album_name,
                "album_id": self._local_album_id("/".join(parts[:2])) if album_name else "",
                "duration": duration,
            }
            return self._adapter._to_ma_object("track", payload)
        try:
            return await self._adapter.mapped_track_for_ma(item_id)
        except Exception as exc:
            self._raise_unavailable(exc, "track lookup")
            return self._adapter._to_ma_object(
                "track",
                {
                    "item_id": str(item_id),
                    "name": str(item_id),
                    "media_type": "track",
                },
            )

    async def get_artist(self, prov_artist_id: str) -> Any:
        decoded_artist_id = self._adapter.decode_provider_id(prov_artist_id)
        explicit_artist_uri = "://artist/" in decoded_artist_id
        cached_name = (
            self._artist_name_cache.get(str(prov_artist_id).strip())
            or self._artist_name_cache.get(decoded_artist_id)
            or ""
        )
        if not cached_name and explicit_artist_uri:
            resolved = await self._resolve_artist_name_from_catalog(decoded_artist_id)
            if resolved:
                cached_name = resolved
                self._artist_name_cache[str(prov_artist_id).strip()] = resolved
                self._artist_name_cache[decoded_artist_id] = resolved
        if not cached_name and explicit_artist_uri:
            resolved = await self._resolve_artist_name_from_locked_tracks(decoded_artist_id)
            if resolved:
                cached_name = resolved
                self._artist_name_cache[str(prov_artist_id).strip()] = resolved
                self._artist_name_cache[decoded_artist_id] = resolved
        if decoded_artist_id.startswith("local-artist-") or decoded_artist_id.startswith("artist/"):
            name = decoded_artist_id
            if decoded_artist_id.startswith("local-artist-"):
                name = unquote_plus(decoded_artist_id.replace("local-artist-", "", 1))
            elif decoded_artist_id.startswith("artist/"):
                name = unquote_plus(decoded_artist_id.replace("artist/", "", 1))
            if name:
                self._artist_name_cache[str(prov_artist_id).strip()] = name
                self._artist_name_cache[decoded_artist_id] = name
            return self._adapter._to_ma_object(
                "artist",
                {
                    "item_id": str(prov_artist_id),
                    "name": (name or "Unknown Artist").strip(),
                    "media_type": "artist",
                },
            )
        try:
            payload = await self._provider.get_artist(decoded_artist_id)
            if isinstance(payload, dict):
                returned_id = self._adapter.decode_provider_id(payload.get("id") or "")
                requested_tail = decoded_artist_id.rsplit("/", 1)[-1]
                returned_tail = returned_id.rsplit("/", 1)[-1] if returned_id else ""
                # Guard against stale/older backend behavior returning unrelated artists.
                if returned_id and requested_tail and returned_tail and returned_tail != requested_tail:
                    raise ValueError("backend returned mismatched artist id")
                resolved_name = str(payload.get("name") or decoded_artist_id or prov_artist_id).strip()
                if self._looks_like_uri_name(resolved_name):
                    inferred_name = await self._resolve_artist_name_from_catalog(decoded_artist_id)
                    if inferred_name:
                        resolved_name = inferred_name
                if self._looks_like_uri_name(resolved_name):
                    inferred_from_albums = await self._resolve_artist_name_from_albums(
                        decoded_artist_id,
                        artist_name=(cached_name or None),
                    )
                    if inferred_from_albums:
                        resolved_name = inferred_from_albums
                if self._looks_like_uri_name(resolved_name):
                    inferred_from_tracks = await self._resolve_artist_name_from_locked_tracks(
                        decoded_artist_id,
                        artist_name=(cached_name or None),
                    )
                    if inferred_from_tracks:
                        resolved_name = inferred_from_tracks
                # Guard stale/ambiguous upstream ids: when we already have a reliable cached artist
                # name (from search context), do not override it with a conflicting backend name.
                if (
                    cached_name
                    and resolved_name
                    and not self._looks_like_uri_name(cached_name)
                    and self._is_artist_name_conflict(cached_name, resolved_name)
                ):
                    resolved_name = cached_name
                if self._looks_like_uri_name(resolved_name):
                    resolved_name = self._safe_display_name_from_id(
                        cached_name or decoded_artist_id or prov_artist_id,
                        "Unknown Artist",
                    )
                if resolved_name:
                    self._artist_name_cache[str(prov_artist_id).strip()] = resolved_name
                    self._artist_name_cache[decoded_artist_id] = resolved_name
                    if returned_id:
                        self._artist_name_cache[returned_id] = resolved_name
                return self._adapter._to_ma_object(
                    "artist",
                    {
                        "item_id": str(
                            self._adapter._encode_provider_id(payload.get("id") or decoded_artist_id or prov_artist_id)
                        ),
                        "name": resolved_name or str(decoded_artist_id or prov_artist_id),
                        "media_type": "artist",
                        "image_url": payload.get("artist_image_url") or payload.get("image_url"),
                    },
                )
        except Exception as exc:
            self._raise_unavailable(exc, "artist lookup")
        fallback_query = decoded_artist_id
        if explicit_artist_uri:
            fallback_query = (
                str(cached_name or "").strip()
                or self._id_tail(decoded_artist_id)
                or decoded_artist_id
            )
        try:
            payload = await self._adapter.mapped_search_for_ma(fallback_query)
        except Exception as exc:
            self._raise_unavailable(exc, "artist fallback search")
            payload = {"artists": []}
        for artist in payload.get("artists", []):
            artist_id = str(
                getattr(artist, "item_id", "")
                or (artist.get("item_id", "") if isinstance(artist, dict) else "")
            )
            artist_name = str(
                getattr(artist, "name", "")
                or (artist.get("name", "") if isinstance(artist, dict) else "")
            ).strip()
            if artist_id == str(prov_artist_id):
                if artist_name:
                    self._artist_name_cache[str(prov_artist_id).strip()] = artist_name
                    self._artist_name_cache[decoded_artist_id] = artist_name
                return artist
            decoded_match_id = self._adapter.decode_provider_id(artist_id)
            if explicit_artist_uri and decoded_match_id and decoded_match_id == decoded_artist_id:
                if artist_name:
                    self._artist_name_cache[str(prov_artist_id).strip()] = artist_name
                    self._artist_name_cache[decoded_artist_id] = artist_name
                return artist
        if explicit_artist_uri:
            if not cached_name or self._looks_like_uri_name(cached_name):
                inferred_from_albums = await self._resolve_artist_name_from_albums(decoded_artist_id)
                if inferred_from_albums:
                    cached_name = inferred_from_albums
                    self._artist_name_cache[str(prov_artist_id).strip()] = inferred_from_albums
                    self._artist_name_cache[decoded_artist_id] = inferred_from_albums
            fallback_name = str(cached_name or "").strip()
            if not fallback_name or self._looks_like_uri_name(fallback_name):
                tail_name = self._id_tail(decoded_artist_id)
                if tail_name:
                    fallback_name = tail_name
            if not fallback_name:
                fallback_name = self._safe_display_name_from_id(decoded_artist_id or prov_artist_id, "Unknown Artist")
            return self._adapter._to_ma_object(
                "artist",
                {
                    "item_id": str(self._adapter._encode_provider_id(decoded_artist_id or prov_artist_id)),
                    "name": fallback_name,
                    "media_type": "artist",
                },
            )
        if payload.get("artists"):
            return payload["artists"][0]
        return self._adapter._to_ma_object(
            "artist",
            {
                "item_id": str(prov_artist_id),
                "name": self._safe_display_name_from_id(decoded_artist_id or prov_artist_id, "Unknown Artist"),
                "media_type": "artist",
            },
        )

    async def get_artist_albums(self, prov_artist_id: str) -> list[Any]:
        decoded_artist_id = self._adapter.decode_provider_id(prov_artist_id)
        if decoded_artist_id.startswith("local-artist-") or decoded_artist_id.startswith("artist/"):
            artist_name = decoded_artist_id
            if decoded_artist_id.startswith("local-artist-"):
                artist_name = unquote_plus(decoded_artist_id.replace("local-artist-", "", 1))
            elif decoded_artist_id.startswith("artist/"):
                artist_name = unquote_plus(decoded_artist_id.replace("artist/", "", 1))
            artist_path = str(artist_name or "").strip()
            if not artist_path:
                return []
            try:
                entries = await self._provider.browse_library(artist_path)
            except Exception as exc:
                self._raise_unavailable(exc, "artist albums browse")
                return []
            mapped: list[Any] = []
            for entry in entries:
                if not isinstance(entry, dict) or str(entry.get("entry_type", "")).lower() != "album":
                    continue
                album_name = str(entry.get("name") or "").strip()
                album_path = str(entry.get("path") or "").strip()
                if not album_name or not album_path:
                    continue
                image_url = None
                try:
                    image_url = await self._provider.get_library_art_url(album_path, artist_level=False)
                except Exception:
                    image_url = None
                mapped.append(
                    self._adapter._to_ma_object(
                        "album",
                        {
                            "item_id": self._local_album_id(album_path),
                            "name": album_name,
                            "media_type": "album",
                            "artist": artist_name,
                            "artist_id": self._local_artist_id(artist_name),
                            "image_url": image_url,
                        },
                    )
                )
            return mapped
        cached_name = (
            self._artist_name_cache.get(str(prov_artist_id).strip())
            or self._artist_name_cache.get(decoded_artist_id)
            or ""
        )
        if not cached_name and "://artist/" in decoded_artist_id:
            try:
                artist_payload = await self._provider.get_artist(decoded_artist_id)
            except Exception:
                artist_payload = {}
            if isinstance(artist_payload, dict):
                resolved_name = str(artist_payload.get("name") or "").strip()
                if resolved_name:
                    cached_name = resolved_name
                    self._artist_name_cache[str(prov_artist_id).strip()] = resolved_name
                    self._artist_name_cache[decoded_artist_id] = resolved_name
        if not cached_name and "://artist/" in decoded_artist_id:
            resolved = await self._resolve_artist_name_from_catalog(decoded_artist_id)
            if resolved:
                cached_name = resolved
                self._artist_name_cache[str(prov_artist_id).strip()] = resolved
                self._artist_name_cache[decoded_artist_id] = resolved
        if not cached_name and "://artist/" in decoded_artist_id:
            resolved = await self._resolve_artist_name_from_albums(decoded_artist_id)
            if resolved:
                cached_name = resolved
                self._artist_name_cache[str(prov_artist_id).strip()] = resolved
                self._artist_name_cache[decoded_artist_id] = resolved
        if (not cached_name or self._looks_like_uri_name(cached_name)) and "://artist/" in decoded_artist_id:
            resolved = await self._resolve_artist_name_from_locked_tracks(
                decoded_artist_id,
                artist_name=(cached_name or None),
            )
            if resolved and not self._looks_like_uri_name(resolved):
                cached_name = resolved
                self._artist_name_cache[str(prov_artist_id).strip()] = resolved
                self._artist_name_cache[decoded_artist_id] = resolved
        if self._looks_like_uri_name(cached_name):
            cached_name = ""
        try:
            albums = await self._provider.get_artist_albums(
                decoded_artist_id,
                artist_name=(cached_name or None),
                limit=self._default_search_limit * 4,
            )
        except Exception as exc:
            self._raise_unavailable(exc, "artist albums")
            albums = []
        if albums:
            strict_matches: list[dict[str, Any]] = []
            name_matches: list[dict[str, Any]] = []
            for album in albums:
                if not isinstance(album, dict):
                    continue
                candidate_id = album.get("artist_id")
                candidate_name = album.get("artist_name")
                if self._is_artist_match(decoded_artist_id, "", candidate_id, None):
                    if cached_name and candidate_name and self._is_artist_name_conflict(cached_name, str(candidate_name)):
                        continue
                    strict_matches.append(album)
                elif cached_name and self._is_artist_match(decoded_artist_id, cached_name, None, candidate_name):
                    name_matches.append(album)
            if strict_matches:
                albums = strict_matches
            elif name_matches:
                albums = name_matches
            elif cached_name:
                # Keep sparse rows (missing artist_name) to avoid false-empty artist pages.
                # If all rows have conflicting explicit artist names, force fallback search path.
                unknown_name_rows = [
                    item for item in albums
                    if isinstance(item, dict) and not str(item.get("artist_name") or "").strip()
                ]
                albums = unknown_name_rows if unknown_name_rows else []
        if not albums and cached_name:
            try:
                fallback = await self._provider.search_items(
                    cached_name,
                    limit=self._default_search_limit * 4,
                    artist_id=decoded_artist_id,
                    artist_name=cached_name,
                )
                albums = [
                    item for item in (fallback.get("albums", []) if isinstance(fallback, dict) else [])
                    if isinstance(item, dict)
                ]
            except Exception as exc:
                self._raise_unavailable(exc, "artist albums fallback search")
                albums = []
        if not albums:
            query = cached_name or self._id_tail(decoded_artist_id)
            if query:
                try:
                    fallback = await self._provider.search_items(
                        query,
                        limit=self._default_search_limit * 4,
                        artist_id=decoded_artist_id,
                        artist_name=(cached_name or None),
                    )
                    albums = [
                        item for item in (fallback.get("albums", []) if isinstance(fallback, dict) else [])
                        if isinstance(item, dict)
                    ]
                except Exception as exc:
                    self._raise_unavailable(exc, "artist albums id fallback search")
                    albums = []
        if not albums:
            # Final ID-only recovery: query by id-tail, derive a stable artist name from track/album hits,
            # then retry with strict artist_id filter.
            id_tail_query = self._id_tail(decoded_artist_id)
            if id_tail_query:
                try:
                    probe = await self._provider.search_items(
                        id_tail_query,
                        limit=self._default_search_limit * 4,
                        artist_id=decoded_artist_id,
                    )
                except Exception as exc:
                    self._raise_unavailable(exc, "artist albums id probe")
                    probe = {}
                candidate_name = ""
                probe_albums = probe.get("albums", []) if isinstance(probe, dict) else []
                if isinstance(probe_albums, list):
                    for item in probe_albums:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("artist_name") or "").strip()
                        if name and not self._looks_like_uri_name(name):
                            candidate_name = name
                            break
                if not candidate_name:
                    probe_tracks = probe.get("tracks", []) if isinstance(probe, dict) else []
                    if isinstance(probe_tracks, list):
                        for item in probe_tracks:
                            if not isinstance(item, dict):
                                continue
                            name = str(item.get("artist_name") or "").strip()
                            if name and not self._looks_like_uri_name(name):
                                candidate_name = name
                                break
                if candidate_name:
                    self._artist_name_cache[str(prov_artist_id).strip()] = candidate_name
                    self._artist_name_cache[decoded_artist_id] = candidate_name
                    try:
                        fallback = await self._provider.search_items(
                            candidate_name,
                            limit=self._default_search_limit * 4,
                            artist_id=decoded_artist_id,
                            artist_name=candidate_name,
                        )
                        albums = [
                            item for item in (fallback.get("albums", []) if isinstance(fallback, dict) else [])
                            if isinstance(item, dict)
                        ]
                    except Exception as exc:
                        self._raise_unavailable(exc, "artist albums id-derived-name fallback")
                        albums = []
        if not albums and cached_name:
            try:
                generic = await self._provider.search_items(
                    cached_name,
                    limit=self._default_search_limit * 2,
                )
            except Exception as exc:
                self._raise_unavailable(exc, "artist albums generic search")
                generic = {}
            artist_candidates: list[str] = []
            for artist in (generic.get("artists", []) if isinstance(generic, dict) else []):
                if not isinstance(artist, dict):
                    continue
                name = str(artist.get("name") or "").strip().lower()
                if name != cached_name.strip().lower():
                    continue
                candidate_id = self._adapter.decode_provider_id(artist.get("id") or "")
                if candidate_id and candidate_id not in artist_candidates:
                    artist_candidates.append(candidate_id)
            best_albums: list[dict[str, Any]] = []
            for candidate_id in artist_candidates[:3]:
                try:
                    candidate_albums = await self._provider.get_artist_albums(
                        candidate_id,
                        artist_name=cached_name,
                        limit=self._default_search_limit * 4,
                    )
                except Exception as exc:
                    self._raise_unavailable(exc, "artist albums candidate lookup")
                    candidate_albums = []
                if len(candidate_albums) > len(best_albums):
                    best_albums = candidate_albums
                    self._artist_name_cache[candidate_id] = cached_name
            if best_albums:
                albums = best_albums
        mapped: list[Any] = []
        for album in albums:
            if not isinstance(album, dict):
                continue
            # Bug 1 fix: skip albums with empty name rather than emit a
            # phantom "Unknown Album" row in MA's artist page.
            album_name = (
                str(album.get("name") or album.get("album_name") or "").strip()
            )
            if not album_name:
                _LOGGER.debug(
                    "get_artist_albums(%s): skipping unnamed album row id=%r",
                    prov_artist_id, album.get("id") or album.get("album_id"),
                )
                continue
            mapped.append(
                self._adapter._to_ma_object(
                    "album",
                    {
                        "item_id": str(
                            self._adapter._encode_provider_id(album.get("id") or album.get("album_id") or "")
                        ),
                        "name": album_name,
                        "media_type": "album",
                        "artist": album.get("artist_name"),
                        "artist_id": self._adapter._encode_provider_id(
                            album.get("artist_id") or decoded_artist_id
                        ),
                        "year": album.get("year"),
                        "image_url": album.get("image_url"),
                    },
                )
            )
            album_artist_name = str(album.get("artist_name") or "").strip()
            album_artist_id = self._adapter.decode_provider_id(album.get("artist_id") or "")
            if album_artist_name and not self._looks_like_uri_name(album_artist_name):
                self._artist_name_cache[str(prov_artist_id).strip()] = album_artist_name
                self._artist_name_cache[decoded_artist_id] = album_artist_name
                if album_artist_id:
                    self._artist_name_cache[album_artist_id] = album_artist_name
        return mapped

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Any]:
        decoded_artist_id = self._adapter.decode_provider_id(prov_artist_id)
        cached_name = (
            self._artist_name_cache.get(str(prov_artist_id).strip())
            or self._artist_name_cache.get(decoded_artist_id)
            or ""
        ).strip()
        if not cached_name and "://artist/" in decoded_artist_id:
            try:
                artist_payload = await self._provider.get_artist(decoded_artist_id)
            except Exception as exc:
                self._raise_unavailable(exc, "artist toptracks artist lookup")
                artist_payload = {}
            if isinstance(artist_payload, dict):
                cached_name = str(artist_payload.get("name") or "").strip()
                if cached_name:
                    self._artist_name_cache[str(prov_artist_id).strip()] = cached_name
                    self._artist_name_cache[decoded_artist_id] = cached_name

        query = cached_name or self._id_tail(decoded_artist_id) or str(prov_artist_id)
        try:
            payload = await self._provider.search_items(
                query,
                limit=self._default_search_limit * 8,
                artist_id=(decoded_artist_id if "://artist/" in decoded_artist_id else None),
                artist_name=(cached_name or None),
            )
        except Exception as exc:
            self._raise_unavailable(exc, "artist toptracks")
            payload = {"tracks": []}
        tracks = list(payload.get("tracks", [])) if isinstance(payload, dict) else []

        filtered: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for track in tracks:
            if not isinstance(track, dict):
                continue
            if not self._is_artist_match(
                decoded_artist_id,
                cached_name,
                track.get("artist_id"),
                track.get("artist_name"),
            ):
                continue
            raw_id = str(track.get("id") or "").strip()
            decoded_track_id = self._adapter.decode_provider_id(raw_id)
            key = decoded_track_id or raw_id
            if not key or key in seen_ids:
                continue
            seen_ids.add(key)
            filtered.append(track)

        return [
            self._adapter._to_ma_object("track", self._adapter._map_track_item(item))
            for item in filtered[: max(10, self._default_search_limit * 2)]
        ]

    async def _resolve_library_track_to_upstream(self, prov_track_id: str) -> str:
        """Find a ``monochrome://track/...`` id that matches a library track.

        Used by :meth:`get_similar_tracks` so local FLAC tracks can reach the
        upstream TRACK_MIX recommender. We take the track's artist and title,
        ask the streamloader backend for a search, then prefer an exact
        case-insensitive ``artist`` + ``title`` hit. Returns an empty string
        if no confident match is found (caller falls back to artist toptracks).
        """
        try:
            track = await self.get_track(prov_track_id)
        except Exception:
            return ""

        if isinstance(track, dict):
            artist_name = str(track.get("artist") or track.get("artist_name") or "").strip()
            track_name = str(track.get("name") or "").strip()
        else:
            track_name = str(getattr(track, "name", "") or "").strip()
            artist_name = str(getattr(track, "artist_name", "") or "").strip()
            if not artist_name:
                candidates = list(getattr(track, "artists", []) or [])
                if candidates:
                    first = candidates[0]
                    artist_name = str(
                        first.get("name") if isinstance(first, dict) else getattr(first, "name", "")
                    ).strip()

        # Filename often carries a leading "NN - " or "NN. " that is not part of the title.
        if track_name:
            track_name = re.sub(r"^\s*\d{1,3}\s*[-.\u2013]\s*", "", track_name).strip()

        if not artist_name or not track_name:
            return ""

        query = f"{artist_name} {track_name}"
        try:
            search = await self._adapter.mapped_search(query)
        except Exception:
            return ""

        target_name = track_name.lower()
        target_artist = artist_name.lower()
        for candidate in search.tracks:
            if not isinstance(candidate, dict):
                continue
            cand_name = str(candidate.get("name") or "").strip().lower()
            cand_artist = str(candidate.get("artist") or "").strip().lower()
            cand_id = str(candidate.get("item_id") or "").strip()
            if not cand_id:
                continue
            if cand_name == target_name and cand_artist == target_artist:
                decoded = self._adapter.decode_provider_id(cand_id)
                if decoded.startswith("monochrome://track/"):
                    return decoded
        return ""

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Any]:
        """Return tracks similar to ``prov_track_id``.

        Enables MA's "Don't stop the music" feature. Three strategies, tried
        in order until one produces results:

        1. If the seed is a ``monochrome://track/...`` URI, ask streamloader's
           ``/api/track/similar`` for Tidal's TRACK_MIX directly.
        2. If the seed is a ``library:...`` URI (on-disk FLAC), resolve it to
           a monochrome track id by searching upstream for ``<artist> <title>``
           and picking a confident match, then ask TRACK_MIX for that id.
        3. Fall back to "more from the same artist" via
           :meth:`get_artist_toptracks` when no upstream match is available.
        """
        decoded_track_id = self._adapter.decode_provider_id(prov_track_id)
        desired = max(1, int(limit) or 25)

        # Determine the upstream (monochrome) track id we should query the mix for.
        upstream_track_id = ""
        if decoded_track_id and "://track/" in decoded_track_id:
            upstream_track_id = decoded_track_id
        elif decoded_track_id and (
            decoded_track_id.startswith("library:") or decoded_track_id.startswith("library/")
        ):
            upstream_track_id = await self._resolve_library_track_to_upstream(prov_track_id)

        # 1+2. If we have an upstream id, fetch TRACK_MIX-based similar tracks.
        if upstream_track_id:
            try:
                upstream_items = await self._provider.client.similar_tracks(
                    upstream_track_id, limit=desired
                )
            except Exception:
                upstream_items = []
            mapped_upstream: list[Any] = []
            for item in upstream_items:
                if not isinstance(item, dict):
                    continue
                try:
                    mapped_upstream.append(
                        self._adapter._to_ma_object("track", self._adapter._map_track_item(item))
                    )
                except Exception:
                    continue
            if mapped_upstream:
                return mapped_upstream[:desired]

        # 2. Fallback: resolve the track's artist and return more of their tracks.
        try:
            track = await self.get_track(prov_track_id)
        except Exception as exc:
            self._raise_unavailable(exc, "similar tracks: track lookup")
            return []

        artist_id = ""
        artist_name = ""
        if isinstance(track, dict):
            artist_id = str(track.get("artist_id") or "").strip()
            artist_name = str(track.get("artist") or track.get("artist_name") or "").strip()
        else:
            candidates = list(getattr(track, "artists", []) or [])
            if candidates:
                first = candidates[0]
                if isinstance(first, dict):
                    artist_id = str(first.get("item_id") or "").strip()
                    artist_name = str(first.get("name") or "").strip()
                else:
                    artist_id = str(getattr(first, "item_id", "") or "").strip()
                    artist_name = str(getattr(first, "name", "") or "").strip()
            if not artist_name:
                artist_name = str(getattr(track, "artist_name", "") or "").strip()

        if not artist_id and artist_name:
            # Best-effort local resolution if the track only exposes a name.
            artist_id = self._local_artist_id(artist_name)
        if not artist_id:
            return []

        try:
            tracks = await self.get_artist_toptracks(artist_id)
        except Exception as exc:
            self._raise_unavailable(exc, "similar tracks: artist toptracks")
            return []

        decoded_origin = self._adapter.decode_provider_id(prov_track_id)
        result: list[Any] = []
        for entry in tracks:
            entry_id = ""
            if isinstance(entry, dict):
                entry_id = str(entry.get("item_id") or "").strip()
            else:
                entry_id = str(getattr(entry, "item_id", "") or "").strip()
            if entry_id and (
                entry_id == str(prov_track_id)
                or self._adapter.decode_provider_id(entry_id) == decoded_origin
            ):
                continue
            result.append(entry)
            if len(result) >= max(1, int(limit) or 25):
                break
        return result

    async def get_album(self, prov_album_id: str) -> Any:
        decoded_album_id = self._adapter.decode_provider_id(prov_album_id)
        if decoded_album_id.startswith("local-album-") or decoded_album_id.startswith("album/"):
            name = decoded_album_id
            if decoded_album_id.startswith("local-album-"):
                name = unquote_plus(decoded_album_id.replace("local-album-", "", 1))
            elif decoded_album_id.startswith("album/"):
                name = unquote_plus(decoded_album_id.replace("album/", "", 1))
            album_path = str(name)
            artist_name = ""
            segments = [segment for segment in str(name).split("/") if segment]
            if len(segments) >= 2:
                artist_name = segments[-2]
                name = segments[-1]
            # Try rich filesystem-sourced metadata from streamloader; fall back to stub on any failure.
            if artist_name and name:
                try:
                    local_payload = await self._provider.client.local_album(artist_name, name)
                    album_data = local_payload.get("album") if isinstance(local_payload.get("album"), dict) else None
                    if isinstance(album_data, dict) and album_data.get("name"):
                        image_url = album_data.get("image_url")
                        if image_url and image_url.startswith("/"):
                            image_url = self._provider.client.build_absolute_url(image_url)
                        return self._adapter._to_ma_object(
                            "album",
                            {
                                "item_id": str(prov_album_id),
                                "name": album_data.get("name"),
                                "media_type": "album",
                                "artist": album_data.get("artist_name") or artist_name,
                                "artist_id": self._local_artist_id(artist_name),
                                "year": album_data.get("year"),
                                "image_url": image_url,
                            },
                        )
                except Exception as exc:
                    # Bug 1 fix: surface the failure so we can tell metadata-gap
                    # from backend-flake. Was a silent ``except Exception: pass``.
                    _LOGGER.warning(
                        "get_album(%s): /api/album/local failed for artist=%r album=%r: %s",
                        prov_album_id, artist_name, name, exc,
                    )
            # Bug 1 fix: before returning a stub with "Unknown Album", try one
            # last folder-name lookup via browse_library. Capped to a single
            # call (no retry) so the album-page hot path stays fast.
            if not str(name or "").strip():
                try:
                    entries = await self._provider.browse_library(album_path)
                except Exception as exc:
                    _LOGGER.warning(
                        "get_album(%s): browse_library fallback failed for path=%r: %s",
                        prov_album_id, album_path, exc,
                    )
                    entries = []
                for entry in entries or []:
                    if not isinstance(entry, dict):
                        continue
                    entry_name = str(entry.get("name") or "").strip()
                    entry_type = str(entry.get("entry_type") or "").lower()
                    if entry_name and entry_type in ("album", "folder"):
                        name = entry_name
                        break
            return self._adapter._to_ma_object(
                "album",
                {
                    "item_id": str(prov_album_id),
                    "name": (name or "Unknown Album").strip(),
                    "media_type": "album",
                    "artist": artist_name or None,
                    "artist_id": self._local_artist_id(artist_name) if artist_name else None,
                },
            )
        try:
            payload = await self._provider.client.album(str(decoded_album_id))
        except Exception as exc:
            self._raise_unavailable(exc, "album lookup")
            payload = {}
        if not isinstance(payload, dict):
            return self._adapter._to_ma_object(
                "album",
                {
                    "item_id": str(prov_album_id),
                    "name": str(prov_album_id),
                    "media_type": "album",
                },
            )
        # streamloader /api/album returns AlbumResponse: {"album": MediaItem, "tracks": [...]}.
        # Unwrap the envelope; tolerate a flat dict in case the contract changes.
        album_data = payload.get("album") if isinstance(payload.get("album"), dict) else payload
        # Bug 1 fix: if the backend response lacks a name, raise so MA's caller
        # surfaces this as "album not found" instead of rendering a phantom
        # "Unknown Album" placeholder. _raise_unavailable only re-raises for
        # transport-error needles, so a plain ValueError is used here.
        album_name = (
            str(album_data.get("name") or "").strip()
            if isinstance(album_data, dict)
            else ""
        )
        if not album_name:
            _LOGGER.warning(
                "get_album(%s): backend returned album without name (data=%r)",
                prov_album_id, album_data,
            )
            raise ValueError(
                f"backend returned album {prov_album_id} without name"
            )
        item = {
            "item_id": str(self._adapter._encode_provider_id(album_data.get("id", decoded_album_id))),
            "name": album_name,
            "media_type": "album",
            "artist": album_data.get("artist_name"),
            "year": album_data.get("year"),
            "image_url": album_data.get("image_url"),
        }
        return self._adapter._to_ma_object("album", item)

    async def get_album_tracks(self, album_id: str) -> list[Any]:
        decoded = self._adapter.decode_provider_id(album_id)
        if decoded.startswith("local-album-") or decoded.startswith("album/"):
            album_path = decoded
            if decoded.startswith("local-album-"):
                album_path = unquote_plus(decoded.replace("local-album-", "", 1))
            elif decoded.startswith("album/"):
                album_path = unquote_plus(decoded.replace("album/", "", 1))
            try:
                entries = await self._provider.browse_library(album_path)
            except Exception:
                return []
            segments = [segment for segment in str(album_path).split("/") if segment]
            artist_name = segments[-2] if len(segments) >= 2 else ""
            album_name = segments[-1] if segments else ""
            mapped: list[Any] = []
            for entry in entries:
                if not isinstance(entry, dict) or str(entry.get("entry_type", "")).lower() != "track":
                    continue
                track_name = self._strip_extension(str(entry.get("name") or "").strip())
                track_path = str(entry.get("path") or "").strip()
                if not track_name or not track_path:
                    continue
                mapped.append(
                    self._adapter._to_ma_object(
                        "track",
                        {
                            "item_id": f"library:{quote(track_path, safe='')}",
                            "name": track_name,
                            "media_type": "track",
                            "artist": artist_name,
                            "artist_id": self._local_artist_id(artist_name) if artist_name else None,
                            "album": album_name or None,
                            "album_id": self._local_album_id(album_path),
                            "duration": self._coerce_positive_int(entry.get("duration")),
                        },
                    )
                )
            return mapped
        try:
            return await self._adapter.mapped_album_tracks_for_ma(album_id)
        except Exception as exc:
            self._raise_unavailable(exc, "album tracks lookup")
            return []

    async def get_stream_details(self, item_id: str, *args: Any, **kwargs: Any) -> Any:
        duration: int | None = None
        sample_rate: int | None = None
        bit_depth: int | None = None
        channels: int | None = None
        bit_rate: int | None = None
        stream_quality: str | None = None
        rel = self._library_relative_path(item_id)
        if rel:
            rel_parts = [part for part in rel.split("/") if part]
            if len(rel_parts) >= 3:
                album_path = "/".join(rel_parts[:2])
                try:
                    entries = await self._provider.browse_library(album_path)
                except Exception:
                    entries = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if str(entry.get("entry_type") or "").lower() != "track":
                        continue
                    if str(entry.get("path") or "").strip("/") != rel.strip("/"):
                        continue
                    parsed_duration = self._coerce_positive_int(entry.get("duration"))
                    if parsed_duration is not None:
                        duration = parsed_duration
                    break
            # Keep startup playback stable: disable seek probing for local streams too.
            rel_can_seek = False
            payload = {
                "url": await self._provider.get_library_stream_url(rel),
                "can_seek": rel_can_seek,
                "mime_type": self._infer_stream_mime("audio/*", item_id=rel),
            }
        else:
            track_name: str | None = None
            track_artist: str | None = None
            try:
                track = await self.get_track(item_id)
                duration_val = getattr(track, "duration", None)
                parsed_duration = self._coerce_positive_int(duration_val)
                if parsed_duration is not None:
                    duration = parsed_duration
                if isinstance(track, dict):
                    track_name = str(track.get("name") or "").strip() or None
                    track_artist = str(track.get("artist") or track.get("artist_name") or "").strip() or None
                    quality_text = str(track.get("quality") or "").strip()
                    if quality_text:
                        stream_quality = quality_text
                else:
                    track_name = str(getattr(track, "name", "") or "").strip() or None
                    artists = getattr(track, "artists", None)
                    if isinstance(artists, list) and artists:
                        first_artist = artists[0]
                        if isinstance(first_artist, dict):
                            track_artist = str(first_artist.get("name") or "").strip() or None
                        else:
                            track_artist = str(getattr(first_artist, "name", "") or "").strip() or None
                    if not track_artist:
                        track_artist = str(getattr(track, "artist_str", "") or "").strip() or None
                    quality_text = str(getattr(track, "quality", "") or "").strip()
                    if quality_text:
                        stream_quality = quality_text
                    sr, bd, ch, br = self._audio_format_hints_from_track(track)
                    sample_rate = sample_rate or sr
                    bit_depth = bit_depth or bd
                    channels = channels or ch
                    bit_rate = bit_rate or br
            except Exception:
                duration = None
                track_name = None
                track_artist = None
            if duration is None:
                try:
                    mapped_track = await self._adapter.mapped_track(item_id)
                except Exception as exc:
                    self._raise_unavailable(exc, "stream details track lookup")
                    mapped_track = {}
                if isinstance(mapped_track, dict):
                    parsed_duration = self._coerce_positive_int(mapped_track.get("duration"))
                    if parsed_duration is not None:
                        duration = parsed_duration
                    if not track_name:
                        track_name = str(mapped_track.get("name") or "").strip() or None
                    if not track_artist:
                        track_artist = str(mapped_track.get("artist") or mapped_track.get("artist_name") or "").strip() or None
                    if not stream_quality:
                        quality_text = str(mapped_track.get("quality") or "").strip()
                        if quality_text:
                            stream_quality = quality_text
            stream_label = " - ".join(part for part in [track_artist, track_name] if part) or track_name
            stream_context = self._stream_context_label(*args, **kwargs)
            if stream_context:
                stream_label = f"{stream_label} [{stream_context}]" if stream_label else stream_context
            try:
                try:
                    payload = await self._adapter.stream_details(
                        item_id,
                        label=stream_label,
                        session_label=(stream_context or None),
                    )
                except TypeError:
                    # Backward compatibility for older adapter mocks that do not accept session_label.
                    payload = await self._adapter.stream_details(item_id, label=stream_label)
            except Exception as exc:
                self._raise_unavailable(exc, "stream details")
                raise
            payload["mime_type"] = self._infer_stream_mime(
                payload.get("mime_type"),
                quality=stream_quality,
                item_id=item_id,
            )
            await self._maybe_auto_queue_downloads(item_id)
        # Provider/local streams are more stable in MA when seek probing/range-jumps are disabled.
        can_seek = False
        resolved_content_type = self._content_type_from_mime(payload.get("mime_type"))
        payload["can_seek"] = can_seek
        try:
            from music_assistant_models.enums import ContentType, MediaType, StreamType
            from music_assistant_models.media_items import AudioFormat
            from music_assistant_models.streamdetails import StreamDetails

            return StreamDetails(
                provider=getattr(self, "domain", self._provider_domain),
                item_id=item_id,
                media_type=MediaType.TRACK,
                stream_type=StreamType.HTTP,
                audio_format=AudioFormat(
                    content_type=resolved_content_type or ContentType.UNKNOWN,
                    sample_rate=sample_rate or 44100,
                    bit_depth=bit_depth or 16,
                    channels=channels or 2,
                    bit_rate=bit_rate,
                ),
                path=payload.get("url"),
                duration=duration,
                can_seek=can_seek,
                allow_seek=can_seek,
            )
        except Exception:
            pass
        if StreamDetailsModel is None:
            return payload
        try:
            return StreamDetailsModel(
                provider=getattr(self, "domain", self._provider_domain),
                item_id=item_id,
                audio_format=kwargs.get("audio_format"),
                direct=payload.get("url"),
                can_seek=can_seek,
            )
        except Exception:
            return payload

    async def _maybe_auto_queue_downloads(self, item_id: str) -> None:
        if not (self._auto_download_track_on_play or self._auto_download_album_on_play):
            return
        decoded_track_id = self._adapter.decode_provider_id(item_id)
        if not decoded_track_id:
            return
        if self._auto_download_track_on_play and decoded_track_id not in self._seen_auto_download_tracks:
            self._seen_auto_download_tracks.add(decoded_track_id)
            try:
                await self.queue_track_download(decoded_track_id)
            except Exception:
                pass
        if not self._auto_download_album_on_play:
            return
        try:
            track_payload = await self._adapter.mapped_track(item_id)
        except Exception:
            track_payload = {}
        album_id_raw = str((track_payload or {}).get("album_id") or "").strip()
        if not album_id_raw:
            return
        decoded_album_id = self._adapter.decode_provider_id(album_id_raw)
        if not decoded_album_id or decoded_album_id in self._seen_auto_download_albums:
            return
        self._seen_auto_download_albums.add(decoded_album_id)
        try:
            await self.queue_album_download(
                decoded_album_id,
                missing_only=self._auto_download_album_missing_only,
            )
        except Exception:
            pass

    async def queue_track_download(self, track_id: str) -> dict[str, Any]:
        return await self._provider.queue_track_download(self._adapter.decode_provider_id(track_id))

    async def queue_album_download(self, album_id: str, *, missing_only: bool = False) -> dict[str, Any]:
        return await self._provider.queue_album_download(
            self._adapter.decode_provider_id(album_id),
            missing_only=missing_only,
        )

    async def _mark_playlog_user_initiated(
        self,
        *,
        media_type_value: str,
        item_id: str,
        provider_hint: str | None = None,
    ) -> None:
        db = getattr(getattr(self.mass, "music", None), "database", None)
        if db is None:
            return
        normalized_media_type = str(media_type_value or "").strip().lower()
        normalized_item_id = str(item_id or "").strip()
        if not normalized_media_type or not normalized_item_id:
            return
        decoded_item_id = self._adapter.decode_provider_id(normalized_item_id)
        encoded_decoded_item_id = self._adapter._encode_provider_id(decoded_item_id) if decoded_item_id else ""
        candidate_item_ids = {normalized_item_id}
        if decoded_item_id:
            candidate_item_ids.add(decoded_item_id)
        if encoded_decoded_item_id:
            candidate_item_ids.add(encoded_decoded_item_id)
            candidate_item_ids.add(f"streamloader/{encoded_decoded_item_id}")
            candidate_item_ids.add(f"streamloader://{encoded_decoded_item_id}")

        provider_candidates = {
            str(provider_hint or "").strip(),
            str(getattr(self, "instance_id", "") or "").strip(),
            str(getattr(self, "domain", "") or "").strip(),
            str(getattr(self, "_provider_domain", "") or "").strip(),
            "streamloader",
        }
        provider_candidates = {value for value in provider_candidates if value}
        id_tail = ""
        if decoded_item_id and "/" in decoded_item_id:
            id_tail = decoded_item_id.rsplit("/", 1)[-1].strip()
        like_tail = f"%/{id_tail}" if id_tail else ""
        # Playback callbacks can race DB row insert by a short margin.
        for attempt in range(5):
            updated_rows = 0
            for provider_value in provider_candidates:
                for candidate in candidate_item_ids:
                    result = await db.execute(
                        "UPDATE playlog SET user_initiated = 1 "
                        "WHERE item_id = :item_id AND provider = :provider AND media_type = :media_type",
                        {
                            "item_id": candidate,
                            "provider": provider_value,
                            "media_type": normalized_media_type,
                        },
                    )
                    try:
                        updated_rows += int(getattr(result, "rowcount", 0) or 0)
                    except Exception:
                        pass
                if like_tail:
                    result = await db.execute(
                        "UPDATE playlog SET user_initiated = 1 "
                        "WHERE item_id LIKE :item_id_like AND provider = :provider "
                        "AND media_type = :media_type",
                        {
                            "item_id_like": like_tail,
                            "provider": provider_value,
                            "media_type": normalized_media_type,
                        },
                    )
                    try:
                        updated_rows += int(getattr(result, "rowcount", 0) or 0)
                    except Exception:
                        pass
            if updated_rows <= 0:
                # MA/provider variants can store a slightly different provider value.
                # As a safety net, promote matching item/media rows regardless of provider.
                for candidate in candidate_item_ids:
                    await db.execute(
                        "UPDATE playlog SET user_initiated = 1 "
                        "WHERE item_id = :item_id AND media_type = :media_type",
                        {
                            "item_id": candidate,
                            "media_type": normalized_media_type,
                        },
                    )
                if like_tail:
                    await db.execute(
                        "UPDATE playlog SET user_initiated = 1 "
                        "WHERE item_id LIKE :item_id_like AND media_type = :media_type",
                        {
                            "item_id_like": like_tail,
                            "media_type": normalized_media_type,
                        },
                    )
            if attempt < 4:
                await asyncio.sleep(0.2)
        if hasattr(db, "commit"):
            await db.commit()

    async def on_streamed(self, streamdetails: Any) -> None:
        """Mark Streamloader playlog rows as user-initiated when stream starts."""
        try:
            item_id = str(getattr(streamdetails, "item_id", "") or "").strip()
            provider_hint = str(getattr(streamdetails, "provider", "") or "").strip()
            media_type = getattr(streamdetails, "media_type", "track")
            media_type_value = str(getattr(media_type, "value", media_type) or "track").strip().lower()
            if not item_id:
                return
            await self._mark_playlog_user_initiated(
                media_type_value=media_type_value,
                item_id=item_id,
                provider_hint=provider_hint or None,
            )
        except Exception:
            return

    async def on_played(
        self,
        media_type: Any,
        prov_item_id: str,
        fully_played: bool = True,
        position: int | None = None,
        media_item: Any = None,
        is_playing: bool = False,
    ) -> None:
        """Align playlog rows with MA Home's 'user initiated' recently played filter.

        MA queue playback reports often mark `user_initiated=False`, while the Home Recently Played
        section queries only `user_initiated=1`. We flip the playlog bit for this provider row
        after queue playback reporting so completed Streamloader plays can show up there.
        """
        _ = fully_played, position, is_playing
        try:
            item_id = str(getattr(media_item, "item_id", "") or "").strip()
            if not item_id:
                item_id = str(prov_item_id or "").strip()
            provider = str(getattr(media_item, "provider", "") or "").strip()
            if not provider:
                provider = str(getattr(self, "domain", self._provider_domain))
            media_type_value = str(getattr(media_type, "value", media_type) or "").strip().lower()
            if not item_id or not provider or not media_type_value:
                return
            await self._mark_playlog_user_initiated(
                media_type_value=media_type_value,
                item_id=item_id,
                provider_hint=provider,
            )
        except Exception:
            return
