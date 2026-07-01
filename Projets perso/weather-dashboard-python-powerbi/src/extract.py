
import requests
import pandas as pd
from config import BASE_URL

def get_weather(city, latitude, longitude):

    url = (
      BASE_URL
      + f"?latitude={latitude}"
      + f"&longitude={longitude}"
      + "&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m"
      + "&forecast_days=1"
    )

    response = requests.get(url, timeout=10)
    response.raise_for_status()

    data = response.json()["hourly"]

    df = pd.DataFrame({
        "datetime": data["time"],
        "temperature": data["temperature_2m"],
        "humidity": data["relative_humidity_2m"],
        "wind_speed": data["wind_speed_10m"]
    })

    df["city"] = city

    return df
