import requests
import pandas as pd
import re
import unicodedata
import threading
import time
import random
import xml.etree.ElementTree as ET
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

ENDPOINT_URL = "https://data.bnf.fr/sparql"

# Les 5 scénarios principaux peuvent tourner en parallèle
MAX_WORKERS = 5

# Enrichissement par lots
MAX_WORKERS_ENRICHISSEMENT = 4
TAILLE_LOT_ENRICHISSEMENT = 150

# Pour les sujets trouvés
TAILLE_LOT_SUJETS = 50

TIMEOUT = 180
MAX_RETRIES = 3

HEADERS = {
    "Accept": "application/sparql-results+json",
    "User-Agent": "Mozilla/5.0"
}


# ============================================================
# CONTROLE DES NOTICES NUMERIQUES BNF — SRU / UNIMARC 182$c
# ============================================================

SRU_BNF_URL = "https://catalogue.bnf.fr/api/SRU"

# Paramètres issus du code de contrôle BnF :
# - lots de 50 ARK
# - 20 workers SRU
# - en cas d'incertitude, la notice est conservée
SRU_WORKERS = 20
SRU_MAX_ARKS_PAR_LOT = 50
SRU_CONNECT_TIMEOUT = 10
SRU_READ_TIMEOUT = 45
SRU_RETRIES = 3


# ============================================================
# COMMUNES DU CANTON DE GENÈVE
# ============================================================

def charger_communes():
    """
    Charge automatiquement les communes depuis :

        Liste_communes/liste commune geneve.txt

    Formats acceptés :
        "Genf",
        "Genève",
        "Geneve",

    ou :
        Genf
        Genève
        Geneve

    Les lignes vides sont ignorées et les doublons exacts sont
    supprimés en conservant l'ordre d'origine.
    """

    if "__file__" in globals():
        dossier_base = Path(__file__).resolve().parent
    else:
        dossier_base = Path.cwd()

    candidats = [
        dossier_base / "Liste_communes" / "liste commune geneve.txt",
        dossier_base / "Projet_BGE" / "Liste_communes" / "liste commune geneve.txt",
        Path.cwd() / "Liste_communes" / "liste commune geneve.txt",
        Path.cwd() / "Projet_BGE" / "Liste_communes" / "liste commune geneve.txt",
    ]

    fichier = next(
        (
            chemin
            for chemin in candidats
            if chemin.is_file()
        ),
        None
    )

    if fichier is None:
        chemins_testes = "\n".join(
            f" - {chemin}"
            for chemin in candidats
        )

        raise FileNotFoundError(
            "Impossible de trouver le fichier des communes.\n"
            "Emplacements testés :\n"
            f"{chemins_testes}"
        )

    communes = []

    with fichier.open(
        "r",
        encoding="utf-8-sig"
    ) as f:

        for ligne in f:
            ligne = ligne.strip()

            if not ligne:
                continue

            ligne = (
                ligne
                .rstrip(",")
                .strip()
                .strip('"')
                .strip("'")
                .strip()
            )

            if ligne:
                communes.append(ligne)

    communes = list(
        dict.fromkeys(communes)
    )

    if not communes:
        raise ValueError(
            f"Aucune commune valide trouvée dans : {fichier}"
        )

    print(
        f"Communes chargées : {len(communes)}"
        f" | fichier : {fichier}"
    )

    return communes


COMMUNES_GENEVE = charger_communes()


# ============================================================
# SESSION HTTP PAR THREAD
# ============================================================

thread_local = threading.local()


def creer_session():

    session = requests.Session()

    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        status=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[
            429,
            500,
            502,
            503,
            504
        ],
        allowed_methods=["POST"]
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10
    )

    session.mount(
        "https://",
        adapter
    )

    session.headers.update(
        HEADERS
    )

    return session


def get_session():

    if not hasattr(
        thread_local,
        "session"
    ):
        thread_local.session = creer_session()

    return thread_local.session


# ============================================================
# PREFIXES SPARQL
# ============================================================

PREFIXES = """
PREFIX bnf-onto: <http://data.bnf.fr/ontology/bnf-onto/>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX lc: <http://id.loc.gov/vocabulary/relators/>
PREFIX rdagroup2elements: <http://rdvocab.info/ElementsGr2/>
PREFIX rdarelationships: <http://rdvocab.info/RDARelationshipsWEMI/>
PREFIX rdvocab: <http://rdvocab.info/Elements/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
"""


# ============================================================
# NORMALISATION DES TEXTES
# ============================================================

def normaliser_texte(texte):

    if texte is None:
        return ""

    texte = str(texte).lower().strip()

    texte = (
        texte
        .replace("œ", "oe")
        .replace("æ", "ae")
        .replace("’", "'")
    )

    texte = "".join(
        c
        for c in unicodedata.normalize(
            "NFD",
            texte
        )
        if unicodedata.category(c) != "Mn"
    )

    return texte


# ============================================================
# VARIANTES DES COMMUNES
# ============================================================

def construire_variantes():

    variantes = set()

    for commune in COMMUNES_GENEVE:

        variantes.add(
            commune.lower()
        )

        variantes.add(
            normaliser_texte(commune)
        )

    variantes.update([
        "geneva",
        "genf",
        "ginevra"
    ])

    return sorted(
        variantes,
        key=len,
        reverse=True
    )


VARIANTES = construire_variantes()


# ============================================================
# EXPRESSION PLEIN TEXTE VIRTUOSO
# ============================================================

def construire_bif_contains():

    termes = []

    for variante in VARIANTES:

        variante = variante.replace(
            "'",
            " "
        ).strip()

        if variante:
            termes.append(
                f"'{variante}'"
            )

    return " OR ".join(termes)


BIF_COMMUNES = construire_bif_contains()





# ============================================================
# TEST PYTHON :
# LE TEXTE CONTIENT-IL UNE COMMUNE ?
# ============================================================

COMMUNES_NORMALISEES = sorted(
    {
        normaliser_texte(x)
        for x in COMMUNES_GENEVE
    }
    |
    {
        "geneva",
        "genf",
        "ginevra"
    },
    key=len,
    reverse=True
)


def contient_commune(texte):

    texte = normaliser_texte(
        texte
    )

    if not texte:
        return False

    for commune in COMMUNES_NORMALISEES:

        if commune == "gy":

            if re.search(
                r"(?<![a-z])gy(?![a-z])",
                texte
            ):
                return True

        else:

            if commune in texte:
                return True

    return False


# ============================================================
# ISBN :
# ACCEPTER LES DEUX PROPRIETES BnF
# ============================================================

BLOC_ISBN = """
?genevensia ?isbnProperty ?isbn .

VALUES ?isbnProperty {
    bnf-onto:isbn
    bnf-onto:ISBN
}
"""


# ============================================================
# REQUETE SPARQL
# ============================================================

def requete_sparql(query):

    session = get_session()

    response = session.post(
        ENDPOINT_URL,
        data={
            "query": query,
            "format": "application/sparql-results+json"
        },
        timeout=TIMEOUT
    )

    response.raise_for_status()

    try:
        return response.json()

    except Exception:

        print(
            response.text[:1000]
        )

        raise


# ============================================================
# JSON -> LIGNES
# ============================================================

