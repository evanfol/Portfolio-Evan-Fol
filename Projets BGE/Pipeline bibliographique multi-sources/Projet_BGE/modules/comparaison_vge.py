import asyncio
import re
import xml.etree.ElementTree as ET

from pathlib import Path

import aiohttp
import pandas as pd
from tqdm.auto import tqdm


# ============================================================
# CONFIGURATION CHEMINS
# ============================================================
#
# Projet_BGE/
# │
# ├── table_finale.xlsx
# │
# ├── modules/
# │   └── comparaison_vge.py
# │
# └── output/
#     └── Acquisition_non.csv
#
# ============================================================

DOSSIER_MODULE = Path(__file__).resolve().parent

DOSSIER_PROJET = DOSSIER_MODULE.parent

DOSSIER_OUTPUT = (
    DOSSIER_PROJET
    / "output"
)

CHEMIN_TABLE_FINALE = (
    DOSSIER_PROJET
    / "table_finale.xlsx"
)

CHEMIN_ACQUISITION_NON = (
    DOSSIER_OUTPUT
    / "Acquisition_non.csv"
)


# ============================================================
# OUTIL : TROUVER UNE COLONNE
# ============================================================

def trouver_colonne(
    df,
    nom
):
    """
    Recherche une colonne sans tenir compte :
    - des majuscules/minuscules
    - des espaces avant/après
    """

    nom_recherche = (
        str(nom)
        .strip()
        .lower()
    )

    for colonne in df.columns:

        nom_colonne = (
            str(colonne)
            .strip()
            .lower()
        )

        if nom_colonne == nom_recherche:

            return colonne

    return None


# ============================================================
# VALIDATION ISBN-10
# ============================================================

def isbn10_valide(
    isbn10
):

    if isbn10 is None:
        return False

    isbn10 = (
        str(isbn10)
        .upper()
        .strip()
    )

    if len(isbn10) != 10:
        return False

    if not re.fullmatch(
        r"\d{9}[\dX]",
        isbn10
    ):
        return False

    total = 0

    for i, caractere in enumerate(
        isbn10
    ):

        if caractere == "X":

            valeur = 10

        else:

            valeur = int(
                caractere
            )

        poids = 10 - i

        total += (
            valeur
            * poids
        )

    return (
        total % 11
        == 0
    )


# ============================================================
# VALIDATION ISBN-13
# ============================================================

def isbn13_valide(
    isbn13
):

    if isbn13 is None:
        return False

    isbn13 = (
        str(isbn13)
        .strip()
    )

    if len(isbn13) != 13:
        return False

    if not isbn13.isdigit():
        return False

    total = 0

    for i, caractere in enumerate(
        isbn13[:12]
    ):

        chiffre = int(
            caractere
        )

        if i % 2 == 0:

            total += chiffre

        else:

            total += (
                chiffre * 3
            )

    check = (
        10
        -
        (total % 10)
    ) % 10

    return (
        check
        ==
        int(isbn13[-1])
    )


# ============================================================
# ISBN-10 -> ISBN-13
# ============================================================

def isbn10_vers_13(
    isbn10
):

    if not isbn10_valide(
        isbn10
    ):

        return None

    isbn10 = (
        str(isbn10)
        .upper()
        .strip()
    )

    base = (
        "978"
        +
        isbn10[:9]
    )

    total = 0

    for i, caractere in enumerate(
        base
    ):

        chiffre = int(
            caractere
        )

        if i % 2 == 0:

            total += chiffre

        else:

            total += (
                chiffre * 3
            )

    check = (
        10
        -
        (total % 10)
    ) % 10

    return (
        base
        +
        str(check)
    )


# ============================================================
# NORMALISATION ISBN
# ============================================================

def normaliser_isbn(
    value
):
    """
    Retourne systématiquement :

        ISBN-13 valide

    ou :

        None

    Les ISBN-10 valides sont convertis en ISBN-13.
    """

    if pd.isna(
        value
    ):

        return None

    texte = (
        str(value)
        .upper()
        .strip()
    )

    if not texte:

        return None

    isbn = re.sub(
        r"[^0-9X]",
        "",
        texte
    )

    # --------------------------------------------------------
    # ISBN-10
    # --------------------------------------------------------

    if len(isbn) == 10:

        return isbn10_vers_13(
            isbn
        )

    # --------------------------------------------------------
    # ISBN-13
    # --------------------------------------------------------

    if len(isbn) == 13:

        if isbn13_valide(
            isbn
        ):

            return isbn

        return None

    return None


