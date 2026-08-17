# -*- coding: utf-8 -*-

import requests
import pandas as pd
import xml.etree.ElementTree as ET
import re
import time
import threading

from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

URL_NETWORK = (
    "https://swisscovery.slsp.ch/view/sru/41SLSP_NETWORK"
)


# ============================================================
# PAGINATION
#
# Benchmark validé :
# 50 = valeur conservant toutes les notices.
# ============================================================

TAILLE_LOT = 50


# ============================================================
# CONCURRENCE
#
# Benchmark final :
#
# Phase 1 : 55 workers
# Phase 2 : 55 workers
# ============================================================

WORKERS_PHASE1 = 55
WORKERS_PHASE2 = 55


# ============================================================
# REQUÊTES NORMALES
#
# Benchmark final :
#
# 1 retry
# backoff = 0.10
# ============================================================

FAST_RETRIES = 1
FAST_BACKOFF = 0.10


# ============================================================
# RÉPARATION
#
# Faible concurrence + retries élevés.
# ============================================================

REPAIR_RETRIES = 5
REPAIR_BACKOFF = 0.50

REPAIR_WORKERS = 6
REPAIR_PASSES = 2


# ============================================================
# TIMEOUTS
# ============================================================

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 45


# ============================================================
# NAMESPACES
# ============================================================

NS = {

    "srw":
        "http://www.loc.gov/zing/srw/",

    "marc":
        "http://www.loc.gov/MARC21/slim",

}


# ============================================================
# REGEX PRÉCOMPILÉES
# ============================================================

PPN_NETTOYAGE_RE = re.compile(
    r"[^0-9X]"
)

EXCEL_DECIMAL_RE = re.compile(
    r"\.0$"
)

PONCTUATION_RE = re.compile(
    r"\s*[/:;,]\s*$"
)

ISBN10_RE = re.compile(
    r"\d{9}[\dX]"
)

ISBN13_RE = re.compile(
    r"\d{13}"
)

ISBN_DEBUT_RE = re.compile(
    r"^\s*([0-9X][0-9X\-\s]{8,30})"
)

ISBN_NETTOYAGE_RE = re.compile(
    r"[^0-9X]"
)

ANNEE_RE = re.compile(
    r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b"
)


# ============================================================
# SESSIONS HTTP PAR THREAD
# ============================================================

thread_local = threading.local()


def creer_session(
    retries,
    backoff,
    pool_size,
):

    session = requests.Session()

    retry = Retry(

        total=retries,

        connect=retries,

        read=retries,

        status=retries,

        backoff_factor=backoff,

        status_forcelist=[
            429,
            500,
            502,
            503,
            504,
        ],

        allowed_methods=[
            "GET"
        ],

        raise_on_status=False,

    )

    adapter = HTTPAdapter(

        max_retries=retry,

        pool_connections=pool_size + 5,

        pool_maxsize=pool_size + 5,

        pool_block=False,

    )

    session.mount(
        "https://",
        adapter,
    )

    session.headers.update({

        "User-Agent":
            "Geneve-Bibliographic-Research-Swisscovery/6.0",

        "Accept-Encoding":
            "gzip, deflate",

    })

    return session


def obtenir_session_fast():

    if not hasattr(
        thread_local,
        "session_fast"
    ):

        thread_local.session_fast = creer_session(

            retries=FAST_RETRIES,

            backoff=FAST_BACKOFF,

            pool_size=max(
                WORKERS_PHASE1,
                WORKERS_PHASE2,
            ),

        )

    return thread_local.session_fast


def obtenir_session_repair():

    if not hasattr(
        thread_local,
        "session_repair"
    ):

        thread_local.session_repair = creer_session(

            retries=REPAIR_RETRIES,

            backoff=REPAIR_BACKOFF,

            pool_size=REPAIR_WORKERS,

        )

    return thread_local.session_repair


# ============================================================
# NORMALISATION PPN
# ============================================================

def normaliser_ppn(ppn):

    if pd.isna(ppn):
        return None

    ppn = (
        str(ppn)
        .strip()
        .upper()
    )

    ppn = EXCEL_DECIMAL_RE.sub(
        "",
        ppn,
    )

    ppn = PPN_NETTOYAGE_RE.sub(
        "",
        ppn,
    )

    if not ppn:
        return None

    if (
        ppn.isdigit()
        and
        len(ppn) < 9
    ):

        ppn = ppn.zfill(9)

    return ppn


