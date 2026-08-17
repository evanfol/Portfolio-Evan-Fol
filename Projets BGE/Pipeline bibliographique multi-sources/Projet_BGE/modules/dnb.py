import requests
import pandas as pd
import re
import time
import threading
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

DNB_SPARQL_URL = "https://sparql.dnb.de/api/dnbgnd"
DNB_SRU_URL = "https://services.dnb.de/sru/dnb"

# Contrôle du support physique/numérique via MARC21 : 338 $b
# cr = online resource => notice exclue
DNB_MARC_BATCH_SIZE = 20
DNB_MARC_WORKERS = 6

# Validé par les benchmarks
DNB_BATCH_SIZE = 100
DNB_WORKERS = 4

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 120

MAX_RETRIES = 4


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

        backoff_factor=0.8,

        status_forcelist=[
            429,
            500,
            502,
            503,
            504
        ],

        allowed_methods=[
            "GET",
            "POST"
        ],

        raise_on_status=False
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=DNB_WORKERS + 2,
        pool_maxsize=DNB_WORKERS + 2
    )

    session.mount(
        "https://",
        adapter
    )

    session.headers.update({
        "User-Agent":
            "Geneve-Bibliographic-Research-DNB/1.0",

        "Accept":
            "application/sparql-results+json",

        "Accept-Encoding":
            "gzip, deflate"
    })

    return session


def obtenir_session():

    if not hasattr(
        thread_local,
        "session"
    ):
        thread_local.session = creer_session()

    return thread_local.session


# ============================================================
# NORMALISATION GND
# ============================================================

def normaliser_gnd(valeur):

    if pd.isna(valeur):
        return None

    valeur = (
        str(valeur)
        .strip()
    )

    if not valeur:
        return None

    # --------------------------------------------------------
    # URL complète
    #
    # https://d-nb.info/gnd/171556755
    # --------------------------------------------------------

    match = re.search(
        r"d-nb\.info/gnd/([^/?#]+)",
        valeur,
        re.I
    )

    if match:
        valeur = (
            match
            .group(1)
            .strip()
        )

    # --------------------------------------------------------
    # (DE-588)171556755
    # --------------------------------------------------------

    valeur = re.sub(
        r"^\(DE-588\)",
        "",
        valeur,
        flags=re.I
    )

    # --------------------------------------------------------
    # GND:171556755
    # --------------------------------------------------------

    valeur = re.sub(
        r"^GND\s*:\s*",
        "",
        valeur,
        flags=re.I
    )

    valeur = valeur.strip()

    return valeur or None


# ============================================================
# ISBN
# ============================================================

ISBN10_RE = re.compile(
    r"\d{9}[\dX]"
)

ISBN13_RE = re.compile(
    r"\d{13}"
)

ISBN_NETTOYAGE_RE = re.compile(
    r"[^0-9X]"
)


def isbn10_valide(isbn):

    if not ISBN10_RE.fullmatch(isbn):
        return False

    total = 0

    for i, c in enumerate(isbn):

        valeur = (
            10
            if c == "X"
            else int(c)
        )

        total += (
            (10 - i)
            * valeur
        )

    return total % 11 == 0


def isbn13_valide(isbn):

    if not ISBN13_RE.fullmatch(isbn):
        return False

    somme = 0

    for i, c in enumerate(
        isbn[:12]
    ):

        n = int(c)

        somme += (
            n
            if i % 2 == 0
            else n * 3
        )

    cle = (
        10 - somme % 10
    ) % 10

    return (
        cle
        == int(isbn[-1])
    )


def isbn10_vers_13(isbn10):

    base = (
        "978"
        + isbn10[:9]
    )

    somme = 0

    for i, c in enumerate(base):

        n = int(c)

        somme += (
            n
            if i % 2 == 0
            else n * 3
        )

    cle = (
        10 - somme % 10
    ) % 10

    return (
        base
        + str(cle)
    )


