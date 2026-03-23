import RPi.GPIO as GPIO
import time

led = 23  # GPIO 23 = ??? 16

GPIO.setmode(GPIO.BCM)
GPIO.setup(led, GPIO.OUT)

print("LED ON")
GPIO.output(led, GPIO.HIGH)
time.sleep(3)

print("LED OFF")
GPIO.output(led, GPIO.LOW)

GPIO.cleanup()