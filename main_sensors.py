#!/usr/bin/env python3
"""
PuppyCare — Raspberry Pi Sensor Station
Production Edition — final cleanup pass

Active sensors:
  DHT22/AM2302  GPIO 22   — temperature + humidity
  KY-038        GPIO 17   — sound / bark detection
  HC-SR501 PIR  GPIO 27   — motion (active HIGH, 5 s warm-up)
  LDR digital   GPIO 4    — ambient light (light/dark)

Firebase paths:
  kennel/sensors    — temperature, humidity, light, motion, sound, sleeping,
                      motion_streak, sound_streak, timestamp
  kennel/sound      — bark detection: bark_detected, bark_count_5s,
                      sustained_sound, sound_active
  kennel/alert      — alert metadata: level, reasons, sleeping (deduplicated)
  kennel/camera     — cache-busted snapshot URL + timestamp
  kennel/diagnostics— internal: dht_errors, last_error, alert_level
  kennel/heartbeat  — monotonic heartbeat so iOS can detect a dead Pi unit

Alert levels (3-tier, aligned end-to-end with iOS):
  normal | warning | critical
"""

import os
import time
import threading
import logging
import sys
from collections import deque
from datetime import datetime

import board
import adafruit_dht
import RPi.GPIO as GPIO
import firebase_admin
from firebase_admin import credentials, db

# ─────────────────────────── logging ──────────────────────────────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("puppycare")

# ─────────────────────────── GPIO pins ────────────────────────────────────────
PIN_DHT   = board.D22   # DHT22/AM2302 — data
PIN_LDR   = 4           # LDR photoresistor digital out — BCM
PIN_SOUND = 17          # KY-038 digital out — BCM
PIN_PIR   = 27          # HC-SR501 PIR — BCM (active HIGH)

# LDR module polarity: HIGH = light, LOW = dark on most cheap modules.
# Flip this if day/night appear inverted in the app.
LDR_ACTIVE_HIGH = True
LDR_DEBOUNCE_SAMPLES = 3
LDR_POLL_INTERVAL    = 0.2

# ─────────────────────────── tuning constants ─────────────────────────────────
DHT_POLL_INTERVAL   = 10      # seconds between DHT reads
DHT_RETRY_ATTEMPTS  = 5       # attempts per poll cycle
DHT_RETRY_DELAY     = 0.5     # seconds between retry attempts
DHT_REINIT_AFTER    = 3       # consecutive failed cycles before reinit
DHT_CACHE_SECONDS   = 60      # carry forward last valid reading for this long

SOUND_WINDOW        = 5.0     # seconds — bark-burst detection window
BARK_THRESHOLD      = 3       # events in SOUND_WINDOW to fire bark_detected
SUSTAINED_THRESHOLD = 1.5     # seconds of continuous sound = sustained_sound
SOUND_POLL          = 0.05    # seconds — sound sampling interval

PIR_WARMUP          = 5       # seconds — HC-SR501 stabilisation delay
PIR_HOLDOFF         = 10      # seconds — ignore re-triggers within this window

SLEEP_QUIET_STILL = 420  # seconds of quiet+still before sleep=True

# HC-SR501 active HIGH (do not enable internal pull-up; the module drives the line).
PIR_ACTIVE_LEVEL = GPIO.HIGH

# ── Streak / combined-alert thresholds ────────────────────────────────────────
# Combined CRITICAL fires when BOTH streaks reach their thresholds simultaneously
# (sustained co-activity for ≥ ~15 s).
COMBINED_MOTION_STREAK = 3
COMBINED_SOUND_STREAK  = 3

# Decay grace: number of empty cycles before we start dropping a streak.
# Prevents one missed PIR sample from breaking a real co-occurrence detection.
STREAK_DECAY_GRACE = 1

# Hysteresis: continuous calm required before alert level can drop.
ALERT_RECOVERY_SECS = {
    "warning":  30,    # 30 s of calm → drop from warning
    "critical": 120,   # 2 min of calm → drop from critical
}

FIREBASE_UPDATE_INTERVAL = 5  # seconds between kennel/sensors pushes

