"""Tests for the price sensors' ranking, filtering, attributes and statistics settings.

The sensors only read coordinator.data, coordinator.entry and
coordinator.last_update_success, so a plain namespace stands in for the
coordinator and no Home Assistant runtime is needed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry

from custom_components.fuel_prices_uk.const import (
    CONF_ADDRESS,
    CONF_FUELTYPES,
    CONF_LOCATION,
    CONF_MAX_DATA_AGE_DAYS,
    CONF_RADIUS,
    KM_TO_MILES,
)
from custom_components.fuel_prices_uk.sensor import (
    CheapestFuelPriceSensor,
    NearestFuelStationSensor,
    _derive_location_strings,
    _parse_last_updated,
)
from tests.helpers import make_config_entry

RECENT = datetime.now(UTC).isoformat()


def _entry(data: dict[str, Any] | None = None, options: dict[str, Any] | None = None) -> ConfigEntry:
    base: dict[str, Any] = {
        CONF_FUELTYPES: ["E10", "B7"],
        CONF_LOCATION: {"latitude": 51.5, "longitude": -0.12},
        CONF_RADIUS: 8.0,
    }
    base.update(data or {})
    return make_config_entry(base, options, title="Fuel Prices UK - Home")


def _coordinator(stations: list[dict[str, Any]], entry: ConfigEntry | None = None) -> SimpleNamespace:
    return SimpleNamespace(data=stations, entry=entry or _entry(), last_update_success=True)


def _station(
    site_id: str,
    price: float,
    *,
    distance: float = 1.0,
    fuel_type: str = "E10",
    last_updated: str = RECENT,
) -> dict[str, Any]:
    return {
        "site_id": site_id,
        "id": site_id,
        "name": f"Station {site_id}",
        "brand": "TestCo",
        "address": "1 High Street",
        "postcode": "SW1A 1AA",
        "latitude": 51.5,
        "longitude": -0.12,
        "distance": distance,
        "prices": {fuel_type: {"price": price, "last_updated": last_updated}},
    }


def _refreshed(sensor: CheapestFuelPriceSensor) -> CheapestFuelPriceSensor:
    sensor._refresh_snapshot()
    return sensor


def _attributes(sensor: CheapestFuelPriceSensor) -> Mapping[str, Any]:
    """Return the sensor's attributes; HA types them as optional, but these sensors always set them."""
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    return attributes


def test_price_sensor_keeps_long_term_statistics() -> None:
    """MEASUREMENT makes HA compile long-term statistics; MONETARY would forbid it."""
    sensor = CheapestFuelPriceSensor(_coordinator([]), _entry(), "E10")

    assert sensor.state_class is SensorStateClass.MEASUREMENT
    assert sensor.device_class is None
    assert sensor.native_unit_of_measurement == "GBP"


def test_nearest_sensor_keeps_long_term_statistics() -> None:
    sensor = NearestFuelStationSensor(_coordinator([]), _entry(), "E10")

    assert sensor.state_class is SensorStateClass.MEASUREMENT
    assert sensor.device_class is None


def test_cheapest_sensors_rank_stations_by_price() -> None:
    coordinator = _coordinator([_station("a", 1.459), _station("b", 1.399), _station("c", 1.429)])

    ranked = [_refreshed(CheapestFuelPriceSensor(coordinator, coordinator.entry, "E10", rank)) for rank in (1, 2, 3)]

    assert [s.native_value for s in ranked] == [1.399, 1.429, 1.459]
    assert [_attributes(s)["station_name"] for s in ranked] == ["Station b", "Station c", "Station a"]
    assert _attributes(ranked[1])["price_rank_label"] == "2nd"


def test_equal_prices_rank_in_a_stable_order() -> None:
    coordinator = _coordinator([_station("z", 1.399), _station("a", 1.399)])

    first = _refreshed(CheapestFuelPriceSensor(coordinator, coordinator.entry, "E10", 1))

    assert _attributes(first)["station_name"] == "Station a"


def test_rank_beyond_available_stations_has_no_value() -> None:
    coordinator = _coordinator([_station("a", 1.459)])

    sensor = _refreshed(CheapestFuelPriceSensor(coordinator, coordinator.entry, "E10", 2))

    assert sensor.native_value is None
    assert "station_name" not in _attributes(sensor)


