import RPi.GPIO as GPIO
import time

channel = 4

GPIO.setmode(GPIO.BCM)
GPIO.setup(channel, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

while True:
    if GPIO.input(channel) == 1:
        print("Light detected")
    else:
        print("Dark")
    time.sleep(0.5)
