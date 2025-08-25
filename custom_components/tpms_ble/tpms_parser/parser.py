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
    
    def supported(self, service_info) -> bool:
        """Check if device is a supported TPMS sensor."""
        manufacturer_data = service_info.manufacturer_data
        service_uuids = service_info.service_uuids
        
        # TomTom TPMS with FBB0 service UUID and manufacturer ID 256
        if "0000fbb0-0000-1000-8000-00805f9b34fb" in service_uuids:
            if manufacturer_data and 256 in manufacturer_data:
                _LOGGER.debug("TomTom TPMS detected by FBB0 service UUID + manufacturer 256: %s", service_info.address)
                return True
                
        # Type B (service UUID based)
        if "000027a5-0000-1000-8000-00805f9b34fb" in service_uuids:
            _LOGGER.debug("TPMS Type B detected by service UUID: %s", service_info.address)
            return True
            
        # Type A (manufacturer ID 256 based, without FBB0)
        if manufacturer_data and 256 in manufacturer_data:
            _LOGGER.debug("TPMS Type A detected by manufacturer ID 256: %s", service_info.address)
            return True
        
        _LOGGER.debug("Device not recognized as supported TPMS: %s (name: %s, manufacturer_data: %s, service_uuids: %s)", 
                     service_info.address, service_info.name, 
                     list(manufacturer_data.keys()) if manufacturer_data else "None",
                     list(service_uuids))
        return False

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
        elif company_id == 256 and "0000fbb0-0000-1000-8000-00805f9b34fb" in service_info.service_uuids:
            # TomTom TPMS with FBB0 service UUID and manufacturer ID 256 (0x0100)
            self._process_tpms_tomtom(address, local_name, mfr_data, company_id)
        elif company_id == 256:
            self._process_tpms_a(address, local_name, mfr_data)
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

    def _process_tpms_tomtom(self, address: str, local_name: str, data: bytes, company_id: int) -> None:
        """Parser for TomTom TPMS sensors with FBB0 service UUID and manufacturer_id 256 (0x0100).
        
        Data format (18 bytes):
        bytes 0-1:   0100 Manufacturer (TomTom)
        byte 2:      XX   Sensor Number (80:1, 81:2, 82:3, 83:4, ...)
        bytes 3-4:   EACA Address Prefix
        bytes 5-7:   XXXXXX Sensor Address
        bytes 8-11:  XXXXXXXX Pressure (kPa, little-endian)
        bytes 12-15: XXXXXXXX Temperature (Celsius, little-endian)
        byte 16:     XX Battery Percentage
        byte 17:     XX Alarm Flag (00: OK, 01: No Pressure)
        """
        _LOGGER.debug("Parsing TomTom TPMS sensor: %s", data.hex())
        msg_length = len(data)
        if msg_length != 18:
            _LOGGER.error("TomTom TPMS data length should be 18, got %d", msg_length)
            return
            
        try:
            # Extract sensor number for identification
            sensor_number = data[2]
            sensor_id = sensor_number - 0x80 + 1  # 0x80=1, 0x81=2, etc.
            
            # Extract sensor address (bytes 5-7)
            sensor_addr = data[5:8].hex().upper()
            
            # Extract pressure (bytes 8-9) - auto-detect endian format
            pressure_big = int.from_bytes(data[8:10], byteorder='big', signed=False)
            pressure_little = int.from_bytes(data[8:10], byteorder='little', signed=False)
            
            # Heuristic: choose endian format that gives realistic pressure (1.5-7.0 bar)
            pressure_bar_big = (pressure_big / 100) / 100  # kPa to bar
            pressure_bar_little = (pressure_little / 100) / 100  # kPa to bar
            
            if 1.5 <= pressure_bar_big <= 7.0:
                pressure_raw = pressure_big
                pressure_kpa = pressure_raw / 100
                pressure_bar = pressure_bar_big
                endian_used = "big"
            elif 1.5 <= pressure_bar_little <= 7.0:
                pressure_raw = pressure_little
                pressure_kpa = pressure_raw / 100
                pressure_bar = pressure_bar_little
                endian_used = "little"
            else:
                # Fallback to big-endian if both are out of range
                pressure_raw = pressure_big
                pressure_kpa = pressure_raw / 100
                pressure_bar = pressure_bar_big
                endian_used = "big (fallback)"
            
            # Extract temperature (bytes 12-13, LITTLE-endian, in Celsius/100)
            temp_raw = int.from_bytes(data[12:14], byteorder='little', signed=False)
            temperature = temp_raw / 100  # Convert to Celsius
            
            # Extract battery (byte 16)
            battery = data[16] if data[16] <= 100 else 100
            
            # Extract alarm flag (byte 17)
            alarm_flag = data[17]
            alarm = alarm_flag != 0
            
            _LOGGER.info("TomTom TPMS parsed - Sensor %d (%s): pressure=%.2f bar (%.0f kPa), temp=%.1f°C, battery=%d%%, alarm=%s [pressure: %s-endian]", 
                        sensor_id, sensor_addr, pressure_bar, pressure_kpa, temperature, battery, alarm, endian_used)
            
            self._update_sensors(address, pressure_bar, battery, temperature, alarm)
            
        except Exception as e:
            _LOGGER.error("Error parsing TomTom TPMS data: %s, data: %s", e, data.hex())
            # Fallback: create sensors with zero values to show device exists
            self._update_sensors(address, 0.0, 0, 0, False)

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