# ============================================================
# OUTILS XML
# ============================================================

def sous_champ(
    champ,
    code,
):

    if champ is None:
        return None

    element = champ.find(
        f"marc:subfield[@code='{code}']",
        NS,
    )

    if (
        element is not None
        and
        element.text
    ):

        return element.text.strip()

    return None


def sous_champs(
    champ,
    code,
):

    if champ is None:
        return []

    return [

        element.text.strip()

        for element
        in champ.findall(
            f"marc:subfield[@code='{code}']",
            NS,
        )

        if element.text

    ]


# ============================================================
# NETTOYAGE TEXTE
# ============================================================

def nettoyer_texte(texte):

    if texte is None:
        return None

    texte = (
        str(texte)
        .strip()
    )

    texte = PONCTUATION_RE.sub(
        "",
        texte,
    )

    texte = texte.strip()

    return texte or None


# ============================================================
# VALIDATION ISBN-10
# ============================================================

def isbn10_valide(isbn):

    if not ISBN10_RE.fullmatch(
        isbn
    ):
        return False

    total = 0

    for i, caractere in enumerate(
        isbn
    ):

        valeur = (
            10
            if caractere == "X"
            else int(caractere)
        )

        total += (
            (10 - i)
            * valeur
        )

    return (
        total % 11
        == 0
    )


# ============================================================
# VALIDATION ISBN-13
# ============================================================

def isbn13_valide(isbn):

    if not ISBN13_RE.fullmatch(
        isbn
    ):
        return False

    somme = 0

    for i, chiffre in enumerate(
        isbn[:12]
    ):

        n = int(chiffre)

        somme += (
            n
            if i % 2 == 0
            else n * 3
        )

    cle = (
        10
        - somme % 10
    ) % 10

    return (
        cle
        == int(isbn[-1])
    )


# ============================================================
# CONVERSION ISBN-10 -> ISBN-13
# ============================================================

def isbn10_vers_13(isbn10):

    base = (
        "978"
        + isbn10[:9]
    )

    somme = 0

    for i, chiffre in enumerate(
        base
    ):

        n = int(chiffre)

        somme += (
            n
            if i % 2 == 0
            else n * 3
        )

    cle = (
        10
        - somme % 10
    ) % 10

    return (
        base
        + str(cle)
    )


# ============================================================
# NORMALISATION ISBN
# ============================================================

def normaliser_isbn(valeur):

    if valeur is None:
        return None

    texte = (
        str(valeur)
        .upper()
        .strip()
    )

    match = ISBN_DEBUT_RE.match(
        texte
    )

    if match:

        texte = match.group(1)

    isbn = ISBN_NETTOYAGE_RE.sub(
        "",
        texte,
    )

    # ========================================================
    # ISBN-13
    # ========================================================

    if len(isbn) == 13:

        if isbn13_valide(
            isbn
        ):

            return isbn

        return None

    # ========================================================
    # ISBN-10
    # ========================================================

    if len(isbn) == 10:

        if not isbn10_valide(
            isbn
        ):

            return None

        isbn13 = isbn10_vers_13(
            isbn
        )

        if isbn13_valide(
            isbn13
        ):

            return isbn13

    return None


# ============================================================
# ANNÉE
# ============================================================

def extraire_annee(valeurs):

    for valeur in valeurs:

        if not valeur:
            continue

        match = ANNEE_RE.search(
            str(valeur)
        )

        if match:

            return int(
                match.group(1)
            )

    return None


# ============================================================
# EXTRACTION D'UNE NOTICE MARC
# ============================================================