# ── Camera snapshot refresh ──────────────────────────────────────────────────
CAMERA_STREAM_HOST              = os.environ.get("PUPPYCARE_CAMERA_HOST", "raspberrypi.local")
CAMERA_STREAM_PORT              = int(os.environ.get("PUPPYCARE_CAMERA_PORT", "8081"))
CAMERA_EVENT_COOLDOWN_SEC       = 8.0
CAMERA_PERIODIC_SEC             = 30.0
CAMERA_POLL_INTERVAL            = 0.5
CAMERA_HEAVY_MOTION_COUNT       = 3
CAMERA_HEAVY_MOTION_WINDOW_SEC  = 10.0

# ─────────────────────────── Firebase init ────────────────────────────────────
# Both values must come from the environment in production.
# Example .env / systemd EnvironmentFile:
#   PUPPYCARE_FIREBASE_KEY=/home/pi/puppycare-firebase-key.json
#   PUPPYCARE_FIREBASE_DB_URL=https://your-project.firebaseio.com
FIREBASE_KEY_PATH = os.environ.get(
    "PUPPYCARE_FIREBASE_KEY",
    "/home/pi/puppycare-firebase-key.json",
)
FIREBASE_DB_URL = os.environ.get("PUPPYCARE_FIREBASE_DB_URL")

if not FIREBASE_DB_URL:
    log.error("PUPPYCARE_FIREBASE_DB_URL is not set — refusing to start.")
    sys.exit(1)

cred = credentials.Certificate(FIREBASE_KEY_PATH)
firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
db_ref = db.reference("kennel")

# ─────────────────────────── GPIO setup ───────────────────────────────────────
GPIO.setmode(GPIO.BCM)
GPIO.setup(PIN_LDR,   GPIO.IN)
GPIO.setup(PIN_SOUND, GPIO.IN)
GPIO.setup(PIN_PIR,   GPIO.IN)   # HC-SR501 has its own output driver; no pull needed


def safe_gpio_read(pin, default=GPIO.LOW):
    """Read a GPIO pin, returning `default` instead of propagating on error.

    RPi.GPIO can raise RuntimeError if the GPIO context is lost (rare hardware
    glitch, library re-init). On a unit that runs 24/7 an unhandled exception
    here would kill the polling thread silently — that sensor then stays frozen
    until the whole Pi is rebooted, with nothing in the logs. Swallowing the
    error and logging it keeps every other sensor alive and leaves a trail.
    """
    try:
        return GPIO.input(pin)
    except Exception as e:               # noqa: BLE001 — deliberately broad
        log.warning("GPIO read failed on pin %s: %s", pin, e)
        return default

# ─────────────────────────── shared state ─────────────────────────────────────
state_lock = threading.Lock()
state = {
    # DHT22
    "temperature": None,
    "humidity":    None,
    "dht_last_valid_time": 0.0,
    "dht_consecutive_failures": 0,

    # Sensors
    "light":    False,
    "sound":    False,
    "motion":   False,
    "sleeping": False,

    # Bark detection
    "bark_detected":   False,
    "bark_count_5s":   0,
    "sustained_sound": False,

    # Streak counters
    "motion_streak": 0,
    "sound_streak":  0,

    # Diagnostics
    "dht_total_errors": 0,
    "dht_last_error":   "",
}

# ─────────────────────────── DHT22 reader ─────────────────────────────────────

_dht_device = None

def _get_dht_device():
    global _dht_device
    if _dht_device is None:
        _dht_device = adafruit_dht.DHT22(PIN_DHT, use_pulseio=False)
        log.info("DHT22 initialised on %s (use_pulseio=False)", PIN_DHT)
    return _dht_device

def _reinit_dht():
    global _dht_device
    if _dht_device is not None:
        try:
            _dht_device.exit()
        except Exception:
            pass
        _dht_device = None
    time.sleep(1.0)
    _get_dht_device()
    log.info("DHT22 re-initialised after consecutive failures")

