
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from config import LOG_PATH

os.makedirs("../logs", exist_ok=True)


class ParisFormatter(logging.Formatter):

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(
            record.created,
            tz=ZoneInfo("Europe/Paris")
        )

        if datefmt:
            return dt.strftime(datefmt)

        return dt.isoformat(sep=" ", timespec="seconds")


handler = logging.FileHandler(LOG_PATH)

handler.setFormatter(
    ParisFormatter(
        "%(asctime)s - %(levelname)s - %(message)s"
    )
)

logger = logging.getLogger("weather_pipeline")
logger.setLevel(logging.INFO)

logger.handlers.clear()
logger.addHandler(handler)