def bindings_vers_lignes(
    resultat,
    raison=None
):

    lignes = []

    bindings = (
        resultat
        .get("results", {})
        .get("bindings", [])
    )

    for binding in bindings:

        ligne = {
            cle: valeur.get("value")
            for cle, valeur
            in binding.items()
        }

        if raison:
            ligne["raison"] = raison

        lignes.append(
            ligne
        )

    return lignes


# ============================================================
# 1 — TITRE
# ============================================================

def chercher_titre():

    debut = time.time()

    query = PREFIXES + f"""

SELECT DISTINCT
    ?genevensia
    ?isbn
    ?titreRecherche

WHERE {{

    {BLOC_ISBN}

    ?genevensia
        dcterms:title
        ?titreRecherche .

    ?titreRecherche
        bif:contains
        "{BIF_COMMUNES}" .
}}
"""

    resultat = requete_sparql(
        query
    )

    lignes = bindings_vers_lignes(
        resultat,
        "titre"
    )

    lignes = [
        ligne
        for ligne in lignes
        if contient_commune(
            ligne.get(
                "titreRecherche"
            )
        )
    ]

    for ligne in lignes:
        ligne.pop(
            "titreRecherche",
            None
        )

    print(
        f"✓ titre : "
        f"{len(lignes):,} lignes "
        f"({time.time()-debut:.1f}s)"
    )

    return lignes


# ============================================================
# SUPPRESSION DES ACCENTS
# ============================================================

def sans_accents(texte):

    texte = (
        texte
        .replace("œ", "oe")
        .replace("Œ", "OE")
    )

    return "".join(
        caractere
        for caractere in unicodedata.normalize(
            "NFD",
            texte
        )
        if unicodedata.category(caractere) != "Mn"
    )


# ============================================================
# 2 — LIEU DE PUBLICATION
# ============================================================

def chercher_lieu_publication():

    debut = time.time()

    variantes = set()

    for commune in COMMUNES_GENEVE:

        if commune == "Gy":
            continue

        variantes.add(commune)

        variante_ascii = sans_accents(commune)

        if variante_ascii != commune:
            variantes.add(variante_ascii)

    variantes.update([
        "Geneva",
        "Genf",
        "Ginevra"
    ])

    recherche = " OR ".join(
        f"'{variante}'"
        for variante in sorted(
            variantes,
            key=len,
            reverse=True
        )
    )

    query_principale = PREFIXES + f"""

SELECT DISTINCT
    ?genevensia
    ?isbn
    ?lieuPublication

WHERE {{

    ?genevensia
        rdvocab:placeOfPublication
        ?lieuPublication ;
        bnf-onto:isbn
        ?isbn .

    ?lieuPublication
        bif:contains
        "{recherche}" .
}}
"""

    query_gy = PREFIXES + """

SELECT DISTINCT
    ?genevensia
    ?isbn
    ?lieuPublication

WHERE {

    ?genevensia
        rdvocab:placeOfPublication
        ?lieuPublication ;
        bnf-onto:isbn
        ?isbn .

    FILTER(
        REGEX(
            STR(?lieuPublication),
            "(^|[^A-Za-zÀ-ÿ])Gy([^A-Za-zÀ-ÿ]|$)",
            "i"
        )
    )
}
"""

    lignes = []

    with ThreadPoolExecutor(
        max_workers=2
    ) as executor:

        future_principale = executor.submit(
            requete_sparql,
            query_principale
        )

        future_gy = executor.submit(
            requete_sparql,
            query_gy
        )

        resultat_principal = (
            future_principale.result()
        )

        resultat_gy = (
            future_gy.result()
        )

    for x in (
        resultat_principal
        .get("results", {})
        .get("bindings", [])
    ):

        lignes.append({
            "genevensia":
                x["genevensia"]["value"],

            "isbn":
                x["isbn"]["value"],

            "lieuPublication":
                x["lieuPublication"]["value"],

            "raison":
                "lieu_publication"
        })

    for x in (
        resultat_gy
        .get("results", {})
        .get("bindings", [])
    ):

        lignes.append({
            "genevensia":
                x["genevensia"]["value"],

            "isbn":
                x["isbn"]["value"],

            "lieuPublication":
                x["lieuPublication"]["value"],

            "raison":
                "lieu_publication"
        })

    if lignes:

        df_temp = pd.DataFrame(lignes)

        df_temp = (
            df_temp
            .drop_duplicates(
                subset=[
                    "genevensia",
                    "isbn",
                    "lieuPublication"
                ]
            )
            .reset_index(drop=True)
        )

        lignes = df_temp.to_dict(
            orient="records"
        )

    print(
        f"✓ lieu publication : "
        f"{len(lignes):,} lignes "
        f"({time.time()-debut:.1f}s)"
    )

    return lignes


# ============================================================
# 3 — SUJET
# ============================================================

def chercher_sujet():

    debut = time.time()

    variantes = set()

    for commune in COMMUNES_GENEVE:

        if commune == "Gy":
            continue

        variantes.add(commune)

        variante_ascii = sans_accents(commune)

        if variante_ascii != commune:
            variantes.add(variante_ascii)

    variantes.update([
        "Geneva",
        "Genf",
        "Ginevra"
    ])

    recherche = " OR ".join(
        f"'{variante}'"
        for variante in sorted(
            variantes,
            key=len,
            reverse=True
        )
    )

    query_principale = PREFIXES + f"""

SELECT DISTINCT
    ?genevensia
    ?isbn
    ?sujet
    ?labelSujet

WHERE {{

    ?genevensia
        dcterms:subject
        ?sujet ;
        bnf-onto:isbn
        ?isbn .

    ?sujet
        skos:prefLabel
        ?labelSujet .

    ?labelSujet
        bif:contains
        "{recherche}" .
}}
"""

    query_gy = PREFIXES + """

SELECT DISTINCT
    ?genevensia
    ?isbn
    ?sujet
    ?labelSujet

WHERE {

    ?genevensia
        dcterms:subject
        ?sujet ;
        bnf-onto:isbn
        ?isbn .

    ?sujet
        skos:prefLabel
        ?labelSujet .

    FILTER(
        REGEX(
            STR(?labelSujet),
            "(^|[^A-Za-zÀ-ÿ])Gy([^A-Za-zÀ-ÿ]|$)",
            "i"
        )
    )
}
"""

    lignes = []

    with ThreadPoolExecutor(
        max_workers=2
    ) as executor:

        future_principal = executor.submit(
            requete_sparql,
            query_principale
        )

        future_gy = executor.submit(
            requete_sparql,
            query_gy
        )

        resultat_principal = (
            future_principal.result()
        )

        resultat_gy = (
            future_gy.result()
        )

    for resultat in [
        resultat_principal,
        resultat_gy
    ]:

        for x in (
            resultat
            .get("results", {})
            .get("bindings", [])
        ):

            lignes.append({
                "genevensia":
                    x["genevensia"]["value"],

                "isbn":
                    x["isbn"]["value"],

                "sujet":
                    x["sujet"]["value"],

                "labelSujet":
                    x["labelSujet"]["value"],

                "raison":
                    "sujet"
            })

    if lignes:

        df_temp = pd.DataFrame(lignes)

        df_temp = (
            df_temp
            .drop_duplicates(
                subset=[
                    "genevensia",
                    "isbn",
                    "sujet"
                ]
            )
            .reset_index(drop=True)
        )

        lignes = df_temp.to_dict(
            orient="records"
        )

    print(
        f"✓ sujet : "
        f"{len(lignes):,} lignes "
        f"({time.time()-debut:.1f}s)"
    )

    return lignes