# ============================================================
# ISBN-13 -> ISBN-10
# ============================================================

def isbn13_vers_10(
    isbn13
):
    """
    Convertit un ISBN-13 commençant par 978
    en ISBN-10.

    ISBN 979 :
        aucun ISBN-10 équivalent.
    """

    isbn13 = normaliser_isbn(
        isbn13
    )

    if isbn13 is None:

        return None

    if not isbn13.startswith(
        "978"
    ):

        return None

    base = isbn13[
        3:12
    ]

    total = 0

    for i, caractere in enumerate(
        base
    ):

        chiffre = int(
            caractere
        )

        poids = (
            10 - i
        )

        total += (
            chiffre
            * poids
        )

    cle = (
        11
        -
        (total % 11)
    ) % 11

    if cle == 10:

        caractere_cle = "X"

    else:

        caractere_cle = str(
            cle
        )

    isbn10 = (
        base
        +
        caractere_cle
    )

    if not isbn10_valide(
        isbn10
    ):

        return None

    return isbn10


# ============================================================
# FORMES ISBN À RECHERCHER DANS VGE
# ============================================================

def formes_recherche_isbn(
    value
):
    """
    ISBN 978 :
        ISBN-13 + ISBN-10

    ISBN 979 :
        ISBN-13 uniquement
    """

    isbn13 = normaliser_isbn(
        value
    )

    if isbn13 is None:

        return set()

    formes = {
        isbn13
    }

    isbn10 = isbn13_vers_10(
        isbn13
    )

    if isbn10:

        formes.add(
            isbn10
        )

    return formes


# ============================================================
# NORMALISER COLONNE ISBN
# ============================================================

def normaliser_colonne_isbn(
    df,
    colonne
):

    return (
        df[colonne]
        .map(
            normaliser_isbn
        )
        .astype("string")
    )


# ============================================================
# CHARGER ACQUISITION = NON DE table_finale.xlsx
# ============================================================

def charger_acquisitions_non_table_finale():
    """
    Charge table_finale.xlsx et retourne uniquement
    les lignes Acquisition = Non avec ISBN valide.
    """

    if not CHEMIN_TABLE_FINALE.exists():

        raise FileNotFoundError(
            "\nFichier introuvable :\n"
            f"{CHEMIN_TABLE_FINALE}"
        )

    df = pd.read_excel(
        CHEMIN_TABLE_FINALE,
        dtype="string"
    )

    df.columns = (
        df.columns
        .astype(str)
        .str.strip()
    )

    colonne_acquisition = trouver_colonne(
        df,
        "Acquisition"
    )

    colonne_isbn = trouver_colonne(
        df,
        "isbn_normalise"
    )

    if colonne_acquisition is None:

        raise ValueError(
            "La colonne 'Acquisition' est absente de "
            "table_finale.xlsx.\n"
            f"Colonnes disponibles : "
            f"{df.columns.tolist()}"
        )

    if colonne_isbn is None:

        raise ValueError(
            "La colonne 'isbn_normalise' est absente de "
            "table_finale.xlsx."
        )

    acquisition = (
        df[colonne_acquisition]
        .astype("string")
        .str.strip()
        .str.lower()
    )

    masque_non = (
        acquisition
        .eq("non")
        .fillna(False)
    )

    df_non = (
        df.loc[
            masque_non
        ]
        .copy()
        .reset_index(drop=True)
    )

    if df_non.empty:

        return df_non

    if (
        colonne_isbn
        !=
        "isbn_normalise"
    ):

        df_non = (
            df_non.rename(
                columns={
                    colonne_isbn:
                        "isbn_normalise"
                }
            )
        )

    df_non[
        "isbn_normalise"
    ] = (
        normaliser_colonne_isbn(
            df_non,
            "isbn_normalise"
        )
    )

    df_non = (
        df_non
        .dropna(
            subset=[
                "isbn_normalise"
            ]
        )
        .drop_duplicates(
            subset=[
                "isbn_normalise"
            ],
            keep="first"
        )
        .reset_index(drop=True)
    )

    return df_non


# ============================================================
# CHARGER Acquisition_non.csv
# ============================================================