def normaliser_isbn(valeur):

    if not valeur:
        return None

    isbn = (
        ISBN_NETTOYAGE_RE.sub(
            "",
            str(valeur)
            .upper()
            .strip()
        )
    )

    # --------------------------------------------------------
    # ISBN-13
    # --------------------------------------------------------

    if len(isbn) == 13:

        if isbn13_valide(isbn):
            return isbn

        return None

    # --------------------------------------------------------
    # ISBN-10
    # --------------------------------------------------------

    if len(isbn) == 10:

        if not isbn10_valide(isbn):
            return None

        isbn13 = (
            isbn10_vers_13(
                isbn
            )
        )

        if isbn13_valide(isbn13):
            return isbn13

    return None


# ============================================================
# TEXTE
# ============================================================

ANNEE_RE = re.compile(
    r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b"
)


def nettoyer_texte(valeur):

    if valeur is None:
        return None

    valeur = (
        str(valeur)
        .strip()
    )

    return valeur or None


def extraire_annee(valeur):

    if not valeur:
        return None

    match = ANNEE_RE.search(
        str(valeur)
    )

    if not match:
        return None

    return int(
        match.group(1)
    )


# ============================================================
# IDN DNB
# ============================================================

DNB_ID_RE = re.compile(
    r"https?://d-nb\.info/([^/?#]+)",
    re.I
)


def extraire_idn_dnb(uri):

    if not uri:
        return None

    match = DNB_ID_RE.search(
        uri
    )

    if not match:
        return None

    return (
        match
        .group(1)
        .strip()
    )


# ============================================================
# CONTRÔLE MARC21 338 $b
#
# 338 $b = cr  => ressource en ligne / numérique
# La notice entière est alors exclue, donc aucun de ses ISBN
# n'est conservé.
#
# Pour les performances :
# - contrôle une seule fois par IDN DNB unique ;
# - requêtes SRU groupées par lots ;
# - parallélisation des lots ;
# - session HTTP réutilisée par thread.
# ============================================================

MARC_NS = "http://www.loc.gov/MARC21/slim"


def construire_requete_sru_idn(liste_idn):
    """Construit une requête CQL portant sur plusieurs IDN DNB."""

    return " OR ".join(
        f'dnb.idn="{idn}"'
        for idn in liste_idn
    )


def parser_numerique_marcxml(xml_bytes, idn_attendus):
    """
    Retourne {idn: bool}.

    bool=True uniquement si au moins un 338 $b vaut exactement "cr"
    (comparaison insensible à la casse et aux espaces).
    """

    root = ET.fromstring(xml_bytes)
    resultats = {}

    for record in root.findall(f".//{{{MARC_NS}}}record"):

        idn = None

        for controlfield in record.findall(
            f"{{{MARC_NS}}}controlfield"
        ):
            if controlfield.get("tag") == "001":
                idn = (controlfield.text or "").strip()
                break

        if not idn:
            continue

        numerique = False

        for datafield in record.findall(
            f"{{{MARC_NS}}}datafield"
        ):
            if datafield.get("tag") != "338":
                continue

            for subfield in datafield.findall(
                f"{{{MARC_NS}}}subfield"
            ):
                if subfield.get("code") != "b":
                    continue

                valeur = (subfield.text or "").strip().lower()

                if valeur == "cr":
                    numerique = True
                    break

            if numerique:
                break

        resultats[idn] = numerique

    # Les IDN absents du XML ne sont PAS considérés automatiquement
    # comme physiques : on les laisse manquants afin de pouvoir les
    # retenter proprement ensuite.
    return {
        idn: resultats[idn]
        for idn in idn_attendus
        if idn in resultats
    }


def executer_requete_marc_dnb(liste_idn):
    """Interroge le SRU DNB et lit les notices en MARC21-XML."""

    session = obtenir_session()

    response = session.get(
        DNB_SRU_URL,
        params={
            "version": "1.1",
            "operation": "searchRetrieve",
            "query": construire_requete_sru_idn(liste_idn),
            "recordSchema": "MARC21-xml",
            "maximumRecords": len(liste_idn),
        },
        headers={
            "Accept": "application/xml, text/xml;q=0.9, */*;q=0.1"
        },
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )

    response.raise_for_status()

    return parser_numerique_marcxml(
        response.content,
        liste_idn,
    )


