"""Cielo Home fan platform - exposes AC fan speed as a HA fan entity."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cielohomedevice import CieloHomeDevice
from .const import (
    DOMAIN,
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_SUPER_HIGH,
    FAN_ULTRA_HIGH,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Cielo Home fan entities from a config entry."""
    cw_devices: list[CieloHomeDevice] = hass.data[DOMAIN][
        config_entry.entry_id + "_devices"
    ]
    entities = [CieloFanEntity(device) for device in cw_devices]
    async_add_entities(entities)


class CieloFanEntity(FanEntity):
    """Fan entity that exposes AC fan speed for a Cielo Home device.

    Speed steps are built dynamically from whatever fan modes the device
    reports, so this works for any Cielo device regardless of how many
    speeds it supports (3, 4, 5, etc.).

    Percentages are distributed evenly across the available speeds:
      3 speeds -> Low=33%,  Medium=67%,  High=100%
      5 speeds -> Low=20%,  Medium=40%,  High=60%,  Super High=80%,  Ultra High=100%

    Setting percentage to 0 or calling turn_off returns the fan to Auto
    without powering off the AC unit.
    """

    _attr_has_entity_name = True
    _attr_name = "Fan Speed"
    _attr_supported_features = FanEntityFeature.SET_SPEED

    def __init__(self, cw_device: CieloHomeDevice) -> None:
        """Initialize and build speed table dynamically from available fan modes."""
        self._cw_device = cw_device

        # Get all available modes from the device, excluding Auto
        raw_modes = cw_device.get_fan_modes() or []
        self._speed_modes = [m for m in raw_modes if m != FAN_AUTO]

        if not self._speed_modes:
            # Fallback for devices with unrecognised modes
            self._speed_modes = [FAN_LOW, FAN_MEDIUM, FAN_HIGH]

        count = len(self._speed_modes)

        # Distribute percentages evenly -- last step is always exactly 100%
        self._pct_steps = [round((i + 1) * 100 / count) for i in range(count)]
        self._mode_to_pct: dict[str, int] = {
            mode: pct for mode, pct in zip(self._speed_modes, self._pct_steps)
        }
        self._pct_to_mode: dict[int, str] = {
            pct: mode for pct, mode in zip(self._pct_steps, self._speed_modes)
        }

        _LOGGER.debug(
            "%s: fan speed table built: %s",
            cw_device.get_name(),
            self._mode_to_pct,
        )

        self._attr_unique_id = f"{cw_device.get_uniqueid()}_fan_speed"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cw_device.get_uniqueid())},
            name=cw_device.get_name(),
            manufacturer="Cielo Home",
        )

    async def async_added_to_hass(self) -> None:
        """Register as a listener so device state changes push to HA immediately."""
        self._cw_device.add_listener(self)

    async def state_updated(self) -> None:
        """Called by CieloHomeDevice when the device reports a state change."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return True when the AC unit is powered on."""
        return self._cw_device.get_power() == "on"

    @property
    def percentage(self) -> int | None:
        """Return the current fan speed as a percentage.

        Auto mode returns 0. All other modes map per the dynamic table.
        """
        fan_mode = self._cw_device.get_fan_mode()
        return self._mode_to_pct.get(fan_mode, 0)

    @property
    def speed_count(self) -> int:
        """Return the number of discrete speed steps."""
        return len(self._speed_modes)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set fan speed from a percentage by snapping to the nearest step.

        0% sets fan to Auto. All other values snap to the nearest defined
        step and map to the corresponding fan mode.
        """
        if percentage == 0:
            _LOGGER.debug(
                "%s: setting fan to Auto (percentage=0)",
                self._cw_device.get_name(),
            )
            await self._set_auto()
            return

        nearest = min(self._pct_steps, key=lambda s: abs(s - percentage))
        fan_mode = self._pct_to_mode[nearest]

        _LOGGER.debug(
            "%s: setting fan to %s (requested %s%%, snapped to %s%%)",
            self._cw_device.get_name(),
            fan_mode,
            percentage,
            nearest,
        )
        await self.hass.async_add_executor_job(
            self._cw_device.send_fan_mode, fan_mode
        )

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on -- apply percentage if given, otherwise default to Medium."""
        if percentage is not None:
            await self.async_set_percentage(percentage)
        else:
            _LOGGER.debug(
                "%s: turn_on with no percentage, defaulting to Medium",
                self._cw_device.get_name(),
            )
            await self.hass.async_add_executor_job(
                self._cw_device.send_fan_mode, FAN_MEDIUM
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Return fan to Auto mode (does NOT power off the AC)."""
        await self._set_auto()

    async def _set_auto(self) -> None:
        """Set fan to Auto mode."""
        await self.hass.async_add_executor_job(
            self._cw_device.send_fan_mode, FAN_AUTO
        )
