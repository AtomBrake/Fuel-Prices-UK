"""Tests for API client sharing between config entries and the cached-data fallback.

Like test_coordinator_repair.py, these run the coordinator logic on bare
instances (bypassing DataUpdateCoordinator.__init__) with hass mocked out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

import custom_components.fuel_prices_uk as integration
from custom_components.fuel_prices_uk.const import CACHE_REFRESH_MARGIN_SECONDS, CACHED_DATA_GRACE_SECONDS, DOMAIN


def _fake_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    return hass


class TestSharedApiClient:
    """Entries with the same credentials must share one client: one token, one cache (issue #8)."""

    def test_same_credentials_share_one_client(self) -> None:
        hass = _fake_hass()
        with patch.object(integration, "FuelPricesAPI", side_effect=lambda *a, **kw: MagicMock()):
            api_home = integration._acquire_api_client(hass, "entry_home", "id", "secret")
            api_work = integration._acquire_api_client(hass, "entry_work", "id", "secret")
            api_other = integration._acquire_api_client(hass, "entry_other", "other-id", "secret")

        assert api_home is api_work
        assert api_other is not api_home

    def test_client_is_kept_until_last_entry_releases_it(self) -> None:
        hass = _fake_hass()
        with patch.object(integration, "FuelPricesAPI", side_effect=lambda *a, **kw: MagicMock()):
            api = integration._acquire_api_client(hass, "entry_home", "id", "secret")
            integration._acquire_api_client(hass, "entry_work", "id", "secret")

            integration._release_api_client(hass, "entry_home")
            assert integration._acquire_api_client(hass, "entry_home", "id", "secret") is api

            integration._release_api_client(hass, "entry_home")
            integration._release_api_client(hass, "entry_work")
            assert hass.data[DOMAIN][integration.DATA_API_CLIENTS] == {}

            # After everyone has gone, a new entry gets a fresh client.
            assert integration._acquire_api_client(hass, "entry_new", "id", "secret") is not api

    def test_release_without_any_clients_is_harmless(self) -> None:
        integration._release_api_client(_fake_hass(), "never_set_up")


def _bare_coordinator(api_client: MagicMock) -> integration.FuelPricesDataUpdateCoordinator:
    coordinator = integration.FuelPricesDataUpdateCoordinator.__new__(integration.FuelPricesDataUpdateCoordinator)
    coordinator.hass = MagicMock()
    coordinator.api_client = api_client
    coordinator.location = {"latitude": 51.5, "longitude": -0.12}
    coordinator.radius = 5
    coordinator.fuel_types = ["E10"]
    coordinator.stations = []
    coordinator.device_tracker_entity_id = None
    coordinator._cache_max_age = 3600 - CACHE_REFRESH_MARGIN_SECONDS
    coordinator._consecutive_failures = 0
    coordinator._first_failure_at = None
    coordinator._stale_data_issue_id = "stale_data_test_entry"
    return coordinator


class TestCachedDataFallback:
    """A transient upstream failure must not blank every sensor when recent data exists (issue #14)."""

    async def test_poll_passes_cache_max_age_to_fetch(self) -> None:
        coordinator = _bare_coordinator(MagicMock())
        fetch = AsyncMock(return_value=[{"site_id": "s1"}])
        with patch.object(integration, "fetch_stations_by_criteria", fetch), patch.object(integration, "ir"):
            assert await coordinator._async_update_data() == [{"site_id": "s1"}]

        assert fetch.call_args.kwargs["max_age"] == 3600 - CACHE_REFRESH_MARGIN_SECONDS

    async def test_failure_serves_recent_cache_and_counts_towards_repair(self) -> None:
        api = MagicMock()
        api.cached_stations_within_radius.return_value = [{"site_id": "cached"}]
        api.data_age_seconds = 1800
        coordinator = _bare_coordinator(api)

        fetch = AsyncMock(side_effect=RuntimeError("GET /api/v1/pfs/fuel-prices failed (500)"))
        with patch.object(integration, "fetch_stations_by_criteria", fetch), patch.object(integration, "ir"):
            result = await coordinator._async_update_data()

        assert result == [{"site_id": "cached"}]
        assert coordinator._consecutive_failures == 1
        api.cached_stations_within_radius.assert_called_once_with(51.5, -0.12, 5, max_age=CACHED_DATA_GRACE_SECONDS)

    async def test_failure_without_usable_cache_raises_update_failed(self) -> None:
        api = MagicMock()
        api.cached_stations_within_radius.return_value = None
        coordinator = _bare_coordinator(api)

        fetch = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(integration, "fetch_stations_by_criteria", fetch),
            patch.object(integration, "ir"),
            pytest.raises(UpdateFailed),
        ):
            await coordinator._async_update_data()

        assert coordinator._consecutive_failures == 1

    async def test_repeated_cached_fallbacks_still_raise_the_repair(self) -> None:
        api = MagicMock()
        api.cached_stations_within_radius.return_value = [{"site_id": "cached"}]
        api.data_age_seconds = 3600
        coordinator = _bare_coordinator(api)

        fetch = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(integration, "fetch_stations_by_criteria", fetch), patch.object(integration, "ir") as mock_ir:
            for _ in range(3):
                await coordinator._async_update_data()

        mock_ir.async_create_issue.assert_called_once()
