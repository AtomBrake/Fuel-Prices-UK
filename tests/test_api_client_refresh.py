"""Mocked-API tests for FuelPricesAPI's refresh/pagination/fallback logic.

Uses aiointercept to stand in for the live Fuel Finder API so these run
without network access or real credentials. aiointercept routes the client's
real requests to a local aiohttp test server (rather than patching aiohttp
internals, as aioresponses did - which broke with aiohttp 3.14). This is the
highest-value test
file in the suite: it locks in the fix for GitHub issue #14 (incremental
refresh always falling back to an expensive full snapshot because a 404 on
batch 1 was treated as a hard failure instead of "no updates").
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from aiointercept import aiointercept

from custom_components.fuel_prices_uk.api_client import (
    API_BASE_URL,
    PFS_INFO_ENDPOINT,
    PFS_PRICES_ENDPOINT,
    TOKEN_ENDPOINT,
    ApiHttpError,
    FuelPricesAPI,
    TransientApiError,
)

PFS_INFO_PATTERN = re.compile(re.escape(f"{API_BASE_URL}{PFS_INFO_ENDPOINT}") + r"(\?.*)?$")
PFS_PRICES_PATTERN = re.compile(re.escape(f"{API_BASE_URL}{PFS_PRICES_ENDPOINT}") + r"(\?.*)?$")
TOKEN_URL = f"{API_BASE_URL}{TOKEN_ENDPOINT}"


def _station_info_row(node_id: str, name: str = "Test Station") -> dict:
    return {
        "node_id": node_id,
        "trading_name": name,
        "brand_name": "TestCo",
        "location": {"latitude": 51.5, "longitude": -0.12, "postcode": "SW1A 1AA"},
    }


def _price_row(node_id: str, price: float, fuel_type: str = "E10") -> dict:
    return {
        "node_id": node_id,
        "fuel_prices": [
            {
                "fuel_type": fuel_type,
                "price": price,
                "price_last_updated": "2026-01-01T00:00:00Z",
            }
        ],
    }


def _mock_api() -> aiointercept:
    """Intercept requests to the real Fuel Finder host (no real network access).

    Each registered response is used once, in registration order. A request
    with no matching response gets its connection closed, which the client
    sees as ClientConnectionError.
    """
    return aiointercept(mock_external_urls=True)


def _mock_token(m: aiointercept) -> None:
    m.post(TOKEN_URL, status=200, payload={"access_token": "test-token", "expires_in": 3600})


def _token_request_count(m: aiointercept) -> int:
    return sum(1 for (method, _url), _request in m.ordered_requests if method == "POST")


@pytest.fixture
def fast_client(monkeypatch):
    """Patch the inter-request pacing so tests run without real sleeps."""
    import custom_components.fuel_prices_uk.api_client as ac

    monkeypatch.setattr(ac, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(ac, "DEFAULT_429_BACKOFF_SECONDS", 0.01)

    async def _noop_pause() -> None:
        return None

    monkeypatch.setattr(ac.FuelPricesAPI, "_inter_fetch_pause", staticmethod(_noop_pause))
    return ac


async def _full_snapshot_refresh(api: FuelPricesAPI, m: aiointercept, *, station_price: float = 145.9) -> None:
    """Register a minimal successful full-snapshot pair and refresh."""
    _mock_token(m)
    m.get(
        PFS_INFO_PATTERN,
        status=200,
        payload={"total_batches": 1, "data": [_station_info_row("s1")]},
    )
    m.get(
        PFS_PRICES_PATTERN,
        status=200,
        payload={"total_batches": 1, "data": [_price_row("s1", station_price)]},
    )
    await api.get_all_stations(force_refresh=True)


class TestFullSnapshotAndPagination:
    async def test_full_snapshot_success(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c1", client_secret="s1")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m)

        stations = await api.get_all_stations()
        assert len(stations) == 1
        station = stations[0]
        assert station["site_id"] == "s1"
        assert station["latitude"] == 51.5
        assert station["prices"]["E10"]["price"] == pytest.approx(1.459)

    async def test_pagination_stops_via_total_batches_hint(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c2", client_secret="s2")
        async with _mock_api() as m:
            _mock_token(m)
            m.get(
                PFS_INFO_PATTERN,
                status=200,
                payload={
                    "total_batches": 1,
                    "data": [_station_info_row("s1"), _station_info_row("s2")],
                },
            )
            # Two price batches (distinct stations per batch, so the
            # repeated-payload-signature guard doesn't fire), total_batches=2
            # tells the client to stop after batch 2.
            m.get(
                PFS_PRICES_PATTERN,
                status=200,
                payload={"total_batches": 2, "data": [_price_row("s1", 145.9)]},
            )
            m.get(
                PFS_PRICES_PATTERN,
                status=200,
                payload={"total_batches": 2, "data": [_price_row("s2", 132.9, fuel_type="B7")]},
            )
            await api.get_all_stations(force_refresh=True)

        stations = {s["site_id"]: s for s in await api.get_all_stations()}
        assert stations["s1"]["prices"]["E10"]["price"] == pytest.approx(1.459)
        assert stations["s2"]["prices"]["B7"]["price"] == pytest.approx(1.329)

    async def test_404_on_batch_greater_than_one_ends_pagination(self, fast_client, aiohttp_client_session) -> None:
        """Existing behaviour: a 404 on batch 2+ (no total_batches hint) means end-of-pages."""
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c3", client_secret="s3")
        async with _mock_api() as m:
            _mock_token(m)
            m.get(
                PFS_INFO_PATTERN,
                status=200,
                payload={"data": [_station_info_row("s1")]},  # no total_batches hint
            )
            m.get(PFS_INFO_PATTERN, status=404, payload={"message": "not found"})  # batch 2
            m.get(
                PFS_PRICES_PATTERN,
                status=200,
                payload={"data": [_price_row("s1", 145.9)]},
            )
            m.get(PFS_PRICES_PATTERN, status=404, payload={"message": "not found"})  # batch 2

            stations = await api.get_all_stations(force_refresh=True)

        assert len(stations) == 1
        assert stations[0]["prices"]["E10"]["price"] == pytest.approx(1.459)


class TestIncrementalRefresh:
    """Regression coverage for GitHub issue #14.

    Confirmed against the live Fuel Finder API: an incremental request's
    404-on-batch-1 ("Requested batch 1 is not available") happens
    regardless of the effective-start-timestamp format and simply means
    "nothing changed in this window" - it must not abort the whole refresh.
    """

    async def test_incremental_404_on_batch_one_is_treated_as_no_updates(
        self, fast_client, aiohttp_client_session
    ) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c4", client_secret="s4")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m, station_price=145.9)

        assert api._last_refresh is not None
        assert "s1" in api._station_index

        async with _mock_api() as m:
            _mock_token(m)
            # Incremental station-info: 404 on batch 1 -> must be treated as empty.
            m.get(PFS_INFO_PATTERN, status=404, payload={"message": "Requested batch 1 is not available"})
            # Incremental prices: succeeds with a genuinely updated price.
            m.get(
                PFS_PRICES_PATTERN,
                status=200,
                payload={"total_batches": 1, "data": [_price_row("s1", 139.9)]},
            )

            await api._refresh()

            # The full-snapshot fallback endpoints (no effective-start-timestamp)
            # must NOT have been hit - the incremental path must have succeeded
            # on its own instead of aborting to a full snapshot.
            fallback_calls = [
                url for (method, url) in m.requests if method == "GET" and "effective-start-timestamp" not in str(url)
            ]
            assert fallback_calls == [], f"Unexpected full-snapshot fallback calls: {fallback_calls}"

        stations = await api.get_all_stations()
        assert stations[0]["prices"]["E10"]["price"] == pytest.approx(1.399)

    async def test_404_on_batch_one_without_effective_start_still_raises(
        self, fast_client, aiohttp_client_session
    ) -> None:
        """A 404 on batch 1 for a *full* snapshot request (no effective_start) is still a hard failure."""
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c5", client_secret="s5")
        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=404, payload={"message": "not found"})

            with pytest.raises(ApiHttpError):
                await api._fetch_station_info()

    async def test_incremental_fallback_to_full_snapshot_on_genuine_failure(
        self, fast_client, aiohttp_client_session
    ) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c6", client_secret="s6")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m, station_price=145.9)

        async with _mock_api() as m:
            _mock_token(m)
            # Incremental info succeeds with nothing new...
            m.get(PFS_INFO_PATTERN, status=404, payload={"message": "Requested batch 1 is not available"})
            # ...but incremental prices is rejected outright (a non-transient
            # client error, unlike a 5xx).
            m.get(PFS_PRICES_PATTERN, status=400, payload={"message": "Bad Request"})

            # Fallback: full snapshot for both endpoints.
            m.get(
                PFS_INFO_PATTERN,
                status=200,
                payload={"total_batches": 1, "data": [_station_info_row("s1")]},
            )
            m.get(
                PFS_PRICES_PATTERN,
                status=200,
                payload={"total_batches": 1, "data": [_price_row("s1", 150.0)]},
            )

            await api._refresh()

        stations = await api.get_all_stations()
        assert stations[0]["prices"]["E10"]["price"] == pytest.approx(1.5)

    async def test_incremental_5xx_keeps_cache_without_full_snapshot(self, fast_client, aiohttp_client_session) -> None:
        """A 5xx is transient: keep the cache and retry next cycle (issue #14).

        Falling back to a full nationwide snapshot here just sends the
        heaviest possible request to an API that is already erroring.
        """
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c6b", client_secret="s6b")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m, station_price=145.9)
        refreshed_at = api._last_refresh

        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=404, payload={"message": "Requested batch 1 is not available"})
            m.get(PFS_PRICES_PATTERN, status=500, payload={"message": "Internal Server Error"})

            with pytest.raises(ApiHttpError):
                await api._refresh()

            full_snapshot_calls = [
                url for (method, url) in m.requests if method == "GET" and "effective-start-timestamp" not in str(url)
            ]
            assert full_snapshot_calls == []

        assert api._last_refresh == refreshed_at
        assert api._station_index["s1"]["prices"]["E10"]["price"] == pytest.approx(1.459)

    async def test_incremental_network_error_is_transient(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c6c", client_secret="s6c")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m)

        async with _mock_api() as m:
            _mock_token(m)
            # Drops the connection: the client sees ClientConnectionError.
            m.get(PFS_INFO_PATTERN, exception=True)

            with pytest.raises(TransientApiError):
                await api._refresh()

            assert all("effective-start-timestamp" in str(url) for (method, url) in m.requests if method == "GET")


class TestCacheFreshness:
    """The cache lifetime must follow the caller's poll interval, not a fixed hour."""

    async def test_max_age_shorter_than_data_age_triggers_refresh(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c10", client_secret="s10")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m)
        api._last_refresh -= timedelta(seconds=900)

        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=404, payload={"message": "Requested batch 1 is not available"})
            m.get(PFS_PRICES_PATTERN, status=404, payload={"message": "Requested batch 1 is not available"})

            # A 15-minute poll with the default 1-hour cache would have reused
            # the 15-minute-old data; an explicit max_age must not.
            await api.get_stations_within_radius(51.5, -0.12, 5, max_age=840)

            assert any(method == "GET" for (method, _url) in m.requests)
        assert api.data_age_seconds is not None and api.data_age_seconds < 5

    async def test_data_within_max_age_is_reused(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c11", client_secret="s11")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m)

        async with _mock_api() as m:
            stations = await api.get_stations_within_radius(51.5, -0.12, 5, max_age=3540)
            m.assert_not_called()

        assert [s["site_id"] for s in stations] == ["s1"]

    async def test_cached_stations_within_radius_respects_grace_period(
        self, fast_client, aiohttp_client_session
    ) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c12", client_secret="s12")
        assert api.cached_stations_within_radius(51.5, -0.12, 5, max_age=3600) is None

        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m)

        cached = api.cached_stations_within_radius(51.5, -0.12, 5, max_age=3600)
        assert cached is not None
        assert cached[0]["site_id"] == "s1"
        assert cached[0]["distance"] == 0

        api._last_refresh -= timedelta(hours=2)
        assert api.cached_stations_within_radius(51.5, -0.12, 5, max_age=3600) is None


class TestRateLimitAndAuth:
    async def test_429_is_retried_and_then_succeeds(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c7", client_secret="s7")
        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=429, headers={"Retry-After": "0"})
            m.get(
                PFS_INFO_PATTERN,
                status=200,
                payload={"total_batches": 1, "data": [_station_info_row("s1")]},
            )

            stations = await api._fetch_station_info()

        assert len(stations) == 1

    async def test_token_is_cached_across_instances_with_same_credentials(
        self, fast_client, aiohttp_client_session
    ) -> None:
        """Two config entries with the same credentials must share one token (issue #8)."""
        api_a = FuelPricesAPI(session=aiohttp_client_session, client_id="shared", client_secret="secret")
        api_b = FuelPricesAPI(session=aiohttp_client_session, client_id="shared", client_secret="secret")

        async with _mock_api() as m:
            _mock_token(m)  # only ONE token response registered

            token_a = await api_a._get_access_token()
            token_b = await api_b._get_access_token()

            assert token_a == token_b == "test-token"
            assert _token_request_count(m) == 1

    async def test_token_refreshed_by_one_instance_is_used_by_the_other(
        self, fast_client, aiohttp_client_session
    ) -> None:
        """Regression for the issue #8 token ping-pong.

        Fuel Finder invalidates the previous token whenever a new one is
        issued. When instance A gets a 401 and fetches a new token, instance B
        must pick up that new token rather than replaying its own stale copy,
        getting a 401, and requesting yet another token (invalidating A's).
        """
        api_a = FuelPricesAPI(session=aiohttp_client_session, client_id="shared", client_secret="secret")
        api_b = FuelPricesAPI(session=aiohttp_client_session, client_id="shared", client_secret="secret")
        info_payload = {"total_batches": 1, "data": [_station_info_row("s1")]}

        async with _mock_api() as m:
            m.post(TOKEN_URL, status=200, payload={"access_token": "token-1", "expires_in": 3600})
            m.get(PFS_INFO_PATTERN, status=200, payload=info_payload)
            m.get(PFS_INFO_PATTERN, status=200, payload=info_payload)
            await api_a._fetch_station_info()
            await api_b._fetch_station_info()  # B caches nothing of its own

            # token-1 is revoked upstream; A notices first and gets token-2.
            m.get(PFS_INFO_PATTERN, status=401, payload={"message": "Unauthorized"})
            m.post(TOKEN_URL, status=200, payload={"access_token": "token-2", "expires_in": 3600})
            m.get(PFS_INFO_PATTERN, status=200, payload=info_payload)
            await api_a._fetch_station_info()

            # B must now use token-2 straight away: no 401, no new token.
            m.get(PFS_INFO_PATTERN, status=200, payload=info_payload)
            await api_b._fetch_station_info()

            assert _token_request_count(m) == 2
            auth_headers = [
                request.headers["Authorization"] for (method, _url), request in m.ordered_requests if method == "GET"
            ]
            assert auth_headers == [
                "Bearer token-1",
                "Bearer token-1",
                "Bearer token-1",
                "Bearer token-2",
                "Bearer token-2",
            ]

    async def test_stale_rejection_does_not_discard_newer_token(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="shared", client_secret="secret")
        ac = fast_client
        ac._global_token_cache[api._token_cache_key] = ("token-2", datetime.now(UTC) + timedelta(hours=1))

        # A request that was sent with the old token-1 comes back 401 late.
        api._invalidate_token("token-1")

        assert ac._global_token_cache[api._token_cache_key][0] == "token-2"

    async def test_credential_validation_reuses_valid_cached_token(self, fast_client, aiohttp_client_session) -> None:
        """Validating (e.g. adding a second location) must not issue a new token and invalidate the live one."""
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="shared", client_secret="secret")
        async with _mock_api() as m:
            _mock_token(m)
            await api._get_access_token()

            validator = FuelPricesAPI(session=aiohttp_client_session, client_id="shared", client_secret="secret")
            assert await validator.async_validate_credentials() is True

            assert _token_request_count(m) == 1

    async def test_cached_token_does_not_vouch_for_a_different_secret(
        self, fast_client, aiohttp_client_session
    ) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="shared", client_secret="secret")
        async with _mock_api() as m:
            _mock_token(m)
            await api._get_access_token()

            wrong = FuelPricesAPI(session=aiohttp_client_session, client_id="shared", client_secret="wrong")
            m.post(TOKEN_URL, status=401, payload={"message": "invalid client"})
            with pytest.raises(RuntimeError):
                await wrong.async_validate_credentials()


