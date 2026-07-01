
import sqlite3
from logger import logger
from config import DATABASE_PATH

logger.info("Création de la table weather.")

def create_weather_table():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weather (
            city TEXT NOT NULL,
            datetime TEXT NOT NULL,
            temperature REAL,
            humidity REAL,
            wind_speed REAL,
            extraction_date TEXT,
            PRIMARY KEY (city, datetime)
        )
    """)

    conn.commit()
    logger.info("Table weather créée ou déjà existante.")
    conn.close()

    print("Table weather prête avec clé primaire.")

def insert_weather_data(df):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    for _, row in df.iterrows():
        cursor.execute("""
            INSERT INTO weather (
                city,
                datetime,
                temperature,
                humidity,
                wind_speed,
                extraction_date
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(city, datetime) DO UPDATE SET
                temperature = excluded.temperature,
                humidity = excluded.humidity,
                wind_speed = excluded.wind_speed,
                extraction_date = excluded.extraction_date
        """, (
            row["city"],
            row["datetime"],
            row["temperature"],
            row["humidity"],
            row["wind_speed"],
            row["extraction_date"]
        ))

    conn.commit()
    logger.info("Table weather créée ou déjà existante.")
    conn.close()

    print("Insertion / mise à jour terminée.")


def save_dim_city(df):
    conn = sqlite3.connect(DATABASE_PATH)

    df.to_sql(
        "DimCity",
        conn,
        if_exists="replace",
        index=False
    )

    conn.close()

    print("DimCity enregistrée.")