def extraire_notice(
    record,
    ppn,
):

    # ========================================================
    # SUPPORT / CARRIER TYPE
    #
    # 338 $b = cr
    #
    # Ressource électronique :
    # on ignore complètement la notice.
    # ========================================================

    for champ338 in record.findall(
        "marc:datafield[@tag='338']",
        NS,
    ):

        for code_support in sous_champs(
            champ338,
            "b",
        ):

            if (
                code_support
                .strip()
                .lower()
                == "cr"
            ):

                return []

    # ========================================================
    # ISBN
    # ========================================================

    isbn_valides = []

    for champ in record.findall(
        "marc:datafield[@tag='020']",
        NS,
    ):

        for valeur in sous_champs(
            champ,
            "a",
        ):

            isbn = normaliser_isbn(
                valeur
            )

            if isbn:

                isbn_valides.append(
                    isbn
                )

    # ========================================================
    # PAS D'ISBN
    #
    # Inutile de parser le reste.
    # ========================================================

    if not isbn_valides:
        return []

    isbn_valides = list(
        dict.fromkeys(
            isbn_valides
        )
    )

    # ========================================================
    # AUTEUR
    # ========================================================

    champ100 = record.find(
        "marc:datafield[@tag='100']",
        NS,
    )

    auteur = nettoyer_texte(
        sous_champ(
            champ100,
            "a",
        )
    )

    if not auteur:

        champ700 = record.find(
            "marc:datafield[@tag='700']",
            NS,
        )

        auteur = nettoyer_texte(
            sous_champ(
                champ700,
                "a",
            )
        )

    # ========================================================
    # TITRE
    # ========================================================

    champ245 = record.find(
        "marc:datafield[@tag='245']",
        NS,
    )

    titre = nettoyer_texte(
        sous_champ(
            champ245,
            "a",
        )
    )

    sous_titre = nettoyer_texte(
        sous_champ(
            champ245,
            "b",
        )
    )

    if titre and sous_titre:

        titre_complet = (
            f"{titre} : "
            f"{sous_titre}"
        )

    else:

        titre_complet = (
            titre
            or sous_titre
        )

    # ========================================================
    # PUBLICATION
    # ========================================================

    champs_publication = record.findall(
        "marc:datafield[@tag='264']",
        NS,
    )

    if not champs_publication:

        champs_publication = record.findall(
            "marc:datafield[@tag='260']",
            NS,
        )

    lieux = []
    editeurs = []
    annees = []

    for champ in champs_publication:

        lieux.extend(
            sous_champs(
                champ,
                "a",
            )
        )

        editeurs.extend(
            sous_champs(
                champ,
                "b",
            )
        )

        annees.extend(
            sous_champs(
                champ,
                "c",
            )
        )

    # ========================================================
    # NETTOYAGE LIEUX
    # ========================================================

    lieux_nettoyes = [

        valeur_nettoyee

        for valeur in lieux

        if (
            valeur_nettoyee :=
            nettoyer_texte(
                valeur
            )
        )

    ]

    # ========================================================
    # NETTOYAGE ÉDITEURS
    # ========================================================

    editeurs_nettoyes = [

        valeur_nettoyee

        for valeur in editeurs

        if (
            valeur_nettoyee :=
            nettoyer_texte(
                valeur
            )
        )

    ]

    lieux_nettoyes = list(
        dict.fromkeys(
            lieux_nettoyes
        )
    )

    editeurs_nettoyes = list(
        dict.fromkeys(
            editeurs_nettoyes
        )
    )

    lieu_publication = (

        " - ".join(
            lieux_nettoyes
        )

        if lieux_nettoyes

        else None

    )

    editeur = (

        " - ".join(
            editeurs_nettoyes
        )

        if editeurs_nettoyes

        else None

    )

    annee = extraire_annee(
        annees
    )

    # ========================================================
    # UNE LIGNE PAR ISBN
    #
    # IMPORTANT :
    # colonne = isbn_normalise
    # compatible directement avec fusion.py
    # ========================================================

    return [

        {

            "_ppn_requete":
                ppn,

            "isbn_normalise":
                isbn,

            "titre":
                titre_complet,

            "annee":
                annee,

            "nomAuteur":
                auteur,

            "lieuPublication":
                lieu_publication,

            "editeur":
                editeur,

        }

        for isbn
        in isbn_valides

    ]


# ============================================================
# REQUÊTE NETWORK
# ============================================================