def _incremental_starts(m: aiointercept) -> list[str]:
    return [
        request.query["effective-start-timestamp"]
        for (method, _url), request in m.ordered_requests
        if method == "GET" and "effective-start-timestamp" in request.query
    ]


class TestRefreshWindowsAndResync:
    async def test_incremental_window_overlaps_previous_fetch(self, fast_client, aiohttp_client_session) -> None:
        """The next window must start before the previous fetch began, not when it ended.

        Otherwise a price published while a multi-minute fetch was paging can
        land on an already-fetched batch and fall outside the next window.
        """
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c20", client_secret="s20")
        overlap = timedelta(seconds=fast_client.INCREMENTAL_OVERLAP_SECONDS)
        before_fetch = datetime.now(UTC)
        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=200, payload={"total_batches": 1, "data": [_station_info_row("s1")]})
            m.get(PFS_PRICES_PATTERN, status=200, payload={"total_batches": 1, "data": [_price_row("s1", 145.9)]})
            await api._refresh()
        after_fetch = datetime.now(UTC)

        # Anchored to when the fetch started (minus the overlap), not when it finished.
        assert api._updates_since is not None
        assert before_fetch - overlap <= api._updates_since <= after_fetch - overlap
        assert api._updates_since < api._last_refresh - overlap

        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=404, payload={"message": "Requested batch 1 is not available"})
            m.get(PFS_PRICES_PATTERN, status=404, payload={"message": "Requested batch 1 is not available"})
            expected_start = api._updates_since.strftime("%Y-%m-%d %H:%M:%S")
            await api._refresh()

            assert _incremental_starts(m) == [expected_start, expected_start]

    async def test_daily_full_resync_drops_closed_stations(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c21", client_secret="s21")
        async with _mock_api() as m:
            _mock_token(m)
            m.get(
                PFS_INFO_PATTERN,
                status=200,
                payload={"total_batches": 1, "data": [_station_info_row("open"), _station_info_row("closed")]},
            )
            m.get(
                PFS_PRICES_PATTERN,
                status=200,
                payload={"total_batches": 1, "data": [_price_row("open", 145.9), _price_row("closed", 120.9)]},
            )
            await api._refresh()
        assert set(api._station_index) == {"open", "closed"}

        # A day later the closed station is simply absent from the snapshot.
        api._last_full_refresh -= timedelta(seconds=fast_client.FULL_RESYNC_SECONDS + 1)
        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=200, payload={"total_batches": 1, "data": [_station_info_row("open")]})
            m.get(PFS_PRICES_PATTERN, status=200, payload={"total_batches": 1, "data": [_price_row("open", 145.9)]})
            await api._refresh()

            assert _incremental_starts(m) == []

        assert set(api._station_index) == {"open"}

    async def test_failed_resync_keeps_existing_cache(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c22", client_secret="s22")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m)
        api._last_full_refresh -= timedelta(seconds=fast_client.FULL_RESYNC_SECONDS + 1)

        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=500, payload={"message": "Internal Server Error"})
            with pytest.raises(ApiHttpError):
                await api._refresh()

        assert api._station_index["s1"]["prices"]["E10"]["price"] == pytest.approx(1.459)


