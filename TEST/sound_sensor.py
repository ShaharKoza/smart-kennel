import RPi.GPIO as GPIO
import time

CHANNEL = 17

GPIO.setmode(GPIO.BCM)
GPIO.setup(CHANNEL, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

print("Listening for sound...")
last_trigger_time = 0
COOLDOWN = 0.08

try:
    while True:
        current_state = GPIO.input(CHANNEL)
        now = time.time()

        if current_state == 1 and (now - last_trigger_time) > COOLDOWN:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sound detected!")
            last_trigger_time = now

        time.sleep(0.002)

except KeyboardInterrupt:
    print("\nStopping sound test...")

finally:
    GPIO.cleanup()