def verifier_lot_marc_robuste(liste_idn):
    """
    Contrôle robuste d'un lot.

    En cas d'erreur le lot est divisé récursivement, exactement comme
    pour la partie SPARQL, afin qu'un seul IDN problématique ne fasse
    pas perdre tout le lot.
    """

    try:
        resultats = executer_requete_marc_dnb(liste_idn)

        manquants = [
            idn
            for idn in liste_idn
            if idn not in resultats
        ]

        # Si le serveur a répondu mais n'a pas renvoyé certains IDN,
        # on les retente en divisant le lot.
        if manquants:
            if len(liste_idn) == 1:
                return (
                    resultats,
                    [{
                        "idn_dnb": liste_idn[0],
                        "erreur": "Notice MARC absente de la réponse SRU",
                    }],
                )

            milieu = len(liste_idn) // 2

            gauche, erreurs_gauche = verifier_lot_marc_robuste(
                liste_idn[:milieu]
            )
            droite, erreurs_droite = verifier_lot_marc_robuste(
                liste_idn[milieu:]
            )

            gauche.update(droite)

            return (
                gauche,
                erreurs_gauche + erreurs_droite,
            )

        return resultats, []

    except Exception as e:

        if len(liste_idn) <= 1:
            return (
                {},
                [{
                    "idn_dnb": liste_idn[0],
                    "erreur": f"{type(e).__name__}: {e}",
                }],
            )

        milieu = len(liste_idn) // 2

        gauche, erreurs_gauche = verifier_lot_marc_robuste(
            liste_idn[:milieu]
        )
        droite, erreurs_droite = verifier_lot_marc_robuste(
            liste_idn[milieu:]
        )

        gauche.update(droite)

        return (
            gauche,
            erreurs_gauche + erreurs_droite,
        )


def filtrer_notices_numeriques_dnb(
    df_dnb,
    batch_size=DNB_MARC_BATCH_SIZE,
    max_workers=DNB_MARC_WORKERS,
):
    """
    Retire toutes les lignes appartenant à une notice dont 338 $b = cr.

    Retourne :
        df_filtre,
        erreurs_marc,
        nb_numeriques,
        duree
    """

    debut = time.perf_counter()

    if df_dnb.empty:
        return df_dnb, [], 0, 0.0

    liste_idn = (
        df_dnb["idn_dnb"]
        .dropna()
        .astype(str)
        .drop_duplicates()
        .tolist()
    )

    lots = [
        liste_idn[i:i + batch_size]
        for i in range(0, len(liste_idn), batch_size)
    ]

    statut_numerique = {}
    erreurs = []

    print()
    print("=" * 80)
    print("2/3 — CONTRÔLE MARC 338 $b = cr")
    print("=" * 80)
    print(f"IDN uniques        : {len(liste_idn):,}")
    print(f"IDN / requête SRU  : {batch_size}")
    print(f"Requêtes prévues   : {len(lots):,}")
    print(f"Workers             : {max_workers}")
    print("=" * 80)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:

        futures = {
            executor.submit(
                verifier_lot_marc_robuste,
                lot,
            ): numero
            for numero, lot in enumerate(lots, start=1)
        }

        termines = 0

        for future in as_completed(futures):
            termines += 1
            numero = futures[future]

            try:
                resultats_lot, erreurs_lot = future.result()
                statut_numerique.update(resultats_lot)
                erreurs.extend(erreurs_lot)

            except Exception as e:
                erreurs.append({
                    "lot": numero,
                    "erreur": f"{type(e).__name__}: {e}",
                })

            if (
                termines == 1
                or termines == len(lots)
                or termines % 10 == 0
            ):
                print(
                    f"[{termines:>3}/{len(lots)}] "
                    f"IDN contrôlés : {len(statut_numerique):,} "
                    f"| erreurs : {len(erreurs):,}"
                )

    idn_numeriques = {
        idn
        for idn, numerique in statut_numerique.items()
        if numerique
    }

    masque_numerique = (
        df_dnb["idn_dnb"]
        .astype(str)
        .isin(idn_numeriques)
    )

    df_filtre = (
        df_dnb.loc[~masque_numerique]
        .copy()
    )

    duree = time.perf_counter() - debut

    print()
    print(f"Notices numériques : {len(idn_numeriques):,}")
    print(f"Lignes exclues      : {int(masque_numerique.sum()):,}")
    print(f"Erreurs MARC        : {len(erreurs):,}")
    print(f"Durée contrôle MARC : {duree:.2f} s")

    return (
        df_filtre,
        erreurs,
        len(idn_numeriques),
        duree,
    )


