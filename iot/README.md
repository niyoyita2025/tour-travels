# Sensor monitoring (IoT)

Boards such as the ESP32 and ESP8266 send environmental readings to the website. Administrators see them
live, set limits, and receive alerts. The code is in the `iot` app; the travel assistant and booking analytics are
in the `ai_assistant` app.

## Setting up a device

1. Sign in as an administrator and open **IoT sensors > Devices > Add a device**.
2. Give it a device ID (lowercase letters, digits and dashes, for example `kitchen-esp32`) and list the measurements it
   must report, for example `temperature,humidity`.
3. Save. The **token** is shown once, on the next page. Copy it into the board's firmware. It is stored only as a
   keyed hash, so it cannot be shown again. If it is lost, use **Replace token**.
4. Set alert limits under **Alert limits** for the device.

Use `iot/firmware/esp32_dht22_example.ino` as the starting point for an ESP32 with a DHT22 sensor.

## Sending readings (HTTP)

```
POST /api/iot/readings/
Authorization: Bearer <device token>
Content-Type: application/json

{"readings": {"temperature": 23.5, "humidity": 58}, "timestamp": "2026-11-28T15:00:00Z"}
```

| Reply | Meaning |
|---|---|
| 201 | Stored. The reply includes how many readings were saved. |
| 400 | The data was refused. The reply says why (for example, a value outside the sensor's range). |
| 401 | The token is missing, wrong, or the device is deactivated. The reason is the same in each case. |
| 413 / 415 | The body is too large, or it is not JSON. |
| 429 | The device has sent more than its allowance (60 requests a minute by default). Retry after 60 seconds. |

`timestamp` is optional. It must be within the last 24 hours and not in the future.

## Measurements

Defined in `iot/metrics.py`. Each has a unit and the range a real sensor can report. Readings outside that range are
refused. To add a sensor type, add one line there.

Temperature, relative humidity, air pressure, carbon dioxide (CO2), fine dust (PM2.5), coarse dust (PM10), air quality
index, volatile organic compounds, and light level.

## MQTT (optional)

A board can publish to `tour/iot/<device-id>/readings` with the same JSON body, plus `"token": "<device token>"`.
Run the listener with a broker available:

```
pip install paho-mqtt
python manage.py iot_mqtt_listen --host your-broker --port 1883
```

## Alerts and offline detection

* An alert opens when a reading goes above the maximum or below the minimum. Repeated readings update the
  open alert instead of creating another, and the alert closes when the reading is back in range.
* A device that sends nothing for 10 minutes is shown as offline (`IOT_OFFLINE_MINUTES`). A measurement that
  stops arriving raises a "readings missing" alert after 15 minutes (`IOT_MISSING_MINUTES`).
* Admin pages show pop-up notices with a sound (after an administrator turns sound on).
* Run `python manage.py iot_check_devices` on a schedule (for example every 5 minutes) so offline devices are
  noticed when no administrator is watching.

## Data protection

* Tokens are never stored; only a keyed HMAC hash is kept. Each device's token can be replaced at any time.
* Device endpoints accept only the token. Browser sessions and CSRF do not apply to them.
* Readings are validated (type, range, size, and timestamp) before they are stored.
* Staff-only pages and configuration changes are logged in the activity log.
* Use HTTPS on the live site. The example firmware skips certificate checks; see the note in the sketch.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `IOT_OFFLINE_MINUTES` | 10 | Silence after which a device is offline |
| `IOT_MISSING_MINUTES` | 15 | Silence after which a measurement is reported missing |
| `IOT_RATE_LIMIT_PER_MINUTE` | 60 | Readings requests allowed per device per minute |
| `IOT_MAX_READINGS_PER_REQUEST` | 20 | Measurements per request |
| `IOT_MAX_PAST_HOURS` | 24 | Oldest timestamp accepted |
