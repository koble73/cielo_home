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
)

_LOGGER = logging.getLogger(__name__)

# Percentage steps and fan mode mappings when Turbo is NOT available (3 speeds)
PCT_STEPS_NO_TURBO = [33, 66, 100]
FAN_MODE_TO_PCT_NO_TURBO: dict[str, int] = {
    FAN_LOW: 33,
    FAN_MEDIUM: 66,
    FAN_HIGH: 100,
}
PCT_TO_FAN_MODE_NO_TURBO: dict[int, str] = {
    33: FAN_LOW,
    66: FAN_MEDIUM,
    100: FAN_HIGH,
}

# Percentage steps and fan mode mappings when Turbo IS available (4 speeds)
PCT_STEPS_TURBO = [25, 50, 75, 100]
FAN_MODE_TO_PCT_TURBO: dict[str, int] = {
    FAN_LOW: 25,
    FAN_MEDIUM: 50,
    FAN_HIGH: 75,
}
PCT_TO_FAN_MODE_TURBO: dict[int, str] = {
    25: FAN_LOW,
    50: FAN_MEDIUM,
    75: FAN_HIGH,
}


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
    """Fan entity that exposes the AC fan speed of a Cielo Home device.

    On devices that support Turbo, four discrete speeds are exposed:
    Low (25%), Medium (50%), High (75%), Turbo (100%).

    On devices without Turbo, three speeds are exposed:
    Low (33%), Medium (66%), High (100%).

    Setting percentage to 0 or calling turn_off returns the fan to Auto.
    Turning the entity off does NOT power off the AC unit.
    """

    _attr_has_entity_name = True
    _attr_name = "Fan Speed"
    _attr_supported_features = FanEntityFeature.SET_SPEED

    def __init__(self, cw_device: CieloHomeDevice) -> None:
        """Initialize the fan speed entity."""
        self._cw_device = cw_device
        self._has_turbo = cw_device.get_is_turbo_mode()

        # Select the correct lookup tables based on Turbo support
        if self._has_turbo:
            self._pct_steps = PCT_STEPS_TURBO
            self._mode_to_pct = FAN_MODE_TO_PCT_TURBO
            self._pct_to_mode = PCT_TO_FAN_MODE_TURBO
            self._speed_count = 4
        else:
            self._pct_steps = PCT_STEPS_NO_TURBO
            self._mode_to_pct = FAN_MODE_TO_PCT_NO_TURBO
            self._pct_to_mode = PCT_TO_FAN_MODE_NO_TURBO
            self._speed_count = 3

        self._attr_unique_id = f"{cw_device.get_uniqueid()}_fan_speed"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cw_device.get_uniqueid())},
            name=cw_device.get_name(),
            manufacturer="Cielo Home",
        )

    async def async_added_to_hass(self) -> None:
        """Register as a listener so state changes push to HA immediately."""
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

        Turbo mode returns 100% on turbo-capable devices.
        Auto mode returns 0. Low/Medium/High map per the active table.
        """
        if self._has_turbo and self._cw_device.get_turbo() == "on":
            return 100

        fan_mode = self._cw_device.get_fan_mode()
        return self._mode_to_pct.get(fan_mode, 0)

    @property
    def speed_count(self) -> int:
        """Return the number of discrete speed steps."""
        return self._speed_count

    async def async_set_percentage(self, percentage: int) -> None:
        """Set fan speed from a percentage by snapping to the nearest step.

        0% sets fan to Auto.
        100% on a turbo-capable device activates Turbo.
        All other values snap to the nearest step and map to Low/Medium/High.
        """
        if percentage == 0:
            _LOGGER.debug(
                "%s: setting fan to Auto (percentage=0)",
                self._cw_device.get_name(),
            )
            await self._set_fan_auto()
            return

        if self._has_turbo and percentage == 100:
            _LOGGER.debug("%s: activating Turbo", self._cw_device.get_name())
            await self._set_turbo_on()
            return

        nearest_step = min(self._pct_steps, key=lambda s: abs(s - percentage))
        fan_mode = self._pct_to_mode.get(nearest_step)

        _LOGGER.debug(
            "%s: setting fan to %s (requested %s%%, snapped to %s%%)",
            self._cw_device.get_name(),
            fan_mode,
            percentage,
            nearest_step,
        )

        # If Turbo is currently on, turn it off before changing fan speed
        if self._has_turbo and self._cw_device.get_turbo() == "on":
            await self.hass.async_add_executor_job(self._cw_device.send_turbo_off)

        await self.hass.async_add_executor_job(
            self._cw_device.send_fan_mode, fan_mode
        )

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on fan speed control.

        Applies the given percentage if provided, otherwise defaults to Medium.
        """
        if percentage is not None:
            await self.async_set_percentage(percentage)
        else:
            _LOGGER.debug(
                "%s: turn_on with no percentage, defaulting to Medium",
                self._cw_device.get_name(),
            )
            if self._has_turbo and self._cw_device.get_turbo() == "on":
                await self.hass.async_add_executor_job(self._cw_device.send_turbo_off)
            await self.hass.async_add_executor_job(
                self._cw_device.send_fan_mode, FAN_MEDIUM
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Return fan to Auto mode (does NOT power off the AC)."""
        _LOGGER.debug(
            "%s: turn_off called, setting fan to Auto",
            self._cw_device.get_name(),
        )
        await self._set_fan_auto()

    # -- Internal helpers --

    async def _set_fan_auto(self) -> None:
        """Set fan to Auto, turning off Turbo first if active."""
        if self._has_turbo and self._cw_device.get_turbo() == "on":
            await self.hass.async_add_executor_job(self._cw_device.send_turbo_off)
        await self.hass.async_add_executor_job(
            self._cw_device.send_fan_mode, FAN_AUTO
        )

    async def _set_turbo_on(self) -> None:
        """Activate Turbo mode at High fan speed as the base."""
        await self.hass.async_add_executor_job(
            self._cw_device.send_fan_mode, FAN_HIGH
        )
        await self.hass.async_add_executor_job(self._cw_device.send_turbo_on)