# ============================================================
# CONSTRUCTION DE LA REQUÊTE
# ============================================================

def construire_requete_dnb(
    liste_gnd
):

    values = "\n".join(

        f'("{gnd}" '
        f'<https://d-nb.info/gnd/{gnd}>)'

        for gnd
        in liste_gnd
    )

    # ========================================================
    # IMPORTANT
    #
    # Au lieu de multiplier :
    #
    # titre × lieu × éditeur × ISBN
    #
    # on agrège directement les métadonnées.
    #
    # Cela réduit fortement la quantité de données retournées.
    # ========================================================

    query = f"""

PREFIX dc:
    <http://purl.org/dc/elements/1.1/>

PREFIX dct:
    <http://purl.org/dc/terms/>

PREFIX bibo:
    <http://purl.org/ontology/bibo/>

PREFIX rdau:
    <http://rdaregistry.info/Elements/u/>

PREFIX gndo:
    <https://d-nb.info/standards/elementset/gnd#>


SELECT

    ?gnd
    ?document
    ?isbn

    (SAMPLE(?titreValeur)
        AS ?titre)

    (SAMPLE(?anneeValeur)
        AS ?annee)

    (SAMPLE(?auteurNomValeur)
        AS ?nomAuteur)

    (
        GROUP_CONCAT(
            DISTINCT STR(?lieuValeur);
            separator=" - "
        )
        AS ?lieuPublication
    )

    (
        GROUP_CONCAT(
            DISTINCT STR(?editeurValeur);
            separator=" - "
        )
        AS ?editeur
    )


WHERE {{

    # ========================================================
    # GND DEMANDÉS
    # ========================================================

    VALUES (?gnd ?auteur) {{

        {values}

    }}


    # ========================================================
    # RELATION AUTEUR
    #
    # Les tests ont montré :
    #
    # dct:creator       = incomplet
    # relators/aut      = plus complet
    #
    # On garde donc les deux.
    # ========================================================

    {{

        ?document
            dct:creator
            ?auteur .

    }}

    UNION

    {{

        ?document
            <http://id.loc.gov/vocabulary/relators/aut>
            ?auteur .

    }}


    # ========================================================
    # ISBN
    #
    # La DNB utilise :
    #
    # bibo:isbn13
    # bibo:isbn10
    #
    # et NON bibo:isbn.
    # ========================================================

    {{

        ?document
            bibo:isbn13
            ?isbn .

    }}

    UNION

    {{

        ?document
            bibo:isbn10
            ?isbn .

    }}


    # ========================================================
    # TITRE
    # ========================================================

    OPTIONAL {{

        ?document
            dc:title
            ?titreValeur .

    }}


    # ========================================================
    # ANNÉE
    # ========================================================

    OPTIONAL {{

        ?document
            dct:issued
            ?anneeValeur .

    }}


    # ========================================================
    # LIEU
    # ========================================================

    OPTIONAL {{

        ?document
            rdau:P60163
            ?lieuValeur .

    }}


    # ========================================================
    # EDITEUR
    # ========================================================

    OPTIONAL {{

        ?document
            dc:publisher
            ?editeurValeur .

    }}


    # ========================================================
    # NOM DE L'AUTEUR
    # ========================================================

    OPTIONAL {{

        ?auteur
            gndo:preferredNameForThePerson
            ?auteurNomValeur .

    }}

}}

GROUP BY

    ?gnd
    ?document
    ?isbn

"""

    return query


# ============================================================
# EXECUTION SPARQL
# ============================================================

