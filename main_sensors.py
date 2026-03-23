#!/usr/bin/env python3
"""
main_sensors.py — Smart Kennel sensor hub
Sensors : DHT22 (temp/humidity), LDR (light), KY-038 (sound), HC-SR501 (PIR)
Firebase: kennel/dht  kennel/light  kennel/sound  kennel/pir
"""

import sys
import time
import threading
from collections import deque

# Ensure log lines appear immediately even when stdout is piped / redirected
sys.stdout.reconfigure(line_buffering=True)

import RPi.GPIO as GPIO
import Adafruit_DHT
import firebase_admin
from firebase_admin import credentials, db

# ── GPIO pins ──────────────────────────────────────────────────────────────────
DHT_PIN   = 22
LIGHT_PIN = 4
SOUND_PIN = 17
PIR_PIN   = 23

# ── Sound / bark detection ─────────────────────────────────────────────────────
SOUND_WINDOW_SEC = 5.0   # sliding window length in seconds
BARK_THRESHOLD   = 3     # triggers inside window → bark_detected = True
SOUND_DEBOUNCE   = 0.05  # ignore re-triggers within 50 ms of each other

# ── DHT22 sanity bounds ────────────────────────────────────────────────────────
DHT_TEMP_MIN   =   0.0   # °C  — below this is implausible for a kennel
DHT_TEMP_MAX   =  40.0   # °C  — above this is implausible for a kennel
DHT_HUM_MIN    =  10.0   # %rh — below this is a bad frame
DHT_HUM_MAX    =  99.0   # %rh — 100 % is a sensor saturation artefact
DHT_TEMP_DELTA =   5.0   # °C  — max plausible change between readings
DHT_HUM_DELTA  =  10.0   # %rh — max plausible change between readings

# ── PIR motion ────────────────────────────────────────────────────────────────
PIR_WARMUP_SEC  = 30     # HC-SR501 stabilisation time after power-on (mandatory)
PIR_HOLDOFF_SEC = 30.0   # stay motion_detected=True for N s after last trigger
                          # 30s suits brief puppy movements (stretch, roll, shift)

# ── Firebase ───────────────────────────────────────────────────────────────────
FIREBASE_KEY = '/home/pi/smart_kennel/firebase_key.json'
FIREBASE_URL = 'https://smart-kennel-8989c-default-rtdb.firebaseio.com'

PATH_DHT   = 'kennel/dht'
PATH_LIGHT = 'kennel/light'
PATH_SOUND = 'kennel/sound'
PATH_PIR   = 'kennel/pir'

# ── Upload intervals (seconds) ─────────────────────────────────────────────────
DHT_INTERVAL   = 10.0
LIGHT_INTERVAL = 2.0
SOUND_INTERVAL = 2.0
PIR_INTERVAL   = 2.0

# ── Shared DHT state (written by background thread) ───────────────────────────
_dht_lock = threading.Lock()
_dht_data = {'temperature': None, 'humidity': None}

# ──────────────────────────────────────────────────────────────────────────────
# Firebase helpers
# ──────────────────────────────────────────────────────────────────────────────

def init_firebase():
    cred = credentials.Certificate(FIREBASE_KEY)
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_URL})