def charger_historique_acquisition_non():

    if not CHEMIN_ACQUISITION_NON.exists():

        return pd.DataFrame()

    df = pd.read_csv(
        CHEMIN_ACQUISITION_NON,
        dtype="string"
    )

    if df.empty:

        return df

    colonne_isbn = trouver_colonne(
        df,
        "isbn_normalise"
    )

    if colonne_isbn is None:

        raise ValueError(
            "Acquisition_non.csv existe mais ne contient "
            "pas la colonne isbn_normalise."
        )

    if (
        colonne_isbn
        !=
        "isbn_normalise"
    ):

        df = (
            df.rename(
                columns={
                    colonne_isbn:
                        "isbn_normalise"
                }
            )
        )

    df[
        "isbn_normalise"
    ] = (
        normaliser_colonne_isbn(
            df,
            "isbn_normalise"
        )
    )

    df = (
        df
        .dropna(
            subset=[
                "isbn_normalise"
            ]
        )
        .drop_duplicates(
            subset=[
                "isbn_normalise"
            ],
            keep="first"
        )
        .reset_index(drop=True)
    )

    return df


# ============================================================
# ENSEMBLE DES ISBN INTERDITS
# ============================================================

def recuperer_isbn_interdits():
    """
    Retourne un set contenant tous les ISBN normalisés
    présents dans Acquisition_non.csv.
    """

    df_non = (
        charger_historique_acquisition_non()
    )

    if df_non.empty:

        return set()

    return set(
        df_non[
            "isbn_normalise"
        ]
        .map(
            normaliser_isbn
        )
        .dropna()
    )


# ============================================================
# MISE À JOUR Acquisition_non.csv
# + PREMIER FILTRAGE
# ============================================================

