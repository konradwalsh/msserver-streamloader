"""Streamloader Music Assistant provider scaffold.

This is a starter skeleton. Adapt imports/class base types to the MA SDK version
in use when integrating.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

import httpx


@dataclass
class StreamloaderConfig:
    base_url: str
    api_key: str | None = None
    timeout_seconds: float = 25.0
    request_retries: int = 4


class StreamloaderClient:
    def __init__(self, config: StreamloaderConfig) -> None:
        self._base_url = config.base_url.rstrip("/")
        self._api_key = config.api_key
        self._timeout_seconds = max(2.0, float(config.timeout_seconds))
        self._request_retries = max(1, min(8, int(config.request_retries)))
        self._fallback_base_url = self._derive_fallback_base_url(self._base_url)

    @property
    def base_url(self) -> str:
        """Public accessor for the configured streamloader base URL."""
        return self._base_url

    def build_absolute_url(self, relative_path: str) -> str:
        """Turn a relative streamloader API path (e.g. ``/api/library/art/...``)
        into an absolute URL usable from a client such as Music Assistant.
        """
        path = str(relative_path or "").strip()
        if not path:
            return self._base_url
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}{path if path.startswith('/') else '/' + path}"

    @property
    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _derive_fallback_base_url(base_url: str) -> str | None:
        try:
            parts = urlsplit(base_url)
        except Exception:
            return None
        hostname = (parts.hostname or "").strip().lower()
        if hostname not in {"streamloader", "localhost", "127.0.0.1", "0.0.0.0"}:
            return None
        netloc = parts.netloc
        if ":" in netloc:
            port = netloc.rsplit(":", 1)[-1]
            netloc = f"host.docker.internal:{port}"
        else:
            netloc = "host.docker.internal"
        return urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query, parts.fragment)).rstrip("/")

    @staticmethod
    def _is_name_resolution_error(exc: Exception) -> bool:
        text = str(exc).lower()
        needles = ("name or service not known", "temporary failure in name resolution", "nodename nor servname")
        return any(needle in text for needle in needles)

    @staticmethod
    def _is_retryable_transport_error(exc: Exception) -> bool:
        return isinstance(
            exc,
            (
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
                httpx.NetworkError,
            ),
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async def _do_request(base_url: str) -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=self._timeout_seconds, headers=self._headers) as client:
                response = await client.request(
                    method.upper(),
                    f"{base_url}{path}",
                    params=params,
                    json=json_body,
                )
                response.raise_for_status()
                return response.json()

        base_candidates: list[str] = [self._base_url]
        if self._fallback_base_url and self._fallback_base_url != self._base_url:
            base_candidates.append(self._fallback_base_url)

        last_exc: Exception | None = None
        for base in base_candidates:
            for attempt in range(1, self._request_retries + 1):
                try:
                    return await _do_request(base)
                except Exception as exc:
                    last_exc = exc
                    should_try_fallback = (
                        base == self._base_url
                        and self._fallback_base_url is not None
                        and (
                            self._is_name_resolution_error(exc) or self._is_retryable_transport_error(exc)
                        )
                    )
                    if should_try_fallback:
                        break
                    is_last_attempt = attempt >= self._request_retries
                    if not is_last_attempt and self._is_retryable_transport_error(exc):
                        await asyncio.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
                        continue
                    if base != self._base_url:
                        break
                    raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Streamloader request failed without an explicit exception")

    async def health(self) -> dict[str, Any]:
        return await self._request_json("GET", "/api/health")

    async def provider_info(self) -> dict[str, Any]:
        return await self._request_json("GET", "/api/provider")

    async def provider_compat(self) -> dict[str, Any]:
        return await self._request_json("GET", "/api/provider/compat")

    async def settings(self) -> dict[str, Any]:
        return await self._request_json("GET", "/api/settings")

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/api/settings", json_body=payload)

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        artist_id: str | None = None,
        artist_name: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query}
        if limit is not None:
            params["limit"] = max(1, min(int(limit), 500))
        if offset is not None:
            params["offset"] = max(0, int(offset))
        if artist_id:
            params["artist_id"] = artist_id
        if artist_name:
            params["artist_name"] = artist_name
        return await self._request_json("GET", "/api/search", params=params)

    async def album(self, album_id: str) -> dict[str, Any]:
        return await self._request_json("GET", "/api/album", params={"album_id": album_id})

    async def local_album(self, artist: str, album: str) -> dict[str, Any]:
        return await self._request_json("GET", "/api/album/local", params={"artist": artist, "album": album})

    async def track(self, track_id: str) -> dict[str, Any]:
        return await self._request_json("GET", "/api/track", params={"track_id": track_id})

    async def similar_tracks(self, track_id: str, limit: int = 25) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            "/api/track/similar",
            params={"track_id": track_id, "limit": max(1, min(int(limit or 25), 100))},
        )
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def artist(self, artist_id: str, artist_name: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"artist_id": artist_id}
        if artist_name:
            params["artist_name"] = artist_name
        return await self._request_json("GET", "/api/artist", params=params)

    async def artist_albums(
        self,
        artist_id: str,
        *,
        artist_name: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"artist_id": artist_id, "limit": max(1, min(int(limit), 500))}
        if artist_name:
            params["artist_name"] = artist_name
        payload = await self._request_json("GET", "/api/artist/albums", params=params)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def enqueue_track_download(self, track_id: str) -> dict[str, Any]:
        return await self._request_json("POST", "/api/jobs/track", json_body={"track_id": track_id})

    async def enqueue_album_download(self, album_id: str, *, missing_only: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"album_id": album_id}
        if missing_only:
            payload["missing_only"] = True
        return await self._request_json("POST", "/api/jobs/album", json_body=payload)

    async def library_browse(self, path: str = "") -> dict[str, Any]:
        params = {"path": path} if path else None
        return await self._request_json("GET", "/api/library/browse", params=params)

    def _stream_base_url(self) -> str:
        # In containerized MA setups, localhost/streamloader hostnames can be invalid.
        return self._fallback_base_url or self._base_url

    def stream_url(
        self,
        track_id: str,
        label: str | None = None,
        *,
        session_label: str | None = None,
    ) -> str:
        query_payload: dict[str, str] = {"track_id": track_id}
        if label:
            query_payload["label"] = str(label)
        if session_label:
            query_payload["session"] = str(session_label)
        query = urlencode(query_payload)
        if self._api_key:
            query = f"{query}&api_key={quote(self._api_key, safe='')}"
        return f"{self._stream_base_url()}/api/stream?{query}"

    def library_stream_url(self, relative_path: str) -> str:
        encoded_path = quote(relative_path.strip("/"), safe="/")
        url = f"{self._stream_base_url()}/api/library/stream/{encoded_path}"
        if self._api_key:
            # Stream URLs are handed to ffmpeg / MA's stream pipeline directly,
            # which won't carry our Authorization header. Backend's
            # _extract_supplied_key accepts ?api_key= as a fallback.
            url = f"{url}?api_key={quote(self._api_key, safe='')}"
        return url


class StreamloaderMusicProvider:
    """Lightweight provider bridge.

    Replace/extend this class with MA-specific base class inheritance and
    media model mapping inside the Music Assistant runtime.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 25.0,
        request_retries: int = 4,
    ) -> None:
        self.client = StreamloaderClient(
            StreamloaderConfig(
                base_url=base_url,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
                request_retries=request_retries,
            )
        )
        self.instance_id = "streamloader"
        self.domain = "streamloader"

    async def search_items(
        self,
        query: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        artist_id: str | None = None,
        artist_name: str | None = None,
    ) -> dict[str, Any]:
        return await self.client.search(
            query,
            limit=limit,
            offset=offset,
            artist_id=artist_id,
            artist_name=artist_name,
        )

    async def get_album_tracks(self, album_id: str) -> list[dict[str, Any]]:
        payload = await self.client.album(album_id)
        return payload.get("tracks", []) if isinstance(payload, dict) else []

    async def get_track(self, track_id: str) -> dict[str, Any]:
        return await self.client.track(track_id)

    async def get_artist(self, artist_id: str, artist_name: str | None = None) -> dict[str, Any]:
        return await self.client.artist(artist_id, artist_name=artist_name)

    async def get_artist_albums(
        self,
        artist_id: str,
        *,
        artist_name: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return await self.client.artist_albums(artist_id, artist_name=artist_name, limit=limit)

    async def get_stream_url(
        self,
        track_id: str,
        label: str | None = None,
        *,
        session_label: str | None = None,
    ) -> str:
        return self.client.stream_url(track_id, label=label, session_label=session_label)

    async def queue_track_download(self, track_id: str) -> dict[str, Any]:
        return await self.client.enqueue_track_download(track_id)

    async def queue_album_download(self, album_id: str, *, missing_only: bool = False) -> dict[str, Any]:
        return await self.client.enqueue_album_download(album_id, missing_only=missing_only)

    async def browse_library(self, path: str = "") -> list[dict[str, Any]]:
        payload = await self.client.library_browse(path)
        if isinstance(payload, dict):
            entries = payload.get("entries", [])
            if isinstance(entries, list):
                return entries
        return []

    async def get_library_stream_url(self, relative_path: str) -> str:
        return self.client.library_stream_url(relative_path)

    async def get_library_art_url(self, relative_path: str, *, artist_level: bool = False) -> str:
        encoded_path = quote(relative_path.strip("/"), safe="/")
        filename = "artist.jpg" if artist_level else "cover.jpg"
        base = self.client._stream_base_url()
        return f"{base}/api/library/art/{encoded_path}/{filename}"


@dataclass
class MappedSearchResult:
    tracks: list[dict[str, Any]]
    albums: list[dict[str, Any]]
    artists: list[dict[str, Any]]


class StreamloaderMAAdapter:
    """Adapter that translates streamloader payloads into MA-friendly dicts.

    These helpers intentionally return plain dicts to avoid pinning the scaffold
    to one MA SDK version. Inside MA runtime, map these dicts to MA model types.
    """

    def __init__(self, provider: StreamloaderMusicProvider) -> None:
        self.provider = provider
        self._ma_models = self._load_ma_models()

    @staticmethod
    def _coerce_positive_int(value: Any) -> int:
        """Parse API numeric fields that may arrive as int/float/number-like strings."""
        if value is None:
            return 0
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            parsed = int(float(value))
            return parsed if parsed > 0 else 0
        text = str(value).strip()
        if not text:
            return 0
        try:
            parsed = int(float(text))
        except Exception:
            return 0
        return parsed if parsed > 0 else 0

    @staticmethod
    def _artist_sort_name(name: str) -> str:
        text = str(name or "").strip()
        if text.lower().startswith("the "):
            trimmed = text[4:].strip()
            if trimmed:
                return trimmed
        return text

    @staticmethod
    def _encode_provider_id(raw_id: Any) -> str:
        text = str(raw_id or "").strip()
        if not text:
            return ""
        return quote(text, safe="")

    @staticmethod
    def decode_provider_id(encoded_id: Any) -> str:
        text = str(encoded_id or "").strip()
        if not text:
            return ""
        # Some MA call paths can deliver IDs that have been URL-encoded more than once.
        # Decode repeatedly until stable (bounded to avoid pathological loops).
        decoded = text
        for _ in range(6):
            try:
                next_decoded = unquote(decoded)
            except Exception:
                break
            if next_decoded == decoded:
                break
            decoded = next_decoded
        if decoded.startswith("streamloader://"):
            remainder = decoded.replace("streamloader://", "", 1)
            if "/" in remainder:
                media_type, tail = remainder.split("/", 1)
                if media_type in {"artist", "album", "track"} and tail:
                    decoded = tail
        if "/" in decoded and not decoded.startswith(("http://", "https://")):
            prefix, tail = decoded.split("/", 1)
            if prefix in {"streamloader", "library"} and tail.startswith(
                ("monochrome://", "local-artist-", "local-album-", "artist/", "album/", "track/", "library:")
            ):
                decoded = tail
        return decoded

    @staticmethod
    def _load_ma_models() -> dict[str, Any]:
        try:
            from music_assistant_models.enums import ImageType, MediaType
            from music_assistant_models.media_items import (
                Album,
                Artist,
                ItemMapping,
                MediaItemImage,
                MediaItemMetadata,
                ProviderMapping,
                Track,
            )
        except Exception:
            return {}
        return {
            "track": Track,
            "album": Album,
            "artist": Artist,
            "item_mapping": ItemMapping,
            "provider_mapping": ProviderMapping,
            "media_type": MediaType,
            "image_type": ImageType,
            "media_item_image": MediaItemImage,
            "media_item_metadata": MediaItemMetadata,
        }

    def _to_ma_object(self, media_type: str, payload: dict[str, Any]) -> Any:
        if not self._ma_models:
            return payload
        provider_instance = str(getattr(self.provider, "instance_id", "streamloader"))
        provider_domain = str(getattr(self.provider, "domain", "streamloader"))
        media_enum = self._ma_models.get("media_type")
        ProviderMapping = self._ma_models.get("provider_mapping")
        ItemMapping = self._ma_models.get("item_mapping")
        model = self._ma_models.get(media_type)
        ImageType = self._ma_models.get("image_type")
        MediaItemImage = self._ma_models.get("media_item_image")
        MediaItemMetadata = self._ma_models.get("media_item_metadata")
        if not model or not ProviderMapping or not ItemMapping or media_enum is None:
            return payload
        item_id = str(payload.get("item_id", "") or "")
        item_name = str(payload.get("name", "Unknown"))
        provider_mappings = {
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
            )
        }
        image_path = str(payload.get("image_url") or "").strip()
        metadata = None
        if image_path and ImageType and MediaItemImage and MediaItemMetadata:
            try:
                metadata = MediaItemMetadata(
                    images=[
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=image_path,
                            provider=provider_instance,
                            # Let MA proxy image retrieval from provider URLs
                            # so container-only hostnames still render artwork.
                            remotely_accessible=False,
                        )
                    ]
                )
            except Exception:
                metadata = None
        try:
            if media_type == "artist":
                kwargs: dict[str, Any] = {
                    "item_id": item_id,
                    "provider": provider_instance,
                    "name": item_name,
                    "provider_mappings": provider_mappings,
                    "sort_name": self._artist_sort_name(item_name),
                }
                if metadata is not None:
                    kwargs["metadata"] = metadata
                try:
                    return model(**kwargs)
                except TypeError:
                    # MA model compatibility: older builds may not accept sort_name.
                    kwargs.pop("sort_name", None)
                    return model(**kwargs)
            if media_type == "album":
                artist_name = str(payload.get("artist") or "").strip()
                artists = []
                if artist_name:
                    # Match the filesystem scanner's URI format so MA does not create a
                    # ghost artist row for the album. Previously emitted `artist/<lower>`,
                    # which never matched `local-artist-<ExactCase>` from get_library_artists.
                    artists = [
                        ItemMapping(
                            item_id=self._encode_provider_id(f"local-artist-{artist_name}"),
                            provider=provider_instance,
                            name=artist_name,
                            media_type=media_enum.ARTIST,
                        )
                    ]
                year_val = payload.get("year")
                year = int(year_val) if isinstance(year_val, int) or str(year_val).isdigit() else None
                kwargs: dict[str, Any] = {
                    "item_id": item_id,
                    "provider": provider_instance,
                    "name": item_name,
                    "provider_mappings": provider_mappings,
                    "artists": artists,
                    "year": year,
                }
                if metadata is not None:
                    kwargs["metadata"] = metadata
                return model(
                    **kwargs,
                )
            if media_type == "track":
                artist_name = str(payload.get("artist") or "").strip()
                album_name = str(payload.get("album") or "").strip()
                artist_id = str(payload.get("artist_id") or "").strip()
                album_id = str(payload.get("album_id") or "").strip()
                raw_item_id = str(payload.get("item_id") or "").strip()
                if (not artist_name or not album_name) and raw_item_id.startswith(("library://", "library:")):
                    rel = raw_item_id.replace("library://", "", 1)
                    if rel.startswith("library:"):
                        rel = rel.replace("library:", "", 1)
                    rel = unquote(rel).strip("/")
                    parts = [part for part in rel.split("/") if part]
                    if parts:
                        if not artist_name and len(parts) >= 1:
                            artist_name = parts[0]
                        if not album_name and len(parts) >= 2:
                            album_name = parts[1]
                        if not artist_id and artist_name:
                            artist_id = self._encode_provider_id(f"local-artist-{artist_name}")
                        if not album_id and album_name:
                            album_id = self._encode_provider_id(f"local-album-{album_name}")
                artists = []
                album = None
                if artist_name:
                    # Use the same local-artist-<ExactCase> URI the filesystem scanner emits
                    # so MA links tracks to the already-known library artist row.
                    artists = [
                        ItemMapping(
                            item_id=artist_id or self._encode_provider_id(f"local-artist-{artist_name}"),
                            provider=provider_instance,
                            name=artist_name,
                            media_type=media_enum.ARTIST,
                        )
                    ]
                if not artists:
                    # Prevent MA queue validation errors for tracks without artist context.
                    artists = [
                        ItemMapping(
                            item_id=self._encode_provider_id("local-artist-unknown"),
                            provider=provider_instance,
                            name="Unknown Artist",
                            media_type=media_enum.ARTIST,
                        )
                    ]
                if album_name:
                    album = ItemMapping(
                        item_id=album_id or self._encode_provider_id(f"local-album-{album_name}"),
                        provider=provider_instance,
                        name=album_name,
                        media_type=media_enum.ALBUM,
                    )
                duration = self._coerce_positive_int(payload.get("duration"))
                track_no = self._coerce_positive_int(payload.get("track_number"))
                kwargs = {
                    "item_id": item_id,
                    "provider": provider_instance,
                    "name": item_name,
                    "provider_mappings": provider_mappings,
                    "artists": artists,
                    "album": album,
                    "duration": duration,
                    "track_number": track_no,
                }
                if metadata is not None:
                    kwargs["metadata"] = metadata
                return model(**kwargs)
            return payload
        except Exception:
            return payload

    @staticmethod
    def _map_track_item(item: dict[str, Any]) -> dict[str, Any]:
        raw_track_id = item.get("id", "")
        raw_artist_id = item.get("artist_id")
        raw_album_id = item.get("album_id")
        return {
            "item_id": StreamloaderMAAdapter._encode_provider_id(raw_track_id),
            "name": item.get("name") or "Unknown Track",
            "media_type": "track",
            "artist": item.get("artist_name"),
            "artist_id": StreamloaderMAAdapter._encode_provider_id(raw_artist_id),
            "album": item.get("album_name"),
            "album_id": StreamloaderMAAdapter._encode_provider_id(raw_album_id),
            "image_url": item.get("image_url"),
            "duration": item.get("duration"),
            "track_number": item.get("track_number"),
            "year": item.get("year"),
        }

    @staticmethod
    def _map_album_item(item: dict[str, Any]) -> dict[str, Any]:
        raw_album_id = item.get("id", "")
        return {
            "item_id": StreamloaderMAAdapter._encode_provider_id(raw_album_id),
            "name": item.get("name") or "Unknown Album",
            "media_type": "album",
            "artist": item.get("artist_name"),
            "year": item.get("year"),
            "image_url": item.get("image_url"),
        }

    @staticmethod
    def _map_artist_item(item: dict[str, Any]) -> dict[str, Any]:
        raw_artist_id = item.get("id", "")
        return {
            "item_id": StreamloaderMAAdapter._encode_provider_id(raw_artist_id),
            "name": item.get("name") or "Unknown Artist",
            "media_type": "artist",
            "image_url": item.get("artist_image_url") or item.get("image_url"),
        }

    async def mapped_search(self, query: str) -> MappedSearchResult:
        payload = await self.provider.search_items(query)
        tracks = [self._map_track_item(item) for item in payload.get("tracks", [])]
        albums = [self._map_album_item(item) for item in payload.get("albums", [])]
        artists = [self._map_artist_item(item) for item in payload.get("artists", [])]
        return MappedSearchResult(tracks=tracks, albums=albums, artists=artists)

    async def mapped_search_for_ma(self, query: str) -> dict[str, list[Any]]:
        mapped = await self.mapped_search(query)
        return {
            "tracks": [self._to_ma_object("track", item) for item in mapped.tracks],
            "albums": [self._to_ma_object("album", item) for item in mapped.albums],
            "artists": [self._to_ma_object("artist", item) for item in mapped.artists],
        }

    async def mapped_album_tracks(self, album_id: str) -> list[dict[str, Any]]:
        tracks = await self.provider.get_album_tracks(self.decode_provider_id(album_id))
        return [self._map_track_item(item) for item in tracks]

    async def mapped_album_tracks_for_ma(self, album_id: str) -> list[Any]:
        tracks = await self.mapped_album_tracks(album_id)
        return [self._to_ma_object("track", track) for track in tracks]

    async def mapped_track(self, track_id: str) -> dict[str, Any]:
        track = await self.provider.get_track(self.decode_provider_id(track_id))
        return self._map_track_item(track)

    async def mapped_track_for_ma(self, track_id: str) -> Any:
        track = await self.mapped_track(track_id)
        return self._to_ma_object("track", track)

    async def stream_details(
        self,
        track_id: str,
        label: str | None = None,
        *,
        session_label: str | None = None,
        ma_session_id: str | None = None,
        ma_player_name: str | None = None,
    ) -> dict[str, Any]:
        decoded = self.decode_provider_id(track_id)
        try:
            url = await self.provider.get_stream_url(decoded, label=label, session_label=session_label)
        except TypeError:
            # Backward compatibility for provider stubs/mocks that only accept (track_id, label).
            url = await self.provider.get_stream_url(decoded, label=label)
        headers: dict[str, str] = {}
        if ma_session_id:
            headers["X-MA-Session-Id"] = str(ma_session_id)
        if ma_player_name:
            headers["X-MA-Player-Name"] = str(ma_player_name)
        return {
            "url": url,
            "can_seek": True,
            "mime_type": "audio/*",
            "headers": headers,
        }