def read_dht_robust():
    """Read DHT22 with retries and automatic re-initialisation. Never raises."""
    device = _get_dht_device()

    for attempt in range(DHT_RETRY_ATTEMPTS):
        try:
            temperature = device.temperature
            humidity    = device.humidity

            if (temperature is not None and humidity is not None and
                    -10.0 <= temperature <= 60.0 and
                    0.0   <= humidity    <= 100.0):
                with state_lock:
                    state["dht_consecutive_failures"] = 0
                return float(temperature), float(humidity)

            time.sleep(DHT_RETRY_DELAY)

        except RuntimeError as e:
            err_msg = str(e)
            log.debug("DHT retry %d/%d: %s", attempt + 1, DHT_RETRY_ATTEMPTS, err_msg)
            with state_lock:
                state["dht_total_errors"] += 1
                state["dht_last_error"]    = err_msg
            time.sleep(DHT_RETRY_DELAY)

        except Exception as e:
            log.warning("DHT unexpected error: %s", e)
            with state_lock:
                state["dht_total_errors"] += 1
                state["dht_last_error"]    = str(e)
            _reinit_dht()
            device = _get_dht_device()
            time.sleep(DHT_RETRY_DELAY)

    with state_lock:
        state["dht_consecutive_failures"] += 1
        failures = state["dht_consecutive_failures"]

    log.warning("DHT22: all %d attempts failed (consecutive failed cycles: %d)",
                DHT_RETRY_ATTEMPTS, failures)

    if failures >= DHT_REINIT_AFTER:
        _reinit_dht()
        with state_lock:
            state["dht_consecutive_failures"] = 0

    return None, None

def dht_loop():
    """Background thread: poll DHT22 every DHT_POLL_INTERVAL seconds."""
    _get_dht_device()
    while True:
        temp, hum = read_dht_robust()
        now = time.monotonic()

        with state_lock:
            if temp is not None:
                state["temperature"]        = temp
                state["humidity"]           = hum
                state["dht_last_valid_time"] = now
            else:
                age = now - state["dht_last_valid_time"]
                if age > DHT_CACHE_SECONDS:
                    state["temperature"] = None
                    state["humidity"]    = None
                    log.info("DHT cache expired (%.0f s since last valid read)", age)

        time.sleep(DHT_POLL_INTERVAL)

# ─────────────────────────── Sound / bark detection ───────────────────────────

def sound_loop():
    """Sample KY-038 digital output and update bark_detected / bark_count_5s / sustained_sound."""
    events     = deque()
    sound_on_since = None
    prev = safe_gpio_read(PIN_SOUND)

    while True:
        current = safe_gpio_read(PIN_SOUND)
        now     = time.monotonic()

        # Rising edge — sound detected
        if current == GPIO.HIGH and prev == GPIO.LOW:
            events.append(now)
            if sound_on_since is None:
                sound_on_since = now

        # Falling edge — sound ended
        if current == GPIO.LOW and prev == GPIO.HIGH:
            sound_on_since = None

        cutoff = now - SOUND_WINDOW
        while events and events[0] < cutoff:
            events.popleft()

        bark_count   = len(events)
        bark_hit     = bark_count >= BARK_THRESHOLD
        sound_active = current == GPIO.HIGH
        sustained    = (sound_on_since is not None and
                        (now - sound_on_since) >= SUSTAINED_THRESHOLD)

        with state_lock:
            state["sound"]           = sound_active
            state["bark_detected"]   = bark_hit
            state["bark_count_5s"]   = bark_count
            state["sustained_sound"] = sustained

        prev = current
        time.sleep(SOUND_POLL)

# ─────────────────────────── LDR light ────────────────────────────────────────

def light_loop():
    """Digital LDR module: HIGH = light, LOW = dark (configurable via LDR_ACTIVE_HIGH).
    Debounced over LDR_DEBOUNCE_SAMPLES consecutive samples to avoid flicker
    transients from passing shadows or fluorescent ballast."""
    samples = deque(maxlen=LDR_DEBOUNCE_SAMPLES)
    while True:
        raw   = safe_gpio_read(PIN_LDR)
        is_lit = (raw == GPIO.HIGH) if LDR_ACTIVE_HIGH else (raw == GPIO.LOW)
        samples.append(is_lit)
        if len(samples) == LDR_DEBOUNCE_SAMPLES and len(set(samples)) == 1:
            with state_lock:
                state["light"] = samples[0]
        time.sleep(LDR_POLL_INTERVAL)