# ============================================================
# 4 — AUTEUR NE DANS UNE COMMUNE GENEVOISE
# ============================================================

def chercher_naissance():

    debut = time.time()

    query = PREFIXES + f"""

SELECT DISTINCT
    ?genevensia
    ?isbn

WHERE {{

    {BLOC_ISBN}

    ?genevensia
        rdarelationships:expressionManifested
        ?expression .

    ?expression
        lc:aut
        ?auteur .

    ?auteur
        rdagroup2elements:placeOfBirth
        ?lieu .

    ?lieu
        bif:contains
        "{BIF_COMMUNES}" .
}}
"""

    try:

        resultat = requete_sparql(
            query
        )

        lignes = bindings_vers_lignes(
            resultat,
            "naissance_auteur"
        )

    except Exception:

        regex = (
            "("
            +
            "|".join(VARIANTES)
            +
            ")"
        )

        query = PREFIXES + f"""

SELECT DISTINCT
    ?genevensia
    ?isbn

WHERE {{

    {BLOC_ISBN}

    ?genevensia
        rdarelationships:expressionManifested
        ?expression .

    ?expression
        lc:aut
        ?auteur .

    ?auteur
        rdagroup2elements:placeOfBirth
        ?lieu .

    FILTER(
        REGEX(
            LCASE(
                STR(?lieu)
            ),
            "{regex}"
        )
    )
}}
"""

        resultat = requete_sparql(
            query
        )

        lignes = bindings_vers_lignes(
            resultat,
            "naissance_auteur"
        )

    print(
        f"✓ naissance : "
        f"{len(lignes):,} lignes "
        f"({time.time()-debut:.1f}s)"
    )

    return lignes


# ============================================================
# 5 — AUTEUR DECEDE DANS UNE COMMUNE GENEVOISE
# ============================================================

def chercher_deces():

    debut = time.time()

    query = PREFIXES + f"""

SELECT DISTINCT
    ?genevensia
    ?isbn

WHERE {{

    {BLOC_ISBN}

    ?genevensia
        rdarelationships:expressionManifested
        ?expression .

    ?expression
        lc:aut
        ?auteur .

    ?auteur
        rdagroup2elements:placeOfDeath
        ?lieu .

    ?lieu
        bif:contains
        "{BIF_COMMUNES}" .
}}
"""

    try:

        resultat = requete_sparql(
            query
        )

        lignes = bindings_vers_lignes(
            resultat,
            "deces_auteur"
        )

    except Exception:

        regex = (
            "("
            +
            "|".join(VARIANTES)
            +
            ")"
        )

        query = PREFIXES + f"""

SELECT DISTINCT
    ?genevensia
    ?isbn

WHERE {{

    {BLOC_ISBN}

    ?genevensia
        rdarelationships:expressionManifested
        ?expression .

    ?expression
        lc:aut
        ?auteur .

    ?auteur
        rdagroup2elements:placeOfDeath
        ?lieu .

    FILTER(
        REGEX(
            LCASE(
                STR(?lieu)
            ),
            "{regex}"
        )
    )
}}
"""

        resultat = requete_sparql(
            query
        )

        lignes = bindings_vers_lignes(
            resultat,
            "deces_auteur"
        )

    print(
        f"✓ décès : "
        f"{len(lignes):,} lignes "
        f"({time.time()-debut:.1f}s)"
    )

    return lignes


# ============================================================
# DETECTION PARALLELE
# ============================================================

def detection_parallele():

    fonctions = {
        "titre": chercher_titre,
        "lieu_publication": chercher_lieu_publication,
        "sujet": chercher_sujet,
        "naissance": chercher_naissance,
        "deces": chercher_deces
    }

    toutes_lignes = []

    debut = time.time()

    print()
    print("==========================================")
    print("LANCEMENT DES RECHERCHES BnF")
    print("==========================================")
    print()

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {

            executor.submit(
                fonction
            ): nom

            for nom, fonction
            in fonctions.items()
        }

        for future in as_completed(
            futures
        ):

            nom = futures[
                future
            ]

            try:

                lignes = future.result()

                toutes_lignes.extend(
                    lignes
                )

            except Exception as erreur:

                print(
                    f"✗ ERREUR {nom} : "
                    f"{type(erreur).__name__} : "
                    f"{erreur}"
                )

    print()
    print(
        f"Détection terminée en "
        f"{time.time()-debut:.1f}s"
    )

    return toutes_lignes


# ============================================================
# NORMALISATION ISBN
# ============================================================

def normaliser_isbn(isbn):

    if isbn is None:
        return None

    isbn = re.sub(
        r"[^0-9X]",
        "",
        str(isbn).upper().strip()
    )

    def isbn13_valide(valeur):

        if len(valeur) != 13 or not valeur.isdigit():
            return False

        somme = sum(
            int(chiffre) * (
                1 if index % 2 == 0 else 3
            )
            for index, chiffre
            in enumerate(valeur[:12])
        )

        cle = (
            10 - (somme % 10)
        ) % 10

        return cle == int(
            valeur[-1]
        )

    def isbn10_valide(valeur):

        if len(valeur) != 10:
            return False

        if not valeur[:9].isdigit():
            return False

        if not (
            valeur[-1].isdigit()
            or valeur[-1] == "X"
        ):
            return False

        total = 0

        for index, caractere in enumerate(
            valeur
        ):

            poids = 10 - index

            chiffre = (
                10
                if caractere == "X"
                else int(caractere)
            )

            total += poids * chiffre

        return total % 11 == 0

    if len(isbn) == 13:

        if isbn13_valide(isbn):
            return isbn

        return None

    if len(isbn) == 10:

        if not isbn10_valide(isbn):
            return None

        base = "978" + isbn[:9]

        somme = sum(
            int(chiffre) * (
                1 if index % 2 == 0 else 3
            )
            for index, chiffre
            in enumerate(base)
        )

        cle = (
            10 - (somme % 10)
        ) % 10

        isbn13 = (
            base
            +
            str(cle)
        )

        return isbn13

    return None


# ============================================================
# FUSION DETECTION
# ============================================================

def fusionner_detection(
    lignes
):

    if not lignes:

        return pd.DataFrame(
            columns=[
                "genevensia",
                "isbn_normalise",
                "raisons"
            ]
        )

    df = pd.DataFrame(
        lignes
    )

    df["isbn_normalise"] = (
        df["isbn"]
        .map(normaliser_isbn)
    )

    df = (
        df[
            df["isbn_normalise"]
            .notna()
        ]
        .copy()
    )

    df = df.drop_duplicates(
        subset=[
            "genevensia",
            "isbn_normalise",
            "raison"
        ]
    )

    df = (
        df.groupby(
            [
                "genevensia",
                "isbn_normalise"
            ],
            as_index=False
        )
        .agg(
            raisons=(
                "raison",
                lambda x:
                " | ".join(
                    sorted(
                        set(x)
                    )
                )
            )
        )
    )

    return df


# ============================================================
# FILTRE UNIMARC 182$c — VERSION OPTIMISÉE PAR LOTS
#
# 182 $c = c
#     -> média informatique
#     -> ISBN à exclure
#
# Principes :
# - un ARK unique n'est interrogé qu'une seule fois ;
# - plusieurs ARK sont regroupés dans une requête SRU ;
# - lots fixes de 50 ARK par slicing direct ;
# - 20 workers SRU (benchmarkés) ;
# - retry/backoff uniquement sur erreurs transitoires ;
# - 400/414 -> subdivision automatique du lot ;
# - en cas d'incertitude, la notice est conservée.
# ============================================================