def fb_set(path, payload):
    """Upload a dict to a Firebase Realtime Database path. Logs errors."""
    try:
        db.reference(path).set(payload)
    except Exception as exc:
        print(f"[Firebase] {path}: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# DHT22 — background thread (reads are slow / blocking)
# ──────────────────────────────────────────────────────────────────────────────

def _dht_reader_thread():
    """Reads DHT22 every DHT_INTERVAL seconds and caches the result."""
    while True:
        humidity, temperature = Adafruit_DHT.read_retry(
            Adafruit_DHT.AM2302, DHT_PIN
        )
        with _dht_lock:
            if temperature is None or humidity is None:
                pass  # sensor not ready — keep last valid value, log nothing
            elif not (DHT_TEMP_MIN <= temperature <= DHT_TEMP_MAX
                      and DHT_HUM_MIN <= humidity <= DHT_HUM_MAX):
                print(f"[DHT]   discarded (out of range) — "
                      f"t={temperature}  h={humidity}")
            else:
                prev_t = _dht_data['temperature']
                prev_h = _dht_data['humidity']
                if (prev_t is not None
                        and (abs(temperature - prev_t) > DHT_TEMP_DELTA
                             or abs(humidity - prev_h) > DHT_HUM_DELTA)):
                    print(f"[DHT]   discarded (delta too large) — "
                          f"t={temperature} (was {prev_t})  "
                          f"h={humidity} (was {prev_h})")
                else:
                    _dht_data['temperature'] = round(temperature, 1)
                    _dht_data['humidity']    = round(humidity, 1)
        time.sleep(DHT_INTERVAL)


def upload_dht():
    with _dht_lock:
        temp = _dht_data['temperature']
        hum  = _dht_data['humidity']
    if temp is None or hum is None:
        return  # no valid reading yet
    fb_set(PATH_DHT, {
        'temperature': temp,
        'humidity':    hum,
        'timestamp':   time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    print(f"[DHT]   {temp}°C  {hum}%rh")


# ──────────────────────────────────────────────────────────────────────────────
# Light sensor
# ──────────────────────────────────────────────────────────────────────────────

def upload_light():
    detected = GPIO.input(LIGHT_PIN) == GPIO.HIGH
    fb_set(PATH_LIGHT, {
        'light_detected': detected,
        'timestamp':      time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    print(f"[Light] {'Light' if detected else 'Dark'}")


# ──────────────────────────────────────────────────────────────────────────────
# Sound / bark detection — sliding-window counter
# ──────────────────────────────────────────────────────────────────────────────

class SoundDetector:
    """
    Counts sound triggers inside a sliding time window.
    A 'bark' is declared when the trigger count reaches BARK_THRESHOLD.
    Debounce prevents a single noise burst from flooding the window counter.
    """

    def __init__(self):
        self._window  = deque()          # timestamps of recent triggers
        self._last_trigger = 0.0         # for debounce

    def poll(self, now: float):
        """Call this in the fast main loop (~2 ms cadence)."""
        # --- prune stale timestamps ---
        cutoff = now - SOUND_WINDOW_SEC
        while self._window and self._window[0] < cutoff:
            self._window.popleft()

        # --- check GPIO ---
        if GPIO.input(SOUND_PIN) == GPIO.HIGH:
            if (now - self._last_trigger) >= SOUND_DEBOUNCE:
                self._window.append(now)
                self._last_trigger = now

    @property
    def bark_count(self) -> int:
        return len(self._window)

    @property
    def bark_detected(self) -> bool:
        return self.bark_count >= BARK_THRESHOLD

    def upload(self):
        fb_set(PATH_SOUND, {
            'sound_active':   self.bark_count >= 1,  # any noise — catches whining
            'bark_detected':  self.bark_detected,    # sustained burst (>= threshold)
            'bark_count_5s':  self.bark_count,
            'timestamp':      time.strftime('%Y-%m-%dT%H:%M:%S'),
        })
        print(f"[Sound] sound={self.bark_count >= 1}  bark={self.bark_detected}  count={self.bark_count}")


# ──────────────────────────────────────────────────────────────────────────────
# PIR motion — hold-off window
# ──────────────────────────────────────────────────────────────────────────────

class PIRDetector:
    """
    Reads the PIR GPIO every loop iteration and records the last trigger time.
    motion_detected stays True for PIR_HOLDOFF_SEC after the last HIGH reading,
    preventing single-poll false negatives from prematurely clearing the flag.
    """

    def __init__(self):
        self._last_trigger = 0.0

    def poll(self, now: float):
        """Call this in the main loop (~50 ms cadence is fine)."""
        if GPIO.input(PIR_PIN) == GPIO.HIGH:
            self._last_trigger = now

    @property
    def motion_detected(self) -> bool:
        return (time.time() - self._last_trigger) < PIR_HOLDOFF_SEC

    @property
    def last_motion_ts(self) -> str:
        if self._last_trigger == 0.0:
            return 'never'
        return time.strftime('%Y-%m-%dT%H:%M:%S',
                             time.localtime(self._last_trigger))

    def upload(self):
        secs = (int(time.time() - self._last_trigger)
                if self._last_trigger > 0.0 else None)
        fb_set(PATH_PIR, {
            'motion_detected':      self.motion_detected,
            'last_motion':          self.last_motion_ts,
            'seconds_since_motion': secs,
            'timestamp':            time.strftime('%Y-%m-%dT%H:%M:%S'),
        })
        print(f"[PIR]   motion={self.motion_detected}  last={self.last_motion_ts}  secs_since={secs}")


# ──────────────────────────────────────────────────────────────────────────────
# GPIO setup / teardown
# ──────────────────────────────────────────────────────────────────────────────

def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(LIGHT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(SOUND_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(PIR_PIN,   GPIO.IN, pull_up_down=GPIO.PUD_OFF)  # sensor drives pin; PUD_DOWN fights open-drain clones
    # DHT is handled by Adafruit_DHT library, no GPIO.setup needed


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print("Smart Kennel starting up…")
    print(f"  DHT={DHT_PIN}  LIGHT={LIGHT_PIN}  SOUND={SOUND_PIN}  PIR={PIR_PIN}")

    init_firebase()
    setup_gpio()

    # HC-SR501 must stabilise before it gives reliable readings
    print(f"Waiting {PIR_WARMUP_SEC}s for PIR to stabilise…")
    for remaining in range(PIR_WARMUP_SEC, 0, -5):
        print(f"  {remaining}s…")
        time.sleep(5)
    print("PIR ready.")

    # Start DHT background reader
    t = threading.Thread(target=_dht_reader_thread, daemon=True)
    t.start()

    sound = SoundDetector()
    pir   = PIRDetector()

    # Upload timers
    last_dht   = 0.0
    last_light = 0.0
    last_sound = 0.0
    last_pir   = 0.0

    print("Sensors active. Press Ctrl+C to stop.")

    try:
        while True:
            now = time.time()

            # Fast sensor polls
            sound.poll(now)
            pir.poll(now)

            # Timed uploads
            if now - last_dht >= DHT_INTERVAL:
                upload_dht()
                last_dht = now

            if now - last_light >= LIGHT_INTERVAL:
                upload_light()
                last_light = now

            if now - last_sound >= SOUND_INTERVAL:
                sound.upload()
                last_sound = now

            if now - last_pir >= PIR_INTERVAL:
                pir.upload()
                last_pir = now

            time.sleep(0.002)  # 2 ms — tight enough for sound debounce

    except KeyboardInterrupt:
        print("\nShutting down…")

    finally:
        GPIO.cleanup()
        print("GPIO cleaned up.")


if __name__ == '__main__':
    main()