def recuperer_page_network(
    ppn,
    start_record,
    mode="fast",
):

    if mode == "repair":

        session = obtenir_session_repair()

    else:

        session = obtenir_session_fast()

    response = session.get(

        URL_NETWORK,

        params={

            "version":
                "1.2",

            "operation":
                "searchRetrieve",

            "recordSchema":
                "marcxml",

            "query":
                f"alma.authority_id={ppn}",

            "startRecord":
                start_record,

            "maximumRecords":
                TAILLE_LOT,

        },

        timeout=(
            CONNECT_TIMEOUT,
            READ_TIMEOUT,
        ),

    )

    response.raise_for_status()

    return response.content


# ============================================================
# PARSER UNE PAGE
# ============================================================

def parser_page(
    xml_bytes,
    ppn,
):

    root = ET.fromstring(
        xml_bytes
    )

    records = root.findall(
        ".//srw:recordData/marc:record",
        NS,
    )

    lignes = []

    extend = lignes.extend

    for record in records:

        extend(
            extraire_notice(
                record,
                ppn,
            )
        )

    return (
        root,
        lignes,
        len(records),
    )


# ============================================================
# PREMIÈRE PAGE
# ============================================================

def traiter_premiere_page(
    ppn,
    mode="fast",
):

    xml_bytes = recuperer_page_network(

        ppn,

        1,

        mode=mode,

    )

    root, lignes, nb_records = (
        parser_page(
            xml_bytes,
            ppn,
        )
    )

    total_element = root.find(
        ".//srw:numberOfRecords",
        NS,
    )

    if (
        total_element is None
        or
        not total_element.text
    ):

        total = 0

    else:

        total = int(
            total_element.text
        )

    return {

        "ppn":
            ppn,

        "total":
            total,

        "records":
            nb_records,

        "lignes":
            lignes,

    }


# ============================================================
# PAGE SUIVANTE
# ============================================================

def traiter_page_suivante(
    ppn,
    start_record,
    mode="fast",
):

    xml_bytes = recuperer_page_network(

        ppn,

        start_record,

        mode=mode,

    )

    _, lignes, nb_records = (
        parser_page(
            xml_bytes,
            ppn,
        )
    )

    return {

        "ppn":
            ppn,

        "start":
            start_record,

        "records":
            nb_records,

        "lignes":
            lignes,

    }


# ============================================================
# RÉPARATION DES PAGES
# ============================================================

def reparer_pages(
    pages_erreur,
    resultats,
):

    erreurs_restantes = list(
        pages_erreur
    )

    for passage in range(
        1,
        REPAIR_PASSES + 1,
    ):

        if not erreurs_restantes:

            break

        print()

        print(
            f"Réparation pages "
            f"{passage}/{REPAIR_PASSES} : "
            f"{len(erreurs_restantes):,}"
        )

        nouvelles_erreurs = []

        with ThreadPoolExecutor(
            max_workers=REPAIR_WORKERS
        ) as executor:

            futures = {

                executor.submit(

                    traiter_page_suivante,

                    ppn,

                    start,

                    "repair",

                ):
                (
                    ppn,
                    start,
                )

                for ppn, start
                in erreurs_restantes

            }

            for future in as_completed(
                futures
            ):

                ppn, start = futures[
                    future
                ]

                try:

                    resultat = (
                        future.result()
                    )

                    if resultat[
                        "lignes"
                    ]:

                        resultats.extend(

                            resultat[
                                "lignes"
                            ]

                        )

                except Exception:

                    nouvelles_erreurs.append(
                        (
                            ppn,
                            start,
                        )
                    )

        erreurs_restantes = (
            nouvelles_erreurs
        )

    return erreurs_restantes


# ============================================================
# FONCTION PRINCIPALE
# ============================================================

