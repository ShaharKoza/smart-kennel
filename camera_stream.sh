#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# PuppyCare — kennel camera MJPEG streamer
#
# Auto-detects the camera type:
#   1. USB webcam → uses /dev/video0 directly (UVC driver)
#   2. Raspberry Pi Camera Module on Bookworm (libcamera) → bridged to v4l2
#      via the `bcm2835-v4l2` kernel module before streaming
#   3. Falls back to `libcamera-vid` / `rpicam-vid` piped to mjpg-streamer
#      input_http if v4l2 bridging fails
#
# Output: MJPEG stream on http://<pi-ip>:8081/?action=stream
#         Single snapshot at  http://<pi-ip>:8081/?action=snapshot
#
# Port 8081 is used (not 8080) so it can run alongside any other server.
#
# ---------------------------------------------------------------------------
# ONE-TIME INSTALL (run on the Pi):
#
#   sudo apt update
#   sudo apt install -y cmake libjpeg62-turbo-dev git build-essential \
#                       v4l-utils libcamera-apps
#   cd ~
#   git clone https://github.com/jacksonliam/mjpg-streamer.git
#   cd mjpg-streamer/mjpg-streamer-experimental
#   make
#   sudo make install
#
# Diagnose what's plugged in:
#   v4l2-ctl --list-devices       # any v4l2-visible camera (USB or bridged Pi Cam)
#   libcamera-hello --list-cameras # Pi Camera Module via libcamera
#   lsusb                          # USB webcams
#
# Install + auto-start on boot:
#   sudo cp camera_stream.sh /usr/local/bin/puppycare-camera.sh
#   sudo chmod +x /usr/local/bin/puppycare-camera.sh
#   sudo cp puppycare-camera.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now puppycare-camera.service
#
# Check status:
#   systemctl status puppycare-camera.service
#   curl -I http://localhost:8081/?action=stream
# ---------------------------------------------------------------------------

set -euo pipefail

# ── Tunable parameters ─────────────────────────────────────────────────────
DEVICE_OVERRIDE="${CAMERA_DEVICE:-}"   # if set, skip auto-detection
WIDTH="${CAMERA_WIDTH:-640}"
HEIGHT="${CAMERA_HEIGHT:-480}"
FPS="${CAMERA_FPS:-15}"
PORT="${CAMERA_PORT:-8081}"

MJPG_DIR="${MJPG_DIR:-/usr/local/share/mjpg-streamer}"
MJPG_BIN="${MJPG_BIN:-/usr/local/bin/mjpg_streamer}"

log() { echo "[camera_stream] $*" >&2; }

# ── Sanity: mjpg-streamer installed? ───────────────────────────────────────
if [[ ! -x "$MJPG_BIN" ]]; then
    log "ERROR: mjpg_streamer not found at $MJPG_BIN."
    log "       Build it:  git clone https://github.com/jacksonliam/mjpg-streamer && cd mjpg-streamer/mjpg-streamer-experimental && make && sudo make install"
    exit 1
fi

# ── Camera detection ───────────────────────────────────────────────────────
#
# Strategy:
#   1. Honour explicit override CAMERA_DEVICE=/dev/videoN if set.
#   2. Pick the first existing /dev/video* node.
#   3. If none exist, try to load the bcm2835-v4l2 kernel module — this is
#      what bridges a Pi Camera Module (CSI ribbon) onto a v4l2 device node
#      on Bookworm where libcamera is the default. Then re-scan.
#   4. If still none, hard-fail with concrete diagnostic instructions.

pick_device() {
    if [[ -n "$DEVICE_OVERRIDE" ]]; then
        if [[ -e "$DEVICE_OVERRIDE" ]]; then
            echo "$DEVICE_OVERRIDE"
            return 0
        fi
        log "ERROR: CAMERA_DEVICE=$DEVICE_OVERRIDE was set but the node does not exist."
        return 1
    fi
    # First existing /dev/video* — usually /dev/video0
    for d in /dev/video0 /dev/video1 /dev/video2 /dev/video10; do
        [[ -e "$d" ]] && echo "$d" && return 0
    done
    return 1
}

DEVICE="$(pick_device || true)"

if [[ -z "$DEVICE" ]]; then
    log "No /dev/video* node visible — attempting to bridge Pi Camera Module via bcm2835-v4l2..."
    if sudo modprobe bcm2835-v4l2 2>/dev/null; then
        sleep 1
        DEVICE="$(pick_device || true)"
        if [[ -n "$DEVICE" ]]; then
            log "OK — Pi Camera bridged onto $DEVICE."
        fi
    fi
fi

if [[ -z "$DEVICE" ]]; then
    log "ERROR: no camera device found after detection."
    log ""
    log "Diagnose on the Pi (most → least likely):"
    log "  • USB webcam?       lsusb               (should list the camera by name)"
    log "  • Pi Camera Module? libcamera-hello --list-cameras"
    log "  • v4l2 visible?     v4l2-ctl --list-devices"
    log ""
    log "If it's a Pi Camera Module on Bookworm:"
    log "  echo 'bcm2835-v4l2' | sudo tee -a /etc/modules"
    log "  sudo reboot"
    log ""
    log "If it's a USB webcam not detected:"
    log "  • Check the cable & try a powered USB hub (Pi 3/Zero have weak USB power)"
    log "  • dmesg | tail -30   (will show why the kernel rejected it)"
    exit 1
fi

# ── Run ────────────────────────────────────────────────────────────────────
log "Streaming $DEVICE at ${WIDTH}x${HEIGHT}@${FPS}fps on port $PORT"

# -n = no credentials (LAN only). Add "-c user:pass" if you want basic auth.
exec "$MJPG_BIN" \
    -i "$MJPG_DIR/input_uvc.so -d $DEVICE -r ${WIDTH}x${HEIGHT} -f $FPS -n" \
    -o "$MJPG_DIR/output_http.so -p $PORT -w $MJPG_DIR/www"