def executer_requete_dnb(
    liste_gnd
):

    session = obtenir_session()

    query = (
        construire_requete_dnb(
            liste_gnd
        )
    )

    response = session.post(

        DNB_SPARQL_URL,

        data={
            "query":
                query
        },

        headers={
            "Accept":
                "application/sparql-results+json"
        },

        timeout=(
            CONNECT_TIMEOUT,
            READ_TIMEOUT
        )
    )

    response.raise_for_status()

    data = response.json()

    return (
        data
        .get(
            "results",
            {}
        )
        .get(
            "bindings",
            []
        )
    )


# ============================================================
# FALLBACK AUTOMATIQUE
#
# Si 100 GND échouent :
#
# 100
# ↓
# 50 + 50
# ↓
# 25 + 25
# ↓
# ...
#
# Ainsi un problème sur un GND ne fait pas perdre
# tout le lot.
# ============================================================

def requete_dnb_robuste(
    liste_gnd
):

    try:

        bindings = (
            executer_requete_dnb(
                liste_gnd
            )
        )

        return (
            bindings,
            []
        )

    except Exception as e:

        # ----------------------------------------------------
        # Il ne reste qu'un GND.
        # Impossible de diviser davantage.
        # ----------------------------------------------------

        if len(liste_gnd) <= 1:

            return (
                [],
                [
                    {
                        "gnd":
                            liste_gnd[0],

                        "erreur":
                            (
                                f"{type(e).__name__}: "
                                f"{e}"
                            )
                    }
                ]
            )

        # ----------------------------------------------------
        # Découpage
        # ----------------------------------------------------

        milieu = (
            len(liste_gnd)
            // 2
        )

        bindings_gauche, erreurs_gauche = (
            requete_dnb_robuste(
                liste_gnd[:milieu]
            )
        )

        bindings_droite, erreurs_droite = (
            requete_dnb_robuste(
                liste_gnd[milieu:]
            )
        )

        return (
            bindings_gauche
            + bindings_droite,

            erreurs_gauche
            + erreurs_droite
        )


# ============================================================
# PARSING DES RESULTATS
# ============================================================

def parser_bindings_dnb(
    bindings
):

    lignes = []

    append = lignes.append

    for ligne in bindings:

        # ====================================================
        # GND
        # ====================================================

        gnd = (
            ligne
            .get(
                "gnd",
                {}
            )
            .get(
                "value"
            )
        )

        if not gnd:
            continue

        # ====================================================
        # DOCUMENT
        # ====================================================

        document = (
            ligne
            .get(
                "document",
                {}
            )
            .get(
                "value"
            )
        )

        idn_dnb = (
            extraire_idn_dnb(
                document
            )
        )

        if not idn_dnb:
            continue

        # ====================================================
        # ISBN
        # ====================================================

        isbn_brut = (
            ligne
            .get(
                "isbn",
                {}
            )
            .get(
                "value"
            )
        )

        isbn = (
            normaliser_isbn(
                isbn_brut
            )
        )

        if not isbn:
            continue

        # ====================================================
        # TITRE
        # ====================================================

        titre = (
            nettoyer_texte(

                ligne
                .get(
                    "titre",
                    {}
                )
                .get(
                    "value"
                )
            )
        )

        # ====================================================
        # ANNÉE
        # ====================================================

        annee = (
            extraire_annee(

                ligne
                .get(
                    "annee",
                    {}
                )
                .get(
                    "value"
                )
            )
        )

        # ====================================================
        # AUTEUR
        # ====================================================

        nom_auteur = (
            nettoyer_texte(

                ligne
                .get(
                    "nomAuteur",
                    {}
                )
                .get(
                    "value"
                )
            )
        )

        # ====================================================
        # LIEU
        # ====================================================

        lieu = (
            nettoyer_texte(

                ligne
                .get(
                    "lieuPublication",
                    {}
                )
                .get(
                    "value"
                )
            )
        )

        # GROUP_CONCAT vide => ""
        if lieu == "":
            lieu = None

        # ====================================================
        # EDITEUR
        # ====================================================

        editeur = (
            nettoyer_texte(

                ligne
                .get(
                    "editeur",
                    {}
                )
                .get(
                    "value"
                )
            )
        )

        if editeur == "":
            editeur = None

        # ====================================================
        # LIGNE
        # ====================================================

        append({

            "_gnd_requete":
                gnd,

            "idn_dnb":
                idn_dnb,

            "isbn_normalise":
                isbn,

            "titre":
                titre,

            "annee":
                annee,

            "nomAuteur":
                nom_auteur,

            "lieuPublication":
                lieu,

            "editeur":
                editeur
        })

    return lignes


