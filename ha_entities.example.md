# HA Entities Reference

This file is searched by Jarvis when you ask about devices.
Copy to `ha_entities.md` and populate with your own entities,
or run the onboarding script to auto-generate it from your HA instance.

Format: `entity_id — Description (integration/type)`

## Example entries

sensor.living_room_temperature — Living room temperature (Zigbee THS)
sensor.outdoor_temperature — Outdoor temperature (Zigbee THS)
sensor.attic_temperature — Attic temperature (Zigbee THS)
switch.spa_pool — Spa pool power switch (Tasmota)
switch.garage_door — Garage door relay (ESPHome)
sensor.energy_today — Today's energy usage (kWh)
sensor.solar_power — Current solar output (W)
binary_sensor.front_door — Front door contact sensor (Zigbee)
climate.lounge_aircon — Lounge air conditioner (SmartIR)