def filtrer_et_mettre_a_jour_acquisition_non(
    df
):
    """
    1. Lit table_finale.xlsx
    2. récupère Acquisition = Non
    3. complète Acquisition_non.csv
    4. évite les doublons ISBN
    5. supprime les correspondances directes sur
       isbn_normalise

    IMPORTANT :
    un deuxième contrôle sera effectué ensuite sur
    l'ISBN EFFECTIF utilisé par VGE afin d'empêcher
    le fallback "isbn" de réintroduire ces ISBN.
    """

    if df is None:

        raise ValueError(
            "Le DataFrame fourni est None."
        )

    if not isinstance(
        df,
        pd.DataFrame
    ):

        raise TypeError(
            f"Objet reçu invalide : "
            f"{type(df)}"
        )

    df = df.copy()

    colonne_isbn_df = trouver_colonne(
        df,
        "isbn_normalise"
    )

    if colonne_isbn_df is None:

        raise ValueError(
            "La colonne isbn_normalise est absente "
            "du DataFrame à comparer à VGE."
        )

    # ========================================================
    # TABLE FINALE
    # ========================================================

    df_non_excel = (
        charger_acquisitions_non_table_finale()
    )

    # ========================================================
    # HISTORIQUE EXISTANT
    # ========================================================

    df_historique = (
        charger_historique_acquisition_non()
    )

    nb_historique_avant = (
        len(df_historique)
    )

    # ========================================================
    # ACQUISITION ÉVENTUELLEMENT PRÉSENTE DANS DF
    # ========================================================

    colonne_acquisition_df = trouver_colonne(
        df,
        "Acquisition"
    )

    if colonne_acquisition_df is not None:

        acquisition_df = (
            df[
                colonne_acquisition_df
            ]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        masque_non_df = (
            acquisition_df
            .eq("non")
            .fillna(False)
        )

        df_non_courant = (
            df.loc[
                masque_non_df
            ]
            .copy()
            .reset_index(drop=True)
        )

        df = (
            df.loc[
                ~masque_non_df
            ]
            .copy()
            .reset_index(drop=True)
        )

        if not df_non_courant.empty:

            df_non_courant[
                "isbn_normalise"
            ] = (
                df_non_courant[
                    colonne_isbn_df
                ]
                .map(
                    normaliser_isbn
                )
                .astype("string")
            )

            df_non_courant = (
                df_non_courant
                .dropna(
                    subset=[
                        "isbn_normalise"
                    ]
                )
                .drop_duplicates(
                    subset=[
                        "isbn_normalise"
                    ],
                    keep="first"
                )
                .reset_index(drop=True)
            )

    else:

        df_non_courant = (
            pd.DataFrame()
        )

    # ========================================================
    # FUSION HISTORIQUE
    # ========================================================

    morceaux = []

    if not df_historique.empty:

        morceaux.append(
            df_historique
        )

    if not df_non_excel.empty:

        morceaux.append(
            df_non_excel
        )

    if not df_non_courant.empty:

        morceaux.append(
            df_non_courant
        )

    if morceaux:

        df_historique_final = (
            pd.concat(
                morceaux,
                ignore_index=True,
                sort=False
            )
        )

        df_historique_final[
            "isbn_normalise"
        ] = (
            df_historique_final[
                "isbn_normalise"
            ]
            .map(
                normaliser_isbn
            )
            .astype("string")
        )

        df_historique_final = (
            df_historique_final
            .dropna(
                subset=[
                    "isbn_normalise"
                ]
            )
            .drop_duplicates(
                subset=[
                    "isbn_normalise"
                ],
                keep="first"
            )
            .reset_index(drop=True)
        )

    else:

        df_historique_final = (
            pd.DataFrame(
                columns=[
                    "isbn_normalise"
                ]
            )
        )

    # ========================================================
    # ISBN AVANT / APRÈS
    # ========================================================

    isbn_avant = set()

    if (
        not df_historique.empty
        and
        "isbn_normalise"
        in df_historique.columns
    ):

        isbn_avant = set(
            df_historique[
                "isbn_normalise"
            ]
            .dropna()
            .tolist()
        )

    isbn_apres = set(
        df_historique_final[
            "isbn_normalise"
        ]
        .dropna()
        .tolist()
    )

    nouveaux_isbn = (
        isbn_apres
        -
        isbn_avant
    )

    # ========================================================
    # SAUVEGARDE
    # ========================================================

    DOSSIER_OUTPUT.mkdir(
        parents=True,
        exist_ok=True
    )

    df_historique_final.to_csv(
        CHEMIN_ACQUISITION_NON,
        index=False,
        encoding="utf-8-sig"
    )

    # ========================================================
    # PREMIER FILTRE :
    # isbn_normalise DIRECT
    # ========================================================

    isbn_courants = (
        df[
            colonne_isbn_df
        ]
        .map(
            normaliser_isbn
        )
    )

    masque_interdit = (
        isbn_courants
        .isin(
            isbn_apres
        )
    )

    nb_avant_filtre = (
        len(df)
    )

    nb_lignes_supprimees = int(
        masque_interdit.sum()
    )

    nb_isbn_supprimes = (
        isbn_courants.loc[
            masque_interdit
        ]
        .nunique()
    )

    df = (
        df.loc[
            ~masque_interdit
        ]
        .copy()
        .reset_index(drop=True)
    )

    # ========================================================
    # STATS
    # ========================================================

    print()
    print("=" * 70)
    print("FILTRE ACQUISITIONS AVANT VGE")
    print("=" * 70)

    print(
        f"Acquisition = Non dans table_finale : "
        f"{len(df_non_excel):,}"
    )

    print(
        f"Acquisition = Non dans DF courant   : "
        f"{len(df_non_courant):,}"
    )

    print(
        f"ISBN historiques avant              : "
        f"{nb_historique_avant:,}"
    )

    print(
        f"Nouveaux ISBN ajoutés               : "
        f"{len(nouveaux_isbn):,}"
    )

    print(
        f"ISBN total Acquisition_non.csv      : "
        f"{len(isbn_apres):,}"
    )

    print(
        f"Lignes avant filtre ISBN            : "
        f"{nb_avant_filtre:,}"
    )

    print(
        f"Lignes supprimées par historique    : "
        f"{nb_lignes_supprimees:,}"
    )

    print(
        f"ISBN uniques supprimés              : "
        f"{nb_isbn_supprimes:,}"
    )

    print(
        f"Lignes après premier filtre         : "
        f"{len(df):,}"
    )

    print(
        f"Historique                          : "
        f"{CHEMIN_ACQUISITION_NON}"
    )

    print("=" * 70)

    return df


# ============================================================
# CONSTRUIRE L'ISBN EFFECTIF D'UNE LIGNE
# ============================================================

def construire_isbn_par_ligne(
    df
):
    """
    Priorité :

    1. isbn_normalise
    2. isbn

    Retourne une Series d'ISBN-13 normalisés.
    """

    isbn_par_ligne = pd.Series(
        None,
        index=df.index,
        dtype="object"
    )

    if "isbn_normalise" in df.columns:

        isbn_par_ligne = (
            df[
                "isbn_normalise"
            ]
            .map(
                normaliser_isbn
            )
        )

    if "isbn" in df.columns:

        isbn_depuis_isbn = (
            df[
                "isbn"
            ]
            .map(
                normaliser_isbn
            )
        )

        isbn_par_ligne = (
            isbn_par_ligne
            .fillna(
                isbn_depuis_isbn
            )
        )

    return isbn_par_ligne


# ============================================================
# FILTRE CRITIQUE :
# EMPÊCHER LE FALLBACK ISBN DE RÉINTRODUIRE LES INTERDITS
# ============================================================

def filtrer_isbn_effectif_interdit(
    df
):
    """
    Corrige le bug identifié :

    après suppression sur isbn_normalise,
    une ligne pouvait être réintroduite parce que VGE
    utilisait ensuite la colonne "isbn" en fallback.

    Cette fonction travaille donc sur l'ISBN EFFECTIF
    réellement utilisé par VGE.
    """

    df = df.copy()

    isbn_interdits = (
        recuperer_isbn_interdits()
    )

    isbn_par_ligne = (
        construire_isbn_par_ligne(
            df
        )
    )

    masque_interdit = (
        isbn_par_ligne
        .isin(
            isbn_interdits
        )
    )

    nb_avant = (
        len(df)
    )

    nb_lignes = int(
        masque_interdit.sum()
    )

    nb_isbn = (
        isbn_par_ligne.loc[
            masque_interdit
        ]
        .nunique()
    )

    # --------------------------------------------------------
    # Répartition pour contrôle
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print(
        "FILTRE ISBN EFFECTIF — SÉCURITÉ FALLBACK"
    )
    print("=" * 70)

    print(
        f"ISBN interdits                       : "
        f"{len(isbn_interdits):,}"
    )

    print(
        f"Lignes avant filtre effectif         : "
        f"{nb_avant:,}"
    )

    print(
        f"Lignes interdites détectées          : "
        f"{nb_lignes:,}"
    )

    print(
        f"ISBN interdits détectés              : "
        f"{nb_isbn:,}"
    )

    if (
        nb_lignes
        and
        "TypeRecherche"
        in df.columns
    ):

        print()
        print(
            "Lignes retirées par TypeRecherche :"
        )

        print(
            df.loc[
                masque_interdit,
                "TypeRecherche"
            ]
            .value_counts(
                dropna=False
            )
            .to_string()
        )

    # --------------------------------------------------------
    # Suppression
    # --------------------------------------------------------

    df = (
        df.loc[
            ~masque_interdit
        ]
        .copy()
        .reset_index(drop=True)
    )

    # --------------------------------------------------------
    # Recalcul après suppression
    # --------------------------------------------------------

    isbn_par_ligne = (
        construire_isbn_par_ligne(
            df
        )
    )

    intersection = (
        set(
            isbn_par_ligne
            .dropna()
        )
        &
        isbn_interdits
    )

    print()

    print(
        f"Lignes réellement envoyables VGE     : "
        f"{len(df):,}"
    )

    print(
        f"Intersection après filtre             : "
        f"{len(intersection):,}"
    )

    print("=" * 70)

    if intersection:

        raise RuntimeError(
            f"ERREUR : {len(intersection):,} ISBN "
            "Acquisition_non seraient encore envoyés "
            "à VGE."
        )

    return (
        df,
        isbn_par_ligne
    )


# ============================================================
# COMPARAISON VGE
# ============================================================

async def isbn_absents_vge(
    df,
    batch_size=175,
    max_concurrent=12,
    timeout=30,
    retries=3,
    page_size=50
):

    BASE_URL = (
        "https://swisscovery.slsp.ch/"
        "view/sru/41SLSP_VGE"
    )

    NS = {

        "srw":
            "http://www.loc.gov/zing/srw/",

        "marc":
            "http://www.loc.gov/MARC21/slim"
    }

    # ========================================================
    # 1. VÉRIFICATIONS
    # ========================================================

    if df is None:

        raise ValueError(
            "Le DataFrame fourni est None."
        )

    if not isinstance(
        df,
        pd.DataFrame
    ):

        raise TypeError(
            f"Objet reçu invalide : "
            f"{type(df)}"
        )

    if df.empty:

        print(
            "DataFrame vide : "
            "aucun ISBN à comparer."
        )

        return df.copy()

    # ========================================================
    # 2. PREMIER FILTRE ACQUISITION
    # ========================================================

    df = (
        filtrer_et_mettre_a_jour_acquisition_non(
            df
        )
    )

    if df.empty:

        print(
            "Toutes les lignes ont été "
            "exclues par Acquisition_non."
        )

        return df.copy()

    # ========================================================
    # 3. VÉRIFICATION COLONNES ISBN
    # ========================================================

    if (
        "isbn_normalise" not in df.columns
        and
        "isbn" not in df.columns
    ):

        raise ValueError(
            "Aucune colonne ISBN utilisable.\n"
            "Il faut au moins isbn_normalise ou isbn."
        )

    # ========================================================
    # 4. FILTRE ISBN EFFECTIF
    #
    # CORRECTION DU BUG :
    #
    # isbn_normalise peut être vide,
    # donc VGE utilisait ensuite isbn.
    #
    # On contrôle désormais les DEUX via l'ISBN effectif.
    # ========================================================

    (
        df,
        isbn_par_ligne
    ) = filtrer_isbn_effectif_interdit(
        df
    )

    if df.empty:

        print()
        print("=" * 70)
        print("COMPARAISON VGE")
        print("=" * 70)

        print(
            "Aucune ligne restante après "
            "filtrage Acquisition_non."
        )

        print(
            "Aucune requête VGE effectuée."
        )

        print("=" * 70)

        return df.copy()

    # ========================================================
    # 5. ISBN UNIQUES
    # ========================================================

    isbn_uniques = (
        isbn_par_ligne
        .dropna()
        .drop_duplicates()
        .tolist()
    )

    isbn_recherches = set(
        isbn_uniques
    )

    # ========================================================
    # AUCUN ISBN
    # ========================================================

    if not isbn_uniques:

        print()
        print("=" * 70)
        print("COMPARAISON VGE")
        print("=" * 70)

        print(
            "Aucun ISBN valide à rechercher."
        )

        print("=" * 70)

        return (
            df.iloc[0:0]
            .copy()
        )

    # ========================================================
    # 6. LOTS
    # ========================================================

    batches = [

        isbn_uniques[
            i:
            i + batch_size
        ]

        for i in range(
            0,
            len(isbn_uniques),
            batch_size
        )
    ]

    # ========================================================
    # STATS
    # ========================================================

    print()
    print("=" * 70)
    print("COMPARAISON AVEC VGE")
    print("=" * 70)

    print(
        f"Lignes reçues VGE    : "
        f"{len(df):,}"
    )

    print(
        f"ISBN uniques         : "
        f"{len(isbn_uniques):,}"
    )

    print(
        f"Taille des lots      : "
        f"{batch_size}"
    )

    print(
        f"Nombre de lots       : "
        f"{len(batches):,}"
    )

    print(
        f"Concurrence          : "
        f"{max_concurrent}"
    )

    print(
        f"Taille page SRU      : "
        f"{page_size}"
    )

    print()

    # ========================================================
    # 7. RÉSEAU
    # ========================================================

    semaphore = (
        asyncio.Semaphore(
            max_concurrent
        )
    )

    connector = (
        aiohttp.TCPConnector(
            limit=max_concurrent,
            limit_per_host=max_concurrent,
            ttl_dns_cache=600,
            enable_cleanup_closed=True
        )
    )

    timeout_config = (
        aiohttp.ClientTimeout(
            total=timeout,
            connect=10,
            sock_connect=10,
            sock_read=timeout
        )
    )

    compteur_http = 0

    # ========================================================
    # 8. REQUÊTE SRU
    # ========================================================

    async def requete_sru(
        session,
        query,
        start_record
    ):

        nonlocal compteur_http

        params = {

            "version":
                "1.2",

            "operation":
                "searchRetrieve",

            "recordSchema":
                "marcxml",

            "query":
                query,

            "startRecord":
                start_record,

            "maximumRecords":
                page_size
        }

        for tentative in range(
            retries
        ):

            try:

                async with semaphore:

                    compteur_http += 1

                    async with session.get(
                        BASE_URL,
                        params=params
                    ) as response:

                        if response.status == 200:

                            contenu = (
                                await response.read()
                            )

                            return ET.fromstring(
                                contenu
                            )

                        if response.status in {

                            429,
                            500,
                            502,
                            503,
                            504

                        }:

                            retry_after = (
                                response
                                .headers
                                .get(
                                    "Retry-After"
                                )
                            )

                            if retry_after:

                                try:

                                    attente = float(
                                        retry_after
                                    )

                                except ValueError:

                                    attente = (
                                        0.5
                                        *
                                        (
                                            2
                                            **
                                            tentative
                                        )
                                    )

                            else:

                                attente = (
                                    0.5
                                    *
                                    (
                                        2
                                        **
                                        tentative
                                    )
                                )

                            await asyncio.sleep(
                                attente
                            )

                            continue

                        return None

            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                ET.ParseError
            ):

                if (
                    tentative
                    <
                    retries - 1
                ):

                    await asyncio.sleep(
                        0.5
                        *
                        (
                            2
                            **
                            tentative
                        )
                    )

        return None

    # ========================================================
    # 9. EXTRACTION ISBN MARC
    # ========================================================

    def extraire_isbn(
        root
    ):

        presents = set()

        for subfield in root.findall(
            ".//marc:datafield[@tag='020']/marc:subfield[@code='a']",
            NS
        ):

            if not subfield.text:

                continue

            isbn = normaliser_isbn(
                subfield.text
            )

            if isbn:

                presents.add(
                    isbn
                )

        return presents

    # ========================================================
    # 10. NOMBRE DE RÉSULTATS
    # ========================================================

    def lire_nombre_resultats(
        root
    ):

        element = (
            root.find(
                ".//srw:numberOfRecords",
                NS
            )
        )

        if element is None:

            return 0

        try:

            return int(
                element.text
            )

        except (
            TypeError,
            ValueError
        ):

            return 0

    # ========================================================
    # 11. TRAITEMENT D'UN LOT
    # ========================================================

    async def tester_batch(
        session,
        batch
    ):

        # ----------------------------------------------------
        # ISBN13 + ISBN10
        # ----------------------------------------------------

        termes_recherche = set()

        for isbn in batch:

            termes_recherche.update(
                formes_recherche_isbn(
                    isbn
                )
            )

        termes_recherche = sorted(
            termes_recherche
        )

        query = " OR ".join(
            f"alma.isbn={isbn}"
            for isbn
            in termes_recherche
        )

        start_record = 1

        presents_batch = set()

        # ----------------------------------------------------
        # Première page
        # ----------------------------------------------------

        root = await requete_sru(
            session,
            query,
            start_record
        )

        if root is None:

            return (
                batch,
                None
            )

        nombre_total = (
            lire_nombre_resultats(
                root
            )
        )

        presents_batch.update(
            extraire_isbn(
                root
            )
        )

        # ----------------------------------------------------
        # Pagination
        # ----------------------------------------------------

        start_record += (
            page_size
        )

        while (
            start_record
            <=
            nombre_total
        ):

            root = await requete_sru(
                session,
                query,
                start_record
            )

            if root is None:

                return (
                    batch,
                    None
                )

            presents_batch.update(
                extraire_isbn(
                    root
                )
            )

            start_record += (
                page_size
            )

        presents_recherches_batch = (
            set(batch)
            &
            presents_batch
        )

        return (
            batch,
            presents_recherches_batch
        )

    # ========================================================
    # 12. EXÉCUTION PARALLÈLE
    # ========================================================

    presents_recherches = set()

    batches_erreur = []

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout_config
    ) as session:

        tasks = [

            asyncio.create_task(
                tester_batch(
                    session,
                    batch
                )
            )

            for batch in batches
        ]

        for task in tqdm(
            asyncio.as_completed(
                tasks
            ),
            total=len(tasks),
            desc="Vérification VGE",
            unit="lot"
        ):

            batch, resultat = (
                await task
            )

            if resultat is None:

                batches_erreur.extend(
                    batch
                )

            else:

                presents_recherches.update(
                    resultat
                )

    # ========================================================
    # 13. ISBN EN ERREUR
    # ========================================================

    erreur_set = set(
        batches_erreur
    )

    # ========================================================
    # 14. ABSENTS
    # ========================================================

    absents = (
        isbn_recherches
        -
        presents_recherches
        -
        erreur_set
    )

    # ========================================================
    # 15. MASQUE ABSENTS
    # ========================================================

    masque_absent = (
        isbn_par_ligne
        .isin(
            absents
        )
    )

    # ========================================================
    # 16. DATAFRAME ABSENTS
    # ========================================================

    df_absents = (
        df.loc[
            masque_absent
        ]
        .copy()
        .reset_index(drop=True)
    )

    isbn_absents_normalises = (
        isbn_par_ligne.loc[
            masque_absent
        ]
        .reset_index(drop=True)
    )

    df_absents[
        "isbn_normalise"
    ] = (
        isbn_absents_normalises
        .astype("string")
    )

    # ========================================================
    # 17. SÉCURITÉ FINALE ABSOLUE
    #
    # Même si une modification future du code réintroduit
    # accidentellement un ISBN Acquisition_non,
    # il ne pourra pas sortir de cette fonction.
    # ========================================================

    isbn_interdits_final = (
        recuperer_isbn_interdits()
    )

    isbn_absents_final = (
        df_absents[
            "isbn_normalise"
        ]
        .map(
            normaliser_isbn
        )
    )

    masque_interdit_final = (
        isbn_absents_final
        .isin(
            isbn_interdits_final
        )
    )

    nb_securite_finale = int(
        masque_interdit_final.sum()
    )

    if nb_securite_finale:

        print()
        print(
            f"⚠ Sécurité finale : "
            f"{nb_securite_finale:,} lignes "
            "Acquisition_non retirées."
        )

        df_absents = (
            df_absents.loc[
                ~masque_interdit_final
            ]
            .copy()
            .reset_index(drop=True)
        )

    # --------------------------------------------------------
    # Contrôle ultime
    # --------------------------------------------------------

    intersection_finale = (
        set(
            df_absents[
                "isbn_normalise"
            ]
            .map(
                normaliser_isbn
            )
            .dropna()
        )
        &
        isbn_interdits_final
    )

    if intersection_finale:

        raise RuntimeError(
            f"ERREUR : {len(intersection_finale):,} ISBN "
            "Acquisition_non sont encore présents dans "
            "df_absents."
        )

    # ========================================================
    # 18. STATISTIQUES
    # ========================================================

    print()
    print("=" * 70)
    print("RÉSULTATS VGE")
    print("=" * 70)

    print(
        f"ISBN recherchés      : "
        f"{len(isbn_recherches):,}"
    )

    print(
        f"✓ ISBN présents      : "
        f"{len(presents_recherches):,}"
    )

    print(
        f"✗ ISBN absents       : "
        f"{len(absents):,}"
    )

    print(
        f"⚠ ISBN en erreur     : "
        f"{len(erreur_set):,}"
    )

    print(
        f"HTTP effectuées      : "
        f"{compteur_http:,}"
    )

    print(
        f"Lignes retournées    : "
        f"{len(df_absents):,}"
    )

    if (
        "isbn_normalise"
        in df_absents.columns
    ):

        print(
            f"ISBN absents uniques : "
            f"{df_absents['isbn_normalise'].nunique():,}"
        )

    # ========================================================
    # SOURCE
    # ========================================================

    if (
        "Source"
        in df_absents.columns
    ):

        print()
        print(
            "Absents par source :"
        )

        print(
            df_absents[
                "Source"
            ]
            .value_counts(
                dropna=False
            )
            .to_string()
        )

    # ========================================================
    # TYPE RECHERCHE
    # ========================================================

    if (
        "TypeRecherche"
        in df_absents.columns
    ):

        print()
        print(
            "Absents par type de recherche :"
        )

        print(
            df_absents[
                "TypeRecherche"
            ]
            .value_counts(
                dropna=False
            )
            .to_string()
        )

    # ========================================================
    # SOURCE × TYPE
    # ========================================================

    if (
        "Source"
        in df_absents.columns
        and
        "TypeRecherche"
        in df_absents.columns
    ):

        print()
        print(
            "Absents par Source / Type :"
        )

        print(
            df_absents
            .groupby(
                [
                    "Source",
                    "TypeRecherche"
                ],
                dropna=False
            )
            .size()
            .sort_values(
                ascending=False
            )
            .to_string()
        )

    # ========================================================
    # 19. CONTRÔLE COHÉRENCE
    # ========================================================

    total_classe = (
        len(
            presents_recherches
        )
        +
        len(
            absents
        )
        +
        len(
            erreur_set
        )
    )

    print()

    print(
        f"Contrôle              : "
        f"{total_classe:,} / "
        f"{len(isbn_recherches):,} "
        f"ISBN classés"
    )

    print(
        f"Intersection Acquisition_non / "
        f"absents_vge : "
        f"{len(intersection_finale):,}"
    )

    print("=" * 70)

    # ========================================================
    # RETURN
    # ========================================================

    return df_absents