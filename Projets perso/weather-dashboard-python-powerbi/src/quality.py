
import pandas as pd


def check_missing_values(df):
    """
    Vérifie la présence de valeurs manquantes.
    """
    missing = df.isnull().sum()

    print("\nValeurs manquantes :")
    print(missing)

    return missing.sum() == 0


def check_duplicates(df):
    """
    Vérifie les doublons sur (city, datetime).
    """
    duplicates = df.duplicated(
        subset=["city", "datetime"]
    ).sum()

    print(f"\nDoublons : {duplicates}")

    return duplicates == 0


def check_temperature(df):

    invalid = df[
        (df["temperature"] < -60)
        | (df["temperature"] > 60)
    ]

    print(f"\nTempératures invalides : {len(invalid)}")

    return len(invalid) == 0