# ─────────────────────────── PIR motion ───────────────────────────────────────

def pir_loop():
    """HC-SR501 (active HIGH) with PIR_HOLDOFF cooldown and warm-up delay."""
    log.info("PIR: waiting %d s for HC-SR501 warm-up...", PIR_WARMUP)
    time.sleep(PIR_WARMUP)
    log.info("PIR: ready")

    last_trigger = 0.0

    while True:
        raw    = safe_gpio_read(PIN_PIR)
        motion = (raw == PIR_ACTIVE_LEVEL)
        now    = time.monotonic()

        if motion and (now - last_trigger) >= PIR_HOLDOFF:
            last_trigger = now
            with state_lock:
                state["motion"] = True
        elif not motion:
            with state_lock:
                state["motion"] = False

        time.sleep(0.1)

# ─────────────────────────── Sleep detection ──────────────────────────────────

def sleep_loop():
    """Dog is asleep when kennel has been quiet + still for SLEEP_QUIET_STILL seconds."""
    calm_since = None

    while True:
        with state_lock:
            quiet = not state["sound"]
            still = not state["motion"]

        now = time.monotonic()

        if quiet and still:
            if calm_since is None:
                calm_since = now
            elapsed  = now - calm_since
            sleeping = elapsed >= SLEEP_QUIET_STILL
        else:
            calm_since = None
            sleeping   = False

        with state_lock:
            state["sleeping"] = sleeping

        time.sleep(1.0)

# ─────────────────────────── Alert evaluation ─────────────────────────────────

def max_level(current, candidate):
    order = {"normal": 0, "warning": 1, "critical": 2}
    return candidate if order.get(candidate, 0) > order.get(current, 0) else current


def _compute_raw_level(snap, thresholds, motion_streak, sound_streak):
    """
    Stateless: compute what level the raw sensor data warrants RIGHT NOW.

    Returns (level_str, reasons_list).
    levels:  "normal" | "warning" | "critical"

    Notes:
      • Any bark or sustained sound → critical (matches iOS AlertManager).
      • Motion alone → warning (context only; co-occurrence escalates to critical).
      • Combined motion + sound streaks → critical.
    """
    level   = "normal"
    reasons = []
    temp    = snap.get("temperature")

    # ── Temperature ──────────────────────────────────────────────────────────
    if temp is not None:
        warn_high = thresholds.get("warn_high",     28.0)
        crit_high = thresholds.get("critical_high", 32.0)
        warn_low  = thresholds.get("warn_low",      12.0)
        crit_low  = thresholds.get("critical_low",   8.0)

        if temp > crit_high:
            level = "critical"
            reasons.append(f"Temperature critical: {temp:.1f}°C — act immediately")
        elif temp > warn_high:
            level = max_level(level, "warning")
            reasons.append(f"Temperature high: {temp:.1f}°C")
        elif temp < crit_low:
            level = "critical"
            reasons.append(f"Temperature critical low: {temp:.1f}°C — provide warmth immediately")
        elif temp < warn_low:
            level = max_level(level, "warning")
            reasons.append(f"Temperature low: {temp:.1f}°C")
    # If temp is None (DHT failure) we never raise the alert level — sensor
    # errors live in kennel/diagnostics, never in user-facing reasons.

    # ── Combined: sustained motion + sustained sound → critical ──────────────
    if motion_streak >= COMBINED_MOTION_STREAK and sound_streak >= COMBINED_SOUND_STREAK:
        level = "critical"
        reasons.append(
            f"Combined: sustained motion ({motion_streak}×) + "
            f"sound ({sound_streak}×) — check on your dog immediately"
        )
    else:
        # ── Sound (standalone) — any bark fires critical ─────────────────────
        if snap.get("sustained_sound"):
            level = "critical"
            reasons.append("Sustained barking detected")
        elif snap.get("bark_detected"):
            level = "critical"
            reasons.append(f"Barking detected ({snap.get('bark_count_5s', 0)} barks in 5 s)")

        # ── Motion (standalone) — warning ────────────────────────────────────
        if snap.get("motion"):
            level = max_level(level, "warning")
            reasons.append("Motion detected in kennel")

    if not reasons:
        reasons.append("All clear")

    return level, reasons