# ============================================================
# TRAITEMENT D'UN LOT
# ============================================================

def traiter_lot_dnb(
    numero_lot,
    lot
):

    debut = (
        time.perf_counter()
    )

    bindings, erreurs = (
        requete_dnb_robuste(
            lot
        )
    )

    lignes = (
        parser_bindings_dnb(
            bindings
        )
    )

    duree = (
        time.perf_counter()
        - debut
    )

    return {

        "numero_lot":
            numero_lot,

        "lignes":
            lignes,

        "erreurs":
            erreurs,

        "duree":
            duree
    }


# ============================================================
# RÉCUPÉRATION GLOBALE DNB
# ============================================================

def recuperer_toutes_notices_dnb(

    liste_gnd,

    batch_size=DNB_BATCH_SIZE,

    max_workers=DNB_WORKERS
):

    lots = [

        liste_gnd[
            i:
            i + batch_size
        ]

        for i in range(
            0,
            len(liste_gnd),
            batch_size
        )
    ]

    nb_lots = len(
        lots
    )

    toutes_lignes = []

    toutes_erreurs = []

    print()
    print("=" * 80)
    print("1/2 — RÉCUPÉRATION SPARQL DNB")
    print("=" * 80)

    print(
        f"GND uniques       : "
        f"{len(liste_gnd):,}"
    )

    print(
        f"GND / requête     : "
        f"{batch_size}"
    )

    print(
        f"Requêtes prévues  : "
        f"{nb_lots:,}"
    )

    print(
        f"Workers            : "
        f"{max_workers}"
    )

    print("=" * 80)

    debut = (
        time.perf_counter()
    )

    # ========================================================
    # PARALLÉLISME
    # ========================================================

    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:

        futures = {

            executor.submit(
                traiter_lot_dnb,
                numero,
                lot
            ):
            numero

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

            termines += 1

            try:

                resultat = (
                    future.result()
                )

                toutes_lignes.extend(
                    resultat[
                        "lignes"
                    ]
                )

                toutes_erreurs.extend(
                    resultat[
                        "erreurs"
                    ]
                )

                duree_lot = (
                    resultat[
                        "duree"
                    ]
                )

                numero_lot = (
                    resultat[
                        "numero_lot"
                    ]
                )

            except Exception as e:

                numero_lot = (
                    futures[
                        future
                    ]
                )

                duree_lot = 0

                print(
                    f"Erreur inattendue lot "
                    f"{numero_lot}: "
                    f"{type(e).__name__}: {e}"
                )

            # ------------------------------------------------
            # Affichage raisonnable
            # ------------------------------------------------

            print(
                f"[{termines:>3}/{nb_lots}] "
                f"lot {numero_lot:>3} "
                f"| {duree_lot:>6.2f} s "
                f"| lignes cumulées : "
                f"{len(toutes_lignes):,} "
                f"| erreurs : "
                f"{len(toutes_erreurs):,}"
            )

    duree = (
        time.perf_counter()
        - debut
    )

    return (
        toutes_lignes,
        toutes_erreurs,
        duree
    )


# ============================================================
# FONCTION PRINCIPALE
# ============================================================

def ajouter_isbn_dnb_ultra(

    df,

    colonne_gnd="gnd_id",

    dnb_batch_size=DNB_BATCH_SIZE,

    dnb_workers=DNB_WORKERS
):

    debut_total = (
        time.perf_counter()
    )

    # ========================================================
    # CONTRÔLE
    # ========================================================

    if colonne_gnd not in df.columns:

        raise ValueError(
            f"La colonne "
            f"'{colonne_gnd}' "
            f"n'existe pas."
        )

    source = df.copy()

    # ========================================================
    # COLONNES DNB
    # ========================================================

    colonnes_dnb = [

        "idn_dnb",
        "isbn_normalise",
        "titre",
        "annee",
        "nomAuteur",
        "lieuPublication",
        "editeur"

    ]

    # ========================================================
    # SUPPRIMER ANCIENNES COLONNES DNB
    # ========================================================

    source.drop(

        columns=[

            col

            for col
            in colonnes_dnb

            if col
            in source.columns

        ],

        errors="ignore",

        inplace=True
    )

    colonnes_originales = (
        list(
            source.columns
        )
    )

    # ========================================================
    # NORMALISATION GND
    # ========================================================

    source["_gnd_requete"] = (

        source[
            colonne_gnd
        ]

        .map(
            normaliser_gnd
        )

    )

    liste_gnd = (

        source[
            "_gnd_requete"
        ]

        .dropna()

        .drop_duplicates()

        .tolist()

    )

    # ========================================================
    # INFOS
    # ========================================================

    print()
    print("#" * 80)

    print(
        "DNB — VERSION FINALE OPTIMISÉE"
    )

    print("#" * 80)

    print(
        f"Lignes source       : "
        f"{len(source):,}"
    )

    print(
        f"GND uniques         : "
        f"{len(liste_gnd):,}"
    )

    print(
        f"Batch size          : "
        f"{dnb_batch_size}"
    )

    print(
        f"Workers             : "
        f"{dnb_workers}"
    )

    # ========================================================
    # AUCUN GND
    # ========================================================

    if not liste_gnd:

        print(
            "\nAucun identifiant GND."
        )

        return pd.DataFrame(
            columns=(
                colonnes_originales
                + colonnes_dnb
            )
        )

    # ========================================================
    # REQUÊTES DNB
    # ========================================================

    lignes, erreurs, duree_dnb = (
        recuperer_toutes_notices_dnb(

            liste_gnd,

            batch_size=
                dnb_batch_size,

            max_workers=
                dnb_workers
        )
    )

    # ========================================================
    # DATAFRAME
    # ========================================================

    print()
    print("=" * 80)

    print(
        "3/3 — CONSTRUCTION DU DATAFRAME"
    )

    print("=" * 80)

    debut_dataframe = (
        time.perf_counter()
    )

    # ========================================================
    # AUCUN RESULTAT
    # ========================================================

    if not lignes:

        print(
            "\nAucun ISBN DNB trouvé."
        )

        resultat = pd.DataFrame(
            columns=(
                colonnes_originales
                + colonnes_dnb
            )
        )

        resultat.attrs[
            "erreurs_dnb"
        ] = erreurs

        return resultat

    # ========================================================
    # UNE SEULE CRÉATION DE DATAFRAME
    # ========================================================

    df_dnb = (
        pd.DataFrame.from_records(
            lignes
        )
    )

    # ========================================================
    # FILTRE NUMÉRIQUE DNB
    #
    # Une notice avec MARC 338 $b = cr est une ressource
    # en ligne et est retirée AVANT le dédoublonnage / merge.
    # ========================================================

    (
        df_dnb,
        erreurs_marc,
        nb_notices_numeriques,
        duree_marc,
    ) = filtrer_notices_numeriques_dnb(
        df_dnb,
        batch_size=DNB_MARC_BATCH_SIZE,
        max_workers=DNB_MARC_WORKERS,
    )

    erreurs.extend(
        {
            "type": "MARC338",
            **erreur,
        }
        for erreur in erreurs_marc
    )

    nb_avant_dedup = (
        len(
            df_dnb
        )
    )

    # ========================================================
    # DEDOUBLONNAGE
    #
    # IMPORTANT :
    #
    # ISBN10 et ISBN13 du même ouvrage deviennent
    # le même ISBN13 après normalisation.
    #
    # On garde donc :
    #
    # GND + ISBN
    #
    # une seule fois.
    # ========================================================

    df_dnb.drop_duplicates(

        subset=[
            "_gnd_requete",
            "isbn_normalise"
        ],

        keep="first",

        inplace=True
    )

    nb_apres_dedup = (
        len(
            df_dnb
        )
    )

    # ========================================================
    # MERGE
    #
    # INNER :
    #
    # uniquement auteurs avec ISBN DNB.
    #
    # Même comportement que ton traitement SUDOC.
    # ========================================================

    resultat = source.merge(

        df_dnb,

        on="_gnd_requete",

        how="inner",

        sort=False,

        copy=False
    )

    resultat.drop(

        columns=[
            "_gnd_requete"
        ],

        inplace=True
    )

    # ========================================================
    # ORDRE DES COLONNES
    # ========================================================

    resultat = resultat[

        colonnes_originales
        + colonnes_dnb

    ]

    # ========================================================
    # TYPES
    # ========================================================

    resultat[colonne_gnd] = (

        resultat[
            colonne_gnd
        ]

        .astype(
            "string"
        )

    )

    resultat["idn_dnb"] = (

        resultat[
            "idn_dnb"
        ]

        .astype(
            "string"
        )

    )

    resultat["isbn_normalise"] = (

        resultat[
            "isbn_normalise"
        ]

        .astype(
            "string"
        )

    )

    resultat["annee"] = (

        pd.to_numeric(

            resultat[
                "annee"
            ],

            errors="coerce"

        )

        .astype(
            "Int64"
        )

    )

    resultat.reset_index(
        drop=True,
        inplace=True
    )

    # ========================================================
    # ATTRIBUT ERREURS
    # ========================================================

    resultat.attrs[
        "erreurs_dnb"
    ] = erreurs

    # ========================================================
    # DURÉES
    # ========================================================

    duree_dataframe = (
        time.perf_counter()
        - debut_dataframe
    )

    duree_totale = (
        time.perf_counter()
        - debut_total
    )

    # ========================================================
    # STATISTIQUES
    # ========================================================

    nb_gnd_resultats = (

        df_dnb[
            "_gnd_requete"
        ]

        .nunique()

    )

    nb_idn = (

        df_dnb[
            "idn_dnb"
        ]

        .nunique()

    )

    nb_isbn = (

        df_dnb[
            "isbn_normalise"
        ]

        .nunique()

    )

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
        f"SPARQL DNB          : "
        f"{duree_dnb:.2f} s"
    )

    print(
        f"Contrôle MARC 338   : "
        f"{duree_marc:.2f} s"
    )

    print(
        f"DataFrame           : "
        f"{duree_dataframe:.2f} s"
    )

    print(
        f"DURÉE TOTALE        : "
        f"{duree_totale:.2f} s "
        f"({duree_totale / 60:.2f} min)"
    )

    print()

    print(
        f"GND recherchés      : "
        f"{len(liste_gnd):,}"
    )

    print(
        f"GND avec ISBN       : "
        f"{nb_gnd_resultats:,}"
    )

    print(
        f"GND sans ISBN       : "
        f"{len(liste_gnd) - nb_gnd_resultats:,}"
    )

    print()

    print(
        f"Notices numériques exclues : "
        f"{nb_notices_numeriques:,}"
    )

    print(
        f"IDN DNB uniques     : "
        f"{nb_idn:,}"
    )

    print(
        f"ISBN uniques        : "
        f"{nb_isbn:,}"
    )

    print()

    print(
        f"Lignes avant dedup  : "
        f"{nb_avant_dedup:,}"
    )

    print(
        f"Lignes après dedup  : "
        f"{nb_apres_dedup:,}"
    )

    print(
        f"Lignes finales      : "
        f"{len(resultat):,}"
    )

    print()

    print(
        f"Erreurs définitives : "
        f"{len(erreurs):,}"
    )

    # ========================================================
    # COMPLETUDE
    # ========================================================

    print()

    print(
        "Complétude :"
    )

    for colonne in [

        "titre",
        "annee",
        "nomAuteur",
        "lieuPublication",
        "editeur"

    ]:

        nb = (
            resultat[
                colonne
            ]
            .notna()
            .sum()
        )

        print(
            f"{colonne:<20}: "
            f"{nb:,}"
            f"/"
            f"{len(resultat):,}"
        )

    print("#" * 80)

    return resultat