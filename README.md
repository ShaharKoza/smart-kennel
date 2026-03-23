# Smart Kennel

Raspberry Pi sensor hub for monitoring a puppy kennel in real time.
Sensor readings are uploaded to Firebase Realtime Database.

## Hardware

| Sensor | GPIO | Purpose |
|---|---|---|
| DHT22 (AM2302) | 22 | Temperature and humidity |
| LDR (light sensor) | 4 | Light / dark detection |
| KY-038 (sound sensor) | 17 | Sound / bark detection |
| HC-SR501 (PIR) | 23 | Motion detection |

## Firebase paths

| Path | Fields |
|---|---|
| `kennel/dht` | `temperature`, `humidity`, `timestamp` |
| `kennel/light` | `light_detected`, `timestamp` |
| `kennel/sound` | `sound_active`, `bark_detected`, `bark_count_5s`, `timestamp` |
| `kennel/pir` | `motion_detected`, `last_motion`, `seconds_since_motion`, `timestamp` |

## Setup

### 1. Install dependencies

```bash
pip install RPi.GPIO firebase-admin
```

Install the Adafruit DHT library:

```bash
cd Adafruit_Python_DHT
sudo python3 setup.py install
```

### 2. Add Firebase credentials

Place your Firebase service account key at:

```
smart_kennel/firebase_key.json
```

This file is excluded from version control by `.gitignore`. Never commit it.

### 3. Run

```bash
python3 main_sensors.py
```

The script will wait 30 seconds on startup for the HC-SR501 PIR sensor to stabilise, then begin uploading readings.

## Sensor behaviour

### DHT22
- Reads every 10 seconds in a background thread
- Bad frames are discarded if the value is outside `0–40 °C` / `10–99 %rh`
- Readings that jump more than `5 °C` or `10 %rh` from the previous value are also discarded
- The last valid reading is kept in Firebase until a new valid one arrives

### Sound
- GPIO polled every 2 ms with a 50 ms debounce
- `sound_active` — any trigger in the last 5 seconds (catches whining)
- `bark_detected` — 3 or more triggers in the last 5 seconds (sustained noise burst)

### PIR
- HC-SR501 requires 30 seconds to stabilise after power-on — the script waits automatically
- `motion_detected` stays `True` for 30 seconds after the last trigger, so brief puppy movements (stretch, roll, shift) do not prematurely clear the flag
- `seconds_since_motion` can be used to infer sleep: large values mean the puppy has been still for a while

### Light
- Digital read every 2 seconds
- `light_detected: true` when the sensor sees light, `false` when dark

## TEST scripts

The `TEST/` directory contains standalone scripts for validating each sensor individually:

```
TEST/dht_sensor.py
TEST/light_sensor.py
TEST/sound_sensor.py
TEST/pir_sensor.py
TEST/led_test.py
```