class AlertStateMachine:
    """
    Manages alert level with hysteresis.

    Escalation: immediate (raw_level > current_level).
    De-escalation: requires ALERT_RECOVERY_SECS of continuous calm.
    """

    LEVELS     = ["normal", "warning", "critical"]
    LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}

    def __init__(self):
        self.current_level         = "normal"
        self.entered_at            = time.monotonic()
        self.recovery_started_at   = None
        self._last_written_level   = None
        self._last_written_reasons = None

    def transition(self, raw_level: str, reasons: list) -> tuple:
        now          = time.monotonic()
        raw_rank     = self.LEVEL_RANK.get(raw_level, 0)
        current_rank = self.LEVEL_RANK.get(self.current_level, 0)

        if raw_rank > current_rank:
            self.current_level       = raw_level
            self.entered_at          = now
            self.recovery_started_at = None

        elif raw_rank < current_rank:
            if self.recovery_started_at is None:
                self.recovery_started_at = now
            elapsed = now - self.recovery_started_at
            needed  = ALERT_RECOVERY_SECS.get(self.current_level, 30)

            if elapsed >= needed:
                self.current_level       = raw_level
                self.entered_at          = now
                self.recovery_started_at = None
        else:
            self.recovery_started_at = None

        changed = (
            self.current_level != self._last_written_level or
            reasons            != self._last_written_reasons
        )
        if changed:
            self._last_written_level   = self.current_level
            self._last_written_reasons = list(reasons)

        return changed, self.current_level, reasons

# ─────────────────────────── Firebase writer ──────────────────────────────────

_thresholds = {
    "warn_high":     28.0,
    "critical_high": 32.0,
    "warn_low":      12.0,
    "critical_low":   8.0,
}

def _load_remote_thresholds():
    """Pull threshold overrides from kennel/config (if written by the iOS app)."""
    global _thresholds
    try:
        cfg = db_ref.child("config").get()
        if isinstance(cfg, dict):
            for k in ("warn_high", "critical_high", "warn_low", "critical_low"):
                if k in cfg:
                    _thresholds[k] = float(cfg[k])
            log.info("Thresholds loaded from Firebase: %s", _thresholds)
    except Exception as e:
        log.warning("Could not load remote thresholds: %s", e)

# ── Network-resilient write helper ────────────────────────────────────────────
#
# Without retries a transient network blip drops a whole cycle of sensor data,
# and the iOS app sees the heartbeat go stale. With a tight retry loop we keep
# the dashboard live even on flaky home Wi-Fi. We also stamp every successful
# write into a module-level monotonic timestamp that gets surfaced in
# kennel/diagnostics.last_successful_write_age_s — so the iOS app can warn the
# user when the Pi is up but its uplink is broken (heartbeat alone can't
# distinguish those two cases).

_last_successful_write_monotonic = 0.0
_failed_write_count_since_success = 0

def _safe_write(operation, label: str, max_attempts: int = 3):
    """Run a Firebase write with exponential backoff.

    `operation` is a zero-arg callable; failures are logged and counted but
    never re-raised — the writer loop must keep running even when the network
    drops entirely.
    """
    global _last_successful_write_monotonic, _failed_write_count_since_success
    delay = 0.5
    for attempt in range(1, max_attempts + 1):
        try:
            operation()
            _last_successful_write_monotonic = time.monotonic()
            _failed_write_count_since_success = 0
            return True
        except Exception as e:
            if attempt < max_attempts:
                log.warning("Firebase %s failed (attempt %d/%d): %s — retrying in %.1fs",
                            label, attempt, max_attempts, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 4.0)
            else:
                log.error("Firebase %s failed after %d attempts: %s",
                          label, max_attempts, e)
                _failed_write_count_since_success += 1
    return False


