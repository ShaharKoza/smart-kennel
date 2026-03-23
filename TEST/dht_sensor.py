import Adafruit_DHT as dht
import time

sensor = dht.AM2302
pin = 22

while True:
    humidity, temperature = dht.read_retry(sensor, pin)
    if humidity is not None and temperature is not None:
        print(f"Temperature: {temperature:.1f}C")
        print(f"Humidity: {humidity:.1f}%")
        print("----------")
    else:
        print("Failed to read sensor")
    time.sleep(2)