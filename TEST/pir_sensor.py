import RPi.GPIO as GPIO
import time

CHANNEL = 23  # ??? ???? 16 = GPIO23

GPIO.setmode(GPIO.BCM)
GPIO.setup(CHANNEL, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

print("Calibrating PIR... wait 30 seconds")
time.sleep(30)

last_state = GPIO.input(CHANNEL)

if last_state == 1:
    print("Initial state: Motion detected")
else:
    print("Initial state: No motion")

try:
    while True:
        current_state = GPIO.input(CHANNEL)

        if current_state != last_state:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

            if current_state == 1:
                print(f"[{timestamp}] Motion detected")
            else:
                print(f"[{timestamp}] No motion")

            last_state = current_state

        time.sleep(0.1)

except KeyboardInterrupt:
    print("\nStopping PIR test...")

finally:
    GPIO.cleanup()