def firebase_writer_loop():
    """Main Firebase push loop."""
    _load_remote_thresholds()

    _alert_sm      = AlertStateMachine()
    _motion_streak = 0
    _sound_streak  = 0
    _motion_quiet_cycles = 0
    _sound_quiet_cycles  = 0

    while True:
        with state_lock:
            snap = dict(state)

        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        # ── Streak counters with grace before decay ──────────────────────────
        if snap["motion"]:
            _motion_streak       = min(_motion_streak + 1, 99)
            _motion_quiet_cycles = 0
        else:
            _motion_quiet_cycles += 1
            if _motion_quiet_cycles > STREAK_DECAY_GRACE:
                _motion_streak = max(_motion_streak - 1, 0)

        sound_active = snap["bark_detected"] or snap["sustained_sound"]
        if sound_active:
            _sound_streak       = min(_sound_streak + 1, 99)
            _sound_quiet_cycles = 0
        else:
            _sound_quiet_cycles += 1
            if _sound_quiet_cycles > STREAK_DECAY_GRACE:
                _sound_streak = max(_sound_streak - 1, 0)

        # ── kennel/sensors ───────────────────────────────────────────────────
        sensors_payload = {
            "motion":        snap["motion"],
            "sleeping":      snap["sleeping"],
            "light":         "light" if snap["light"] else "dark",
            "motion_streak": _motion_streak,
            "sound_streak":  _sound_streak,
            "timestamp":     now_iso,
        }
        if snap["temperature"] is not None:
            sensors_payload["temperature"] = snap["temperature"]
            sensors_payload["humidity"]    = snap["humidity"]

        # ── kennel/sound (single source of truth for soundActive) ────────────
        sound_payload = {
            "sound_active":    snap["sound"],
            "bark_detected":   snap["bark_detected"],
            "bark_count_5s":   snap["bark_count_5s"],
            "sustained_sound": snap["sustained_sound"],
            "timestamp":       now_iso,
        }

        # ── Alert level ──────────────────────────────────────────────────────
        raw_level, reasons = _compute_raw_level(snap, _thresholds, _motion_streak, _sound_streak)
        alert_changed, level, reasons = _alert_sm.transition(raw_level, reasons)

        # ── kennel/diagnostics (internal) ────────────────────────────────────
        # last_successful_write_age_s lets the iOS app distinguish two failure
        # modes that look the same on the dashboard:
        #   (a) Pi is dead          → heartbeat stale + diagnostics stale
        #   (b) Pi up, no uplink    → heartbeat stale + last_successful_write
        #                             grows steadily but diagnostics keeps the
        #                             count of consecutive failures
        last_write_age = (int(time.monotonic() - _last_successful_write_monotonic)
                          if _last_successful_write_monotonic > 0 else -1)
        diag_payload = {
            "dht_total_errors":         snap["dht_total_errors"],
            "dht_consecutive_failures": snap["dht_consecutive_failures"],
            "dht_last_error":           snap["dht_last_error"],
            "dht_last_valid_age_s":     int(time.monotonic() - snap["dht_last_valid_time"])
                                        if snap["dht_last_valid_time"] > 0 else -1,
            "alert_level":              level,
            "motion_streak":            _motion_streak,
            "sound_streak":             _sound_streak,
            "last_successful_write_age_s":   last_write_age,
            "failed_writes_since_success":   _failed_write_count_since_success,
        }

        # Each path retries independently — a transient failure on /sound
        # must not block the heartbeat update, otherwise the iOS "Pi offline"
        # banner would flap every time one sub-write blinks.
        _safe_write(lambda: db_ref.child("sensors").update(sensors_payload),       "sensors")
        _safe_write(lambda: db_ref.child("sound").set(sound_payload),              "sound")
        _safe_write(lambda: db_ref.child("diagnostics").set(diag_payload),         "diagnostics")
        _safe_write(lambda: db_ref.child("heartbeat").set({
            "timestamp": now_iso,
            "epoch_ms":  int(time.time() * 1000),
        }), "heartbeat")

        if alert_changed:
            alert_payload = {
                "level":     level,
                "reasons":   reasons,
                "sleeping":  snap["sleeping"],
                "timestamp": now_iso,
            }
            if _safe_write(lambda: db_ref.child("alert").set(alert_payload), "alert"):
                log.info("Alert → %s: %s", level, reasons)

        time.sleep(FIREBASE_UPDATE_INTERVAL)