sru_thread_local = threading.local()


def _nom_local_xml(tag):
    """Retourne le nom local d'un tag XML, avec ou sans namespace."""

    if not tag:
        return ""

    return str(tag).split("}")[-1]


def _normaliser_ark_bnf(ark):
    """
    Normalise un ARK BnF vers la forme :

        cb399132448
    """

    if ark is None or pd.isna(ark):
        return None

    ark = str(ark).strip()

    if not ark:
        return None

    if "ark:/12148/" in ark:
        ark = ark.split("ark:/12148/", 1)[1]

    ark = ark.split("#", 1)[0].strip()

    return ark or None


def _creer_session_sru():
    """
    Session HTTP dédiée au SRU.

    Les retries sont gérés manuellement afin de contrôler précisément
    les 429, timeouts et subdivisions 400/414.
    """

    session = requests.Session()

    adapter = HTTPAdapter(
        max_retries=0,
        pool_connections=SRU_WORKERS + 2,
        pool_maxsize=SRU_WORKERS + 2,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent":
            "Geneve-Bibliographic-Research-BNF-SRU/5.0",
        "Accept":
            "application/xml, text/xml;q=0.9, */*;q=0.1",
        "Accept-Encoding":
            "gzip, deflate",
    })

    return session


def _obtenir_session_sru():
    """Retourne une session SRU propre au thread courant."""

    if not hasattr(sru_thread_local, "session"):
        sru_thread_local.session = _creer_session_sru()

    return sru_thread_local.session


def _construire_query_sru_arks(arks):
    """Construit une requête CQL compacte pour plusieurs ARK BnF.

    Forme benchmarkée la plus rapide :
        bib.persistentid any "ark:/12148/cb... ark:/12148/cb..."
    """

    valeurs = " ".join(
        f"ark:/12148/{ark}"
        for ark in arks
    )

    return f'bib.persistentid any "{valeurs}"'


def _construire_params_sru(arks):
    """Paramètres SRU d'un lot d'ARK."""

    return {
        "version": "1.2",
        "operation": "searchRetrieve",
        "query": _construire_query_sru_arks(arks),
        "recordSchema": "unimarcXchange",
        "maximumRecords": len(arks),
        "startRecord": 1,
    }


def _construire_lots_sru(arks):
    """Découpe directement les ARK en lots fixes.

    Le calcul dynamique de longueur d'URL a été supprimé après benchmark :
    avec 50 ARK et ``bib.persistentid any``, il ajoutait un coût Python
    important sans bénéfice fonctionnel.
    """

    if not arks:
        return []

    taille = SRU_MAX_ARKS_PAR_LOT

    return [
        arks[i:i + taille]
        for i in range(0, len(arks), taille)
    ]


def _parser_notices_sru_182c(xml_content):
    """
    Parse une réponse SRU ``unimarcXchange``.

    Retourne :
        nb_sru              : numberOfRecords annoncé par le SRU
        statut_par_ark      : {"cb...": True/False}
        non_controllables   : set d'ARK dont le SRU a trouvé la notice,
                              mais n'a pas fourni de MARC exploitable
                              (ex. diagnostic SRU 1/131).

    Signification de ``statut_par_ark`` :
        True  -> 182$c = c -> média informatique -> ISBN à exclure
        False -> pas de 182$c = c OU notice non contrôlable -> ISBN conservé

    Règle de sécurité importante :
    si le SRU renvoie un ``recordIdentifier`` mais ``recordData`` contient
    uniquement un diagnostic et aucune donnée MARC, on NE RETENTE PAS cette
    notice. Elle est considérée comme non contrôlable pour 182$c et son ISBN
    est conservé.
    """

    root = ET.fromstring(xml_content)

    nb_sru = None
    statut_par_ark = {}
    non_controllables = set()

    # --------------------------------------------------------
    # numberOfRecords
    # --------------------------------------------------------
    for element in root.iter():
        if _nom_local_xml(element.tag) == "numberOfRecords":
            try:
                nb_sru = int(element.text)
            except (TypeError, ValueError):
                nb_sru = None
            break

    # --------------------------------------------------------
    # 1) Enveloppes SRU contenant un diagnostic à la place du MARC
    # --------------------------------------------------------
    for record_sru in root.iter():
        if _nom_local_xml(record_sru.tag) != "record":
            continue

        enfants = list(record_sru)
        noms_enfants = {
            _nom_local_xml(enfant.tag)
            for enfant in enfants
        }

        # Une enveloppe SRU a typiquement recordData + recordIdentifier.
        if "recordData" not in noms_enfants or "recordIdentifier" not in noms_enfants:
            continue

        record_identifier = None
        record_data = None

        for enfant in enfants:
            nom = _nom_local_xml(enfant.tag)

            if nom == "recordIdentifier":
                record_identifier = (enfant.text or "").strip()

            elif nom == "recordData":
                record_data = enfant

        ark = _normaliser_ark_bnf(record_identifier)

        if not ark or record_data is None:
            continue

        # Cherche de vraies données MARC à l'intérieur de recordData.
        contient_marc = any(
            _nom_local_xml(element.tag) in {
                "leader",
                "controlfield",
                "datafield",
            }
            for element in record_data.iter()
        )

        if contient_marc:
            continue

        # Cherche un diagnostic SRU.
        contient_diagnostic = any(
            _nom_local_xml(element.tag) == "diagnostic"
            for element in record_data.iter()
        )

        if contient_diagnostic:
            # Pas de MARC => impossible de vérifier 182$c.
            # Par sécurité métier, on garde l'ISBN et on ne retry pas.
            statut_par_ark[ark] = False
            non_controllables.add(ark)

    # --------------------------------------------------------
    # 2) Notices MARC réelles
    # --------------------------------------------------------
    for record in root.iter():

        if _nom_local_xml(record.tag) != "record":
            continue

        noms_enfants = {
            _nom_local_xml(enfant.tag)
            for enfant in list(record)
        }

        # Ignore l'enveloppe SRU et garde uniquement la notice MARC.
        if not (
            "leader" in noms_enfants
            or "controlfield" in noms_enfants
            or "datafield" in noms_enfants
        ):
            continue

        ark = _normaliser_ark_bnf(
            record.attrib.get("id")
        )

        if not ark:
            continue

        media_informatique = False

        for champ in record:

            if _nom_local_xml(champ.tag) != "datafield":
                continue

            if champ.attrib.get("tag") != "182":
                continue

            for sous_champ in champ:

                if _nom_local_xml(sous_champ.tag) != "subfield":
                    continue

                if sous_champ.attrib.get("code") != "c":
                    continue

                valeur = (
                    sous_champ.text or ""
                ).strip().lower()

                if valeur == "c":
                    media_informatique = True
                    break

            if media_informatique:
                break

        statut_par_ark[ark] = media_informatique

    return nb_sru, statut_par_ark, non_controllables


def _attente_retry_sru(response, tentative):
    """Calcule un backoff court, en respectant Retry-After si présent."""

    if response is not None:
        retry_after = response.headers.get("Retry-After")

        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass

    return (
        (2 ** (tentative - 1))
        + random.uniform(0.15, 0.65)
    )