def ajouter_isbn_network_ultra(

    df,

    colonne_ppn="ppn",

    workers_phase1=WORKERS_PHASE1,

    workers_phase2=WORKERS_PHASE2,

):

    debut_total = (
        time.perf_counter()
    )

    # ========================================================
    # CONTRÔLE
    # ========================================================

    if colonne_ppn not in df.columns:

        raise ValueError(

            f"La colonne "
            f"'{colonne_ppn}' "
            f"n'existe pas."

        )

    # ========================================================
    # SOURCE
    # ========================================================

    source = df.copy()

    colonnes_swisscovery = [

        "isbn_normalise",

        "titre",

        "annee",

        "nomAuteur",

        "lieuPublication",

        "editeur",

    ]

    # ========================================================
    # ON SUPPRIME LES ANCIENNES COLONNES NETWORK
    # SI LA FONCTION EST RELANCÉE SUR UN DF DÉJÀ ENRICHI
    # ========================================================

    source.drop(

        columns=[

            col

            for col
            in colonnes_swisscovery

            if col
            in source.columns

        ],

        errors="ignore",

        inplace=True,

    )

    colonnes_originales = list(
        source.columns
    )

    # ========================================================
    # NORMALISATION PPN
    # ========================================================

    source[
        "_ppn_requete"
    ] = (

        source[
            colonne_ppn
        ]

        .map(
            normaliser_ppn
        )

    )

    liste_ppn = (

        source[
            "_ppn_requete"
        ]

        .dropna()

        .drop_duplicates()

        .tolist()

    )

    total_ppn = len(
        liste_ppn
    )

    # ========================================================
    # AFFICHAGE
    # ========================================================

    print()
    print("#" * 80)

    print(
        "SWISSCOVERY NETWORK — VERSION FINALE OPTIMISÉE"
    )

    print("#" * 80)

    print()

    print(
        f"PPN uniques         : "
        f"{total_ppn:,}"
    )

    print(
        f"Workers phase 1     : "
        f"{workers_phase1}"
    )

    print(
        f"Workers phase 2     : "
        f"{workers_phase2}"
    )

    print(
        f"maximumRecords      : "
        f"{TAILLE_LOT}"
    )

    print(
        f"Retries rapides     : "
        f"{FAST_RETRIES}"
    )

    print(
        f"Backoff rapide      : "
        f"{FAST_BACKOFF}"
    )

    print()

    # ========================================================
    # STOCKAGE
    # ========================================================

    resultats = []

    erreurs_ppn = []

    pages_suivantes = []

    pages_erreur = []

    total_notices_annoncees = 0

    total_records_recus = 0

    # ========================================================
    # PHASE 1
    # ========================================================

    print("=" * 80)

    print(
        "1/3 — PREMIÈRES PAGES"
    )

    print("=" * 80)

    debut_phase1 = (
        time.perf_counter()
    )

    with ThreadPoolExecutor(
        max_workers=workers_phase1
    ) as executor:

        futures = {

            executor.submit(

                traiter_premiere_page,

                ppn,

                "fast",

            ):
            ppn

            for ppn
            in liste_ppn

        }

        termines = 0

        for future in as_completed(
            futures
        ):

            termines += 1

            ppn = futures[
                future
            ]

            try:

                resultat = (
                    future.result()
                )

                total = resultat[
                    "total"
                ]

                total_notices_annoncees += (
                    total
                )

                total_records_recus += (

                    resultat[
                        "records"
                    ]

                )

                if resultat[
                    "lignes"
                ]:

                    resultats.extend(

                        resultat[
                            "lignes"
                        ]

                    )

                if total > TAILLE_LOT:

                    pages_suivantes.extend(

                        (

                            ppn,

                            start,

                        )

                        for start
                        in range(

                            TAILLE_LOT + 1,

                            total + 1,

                            TAILLE_LOT,

                        )

                    )

            except Exception:

                erreurs_ppn.append(
                    ppn
                )

            if (

                termines % 250 == 0

                or

                termines == total_ppn

            ):

                print(

                    f"{termines:>6}/"
                    f"{total_ppn} PPN "

                    f"| notices annoncées : "
                    f"{total_notices_annoncees:,} "

                    f"| pages suivantes : "
                    f"{len(pages_suivantes):,} "

                    f"| erreurs : "
                    f"{len(erreurs_ppn):,}"

                )

    duree_phase1 = (

        time.perf_counter()
        - debut_phase1

    )

    # ========================================================
    # PHASE 2
    # ========================================================

    print()
    print("=" * 80)

    print(
        "2/3 — PAGES SUIVANTES"
    )

    print("=" * 80)

    print(
        f"Pages à récupérer : "
        f"{len(pages_suivantes):,}"
    )

    debut_phase2 = (
        time.perf_counter()
    )

    nb_pages = len(
        pages_suivantes
    )

    if pages_suivantes:

        with ThreadPoolExecutor(
            max_workers=workers_phase2
        ) as executor:

            futures = {

                executor.submit(

                    traiter_page_suivante,

                    ppn,

                    start,

                    "fast",

                ):
                (
                    ppn,
                    start,
                )

                for ppn, start
                in pages_suivantes

            }

            termines = 0

            for future in as_completed(
                futures
            ):

                termines += 1

                ppn, start = futures[
                    future
                ]

                try:

                    resultat = (
                        future.result()
                    )

                    total_records_recus += (

                        resultat[
                            "records"
                        ]

                    )

                    if resultat[
                        "lignes"
                    ]:

                        resultats.extend(

                            resultat[
                                "lignes"
                            ]

                        )

                except Exception:

                    pages_erreur.append(
                        (
                            ppn,
                            start,
                        )
                    )

                if (

                    termines % 250 == 0

                    or

                    termines == nb_pages

                ):

                    print(

                        f"{termines:>6}/"
                        f"{nb_pages} pages "

                        f"| résultats ISBN : "
                        f"{len(resultats):,} "

                        f"| erreurs pages : "
                        f"{len(pages_erreur):,}"

                    )

    duree_phase2 = (

        time.perf_counter()
        - debut_phase2

    )

    # ========================================================
    # RÉPARATION
    # ========================================================

    debut_reparation = (
        time.perf_counter()
    )

    # ========================================================
    # PAGES EN ÉCHEC
    # ========================================================

    if pages_erreur:

        pages_erreur = reparer_pages(

            pages_erreur,

            resultats,

        )

    # ========================================================
    # PREMIÈRES PAGES EN ÉCHEC
    # ========================================================

    if erreurs_ppn:

        print()

        print(
            f"Réparation de "
            f"{len(erreurs_ppn):,} "
            f"premières pages..."
        )

        erreurs_finales_ppn = []

        with ThreadPoolExecutor(
            max_workers=REPAIR_WORKERS
        ) as executor:

            futures = {

                executor.submit(

                    traiter_premiere_page,

                    ppn,

                    "repair",

                ):
                ppn

                for ppn
                in erreurs_ppn

            }

            for future in as_completed(
                futures
            ):

                ppn = futures[
                    future
                ]

                try:

                    resultat = (
                        future.result()
                    )

                    if resultat[
                        "lignes"
                    ]:

                        resultats.extend(

                            resultat[
                                "lignes"
                            ]

                        )

                    total = resultat[
                        "total"
                    ]

                    if total > TAILLE_LOT:

                        pages_reparation = [

                            (
                                ppn,
                                start,
                            )

                            for start
                            in range(

                                TAILLE_LOT + 1,

                                total + 1,

                                TAILLE_LOT,

                            )

                        ]

                        pages_restantes = reparer_pages(

                            pages_reparation,

                            resultats,

                        )

                        if pages_restantes:

                            erreurs_finales_ppn.append(
                                ppn
                            )

                except Exception:

                    erreurs_finales_ppn.append(
                        ppn
                    )

        erreurs_ppn = list(
            dict.fromkeys(
                erreurs_finales_ppn
            )
        )

    duree_reparation = (

        time.perf_counter()
        - debut_reparation

    )

    # ========================================================
    # DATAFRAME
    # ========================================================

    print()
    print("=" * 80)

    print(
        "3/3 — DATAFRAME"
    )

    print("=" * 80)

    debut_dataframe = (
        time.perf_counter()
    )

    # ========================================================
    # AUCUN RÉSULTAT
    # ========================================================

    if not resultats:

        resultat_final = pd.DataFrame(

            columns=(

                colonnes_originales

                +

                colonnes_swisscovery

            )

        )

        resultat_final.attrs[
            "ppn_erreurs"
        ] = erreurs_ppn

        resultat_final.attrs[
            "pages_erreurs"
        ] = pages_erreur

        resultat_final.attrs[
            "duree_phase1"
        ] = duree_phase1

        resultat_final.attrs[
            "duree_phase2"
        ] = duree_phase2

        resultat_final.attrs[
            "duree_reparation"
        ] = duree_reparation

        resultat_final.attrs[
            "duree_totale"
        ] = (
            time.perf_counter()
            - debut_total
        )

        return resultat_final

    # ========================================================
    # DATAFRAME ISBN
    # ========================================================

    df_isbn = (
        pd.DataFrame.from_records(
            resultats
        )
    )

    # ========================================================
    # DÉDOUBLONNAGE
    #
    # même PPN + même ISBN normalisé
    # ========================================================

    df_isbn.drop_duplicates(

        subset=[
            "_ppn_requete",
            "isbn_normalise",
        ],

        keep="first",

        inplace=True,

    )

    # ========================================================
    # MERGE
    # ========================================================

    resultat_final = source.merge(

        df_isbn,

        on="_ppn_requete",

        how="inner",

        sort=False,

        copy=False,

    )

    resultat_final.drop(

        columns=[
            "_ppn_requete"
        ],

        inplace=True,

    )

    resultat_final = resultat_final[

        colonnes_originales

        +

        colonnes_swisscovery

    ]

    # ========================================================
    # TYPES
    # ========================================================

    resultat_final[
        colonne_ppn
    ] = (

        resultat_final[
            colonne_ppn
        ]

        .astype(
            "string"
        )

    )

    resultat_final[
        "isbn_normalise"
    ] = (

        resultat_final[
            "isbn_normalise"
        ]

        .astype(
            "string"
        )

    )

    resultat_final[
        "annee"
    ] = (

        pd.to_numeric(

            resultat_final[
                "annee"
            ],

            errors="coerce",

        )

        .astype(
            "Int64"
        )

    )

    resultat_final.reset_index(

        drop=True,

        inplace=True,

    )

    duree_dataframe = (

        time.perf_counter()
        - debut_dataframe

    )

    duree_totale = (

        time.perf_counter()
        - debut_total

    )

    # ========================================================
    # ATTRIBUTS
    # ========================================================

    resultat_final.attrs[
        "ppn_erreurs"
    ] = erreurs_ppn

    resultat_final.attrs[
        "pages_erreurs"
    ] = pages_erreur

    resultat_final.attrs[
        "duree_phase1"
    ] = duree_phase1

    resultat_final.attrs[
        "duree_phase2"
    ] = duree_phase2

    resultat_final.attrs[
        "duree_reparation"
    ] = duree_reparation

    resultat_final.attrs[
        "duree_totale"
    ] = duree_totale

    # ========================================================
    # BILAN
    # ========================================================

    print()
    print("#" * 80)

    print(
        "TERMINÉ"
    )

    print("#" * 80)

    print()

    print(
        f"Premières pages      : "
        f"{duree_phase1:.1f} s"
    )

    print(
        f"Pages suivantes      : "
        f"{duree_phase2:.1f} s"
    )

    print(
        f"Réparation           : "
        f"{duree_reparation:.1f} s"
    )

    print(
        f"DataFrame            : "
        f"{duree_dataframe:.1f} s"
    )

    print()

    print(
        f"DURÉE TOTALE         : "
        f"{duree_totale:.1f} s "
        f"({duree_totale / 60:.2f} min)"
    )

    print()

    print(
        f"PPN interrogés       : "
        f"{total_ppn:,}"
    )

    print(
        f"Notices annoncées    : "
        f"{total_notices_annoncees:,}"
    )

    print(
        f"Records reçus        : "
        f"{total_records_recus:,}"
    )

    print(
        f"Pages supplémentaires: "
        f"{len(pages_suivantes):,}"
    )

    print()

    print(
        f"PPN avec ISBN        : "
        f"{resultat_final[colonne_ppn].nunique():,}"
    )

    print(
        f"ISBN uniques         : "
        f"{resultat_final['isbn_normalise'].nunique():,}"
    )

    print(
        f"Lignes finales       : "
        f"{len(resultat_final):,}"
    )

    print()

    print(
        f"PPN en erreur        : "
        f"{len(erreurs_ppn):,}"
    )

    print(
        f"Pages en erreur      : "
        f"{len(pages_erreur):,}"
    )

    print("#" * 80)

    return resultat_final