# PuppyCare — Raspberry Pi Setup Guide

End-to-end instructions for getting `main_sensors.py` and `camera_stream.sh`
running on a fresh Raspberry Pi.

Expected total time: **~30 minutes** for the sensor unit, **+10 minutes** for
the camera service.

---

## 1. Hardware Bill of Materials

| Component                 | Model                | Where it connects   |
| ------------------------- | -------------------- | ------------------- |
| Single-board computer     | Raspberry Pi 4 / 5   | —                   |
| MicroSD card              | 32 GB Class 10+      | —                   |
| Power supply              | Official 5V/3A USB-C | —                   |
| Temperature + humidity    | DHT22 / AM2302       | GPIO 22 (data)      |
| Sound (microphone)        | KY-038 module        | GPIO 17 (DO pin)    |
| Motion (PIR)              | HC-SR501             | GPIO 27 (OUT)       |
| Ambient light             | LDR digital module   | GPIO 4 (DO pin)     |
| Camera (optional)         | USB webcam OR Pi Cam | USB port OR CSI cable |

Power rails: every sensor's `VCC` to 3.3V (pin 1) — **do not** use 5V; the Pi
GPIO inputs are not 5V-tolerant. Every `GND` to any ground pin (e.g. pin 6).

---

## 2. Raspberry Pi OS — fresh install

1. Flash **Raspberry Pi OS (Bookworm, 64-bit)** to the SD card using
   Raspberry Pi Imager.
2. Click the gear icon before writing and pre-configure:
   - Hostname: `raspberrypi` (the iOS app resolves `raspberrypi.local` via mDNS)
   - SSH: enabled, password authentication
   - Wi-Fi credentials + country
   - User account: name + password (used in the rest of these steps)
3. Boot, wait ~60 seconds for first-boot setup, then SSH in:
   ```bash
   ssh <username>@raspberrypi.local
   ```

---

## 3. System packages

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y \
    python3-pip python3-venv \
    cmake libjpeg62-turbo-dev build-essential git \
    v4l-utils libcamera-apps
```

---

## 4. Sensor station — `main_sensors.py`

### 4a. Copy the script

From your laptop:
```bash
scp pi/main_sensors.py <username>@raspberrypi.local:~/main_sensors.py
```

### 4b. Python dependencies

```bash
cd ~
python3 -m venv puppycare-venv
source puppycare-venv/bin/activate
pip install adafruit-circuitpython-dht firebase-admin RPi.GPIO
```

### 4c. Firebase service-account key

1. Go to your Firebase Console → Project Settings → Service Accounts.
2. Click **Generate new private key** → download the JSON file.
3. Copy it to the Pi:
   ```bash
   scp puppycare-firebase-key.json <username>@raspberrypi.local:~/
   ```

### 4d. Environment configuration

Create `~/.puppycare.env`:
```bash
nano ~/.puppycare.env
```
Paste (replace with your Firebase URL):
```
PUPPYCARE_FIREBASE_KEY=/home/<username>/puppycare-firebase-key.json
PUPPYCARE_FIREBASE_DB_URL=https://your-project-id.firebaseio.com
PUPPYCARE_CAMERA_HOST=raspberrypi.local
PUPPYCARE_CAMERA_PORT=8081
```

### 4e. Smoke test

```bash
source puppycare-venv/bin/activate
set -a; source ~/.puppycare.env; set +a
python3 main_sensors.py
```

You should see threads start (dht, sound, pir, light, sleep, firebase,
camera) and Firebase writes appearing in `kennel/sensors` within ~10 seconds.

Hit `Ctrl+C` once you've verified it works.

### 4f. Auto-start on boot

Create `/etc/systemd/system/puppycare-sensors.service`:
```ini
[Unit]
Description=PuppyCare Sensor Station
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<username>
EnvironmentFile=/home/<username>/.puppycare.env
ExecStart=/home/<username>/puppycare-venv/bin/python /home/<username>/main_sensors.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now puppycare-sensors.service
systemctl status puppycare-sensors.service       # confirm it's active
journalctl -u puppycare-sensors.service -f       # live logs
```

---

## 5. Camera service — `camera_stream.sh`

### 5a. Build mjpg-streamer

```bash
cd ~
git clone https://github.com/jacksonliam/mjpg-streamer.git
cd mjpg-streamer/mjpg-streamer-experimental
make
sudo make install
```

### 5b. Verify the camera is visible

| If you have…             | Run this                          | Expected output                        |
| ------------------------ | --------------------------------- | -------------------------------------- |
| **USB webcam**           | `lsusb`                           | Lists the webcam by name               |
| **Pi Camera Module**     | `libcamera-hello --list-cameras`  | Lists the IMX-### sensor               |
| **Either, via v4l2**     | `v4l2-ctl --list-devices`         | At least one `/dev/video*` node        |

If you have a Pi Camera Module on Bookworm and no `/dev/video*` shows up,
load the v4l2 bridge once-and-for-all:
```bash
echo 'bcm2835-v4l2' | sudo tee -a /etc/modules
sudo reboot
```

### 5c. Install + auto-start

```bash
sudo cp pi/camera_stream.sh /usr/local/bin/puppycare-camera.sh
sudo chmod +x /usr/local/bin/puppycare-camera.sh
sudo cp pi/puppycare-camera.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now puppycare-camera.service
```

Test from your laptop browser:
```
http://raspberrypi.local:8081/?action=stream     # live MJPEG
http://raspberrypi.local:8081/?action=snapshot   # single JPEG
```

---

## 6. iOS app — connect to the same Firebase project

1. Open the project in Xcode.
2. Drop your `GoogleService-Info.plist` into the `Resources/` folder.
3. Build to a device or simulator.
4. The Dashboard's Pi-offline indicator should clear within 60 seconds once
   the sensor script is running.

---

## 7. Troubleshooting

| Symptom                                       | Likely cause                                                | Fix                                                  |
| --------------------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------- |
| Dashboard shows "Pi offline"                  | `puppycare-sensors.service` not running                     | `sudo systemctl restart puppycare-sensors.service`   |
| Temperature stuck at 0 °C                     | DHT22 wiring                                                | Check VCC=3.3V, GND, DATA=GPIO 22                    |
| Motion tile always "Detected"                 | HC-SR501 warming up                                         | Give it 30–60 s after boot, then re-check            |
| `/dev/video0 not found` in camera log         | Pi Camera Module on Bookworm without v4l2 bridge            | See 5b above                                         |
| `last_successful_write_age_s` keeps growing   | Pi can reach the Wi-Fi router but not Firebase              | Test `curl https://your-project.firebaseio.com/.json` |
| Notifications don't arrive                    | iOS denied permission, or no FCM token                      | Settings → PuppyCare → Notifications → Allow         |

For anything else: `journalctl -u puppycare-sensors.service -n 100` is
usually enough to identify the cause.