def _fusionner_resultats_sru(gauche, droite):
    """Fusionne deux triplets (statuts, erreurs, non_controllables)."""

    statut_g, erreurs_g, non_ctrl_g = gauche
    statut_d, erreurs_d, non_ctrl_d = droite

    statut_g.update(statut_d)

    return (
        statut_g,
        erreurs_g + erreurs_d,
        set(non_ctrl_g) | set(non_ctrl_d),
    )


def _executer_lot_sru(arks, tentatives=SRU_RETRIES):
    """
    Exécute un lot SRU.

    Retour :
        statut_par_ark, erreurs, non_controllables

    - 400/414 : subdivision récursive.
    - 429/5xx/timeout/connexion : retry/backoff.
    - diagnostic SRU avec recordIdentifier mais sans MARC :
      conservé immédiatement (False) et AUCUN retry inutile.
    """

    if not arks:
        return {}, [], set()

    session = _obtenir_session_sru()
    params = _construire_params_sru(arks)
    derniere_erreur = None

    for tentative in range(1, tentatives + 1):

        response = None

        try:
            response = session.get(
                SRU_BNF_URL,
                params=params,
                timeout=(
                    SRU_CONNECT_TIMEOUT,
                    SRU_READ_TIMEOUT,
                ),
            )

            # ------------------------------------------------
            # URL/requête refusée : on subdivise le lot.
            # ------------------------------------------------
            if response.status_code in (400, 414):

                if len(arks) <= 1:
                    return {}, [{
                        "arks": list(arks),
                        "erreur": f"HTTP_{response.status_code}",
                        "message": "Lot SRU indivisible refusé",
                    }], set()

                milieu = len(arks) // 2

                return _fusionner_resultats_sru(
                    _executer_lot_sru(
                        arks[:milieu],
                        tentatives=tentatives,
                    ),
                    _executer_lot_sru(
                        arks[milieu:],
                        tentatives=tentatives,
                    ),
                )

            # ------------------------------------------------
            # Erreurs transitoires : retry ciblé.
            # ------------------------------------------------
            if response.status_code == 429 or 500 <= response.status_code <= 599:

                derniere_erreur = requests.exceptions.HTTPError(
                    f"HTTP {response.status_code}"
                )

                if tentative >= tentatives:
                    break

                time.sleep(
                    _attente_retry_sru(
                        response,
                        tentative,
                    )
                )

                continue

            response.raise_for_status()

            nb_sru, statut, non_controllables = (
                _parser_notices_sru_182c(
                    response.content
                )
            )

            # ------------------------------------------------
            # Cohérence SRU.
            # Les diagnostics sans MARC sont désormais comptés dans
            # ``statut`` avec False : ils ne génèrent donc plus d'erreur.
            # ------------------------------------------------
            if nb_sru is not None and nb_sru != len(statut):

                if len(arks) > 1:
                    milieu = len(arks) // 2

                    return _fusionner_resultats_sru(
                        _executer_lot_sru(
                            arks[:milieu],
                            tentatives=tentatives,
                        ),
                        _executer_lot_sru(
                            arks[milieu:],
                            tentatives=tentatives,
                        ),
                    )

                # Unitaire : vraie incohérence non expliquée par un
                # diagnostic SRU reconnu.
                return statut, [{
                    "arks": list(arks),
                    "erreur": "SRU_INCOHERENT",
                    "message": (
                        f"numberOfRecords={nb_sru}, "
                        f"notices_parsees={len(statut)}"
                    ),
                }], non_controllables

            return statut, [], non_controllables

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as e:

            derniere_erreur = e

            if tentative >= tentatives:
                break

            time.sleep(
                _attente_retry_sru(
                    response,
                    tentative,
                )
            )

        except (requests.exceptions.RequestException, ET.ParseError) as e:
            derniere_erreur = e
            break

    return {}, [{
        "arks": list(arks),
        "erreur": (
            type(derniere_erreur).__name__
            if derniere_erreur
            else "ErreurSRU"
        ),
        "message": (
            str(derniere_erreur)[:200]
            if derniere_erreur
            else "Erreur SRU inconnue"
        ),
    }], set()


# ============================================================
# RÉCUPÉRATION AUTOMATIQUE DES VRAIES ERREURS SRU
# ============================================================

SRU_RECOVERY1_WORKERS = 6
SRU_RECOVERY1_LOT_SIZE = 25
SRU_RECOVERY1_PAUSE = 2.0

SRU_RECOVERY2_WORKERS = 2
SRU_RECOVERY2_LOT_SIZE = 10
SRU_RECOVERY2_RETRIES = 6
SRU_RECOVERY2_PAUSE = 5.0

DERNIER_DIAGNOSTIC_SRU = {}


def _unique_ordonnee(valeurs):
    return list(dict.fromkeys(valeurs))


def _extraire_arks_erreurs_sru(erreurs):
    """Retourne les ARK uniques réellement contenus dans les erreurs."""

    arks = []

    for erreur in erreurs:

        if not isinstance(erreur, dict):
            continue

        valeurs = erreur.get("arks", [])

        if not valeurs:
            continue

        for ark in valeurs:
            ark = _normaliser_ark_bnf(ark)

            if ark:
                arks.append(ark)

    return _unique_ordonnee(arks)


def _decouper_lots(arks, taille):
    return [
        arks[i:i + taille]
        for i in range(0, len(arks), taille)
    ]


def _executer_passe_sru(
    lots,
    workers,
    tentatives,
    titre,
    progression=20,
):
    """Exécute une passe SRU et renvoie statuts/erreurs/non-controllables."""

    if not lots:
        return {}, [], set()

    print()
    print("=" * 80)
    print(titre)
    print("=" * 80)
    print(f"Lots                 : {len(lots):,}")
    print(f"Workers              : {workers}")
    print(f"Tentatives / lot     : {tentatives}")
    print("=" * 80)

    debut = time.perf_counter()

    statut_par_ark = {}
    erreurs = []
    non_controllables = set()
    termines = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:

        futures = {
            executor.submit(
                _executer_lot_sru,
                lot,
                tentatives,
            ): lot
            for lot in lots
        }

        for future in as_completed(futures):

            termines += 1
            lot = futures[future]

            try:
                statut_lot, erreurs_lot, non_ctrl_lot = future.result()

                statut_par_ark.update(statut_lot)
                erreurs.extend(erreurs_lot)
                non_controllables.update(non_ctrl_lot)

            except Exception as e:
                erreurs.append({
                    "arks": list(lot),
                    "erreur": type(e).__name__,
                    "message": str(e)[:200],
                })

            if (
                termines % progression == 0
                or termines == len(lots)
            ):
                ecoule = time.perf_counter() - debut
                nb_medias = sum(
                    bool(valeur)
                    for valeur in statut_par_ark.values()
                )

                print(
                    f"{termines:>4}/{len(lots)} lots "
                    f"| ARK contrôlés : {len(statut_par_ark):,} "
                    f"| médias info. : {nb_medias:,} "
                    f"| non contrôlables : {len(non_controllables):,} "
                    f"| erreurs : {len(erreurs):,} "
                    f"| {ecoule:.1f} s"
                )

    return statut_par_ark, erreurs, non_controllables