class TestPriceSanity:
    async def test_implausible_prices_are_not_stored(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c23", client_secret="s23")
        async with _mock_api() as m:
            _mock_token(m)
            m.get(
                PFS_INFO_PATTERN,
                status=200,
                payload={"total_batches": 1, "data": [_station_info_row("zero"), _station_info_row("ok")]},
            )
            m.get(
                PFS_PRICES_PATTERN,
                status=200,
                payload={"total_batches": 1, "data": [_price_row("zero", 0), _price_row("ok", 145.9)]},
            )
            await api._refresh()

        assert "E10" not in api._station_index["zero"]["prices"]
        assert api._station_index["ok"]["prices"]["E10"]["price"] == pytest.approx(1.459)

    async def test_junk_update_keeps_previous_price(self, fast_client, aiohttp_client_session) -> None:
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c24", client_secret="s24")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m, station_price=145.9)

        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=404, payload={"message": "Requested batch 1 is not available"})
            # 5.999 GBP/L once scaled from pence - a feed error, not a price.
            m.get(PFS_PRICES_PATTERN, status=200, payload={"total_batches": 1, "data": [_price_row("s1", 599.9)]})
            await api._refresh()

        assert api._station_index["s1"]["prices"]["E10"]["price"] == pytest.approx(1.459)


