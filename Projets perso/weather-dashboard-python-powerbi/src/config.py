
# ==========================
# API
# ==========================

BASE_URL = "https://api.open-meteo.com/v1/forecast"

# ==========================
# DATABASE
# ==========================

DATABASE_PATH = "../data/weather.db"

# ==========================
# OUTPUT
# ==========================

CSV_PATH = "../data/weather_all_cities.csv"

# ==========================
# LOGS
# ==========================

LOG_PATH = "../logs/pipeline.log"

# ==========================
# VILLES
# ==========================

CITIES = {
    "Paris": {
        "latitude": 48.8566,
        "longitude": 2.3522,
        "country": "France",
        "region": "Île-de-France"
    },
    "Lyon": {
        "latitude": 45.7640,
        "longitude": 4.8357,
        "country": "France",
        "region": "Auvergne-Rhône-Alpes"
    },
    "Marseille": {
        "latitude": 43.2965,
        "longitude": 5.3698,
        "country": "France",
        "region": "Provence-Alpes-Côte d'Azur"
    },
    "Lille": {
        "latitude": 50.6292,
        "longitude": 3.0573,
        "country": "France",
        "region": "Hauts-de-France"
    },
    "Bordeaux": {
        "latitude": 44.8378,
        "longitude": -0.5792,
        "country": "France",
        "region": "Nouvelle-Aquitaine"
    }
}