def filtrer_medias_informatiques_bnf(
    df_isbn,
    max_workers=SRU_WORKERS,
):
    """
    Exclut les notices dont le champ UNIMARC contient ``182$c = c``.

    Stratégie finale :

    1. Passe principale ultra-rapide
       - 20 workers
       - 50 ARK / lot
       - GET
       - CQL ``bib.persistentid any``
       - slicing direct

    2. Récupération des VRAIES erreurs uniquement
       - 6 workers
       - 25 ARK / lot

    3. Dernière récupération des erreurs résiduelles uniquement
       - 2 workers
       - 10 ARK / lot
       - 6 tentatives

    Cas spécial :
    si la BnF renvoie un ``recordIdentifier`` mais seulement un diagnostic
    SRU dans ``recordData`` (aucun MARC / aucune zone 182), l'ARK est classé
    ``non_controllable_182`` : l'ISBN est conservé et aucune récupération
    supplémentaire n'est lancée pour lui.
    """

    global DERNIER_DIAGNOSTIC_SRU

    if df_isbn.empty or "ark_bnf" not in df_isbn.columns:
        return df_isbn, 0

    arks_uniques = (
        df_isbn["ark_bnf"]
        .dropna()
        .map(_normaliser_ark_bnf)
        .dropna()
        .drop_duplicates()
        .tolist()
    )

    if not arks_uniques:
        return df_isbn, 0

    # --------------------------------------------------------
    # PASSAGE PRINCIPAL
    # --------------------------------------------------------
    lots_principaux = _construire_lots_sru(
        arks_uniques
    )

    print()
    print("=" * 80)
    print("CONTRÔLE UNIMARC 182$c — SRU BNF FINAL")
    print("=" * 80)
    print(f"ARK uniques             : {len(arks_uniques):,}")
    print(f"Lots SRU                : {len(lots_principaux):,}")
    print(f"ARK / lot max           : {SRU_MAX_ARKS_PAR_LOT}")
    print("Construction lots       : slicing direct")
    print("CQL                     : bib.persistentid any")
    print("HTTP                    : GET")
    print(f"Workers SRU             : {max_workers}")
    print("=" * 80)

    debut = time.perf_counter()

    statut_par_ark, erreurs_1, non_ctrl_1 = _executer_passe_sru(
        lots=lots_principaux,
        workers=max_workers,
        tentatives=SRU_RETRIES,
        titre="PASSAGE SRU PRINCIPAL",
        progression=10,
    )

    non_controllables = set(non_ctrl_1)

    # --------------------------------------------------------
    # PASSAGE DE RÉCUPÉRATION 1
    # --------------------------------------------------------
    arks_erreurs_1 = _extraire_arks_erreurs_sru(erreurs_1)

    arks_recup_1 = [
        ark
        for ark in arks_erreurs_1
        if ark not in statut_par_ark
    ]

    erreurs_2 = []

    if arks_recup_1:
        print()
        print(
            f"Récupération 1 : {len(arks_recup_1):,} ARK en vraie erreur. "
            f"Pause {SRU_RECOVERY1_PAUSE:.1f} s..."
        )

        time.sleep(SRU_RECOVERY1_PAUSE)

        lots_recup_1 = _decouper_lots(
            arks_recup_1,
            SRU_RECOVERY1_LOT_SIZE,
        )

        statut_2, erreurs_2, non_ctrl_2 = _executer_passe_sru(
            lots=lots_recup_1,
            workers=SRU_RECOVERY1_WORKERS,
            tentatives=SRU_RETRIES,
            titre="RÉCUPÉRATION SRU 1 — 6 WORKERS / 25 ARK",
            progression=20,
        )

        statut_par_ark.update(statut_2)
        non_controllables.update(non_ctrl_2)

    # --------------------------------------------------------
    # PASSAGE DE RÉCUPÉRATION 2
    # --------------------------------------------------------
    arks_erreurs_2 = _extraire_arks_erreurs_sru(erreurs_2)

    arks_recup_2 = [
        ark
        for ark in arks_erreurs_2
        if ark not in statut_par_ark
    ]

    erreurs_3 = []

    if arks_recup_2:
        print()
        print(
            f"Récupération 2 : {len(arks_recup_2):,} ARK encore en vraie erreur. "
            f"Pause {SRU_RECOVERY2_PAUSE:.1f} s..."
        )

        time.sleep(SRU_RECOVERY2_PAUSE)

        lots_recup_2 = _decouper_lots(
            arks_recup_2,
            SRU_RECOVERY2_LOT_SIZE,
        )

        statut_3, erreurs_3, non_ctrl_3 = _executer_passe_sru(
            lots=lots_recup_2,
            workers=SRU_RECOVERY2_WORKERS,
            tentatives=SRU_RECOVERY2_RETRIES,
            titre="RÉCUPÉRATION SRU 2 — 2 WORKERS / 10 ARK",
            progression=10,
        )

        statut_par_ark.update(statut_3)
        non_controllables.update(non_ctrl_3)

    # --------------------------------------------------------
    # ERREURS RÉELLEMENT RÉSIDUELLES
    # --------------------------------------------------------
    arks_erreurs_finales = [
        ark
        for ark in _extraire_arks_erreurs_sru(erreurs_3)
        if ark not in statut_par_ark
    ]
    arks_erreurs_finales = _unique_ordonnee(arks_erreurs_finales)

    # --------------------------------------------------------
    # ARK absents du résultat SRU sans erreur explicite.
    # Ils sont conservés par sécurité.
    # --------------------------------------------------------
    arks_non_resolus = [
        ark
        for ark in arks_uniques
        if ark not in statut_par_ark
    ]

    set_erreurs_finales = set(arks_erreurs_finales)

    arks_absents_sru = [
        ark
        for ark in arks_non_resolus
        if ark not in set_erreurs_finales
    ]

    # --------------------------------------------------------
    # Application vectorisée du filtre.
    # False inclut : notice sans 182$c=c ET diagnostic sans MARC.
    # --------------------------------------------------------
    arks_lignes = (
        df_isbn["ark_bnf"]
        .map(_normaliser_ark_bnf)
    )

    masque_media = (
        arks_lignes
        .map(statut_par_ark)
        .eq(True)
    )

    nb_lignes_exclues = int(masque_media.sum())

    nb_arks_exclus = int(
        sum(
            bool(valeur)
            for valeur in statut_par_ark.values()
        )
    )

    resultat = (
        df_isbn.loc[~masque_media]
        .copy()
        .reset_index(drop=True)
    )

    duree = time.perf_counter() - debut

    # --------------------------------------------------------
    # Diagnostic détaillé
    # --------------------------------------------------------
    DERNIER_DIAGNOSTIC_SRU = {
        "arks_total": len(arks_uniques),
        "arks_controles_final": len(statut_par_ark),
        "arks_media": nb_arks_exclus,
        "lignes_media_exclues": nb_lignes_exclues,
        "arks_non_controllables_182": len(non_controllables),
        "liste_arks_non_controllables_182": sorted(non_controllables),
        "erreurs_passage_principal": len(erreurs_1),
        "arks_relances_recuperation_1": len(arks_recup_1),
        "erreurs_recuperation_1": len(erreurs_2),
        "arks_relances_recuperation_2": len(arks_recup_2),
        "erreurs_recuperation_2": len(erreurs_3),
        "arks_erreurs_finales": len(arks_erreurs_finales),
        "liste_arks_erreurs_finales": arks_erreurs_finales,
        "arks_absents_sru": len(arks_absents_sru),
        "liste_arks_absents_sru": arks_absents_sru,
        "duree": duree,
    }

    # --------------------------------------------------------
    # Attributs utiles sur le DataFrame retourné.
    # --------------------------------------------------------
    resultat.attrs["erreurs_sru_182c_initiales"] = erreurs_1
    resultat.attrs["erreurs_sru_182c_recuperation_1"] = erreurs_2
    resultat.attrs["erreurs_sru_182c_finales"] = erreurs_3
    resultat.attrs["nb_arks_sru_controles"] = len(statut_par_ark)
    resultat.attrs["nb_arks_sru_inconnus"] = len(arks_non_resolus)
    resultat.attrs["nb_arks_media_informatique"] = nb_arks_exclus
    resultat.attrs["nb_arks_non_controllables_182"] = len(non_controllables)

    # --------------------------------------------------------
    # BILAN FINAL
    # --------------------------------------------------------
    print()
    print("=" * 80)
    print("BILAN FINAL SRU")
    print("=" * 80)
    print(f"ARK total                         : {len(arks_uniques):,}")
    print(f"ARK contrôlés / classés           : {len(statut_par_ark):,}")
    print(f"ARK avec 182$c = c                : {nb_arks_exclus:,}")
    print(f"Lignes ISBN exclues                : {nb_lignes_exclues:,}")
    print(f"Diagnostics sans MARC conservés    : {len(non_controllables):,}")
    print(f"ARK absents du SRU                 : {len(arks_absents_sru):,}")
    print(f"VRAIES ERREURS SRU FINALES         : {len(arks_erreurs_finales):,}")
    print(f"Durée contrôle 182$c               : {duree:.1f} s ({duree / 60:.2f} min)")
    print(f"Lignes restantes                   : {len(resultat):,}")
    print("=" * 80)

    return resultat, nb_lignes_exclues



