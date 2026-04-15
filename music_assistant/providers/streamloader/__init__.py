"""Streamloader MA plugin scaffold package.

This module mirrors the structure expected by Music Assistant provider plugins.
It is intentionally light so the same files can be edited outside MA without
hard runtime dependency on MA packages.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .runtime_template import StreamloaderMAProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.mass import MusicAssistant


async def setup(mass: Any, manifest: Any, config: Any) -> StreamloaderMAProvider:
    """Initialize provider instance (MA entrypoint)."""
    return StreamloaderMAProvider(mass=mass, manifest=manifest, config=config)


async def get_config_entries(
    mass: Any,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Return config entries shown in MA settings UI.

    Kept intentionally minimal: connection fields + a few behaviour toggles.
    Server-side concerns (library root, folder/filename templates, download
    tuning) live in streamloader's own UI — streamloader is the source of truth.
    """
    try:
        from music_assistant_models.config_entries import (
            SECURE_STRING_SUBSTITUTE,
            ConfigEntry,
            ConfigEntryType,
        )
        from music_assistant_models.errors import SetupFailedError
    except ImportError as exc:
        raise RuntimeError(
            "Music Assistant config entry models are unavailable. "
            "Use this module inside MA provider runtime."
        ) from exc

    values = dict(values or {})
    existing_values: dict[str, Any] = {}
    if instance_id and hasattr(mass, "config"):
        try:
            existing_cfg = await mass.config.get_provider_config(instance_id)
            raw_values = getattr(existing_cfg, "values", None)
            if isinstance(raw_values, dict):
                existing_values = dict(raw_values)
        except Exception:
            existing_values = {}

    def _extract_raw(value: Any) -> Any:
        if hasattr(value, "value"):
            try:
                return value.value
            except Exception:
                return value
        return value

    def _value_for(key: str, fallback: Any = None) -> Any:
        if key in values:
            raw = _extract_raw(values.get(key))
            if raw is not None:
                return raw
        if key in existing_values:
            raw = _extract_raw(existing_values.get(key))
            if raw is not None:
                return raw
        return fallback

    def _as_int(value: Any, fallback: int) -> int:
        try:
            return int(value)
        except Exception:
            return fallback

    def _as_bool(value: Any, fallback: bool) -> bool:
        if value is None:
            return fallback
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return fallback

    base_url_value = str(_value_for("base_url") or "http://streamloader:8000")

    existing_api_key = existing_values.get("api_key")
    existing_api_key_text = str(existing_api_key) if existing_api_key else ""
    incoming_api_key = _extract_raw(values.get("api_key"))
    if incoming_api_key in (None, "", SECURE_STRING_SUBSTITUTE):
        # MA expects a plain placeholder token in config forms for secure fields.
        api_key_value = SECURE_STRING_SUBSTITUTE if existing_api_key_text else ""
        api_key_for_test = existing_api_key_text
    else:
        api_key_value = str(incoming_api_key)
        api_key_for_test = api_key_value

    success_label: str | None = None
    clear_label: str | None = None
    if action == "clear_api_key":
        api_key_value = ""
        api_key_for_test = ""
        clear_label = "API key cleared. Press Save to persist."

    if action == "test_connection":
        from .provider import StreamloaderClient, StreamloaderConfig

        try:
            client = StreamloaderClient(
                StreamloaderConfig(base_url=base_url_value, api_key=api_key_for_test or None)
            )
            health = await client.health()
            status = str(health.get("status", "")).lower()
            if status not in {"ok", "degraded"}:
                raise SetupFailedError(
                    f"Streamloader health returned status '{status or 'unknown'}'."
                )
            success_label = (
                f"Connection OK: {base_url_value} "
                f"(status: {status}, provider_reachable: "
                f"{bool(health.get('provider_reachable', False))})."
            )
        except SetupFailedError:
            raise
        except Exception as exc:
            raise SetupFailedError(f"Streamloader connection failed: {exc}") from exc

    entries: list[Any] = []
    if success_label:
        entries.append(
            ConfigEntry(
                key="test_connection_result",
                type=ConfigEntryType.LABEL,
                label=success_label,
            )
        )
    if clear_label:
        entries.append(
            ConfigEntry(
                key="clear_api_key_result",
                type=ConfigEntryType.LABEL,
                label=clear_label,
            )
        )

    entries.extend(
        [
            ConfigEntry(
                key="section_connection",
                type=ConfigEntryType.DIVIDER,
                label="Connection",
                required=False,
                category="generic",
            ),
            ConfigEntry(
                key="base_url",
                type=ConfigEntryType.STRING,
                label="Server URL",
                required=True,
                value=base_url_value,
                default_value="http://streamloader:8000",
                category="generic",
                description="Base URL of your streamloader server.",
            ),
            ConfigEntry(
                key="api_key",
                type=ConfigEntryType.SECURE_STRING,
                label="API key (optional)",
                required=False,
                value=api_key_value,
                category="generic",
                description="Only required if streamloader enforces API auth.",
            ),
            ConfigEntry(
                key="clear_api_key",
                type=ConfigEntryType.ACTION,
                label="Clear saved API key",
                required=False,
                action="clear_api_key",
                action_label="Clear",
                immediate_apply=True,
                category="generic",
                description="Remove the stored API key from this provider config.",
            ),
            ConfigEntry(
                key="test_connection",
                type=ConfigEntryType.ACTION,
                label="Test connection",
                required=False,
                action="test_connection",
                action_label="Test",
                immediate_apply=True,
                category="generic",
                description="Validate URL/API key and check streamloader health.",
            ),
            ConfigEntry(
                key="section_behavior",
                type=ConfigEntryType.DIVIDER,
                label="Behavior",
                required=False,
                category="generic",
            ),
            ConfigEntry(
                key="prefer_library_browse",
                type=ConfigEntryType.BOOLEAN,
                label="Show Streamloader library in Browse",
                required=False,
                value=_as_bool(_value_for("prefer_library_browse", True), True),
                default_value=True,
                category="generic",
                description="Expose Streamloader local folders/albums in MA Browse.",
            ),
            ConfigEntry(
                key="auto_download_track_on_play",
                type=ConfigEntryType.BOOLEAN,
                label="Auto-download played tracks",
                required=False,
                value=_as_bool(_value_for("auto_download_track_on_play", False), False),
                default_value=False,
                category="generic",
                description="Queue a track download when playback starts.",
            ),
            ConfigEntry(
                key="auto_download_album_on_play",
                type=ConfigEntryType.BOOLEAN,
                label="Auto-download played track's album",
                required=False,
                value=_as_bool(_value_for("auto_download_album_on_play", False), False),
                default_value=False,
                category="generic",
                description="Queue the parent album when playback starts.",
            ),
            ConfigEntry(
                key="auto_download_album_missing_only",
                type=ConfigEntryType.BOOLEAN,
                label="Album auto-download: missing tracks only",
                required=False,
                value=_as_bool(_value_for("auto_download_album_missing_only", False), False),
                default_value=False,
                category="generic",
                description="Only queue missing tracks instead of full album.",
            ),
            ConfigEntry(
                key="section_advanced",
                type=ConfigEntryType.DIVIDER,
                label="Advanced",
                required=False,
                category="advanced",
            ),
            ConfigEntry(
                key="search_limit",
                type=ConfigEntryType.INTEGER,
                label="Search result limit",
                required=False,
                range=(5, 200),
                value=max(5, min(200, _as_int(_value_for("search_limit", 25), 25))),
                default_value=25,
                category="advanced",
                description="Maximum track/album/artist rows requested per search.",
            ),
            ConfigEntry(
                key="request_timeout_seconds",
                type=ConfigEntryType.INTEGER,
                label="HTTP timeout (seconds)",
                required=False,
                range=(2, 120),
                value=max(2, min(120, _as_int(_value_for("request_timeout_seconds", 25), 25))),
                default_value=25,
                category="advanced",
                description="Per-request timeout when calling streamloader.",
            ),
            ConfigEntry(
                key="request_retries",
                type=ConfigEntryType.INTEGER,
                label="HTTP retries",
                required=False,
                range=(1, 8),
                value=max(1, min(8, _as_int(_value_for("request_retries", 4), 4))),
                default_value=4,
                category="advanced",
                description="Retry count for transient transport errors.",
            ),
            ConfigEntry(
                key="strict_startup_health_check",
                type=ConfigEntryType.BOOLEAN,
                label="Fail startup when streamloader is unreachable",
                required=False,
                value=_as_bool(_value_for("strict_startup_health_check", True), True),
                default_value=True,
                category="advanced",
                description="If enabled, provider setup fails if initial health checks fail.",
            ),
            ConfigEntry(
                key="prefer_direct_stream",
                type=ConfigEntryType.BOOLEAN,
                label="Prefer direct stream URLs",
                required=False,
                value=_as_bool(_value_for("prefer_direct_stream", True), True),
                default_value=True,
                category="advanced",
                description="Use direct stream URLs when available.",
            ),
        ]
    )

    entries.append(
        ConfigEntry(
            key="section_sync_import",
            type=ConfigEntryType.DIVIDER,
            label="Sync / Import",
            required=False,
            category="sync_options",
        )
    )

    try:
        import music_assistant.constants as ma_constants

        native_entry_names = [
            "CONF_ENTRY_LIBRARY_SYNC_ARTISTS",
            "CONF_ENTRY_LIBRARY_SYNC_ALBUMS",
            "CONF_ENTRY_LIBRARY_SYNC_TRACKS",
            "CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS",
            "CONF_ENTRY_LIBRARY_IMPORT_ALBUM_TRACKS",
            "CONF_ENTRY_LIBRARY_IMPORT_PLAYLIST_TRACKS",
            "CONF_ENTRY_PROVIDER_SYNC_INTERVAL_ARTISTS",
            "CONF_ENTRY_PROVIDER_SYNC_INTERVAL_ALBUMS",
            "CONF_ENTRY_PROVIDER_SYNC_INTERVAL_TRACKS",
            "CONF_ENTRY_PROVIDER_SYNC_INTERVAL_PLAYLISTS",
        ]
        found_native_entries: list[Any] = []
        for const_name in native_entry_names:
            entry = getattr(ma_constants, const_name, None)
            if entry is not None:
                found_native_entries.append(entry)
        if found_native_entries:
            entries.extend(found_native_entries)
        else:
            raise RuntimeError("No MA native sync/import constants found")
    except Exception:
        entries.extend(
            [
                ConfigEntry(
                    key="library_sync_artists",
                    type=ConfigEntryType.BOOLEAN,
                    label="Sync library artists",
                    required=False,
                    value=_as_bool(_value_for("library_sync_artists", True), True),
                    default_value=True,
                    category="sync_options",
                    description="Sync artists from Streamloader to Music Assistant.",
                ),
                ConfigEntry(
                    key="library_sync_albums",
                    type=ConfigEntryType.BOOLEAN,
                    label="Sync library albums",
                    required=False,
                    value=_as_bool(_value_for("library_sync_albums", True), True),
                    default_value=True,
                    category="sync_options",
                    description="Sync albums from Streamloader to Music Assistant.",
                ),
                ConfigEntry(
                    key="library_sync_tracks",
                    type=ConfigEntryType.BOOLEAN,
                    label="Sync library tracks",
                    required=False,
                    value=_as_bool(_value_for("library_sync_tracks", True), True),
                    default_value=True,
                    category="sync_options",
                    description="Sync tracks from Streamloader to Music Assistant.",
                ),
                ConfigEntry(
                    key="library_sync_playlists",
                    type=ConfigEntryType.BOOLEAN,
                    label="Sync library playlists",
                    required=False,
                    value=_as_bool(_value_for("library_sync_playlists", False), False),
                    default_value=False,
                    category="sync_options",
                    description="Sync playlists from Streamloader to Music Assistant when supported.",
                ),
            ]
        )

    return tuple(entries)
