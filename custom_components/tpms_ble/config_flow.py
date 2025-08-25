"""Config flow for TPMS BLE integration."""
from __future__ import annotations

import logging
import re
from typing import Any

from .tpms_parser import TPMSBluetoothDeviceData as DeviceData
import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_ADDRESS, CONF_MAC
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class TPMSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for TPMS."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: DeviceData | None = None
        self._discovered_devices: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        device = DeviceData()
        if not device.supported(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery_info = discovery_info
        self._discovered_device = device
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        assert self._discovered_device is not None
        device = self._discovered_device
        assert self._discovery_info is not None
        discovery_info = self._discovery_info
        title = device.title or device.get_device_name() or discovery_info.name
        if user_input is not None:
            return self.async_create_entry(title=title, data={})

        self._set_confirm_only()
        placeholders = {"name": title}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders=placeholders
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device or enter MAC manually."""
        if user_input is not None:
            if CONF_ADDRESS in user_input:
                address = user_input[CONF_ADDRESS]
                title = self._discovered_devices[address]
            elif CONF_MAC in user_input:
                # Normalize MAC address format
                mac_input = user_input[CONF_MAC].strip().upper()
                # Handle different separators: AA:BB:CC:DD:EE:FF, AA-BB-CC-DD-EE-FF, AABBCCDDEEFF
                mac_input = mac_input.replace("-", ":").replace(" ", "")
                if len(mac_input) == 12 and ":" not in mac_input:
                    # Convert AABBCCDDEEFF to AA:BB:CC:DD:EE:FF
                    address = ":".join([mac_input[i:i+2] for i in range(0, 12, 2)])
                else:
                    address = mac_input
                    
                # Validate MAC address format
                if not re.match(r'^([0-9A-F]{2}[:]){5}([0-9A-F]{2})$', address):
                    _LOGGER.error("Invalid MAC address format: %s", user_input[CONF_MAC])
                    return self.async_abort(reason="invalid_mac_format")
                    
                title = f"TPMS {address[-5:].replace(':', '')}"
            else:
                return self.async_abort(reason="invalid_input")
                
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            
            _LOGGER.info("Adding TPMS device: %s (%s)", address, title)
            return self.async_create_entry(title=title, data={})

        # Scan for discovered devices
        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            device = DeviceData()
            if device.supported(discovery_info):
                device_name = device.title or device.get_device_name() or discovery_info.name or f"TPMS {address[-5:].replace(':', '')}"
                self._discovered_devices[address] = device_name
                _LOGGER.info("Discovered TPMS device: %s (%s)", address, device_name)

        # Create schema with discovered devices and manual MAC entry option
        schema_dict = {}
        if self._discovered_devices:
            schema_dict[vol.Optional(CONF_ADDRESS)] = vol.In(self._discovered_devices)
        
        schema_dict[vol.Optional(CONF_MAC)] = vol.All(str, vol.Length(min=12, max=17))

        # If no devices found, require manual entry
        if not self._discovered_devices:
            schema_dict = {vol.Required(CONF_MAC): vol.All(str, vol.Length(min=12, max=17))}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "manual_mac_example": "AA:BB:CC:DD:EE:FF"
            }
        )
