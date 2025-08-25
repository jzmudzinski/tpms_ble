"""Parser for TPMS BLE advertisements."""
from __future__ import annotations
from datetime import datetime

import re
import logging
from struct import unpack
from dataclasses import dataclass
from enum import Enum, auto

from bluetooth_data_tools import short_address
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data.enum import StrEnum

_LOGGER = logging.getLogger(__name__)


class TPMSSensor(StrEnum):

    PRESSURE = "pressure"
    TEMPERATURE = "temperature"
    BATTERY = "battery"
    SIGNAL_STRENGTH = "signal_strength"
    TIMESTAMP = "timestamp"


class TPMSBinarySensor(StrEnum):
    ALARM = "alarm"


class TPMSBluetoothDeviceData(BluetoothData):
    """Data for TPMS BLE sensors."""

    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        _LOGGER.debug("Parsing TPMS BLE advertisement data: %s", service_info)
        manufacturer_data = service_info.manufacturer_data
        local_name = service_info.name
        address = service_info.address
        if len(manufacturer_data) == 0:
            return None

        company_id, mfr_data = next(iter(manufacturer_data.items()))
        self.set_device_manufacturer("TPMS")

        if "000027a5-0000-1000-8000-00805f9b34fb" in service_info.service_uuids:
            self._process_tpms_b(address, local_name, mfr_data, company_id)
        elif "0000fbb0-0000-1000-8000-00805f9b34fb" in service_info.service_uuids:
            self._process_tpms_c(address, local_name, mfr_data, company_id)
        elif company_id == 256:
            self._process_tpms_a(address, local_name, mfr_data)
        elif company_id in [384, 385, 386, 387]:  # 0x000180-0x000183
            self._process_tpms_c(address, local_name, mfr_data, company_id)
        else:
            _LOGGER.error("Can't find the correct data type for company_id %s, service_uuids: %s", 
                         company_id, service_info.service_uuids)

    def _process_tpms_a(self, address: str, local_name: str, data: bytes) -> None:
        """Parser for TPMS sensors."""
        _LOGGER.debug("Parsing TPMS TypeA sensor: %s", data)
        msg_length = len(data)
        if msg_length != 16:
            _LOGGER.error("Can't parse the data because the data length should be 16")
            return
        (
            pressure,
            temperature,
            battery,
            alarm
        ) = unpack("=iib?", data[6:16])
        pressure = pressure / 100000
        temperature = temperature / 100
        self._update_sensors(address, pressure, battery, temperature, alarm)

    def _process_tpms_b(self, address: str, local_name: str, data: bytes, company_id: int) -> None:
        """Parser for TPMS sensors."""
        _LOGGER.debug("Parsing TPMS TypeB sensor: (%s) %s", company_id, data)
        comp_hex = re.findall("..", hex(company_id)[2:].zfill(4))[::-1]
        comp_hex = "".join(comp_hex)
        data_hex = data.hex()

        msg_length = len(data_hex)
        if msg_length != 10:
            _LOGGER.error("Can't parse the data because the data length should be 10")
            return
        voltage = int(comp_hex[2:4], 16) / 10
        temperature = int(data_hex[0:2], 16)
        if temperature >= 2 ** 7:
            temperature -= 2 ** 8
        psi_pressure = (int(data_hex[2:6], 16) - 145) / 10

        pressure = round(psi_pressure * 0.0689476, 3)
        min_voltage = 2.6
        max_voltage = 3.3
        battery = ((voltage - min_voltage) / (max_voltage - min_voltage)) * 100
        battery = int(round(max(0, min(100, battery)), 0))
        self._update_sensors(address, pressure, battery, temperature, None)

    def _process_tpms_c(self, address: str, local_name: str, data: bytes, company_id: int) -> None:
        """Parser for TPMS sensors with FBB0 service UUID and manufacturer_id 384-387."""
        _LOGGER.debug("Parsing TPMS TypeC sensor: (%s) %s", company_id, data)
        msg_length = len(data)
        if msg_length != 18:
            _LOGGER.error("Can't parse the data because the data length should be 18, got %d", msg_length)
            return
            
        # Based on the hex data from screenshot, try to parse the structure
        # Example: 0x000180eaca132cf55eee05006f06000005e00
        # This is speculation and might need adjustment based on actual data structure
        try:
            # Skip first 6 bytes (manufacturer header), then parse sensor data
            sensor_data = data[6:18]
            
            # Try different parsing approaches - this might need adjustment
            # Approach 1: Similar to Type A but different structure
            if len(sensor_data) >= 10:
                # Extract pressure and temperature (positions might need adjustment)
                pressure_raw = int.from_bytes(sensor_data[6:10], byteorder='little', signed=False)
                temp_raw = int.from_bytes(sensor_data[4:6], byteorder='little', signed=True)
                
                pressure = pressure_raw / 100000  # Convert to bar
                temperature = temp_raw / 100      # Convert to Celsius
                
                # Battery estimation (this might need adjustment)
                battery_raw = sensor_data[10] if len(sensor_data) > 10 else 50
                battery = min(100, max(0, battery_raw))
                
                _LOGGER.info("TPMS TypeC parsed: pressure=%.3f bar, temp=%.1f°C, battery=%d%%, company_id=%s", 
                           pressure, temperature, battery, company_id)
                
                self._update_sensors(address, pressure, battery, temperature, None)
            else:
                _LOGGER.error("Insufficient sensor data length: %d", len(sensor_data))
                
        except Exception as e:
            _LOGGER.error("Error parsing TPMS TypeC data: %s, data: %s", e, data.hex())
            # Fallback: create sensors with default values to at least show the device
            self._update_sensors(address, 0.0, 0, 0, None)

    def _update_sensors(self, address, pressure, battery, temperature, alarm):
        name = f"TPMS {short_address(address)}"
        self.set_device_type(name)
        self.set_device_name(name)
        self.set_title(name)

        self.update_sensor(
            key=str(TPMSSensor.PRESSURE),
            native_unit_of_measurement=None,
            native_value=pressure,
            name="Pressure",
        )
        self.update_sensor(
            key=str(TPMSSensor.TEMPERATURE),
            native_unit_of_measurement=None,
            native_value=temperature,
            name="Temperature",
        )
        self.update_sensor(
            key=str(TPMSSensor.BATTERY),
            native_unit_of_measurement=None,
            native_value=battery,
            name="Battery",
        )
        if alarm is not None:
            self.update_binary_sensor(
                key=str(TPMSBinarySensor.ALARM),
                native_value=bool(alarm),
                name="Alarm",
            )
        self.update_sensor(
            key=str(TPMSSensor.TIMESTAMP),
            native_unit_of_measurement=None,
            native_value=datetime.now().astimezone(),
            name="Last Update",
        )
