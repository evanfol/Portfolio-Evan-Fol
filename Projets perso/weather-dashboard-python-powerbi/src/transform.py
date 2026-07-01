
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from config import CITIES

def add_metadata(df):

    df["extraction_date"] = datetime.now(
        ZoneInfo("Europe/Paris")
    ).isoformat()

    return df

def create_dim_city():

    rows = []

    for city, info in CITIES.items():

        rows.append({
            "city": city,
            "latitude": info["latitude"],
            "longitude": info["longitude"],
            "country": info["country"],
            "region": info["region"]
        })

    return pd.DataFrame(rows)