def test_stations_without_the_fuel_type_are_ignored() -> None:
    coordinator = _coordinator([_station("diesel_only", 1.299, fuel_type="B7"), _station("a", 1.459)])

    sensor = _refreshed(CheapestFuelPriceSensor(coordinator, coordinator.entry, "E10"))

    assert sensor.native_value == 1.459


def test_stale_prices_are_skipped_when_max_data_age_is_set() -> None:
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    entry = _entry({CONF_MAX_DATA_AGE_DAYS: 7})
    coordinator = _coordinator([_station("stale", 1.199, last_updated=old), _station("fresh", 1.459)], entry)

    sensor = _refreshed(CheapestFuelPriceSensor(coordinator, entry, "E10"))

    assert sensor.native_value == 1.459
    assert _attributes(sensor)["station_name"] == "Station fresh"


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-17T16:00:00+00:00",
        # What the API client stores when the feed's timestamp has milliseconds.
        "2026-02-17T16:00:00.123000+00:00",
        "2026-02-17T16:00:00.000Z",
        "17/02/2026 16:00:00",
    ],
)
def test_parse_last_updated_accepts_stored_formats(value: str) -> None:
    parsed = _parse_last_updated(value)

    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.replace(microsecond=0) == datetime(2026, 2, 17, 16, 0, tzinfo=UTC)


def test_stale_prices_are_kept_when_max_data_age_is_disabled() -> None:
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    coordinator = _coordinator([_station("stale", 1.199, last_updated=old), _station("fresh", 1.459)])

    sensor = _refreshed(CheapestFuelPriceSensor(coordinator, coordinator.entry, "E10"))

    assert sensor.native_value == 1.199


def test_cheapest_sensor_attributes() -> None:
    coordinator = _coordinator([_station("a", 1.459, distance=3.0)])

    attributes = _attributes(_refreshed(CheapestFuelPriceSensor(coordinator, coordinator.entry, "E10")))

    assert attributes["fuel_type"] == "E10"
    assert attributes["brand"] == "TestCo"
    assert attributes["address"] == "1 High Street"
    assert attributes["postcode"] == "SW1A 1AA"
    assert attributes["last_updated"] == RECENT
    # Stations carry distance in km; the attribute is in miles.
    assert attributes["distance"] == pytest.approx(round(3.0 * KM_TO_MILES, 2))


def test_nearest_sensors_rank_stations_by_distance() -> None:
    coordinator = _coordinator(
        [
            _station("far_cheap", 1.299, distance=6.0),
            _station("near", 1.499, distance=0.5),
            _station("middle", 1.459, distance=2.0),
        ]
    )

    ranked = [_refreshed(NearestFuelStationSensor(coordinator, coordinator.entry, "E10", rank)) for rank in (1, 2, 3)]

    assert [s.native_value for s in ranked] == [1.499, 1.459, 1.299]
    attributes = _attributes(ranked[0])
    assert attributes["distance_rank"] == 1
    assert attributes["distance_rank_label"] == "1st"
    assert "price_rank" not in attributes


def test_sensor_identifiers() -> None:
    entry = _entry()
    coordinator = _coordinator([], entry)

    first = CheapestFuelPriceSensor(coordinator, entry, "E10", 1)
    second = CheapestFuelPriceSensor(coordinator, entry, "E10", 2)
    nearest = NearestFuelStationSensor(coordinator, entry, "B7", 1)

    assert first.unique_id == "entry1_E10_cheapest"
    assert second.unique_id == "entry1_E10_cheapest_2"
    assert nearest.unique_id == "entry1_B7_nearest_1"


def test_location_label_uses_address_when_set() -> None:
    label, _slug = _derive_location_strings(_entry({CONF_ADDRESS: "SW1A 1AA"}))

    assert label == f"SW1A 1AA · {round(8.0 * KM_TO_MILES, 1):g} mi"


def test_blank_address_in_options_overrides_address_in_data() -> None:
    """After switching an address entry to the map, the old address must not name the sensors."""
    entry = _entry(
        {CONF_ADDRESS: "SW1A 1AA"}, options={CONF_ADDRESS: "", CONF_LOCATION: {"latitude": 52.2, "longitude": -1.5}}
    )

    label, _slug = _derive_location_strings(entry)

    assert label.startswith("52.2000,-1.5000 · ")