# ─────────────────────────── Camera snapshot trigger ─────────────────────────

def _publish_camera(reason: str):
    """Write a fresh, cache-busted snapshot URL to kennel/camera."""
    ts  = int(time.time() * 1000)
    url = (f"http://{CAMERA_STREAM_HOST}:{CAMERA_STREAM_PORT}"
           f"/?action=snapshot&t={ts}")
    try:
        db_ref.child("camera").set({
            "url":       url,
            "reason":    reason,
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        log.info("Camera snapshot refreshed (%s)", reason)
    except Exception as e:
        log.error("Camera publish failed: %s", e)

def camera_trigger_loop():
    """Refresh kennel/camera.url on bark, heavy motion, or periodic heartbeat."""
    prev_bark     = False
    prev_motion   = False
    motion_events = deque()
    last_event_ts = 0.0

    _publish_camera("startup")
    last_periodic = time.monotonic()

    while True:
        with state_lock:
            bark_now   = state["bark_detected"]
            motion_now = state["motion"]

        now = time.monotonic()

        bark_edge   = bark_now   and not prev_bark
        motion_edge = motion_now and not prev_motion
        prev_bark   = bark_now
        prev_motion = motion_now

        if motion_edge:
            motion_events.append(now)
        cutoff = now - CAMERA_HEAVY_MOTION_WINDOW_SEC
        while motion_events and motion_events[0] < cutoff:
            motion_events.popleft()
        heavy_motion = len(motion_events) >= CAMERA_HEAVY_MOTION_COUNT

        if bark_edge and (now - last_event_ts) >= CAMERA_EVENT_COOLDOWN_SEC:
            _publish_camera("bark")
            last_event_ts = now
        elif heavy_motion and (now - last_event_ts) >= CAMERA_EVENT_COOLDOWN_SEC:
            _publish_camera("heavy_motion")
            last_event_ts = now
            motion_events.clear()

        if (now - last_periodic) >= CAMERA_PERIODIC_SEC:
            _publish_camera("periodic")
            last_periodic = now

        time.sleep(CAMERA_POLL_INTERVAL)

# ─────────────────────────── main ─────────────────────────────────────────────

def resilient(loop_fn):
    """Wrap a sensor-loop target so an unhandled exception never permanently
    kills the thread. On a 24/7 unit, a single transient error (GPIO glitch,
    lock contention, memory pressure) inside any loop would otherwise leave
    that sensor frozen until a full reboot, with no trace. This catches it,
    logs it, and restarts the loop after a short pause."""
    def runner():
        while True:
            try:
                loop_fn()
            except Exception as e:                       # noqa: BLE001 — deliberate
                log.error("%s crashed: %s — restarting in 2 s", loop_fn.__name__, e)
                time.sleep(2)
    return runner

def main():
    log.info("PuppyCare sensor station starting — Production Edition")

    threads = [
        threading.Thread(target=resilient(dht_loop),             name="dht",      daemon=True),
        threading.Thread(target=resilient(sound_loop),           name="sound",    daemon=True),
        threading.Thread(target=resilient(pir_loop),             name="pir",      daemon=True),
        threading.Thread(target=resilient(light_loop),           name="light",    daemon=True),
        threading.Thread(target=resilient(sleep_loop),           name="sleep",    daemon=True),
        threading.Thread(target=resilient(firebase_writer_loop), name="firebase", daemon=True),
        threading.Thread(target=resilient(camera_trigger_loop),  name="camera",   daemon=True),
    ]

    for t in threads:
        t.start()
        log.info("Thread started: %s", t.name)

    log.info("All threads running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        GPIO.cleanup()
        if _dht_device:
            try:
                _dht_device.exit()
            except Exception:
                pass

if __name__ == "__main__":
    main()