# ============================================================
# CREER DES LOTS
# ============================================================

def creer_lots(
    liste,
    taille
):

    for i in range(
        0,
        len(liste),
        taille
    ):

        yield liste[
            i:i + taille
        ]


# ============================================================
# ENRICHISSEMENT D'UN LOT
# ============================================================

def enrichir_lot(
    uris
):

    values = " ".join(
        f"<{uri}>"
        for uri in uris
    )

    query = PREFIXES + f"""

SELECT DISTINCT
    ?genevensia
    ?titre
    ?annee
    ?lieuPublication
    ?editeur
    ?auteur
    ?nomAuteur
    ?sujet
    ?ppn
    ?uri

WHERE {{

    VALUES ?genevensia {{
        {values}
    }}

    OPTIONAL {{
        ?genevensia
            dcterms:title
            ?titre .
    }}

    OPTIONAL {{
        ?genevensia
            dcterms:date
            ?annee .
    }}

    OPTIONAL {{
        ?genevensia
            rdvocab:placeOfPublication
            ?lieuBrut .

        OPTIONAL {{
            ?lieuBrut
                rdfs:label
                ?lieuLabel1 .
        }}

        OPTIONAL {{
            ?lieuBrut
                skos:prefLabel
                ?lieuLabel2 .
        }}

        BIND(
            COALESCE(
                ?lieuLabel1,
                ?lieuLabel2,
                STR(?lieuBrut)
            )
            AS ?lieuPublication
        )
    }}

    OPTIONAL {{
        ?genevensia
            rdvocab:publishersName
            ?editeur .
    }}

    OPTIONAL {{
        ?genevensia
            rdfs:seeAlso
            ?uri .
    }}

    OPTIONAL {{
        ?genevensia
            dcterms:subject
            ?sujetURI .

        OPTIONAL {{
            ?sujetURI
                skos:prefLabel
                ?sujetLabel1 .
        }}

        OPTIONAL {{
            ?sujetURI
                rdfs:label
                ?sujetLabel2 .
        }}

        BIND(
            COALESCE(
                ?sujetLabel1,
                ?sujetLabel2,
                STR(?sujetURI)
            )
            AS ?sujet
        )
    }}

    OPTIONAL {{
        ?genevensia
            rdarelationships:expressionManifested
            ?expressionAuteur .

        ?expressionAuteur
            lc:aut
            ?auteur .

        OPTIONAL {{
            ?auteur
                foaf:name
                ?nomAuteur .
        }}

        OPTIONAL {{
            ?auteur
                owl:sameAs|rdfs:seeAlso
                ?idrefURI .

            FILTER(
                CONTAINS(
                    LCASE(
                        STR(?idrefURI)
                    ),
                    "idref.fr/"
                )
            )

            BIND(
                REPLACE(
                    STRAFTER(
                        STR(?idrefURI),
                        "idref.fr/"
                    ),
                    "/.*$",
                    ""
                )
                AS ?ppn
            )
        }}
    }}
}}
"""

    resultat = requete_sparql(
        query
    )

    return bindings_vers_lignes(
        resultat
    )


# ============================================================
# ENRICHISSEMENT PARALLELE
# ============================================================

def enrichir_notices(
    df_detection
):

    uris = (
        df_detection[
            "genevensia"
        ]
        .dropna()
        .drop_duplicates()
        .tolist()
    )

    if not uris:
        return pd.DataFrame()

    lots = list(
        creer_lots(
            uris,
            TAILLE_LOT_ENRICHISSEMENT
        )
    )

    print()
    print(
        f"Enrichissement de "
        f"{len(uris):,} notices "
        f"en {len(lots):,} lots..."
    )

    debut = time.time()

    toutes_lignes = []

    workers = min(
        MAX_WORKERS_ENRICHISSEMENT,
        len(lots)
    )

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:

        futures = {

            executor.submit(
                enrichir_lot,
                lot
            ): numero

            for numero, lot
            in enumerate(
                lots,
                start=1
            )
        }

        termines = 0

        for future in as_completed(
            futures
        ):

            numero = futures[
                future
            ]

            try:

                lignes = future.result()

                toutes_lignes.extend(
                    lignes
                )

            except Exception as erreur:

                print()
                print(
                    f"✗ Erreur lot "
                    f"{numero} : "
                    f"{erreur}"
                )

            termines += 1

            print(
                f"\rLots terminés : "
                f"{termines}/{len(lots)}",
                end=""
            )

    print()

    print(
        f"Enrichissement terminé en "
        f"{time.time()-debut:.1f}s"
    )

    if not toutes_lignes:
        return pd.DataFrame()

    return pd.DataFrame(
        toutes_lignes
    )


# ============================================================
# CONCATENER LES VALEURS UNIQUES
# ============================================================

def concat_unique(serie):

    valeurs = {

        str(x).strip()

        for x in serie

        if pd.notna(x)
        and str(x).strip()
    }

    return " | ".join(
        sorted(valeurs)
    )


# ============================================================
# AGREGER L'ENRICHISSEMENT
#
# UNE LIGNE PAR PPN
# ============================================================

