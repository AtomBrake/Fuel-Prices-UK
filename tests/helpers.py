"""Helpers shared by the test modules."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import SOURCE_USER, ConfigEntry

from custom_components.fuel_prices_uk.const import DOMAIN, ENTRY_TITLE


def make_config_entry(
    data: Mapping[str, Any],
    options: Mapping[str, Any] | None = None,
    *,
    entry_id: str = "entry1",
    title: str = ENTRY_TITLE,
) -> ConfigEntry:
    """Return a real config entry for this integration, not registered with any hass.

    Its data and options are read-only, as in Home Assistant, so a different
    set of options means a new entry (see ``with_options``).
    """
    return ConfigEntry(
        data=data,
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id=entry_id,
        minor_version=1,
        options=options,
        source=SOURCE_USER,
        subentries_data=None,
        title=title,
        unique_id=None,
        version=1,
    )


def with_options(entry: ConfigEntry, options: Mapping[str, Any]) -> ConfigEntry:
    """Return a copy of ``entry`` with its options replaced, as saving an options flow does."""
    return make_config_entry(entry.data, options, entry_id=entry.entry_id, title=entry.title)
