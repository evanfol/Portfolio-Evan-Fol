
import pandas as pd

from extract import get_weather
from transform import add_metadata
from load import create_weather_table, insert_weather_data
from quality import (
    check_missing_values,
    check_duplicates,
    check_temperature
)
from logger import logger
from config import CITIES
from transform import create_dim_city
from load import save_dim_city

def run_pipeline():

    logger.info("===== Début du pipeline =====")

    all_data = []

    for city, info in CITIES.items():

        try:
            df = get_weather(
                city,
                info["latitude"],
                info["longitude"]
            )
            all_data.append(df)

            logger.info(f"{city} récupérée avec succès.")

        except Exception as e:
            logger.error(f"Erreur pour {city} : {e}")

    weather_df = pd.concat(all_data, ignore_index=True)

    weather_df = add_metadata(weather_df)

    dim_city = create_dim_city()

    save_dim_city(dim_city)

    if not check_missing_values(weather_df):
      raise ValueError("Valeurs manquantes détectées.")

    if not check_duplicates(weather_df):
      raise ValueError("Doublons détectés.")

    if not check_temperature(weather_df):
      raise ValueError("Températures invalides.")

    create_weather_table()
    insert_weather_data(weather_df)

    print("\nPipeline terminé.")

    logger.info("===== Fin du pipeline =====")

if __name__ == "__main__":
    run_pipeline()