def agreger_enrichissement(
    df
):

    if df.empty:
        return df

    df = df.copy()

    colonnes = [
        "titre",
        "annee",
        "lieuPublication",
        "editeur",
        "auteur",
        "nomAuteur",
        "sujet",
        "ppn",
        "uri"
    ]

    for colonne in colonnes:

        if colonne not in df.columns:
            df[colonne] = None

    # --------------------------------------------------------
    # NETTOYAGE DU PPN
    # --------------------------------------------------------

    df["ppn"] = (
        df["ppn"]
        .astype("string")
        .str.strip()
    )

    df["ppn"] = (
        df["ppn"]
        .replace({
            "": pd.NA,
            "None": pd.NA,
            "nan": pd.NA,
            "<NA>": pd.NA
        })
    )

    # --------------------------------------------------------
    # UNE LIGNE PAR GENEvensia + PPN
    #
    # IMPORTANT :
    # on ne concatène PLUS les PPN.
    # --------------------------------------------------------

    df = (
        df.groupby(
            [
                "genevensia",
                "ppn"
            ],
            as_index=False,
            dropna=False
        )
        .agg({

            "titre":
                concat_unique,

            "annee":
                concat_unique,

            "lieuPublication":
                concat_unique,

            "editeur":
                concat_unique,

            "auteur":
                concat_unique,

            "nomAuteur":
                concat_unique,

            "sujet":
                concat_unique,

            "uri":
                concat_unique
        })
    )

    return df


# ============================================================
# FONCTION PRINCIPALE EXPOSÉE AU PIPELINE
# ============================================================

def recherche_BNF():
    """
    Recherche les notices Genevensia dans data.bnf.fr puis :

    1. détecte les notices selon les critères Genevensia ;
    2. normalise et dédoublonne les ISBN ;
    3. contrôle les notices via le SRU BnF ;
    4. exclut les médias informatiques lorsque UNIMARC 182$c = c ;
    5. conserve les diagnostics SRU sans MARC par sécurité ;
    6. effectue les deux passes de récupération ciblée des vraies erreurs ;
    7. enrichit uniquement les notices restantes ;
    8. retourne le DataFrame final attendu par pipeline.py / fusion.py.

    Aucun traitement réseau n'est lancé lors de l'import du module.
    Les requêtes démarrent uniquement lorsque recherche_BNF() est appelée.
    """


    debut_global = time.time()


    # ------------------------------------------------------------
    # ETAPE 1
    # DETECTION
    # ------------------------------------------------------------

    lignes_detection = (
        detection_parallele()
    )


    # ------------------------------------------------------------
    # ETAPE 2
    # FUSION ET DEDOUBLONNAGE
    # ------------------------------------------------------------

    df_detection = (
        fusionner_detection(
            lignes_detection
        )
    )


    # ------------------------------------------------------------
    # ETAPE 2.5
    # SUPPRESSION DES NOTICES NUMERIQUES
    # UNIMARC 182$c = c
    # ------------------------------------------------------------

    # Le filtre SRU de la version finale travaille sur une colonne ark_bnf.
    # On la dérive directement de l'URI genevensia, puis on la retire après contrôle.
    df_detection["ark_bnf"] = (
        df_detection["genevensia"]
        .map(_normaliser_ark_bnf)
    )

    df_detection, nb_numeriques_exclus = (
        filtrer_medias_informatiques_bnf(
            df_detection
        )
    )

    df_detection = (
        df_detection
        .drop(
            columns=["ark_bnf"],
            errors="ignore"
        )
    )


    print()
    print(
        "=========================================="
    )
    print(
        "APRES DETECTION"
    )
    print(
        "=========================================="
    )

    print(
        f"Notices uniques : "
        f"{df_detection['genevensia'].nunique():,}"
    )

    print(
        f"ISBN uniques : "
        f"{df_detection['isbn_normalise'].nunique():,}"
    )


    print(
        f"Notices numériques exclues : "
        f"{nb_numeriques_exclus:,}"
    )


    # ------------------------------------------------------------
    # STATISTIQUES PAR RAISON
    # ------------------------------------------------------------

    if not df_detection.empty:

        print()
        print(
            "Répartition des critères :"
        )

        for raison in [
            "titre",
            "lieu_publication",
            "sujet",
            "naissance_auteur",
            "deces_auteur"
        ]:

            nombre = (
                df_detection["raisons"]
                .str.contains(
                    raison,
                    regex=False,
                    na=False
                )
                .sum()
            )

            print(
                f"  {raison:<20} : "
                f"{nombre:,}"
            )


    # ------------------------------------------------------------
    # ETAPE 3
    # ENRICHISSEMENT
    # ------------------------------------------------------------

    df_enrichissement = (
        enrichir_notices(
            df_detection
        )
    )


    # ------------------------------------------------------------
    # ETAPE 4
    # AGREGER
    # UNE LIGNE PAR PPN
    # ------------------------------------------------------------

    df_enrichissement = (
        agreger_enrichissement(
            df_enrichissement
        )
    )


    # ------------------------------------------------------------
    # ETAPE 5
    # MERGE
    # ------------------------------------------------------------

    if not df_enrichissement.empty:

        df_bnf_geneve = (
            df_detection.merge(
                df_enrichissement,
                on="genevensia",
                how="left"
            )
        )

    else:

        df_bnf_geneve = (
            df_detection.copy()
        )


    # ------------------------------------------------------------
    # ORDRE DES COLONNES
    # ------------------------------------------------------------

    ordre_colonnes = [
        "ppn",
        "isbn_normalise",
        "titre",
        "annee",
        "nomAuteur",
        "lieuPublication",
        "editeur",
        "sujet",
        "raisons"
    ]


    ordre_colonnes = [
        colonne
        for colonne in ordre_colonnes
        if colonne in df_bnf_geneve.columns
    ]


    df_bnf_geneve = (
        df_bnf_geneve[
            ordre_colonnes
        ]
        .drop_duplicates()
        .reset_index(
            drop=True
        )
    )


    # ------------------------------------------------------------
    # TRI
    # ------------------------------------------------------------

    colonnes_tri = [
        colonne
        for colonne in [
            "ppn",
            "isbn_normalise",
            "titre"
        ]
        if colonne in df_bnf_geneve.columns
    ]


    if colonnes_tri:

        df_bnf_geneve = (
            df_bnf_geneve
            .sort_values(
                colonnes_tri,
                na_position="last"
            )
            .reset_index(
                drop=True
            )
        )


    # ------------------------------------------------------------
    # LISTE ISBN UNIQUE
    # ------------------------------------------------------------

    liste_isbn_bnf = (
        df_bnf_geneve[
            "isbn_normalise"
        ]
        .dropna()
        .drop_duplicates()
        .tolist()
    )


    # ------------------------------------------------------------
    # RESULTATS
    # ------------------------------------------------------------

    duree_totale = (
        time.time()
        -
        debut_global
    )


    print()
    print(
        "=========================================="
    )
    print(
        "TERMINE"
    )
    print(
        "=========================================="
    )

    print(
        f"Lignes finales : "
        f"{len(df_bnf_geneve):,}"
    )

    if "ppn" in df_bnf_geneve.columns:

        print(
            f"PPN uniques : "
            f"{df_bnf_geneve['ppn'].nunique():,}"
        )

    print(
        f"ISBN uniques : "
        f"{len(liste_isbn_bnf):,}"
    )

    print(
        f"Durée totale : "
        f"{duree_totale:.1f} secondes"
    )

    print(
        f"Soit : "
        f"{duree_totale/60:.1f} minutes"
    )



    # ------------------------------------------------------------
    # DIAGNOSTICS DU MODULE
    # ------------------------------------------------------------

    df_bnf_geneve.attrs["nb_numeriques_exclus"] = (
        nb_numeriques_exclus
    )

    df_bnf_geneve.attrs["diagnostic_sru_182c"] = (
        DERNIER_DIAGNOSTIC_SRU.copy()
    )

    # ------------------------------------------------------------
    # RETURN POUR pipeline.py
    # ------------------------------------------------------------

    return df_bnf_geneve