class TestReturnedDataIsNotMutated:
    async def test_incremental_update_does_not_change_previously_returned_stations(
        self, fast_client, aiohttp_client_session
    ) -> None:
        """Data handed to the coordinator must not change under it.

        The coordinator compares previous and new data to decide whether to
        notify sensors; if the merge edited the old snapshot's price dicts in
        place, the two would compare equal and the update could be dropped.
        """
        api = FuelPricesAPI(session=aiohttp_client_session, client_id="c25", client_secret="s25")
        async with _mock_api() as m:
            await _full_snapshot_refresh(api, m, station_price=145.9)
        previous = await api.get_stations_within_radius(51.5, -0.12, 5, max_age=3600)

        async with _mock_api() as m:
            _mock_token(m)
            m.get(PFS_INFO_PATTERN, status=404, payload={"message": "Requested batch 1 is not available"})
            m.get(PFS_PRICES_PATTERN, status=200, payload={"total_batches": 1, "data": [_price_row("s1", 139.9)]})
            await api._refresh()
        current = await api.get_stations_within_radius(51.5, -0.12, 5, max_age=3600)

        assert previous[0]["prices"]["E10"]["price"] == pytest.approx(1.459)
        assert current[0]["prices"]["E10"]["price"] == pytest.approx(1.399)
        assert previous != current
