import requests
import pandas as pd
import re
import time
import threading
import random
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

SPARQL_URL = "https://data.bnf.fr/sparql"
SRU_BNF_URL = "https://catalogue.bnf.fr/api/SRU"

# ------------------------------------------------------------
# SRU BNF — contrôle UNIMARC 182$c
#
# Paramètres finaux issus des benchmarks :
# - 20 workers SRU
# - 50 ARK par lot
# - requête CQL compacte : bib.persistentid any
# - construction des lots par slicing direct
# ------------------------------------------------------------

SRU_WORKERS = 20
SRU_MAX_ARKS_PAR_LOT = 50
SRU_URL_MAX = 7400
SRU_CONNECT_TIMEOUT = 10
SRU_READ_TIMEOUT = 45
SRU_RETRIES = 3


# ------------------------------------------------------------
# TAILLE DES LOTS
#
# Valeur de départ recommandée.
#
# À benchmarker ensuite :
# 10 / 20 / 30 / 40
# ------------------------------------------------------------

BNF_BATCH_SIZE = 20


# ------------------------------------------------------------
# PARALLÉLISME SPARQL
#
# Benchmarks finaux :
# - 8 workers = meilleur compromis débit/stabilité
# - 12 workers provoque des resets de connexion et ralentit fortement
# ------------------------------------------------------------

BNF_WORKERS = 8


# ------------------------------------------------------------
# TIMEOUTS
# ------------------------------------------------------------

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 90


# ------------------------------------------------------------
# RETRIES
#
# Les erreurs DNS sont gérées manuellement.
# ------------------------------------------------------------

BNF_DNS_RETRIES = 5


# ============================================================
# SESSION HTTP PAR THREAD
# ============================================================

thread_local = threading.local()


def creer_session():

    session = requests.Session()

    # --------------------------------------------------------
    # Important :
    #
    # connect=0
    #
    # Le DNS est géré par notre propre boucle de retry.
    # Cela évite les doubles retries urllib3 + Python.
    # --------------------------------------------------------

    retry = Retry(

        total=2,

        connect=0,

        read=2,

        status=2,

        backoff_factor=0.5,

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

        pool_connections=BNF_WORKERS + 4,

        pool_maxsize=BNF_WORKERS + 4
    )

    session.mount(
        "https://",
        adapter
    )

    session.mount(
        "http://",
        adapter
    )

    session.headers.update({

        "User-Agent":
            "Geneve-Bibliographic-Research-BNF/5.0",

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

        thread_local.session = (
            creer_session()
        )

    return thread_local.session


# ============================================================
# NORMALISATION BNF_ID
# ============================================================

BNF_ID_RE = re.compile(
    r"[^0-9A-Za-z]"
)


def normaliser_bnf_id(valeur):

    if pd.isna(valeur):
        return None

    valeur = (
        str(valeur)
        .strip()
    )

    # --------------------------------------------------------
    # Cas Excel / pandas :
    #
    # 166685984.0
    # --------------------------------------------------------

    valeur = re.sub(
        r"\.0$",
        "",
        valeur
    )

    # --------------------------------------------------------
    # Si jamais le préfixe cb est déjà présent
    # --------------------------------------------------------

    if valeur.lower().startswith("cb"):

        valeur = valeur[2:]

    valeur = BNF_ID_RE.sub(
        "",
        valeur
    )

    if not valeur:
        return None

    return valeur


# ============================================================
# ISBN
# ============================================================

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

    return cle == int(
        isbn[-1]
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

    texte = (
        str(valeur)
        .upper()
        .strip()
    )

    # --------------------------------------------------------
    # Exemple :
    #
    # 2-7283-0949-3
    # 978-2-7283-0949-8
    # --------------------------------------------------------

    match = ISBN_DEBUT_RE.match(
        texte
    )

    if match:

        texte = match.group(1)

    isbn = ISBN_NETTOYAGE_RE.sub(
        "",
        texte
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

        isbn13 = isbn10_vers_13(
            isbn
        )

        if isbn13_valide(isbn13):
            return isbn13

    return None


# ============================================================
# TEXTE / ANNÉE
# ============================================================

ANNEE_RE = re.compile(
    r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b"
)


def nettoyer_texte(texte):

    if not texte:
        return None

    texte = (
        str(texte)
        .strip()
    )

    return texte or None


def extraire_annee(valeur):

    if not valeur:
        return None

    match = ANNEE_RE.search(
        str(valeur)
    )

    if match:

        return int(
            match.group(1)
        )

    return None


# ============================================================
# EXTRACTION ARK BNF
# ============================================================

ARK_BNF_RE = re.compile(
    r"/ark:/12148/(cb[0-9A-Za-z]+)",
    re.I
)


def extraire_ark_bnf(uri):

    if not uri:
        return None

    match = ARK_BNF_RE.search(
        str(uri)
    )

    if not match:
        return None

    return match.group(1)


# ============================================================
# REQUÊTE HTTP BNF AVEC RETRIES DNS
# ============================================================

def executer_requete_bnf(

    query,

    tentatives=BNF_DNS_RETRIES
):

    session = obtenir_session()

    derniere_erreur = None

    for tentative in range(
        1,
        tentatives + 1
    ):

        try:

            response = session.post(

                SPARQL_URL,

                data={
                    "query": query,
                    "format": "json"
                },

                timeout=(
                    CONNECT_TIMEOUT,
                    READ_TIMEOUT
                )
            )

            response.raise_for_status()

            return (
                response
                .json()
                .get(
                    "results",
                    {}
                )
                .get(
                    "bindings",
                    []
                )
            )

        # ====================================================
        # DNS / connexion
        # ====================================================

        except requests.exceptions.ConnectionError as e:

            derniere_erreur = e

            if tentative >= tentatives:
                break

            attente = (
                tentative * 3
                + random.uniform(
                    0,
                    0.8
                )
            )

            print(
                f"DNS/connexion BnF "
                f"{tentative}/{tentatives} "
                f"-> retry dans "
                f"{attente:.1f}s"
            )

            time.sleep(
                attente
            )

        # ====================================================
        # Timeout
        # ====================================================

        except requests.exceptions.Timeout as e:

            derniere_erreur = e

            if tentative >= tentatives:
                break

            attente = (
                tentative * 2
                + random.uniform(
                    0,
                    0.8
                )
            )

            print(
                f"Timeout BnF "
                f"{tentative}/{tentatives} "
                f"-> retry dans "
                f"{attente:.1f}s"
            )

            time.sleep(
                attente
            )

        # ====================================================
        # Autres erreurs HTTP
        # ====================================================

        except requests.exceptions.RequestException as e:

            derniere_erreur = e

            if tentative >= tentatives:
                break

            attente = (
                tentative * 2
                + random.uniform(
                    0,
                    0.5
                )
            )

            time.sleep(
                attente
            )

        # ====================================================
        # JSON invalide
        # ====================================================

        except ValueError as e:

            derniere_erreur = e

            break

    if derniere_erreur:

        raise derniere_erreur

    raise RuntimeError(
        "Erreur BnF inconnue"
    )


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
# CONSTRUCTION DE LA REQUÊTE SPARQL
#
# DIRECTEMENT :
#
# bnf_id
#   ↓
# auteur BnF
#   ↓
# expression
#   ↓
# manifestation
#   ↓
# ISBN
# ============================================================

def construire_requete_bnf(
    liste_bnf_id
):

    values = "\n".join(
        (
            f'("{bnf_id}" '
            f'<http://data.bnf.fr/ark:/12148/'
            f'cb{bnf_id}#about>)'
        )
        for bnf_id
        in liste_bnf_id
    )

    query = f"""
PREFIX dcterms:
    <http://purl.org/dc/terms/>

PREFIX foaf:
    <http://xmlns.com/foaf/0.1/>

PREFIX rdarel:
    <http://rdvocab.info/RDARelationshipsWEMI/>

PREFIX rdavocab:
    <http://rdvocab.info/Elements/>

PREFIX bnf:
    <http://data.bnf.fr/ontology/bnf-onto/>


SELECT DISTINCT

    ?bnf_id
    ?auteur
    ?manifestation
    ?isbn
    ?titre
    ?date
    ?lieu
    ?editeur
    ?nomAuteur
    ?relation_bnf

WHERE
{{

    # ========================================================
    # BNF IDs demandés
    # ========================================================

    VALUES (
        ?bnf_id
        ?auteur
    )
    {{
        {values}
    }}


    # ========================================================
    # RELATION AVEC LA PERSONNE
    #
    # 1) contribution :
    #    auteur, traducteur, préfacier, illustrateur, etc.
    #
    # 2) sujet :
    #    la personne est le sujet de la ressource
    # ========================================================

    {{
        # ----------------------------------------------------
        # CAS 1 : CONTRIBUTION
        #
        # On conserve volontairement tous les rôles.
        # ----------------------------------------------------

        ?expression
            ?role
            ?auteur .

        ?manifestation
            rdarel:expressionManifested
            ?expression .

        BIND(
            "contribution"
            AS ?relation_bnf
        )
    }}

    UNION

    {{
        # ----------------------------------------------------
        # CAS 2 : MANIFESTATION AYANT LA PERSONNE COMME SUJET
        # ----------------------------------------------------

        ?manifestation
            dcterms:subject
            ?auteur .

        BIND(
            "sujet"
            AS ?relation_bnf
        )
    }}

    UNION

    {{
        # ----------------------------------------------------
        # CAS 3 : EXPRESSION AYANT LA PERSONNE COMME SUJET
        # ----------------------------------------------------

        ?expression
            dcterms:subject
            ?auteur .

        ?manifestation
            rdarel:expressionManifested
            ?expression .

        BIND(
            "sujet"
            AS ?relation_bnf
        )
    }}


    # ========================================================
    # ISBN OBLIGATOIRE
    # ========================================================

    ?manifestation
        bnf:isbn
        ?isbn .


    # ========================================================
    # MÉTADONNÉES
    # ========================================================

    OPTIONAL
    {{
        ?manifestation
            dcterms:title
            ?titre .
    }}

    OPTIONAL
    {{
        ?manifestation
            dcterms:date
            ?date .
    }}

    OPTIONAL
    {{
        ?manifestation
            rdavocab:placeOfPublication
            ?lieu .
    }}

    OPTIONAL
    {{
        ?manifestation
            rdavocab:publishersName
            ?editeur .
    }}

    OPTIONAL
    {{
        ?auteur
            foaf:name
            ?nomAuteur .
    }}

}}
"""

    return query


# ============================================================
# REQUÊTE D'UN LOT
# ============================================================

def requete_bnf_lot(
    liste_bnf_id
):

    query = construire_requete_bnf(
        liste_bnf_id
    )

    return executer_requete_bnf(
        query
    )


# ============================================================
# FALLBACK ROBUSTE
#
# Exemple :
#
# 20 IDs
# ↓
# erreur
# ↓
# 10 + 10
# ↓
# éventuellement 5 + 5...
#
# Cela permet de ne pas perdre tout un lot.
# ============================================================

def requete_bnf_robuste(
    liste_bnf_id
):

    try:

        return (
            requete_bnf_lot(
                liste_bnf_id
            )
        )

    except Exception:

        if len(liste_bnf_id) <= 1:
            raise

        milieu = (
            len(liste_bnf_id)
            // 2
        )

        gauche = (
            requete_bnf_robuste(

                liste_bnf_id[
                    :milieu
                ]
            )
        )

        droite = (
            requete_bnf_robuste(

                liste_bnf_id[
                    milieu:
                ]
            )
        )

        return (
            gauche
            + droite
        )


# ============================================================
# PARSING DES RÉSULTATS
# ============================================================

def parser_bindings_bnf(
    bindings
):

    lignes = []

    append = lignes.append

    for ligne in bindings:

        # ====================================================
        # BNF_ID
        # ====================================================

        bnf_id = (

            ligne
            .get(
                "bnf_id",
                {}
            )
            .get(
                "value"
            )
        )

        if not bnf_id:
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

        isbn = normaliser_isbn(
            isbn_brut
        )

        if not isbn:
            continue

        # ====================================================
        # MANIFESTATION
        # ====================================================

        manifestation = (

            ligne
            .get(
                "manifestation",
                {}
            )
            .get(
                "value"
            )
        )

        # ====================================================
        # LIGNE FINALE
        # ====================================================

        append({

            "_bnf_id_requete":
                bnf_id,

            "ark_bnf":
                extraire_ark_bnf(
                    manifestation
                ),

            "isbn":
                isbn,

            "titre":
                nettoyer_texte(

                    ligne
                    .get(
                        "titre",
                        {}
                    )
                    .get(
                        "value"
                    )
                ),

            "annee":
                extraire_annee(

                    ligne
                    .get(
                        "date",
                        {}
                    )
                    .get(
                        "value"
                    )
                ),

            "nomAuteur":
                nettoyer_texte(

                    ligne
                    .get(
                        "nomAuteur",
                        {}
                    )
                    .get(
                        "value"
                    )
                ),

            "lieuPublication":
                nettoyer_texte(

                    ligne
                    .get(
                        "lieu",
                        {}
                    )
                    .get(
                        "value"
                    )
                ),

            "editeur":
                nettoyer_texte(

                    ligne
                    .get(
                        "editeur",
                        {}
                    )
                    .get(
                        "value"
                    )
                ),

            "relation_bnf":
                nettoyer_texte(

                    ligne
                    .get(
                        "relation_bnf",
                        {}
                    )
                    .get(
                        "value"
                    )
                )
        })

    return lignes


# ============================================================
# TRAITEMENT D'UN LOT
# ============================================================

def traiter_lot_bnf(
    lot
):

    try:

        bindings = (
            requete_bnf_robuste(
                lot
            )
        )

        lignes = (
            parser_bindings_bnf(
                bindings
            )
        )

        return (
            lignes,
            []
        )

    except Exception as e:

        erreurs = [

            {
                "bnf_id":
                    bnf_id,

                "erreur":
                    type(e).__name__,

                "message":
                    str(e)[:200]
            }

            for bnf_id
            in lot
        ]

        return (
            [],
            erreurs
        )


# ============================================================
# RÉCUPÉRATION PARALLÈLE
# ============================================================

def recuperer_toutes_notices_bnf(

    liste_bnf_id,

    batch_size=BNF_BATCH_SIZE,

    max_workers=BNF_WORKERS
):

    # ========================================================
    # CONSTRUCTION DES LOTS
    # ========================================================

    lots = [

        liste_bnf_id[
            i:
            i + batch_size
        ]

        for i in range(
            0,
            len(liste_bnf_id),
            batch_size
        )
    ]

    nb_lots = len(
        lots
    )

    lignes = []

    erreurs = []

    # ========================================================
    # AFFICHAGE
    # ========================================================

    print()

    print("=" * 80)

    print(
        "1/2 — RÉCUPÉRATION SPARQL BNF"
    )

    print("=" * 80)

    print(
        f"BNF ID uniques    : "
        f"{len(liste_bnf_id):,}"
    )

    print(
        f"BNF ID / requête  : "
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

    print(
        f"Retries DNS        : "
        f"{BNF_DNS_RETRIES}"
    )

    print("=" * 80)

    debut = (
        time.perf_counter()
    )

    # ========================================================
    # THREADPOOL
    # ========================================================

    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:

        futures = {

            executor.submit(
                traiter_lot_bnf,
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

                lignes_lot, erreurs_lot = (
                    future.result()
                )

                lignes.extend(
                    lignes_lot
                )

                erreurs.extend(
                    erreurs_lot
                )

            except Exception as e:

                numero = futures[
                    future
                ]

                print(
                    f"Lot {numero} : "
                    f"{type(e).__name__}: "
                    f"{str(e)[:100]}"
                )

            # =================================================
            # PROGRESSION
            # =================================================

            if (
                termines % 10 == 0
                or termines == nb_lots
            ):

                ecoule = (
                    time.perf_counter()
                    - debut
                )

                print(
                    f"{termines:>5}/"
                    f"{nb_lots} lots "
                    f"| résultats : "
                    f"{len(lignes):,} "
                    f"| erreurs : "
                    f"{len(erreurs):,} "
                    f"| {ecoule:.1f} s"
                )

    return (
        lignes,
        erreurs
    )


# ============================================================
# FONCTION PRINCIPALE
# ============================================================

def ajouter_isbn_bnf_ultra(

    df,

    colonne_bnf="bnf_id",

    batch_size=BNF_BATCH_SIZE,

    max_workers=BNF_WORKERS
):

    debut_total = (
        time.perf_counter()
    )

    # ========================================================
    # CONTRÔLE
    # ========================================================

    if colonne_bnf not in df.columns:

        raise ValueError(

            f"La colonne "
            f"'{colonne_bnf}' "
            f"n'existe pas."
        )

    source = df.copy()

    # ========================================================
    # COLONNES AJOUTÉES PAR BNF
    # ========================================================

    colonnes_bnf = [

        "ark_bnf",

        "isbn",

        "titre",

        "annee",

        "nomAuteur",

        "lieuPublication",

        "editeur",

        "relation_bnf"
    ]

    # ========================================================
    # SUPPRESSION D'ANCIENNES COLONNES
    #
    # Permet de relancer la fonction sur un DataFrame
    # déjà traité sans collision.
    # ========================================================

    source.drop(

        columns=[

            col

            for col
            in colonnes_bnf

            if col in source.columns
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
    # NORMALISATION BNF_ID
    # ========================================================

    source["_bnf_id_requete"] = (

        source[
            colonne_bnf
        ]

        .map(
            normaliser_bnf_id
        )
    )

    # ========================================================
    # LISTE UNIQUE DES AUTORITÉS À INTERROGER
    # ========================================================

    liste_bnf_id = (

        source[
            "_bnf_id_requete"
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
        "BNF — VERSION FINALE OPTIMISÉE"
    )

    print("#" * 80)

    print(
        f"Lignes source      : "
        f"{len(source):,}"
    )

    print(
        f"BNF ID uniques     : "
        f"{len(liste_bnf_id):,}"
    )

    print(
        f"Sans BNF ID        : "
        f"{source['_bnf_id_requete'].isna().sum():,}"
    )

    print(
        f"Batch              : "
        f"{batch_size}"
    )

    print(
        f"Workers            : "
        f"{max_workers}"
    )

    # ========================================================
    # AUCUN BNF_ID
    # ========================================================

    if not liste_bnf_id:

        print(
            "\nAucun bnf_id disponible."
        )

        return pd.DataFrame(

            columns=(
                colonnes_originales
                + colonnes_bnf
            )
        )

    # ========================================================
    # ÉTAPE 1
    #
    # INTERROGATION BNF
    # ========================================================

    t1 = (
        time.perf_counter()
    )

    lignes, erreurs_bnf = (
        recuperer_toutes_notices_bnf(

            liste_bnf_id,

            batch_size=
                batch_size,

            max_workers=
                max_workers
        )
    )

    duree_bnf = (
        time.perf_counter()
        - t1
    )

    # ========================================================
    # PAS DE LIVRES
    # ========================================================

    if not lignes:

        print(
            "\nAucun ISBN BnF trouvé."
        )

        resultat = pd.DataFrame(

            columns=(
                colonnes_originales
                + colonnes_bnf
            )
        )

        resultat.attrs[
            "erreurs_bnf"
        ] = erreurs_bnf

        return resultat

    # ========================================================
    # ÉTAPE 2
    #
    # DATAFRAME
    # ========================================================

    print()

    print("=" * 80)

    print(
        "2/2 — CONSTRUCTION DU DATAFRAME"
    )

    print("=" * 80)

    t2 = (
        time.perf_counter()
    )

    df_isbn = (
        pd.DataFrame.from_records(
            lignes
        )
    )

    nb_lignes_brutes = len(
        df_isbn
    )

    # ========================================================
    # FILTRE 182$c = c
    #
    # Média informatique -> ISBN exclu avant dédoublonnage
    # et avant fusion avec la table originale.
    # ========================================================

    df_isbn, nb_lignes_media_exclues = (
        filtrer_medias_informatiques_bnf(
            df_isbn,
            max_workers=SRU_WORKERS
        )
    )

    # Si toutes les notices trouvées sont électroniques,
    # on retourne un DataFrame vide avec les bonnes colonnes.
    if df_isbn.empty:

        print(
            "\nTous les ISBN BnF trouvés ont été exclus "
            "car 182$c = c."
        )

        resultat = pd.DataFrame(
            columns=(
                colonnes_originales
                + colonnes_bnf
            )
        )

        resultat.attrs[
            "erreurs_bnf"
        ] = erreurs_bnf

        resultat.attrs[
            "nb_lignes_media_exclues"
        ] = nb_lignes_media_exclues

        return resultat

    # ========================================================
    # DÉDOUBLONNAGE
    #
    # Important :
    #
    # La BnF peut retourner :
    #
    # 1782383581
    # 9781782383581
    #
    # Les deux sont normalisés en :
    #
    # 9781782383581
    #
    # Donc :
    #
    # bnf_id + ISBN normalisé
    # = une seule ligne finale.
    # ========================================================

    # ========================================================
    # FUSION DES TYPES DE RELATION
    #
    # Un même ISBN peut être trouvé plusieurs fois pour le même
    # bnf_id, notamment comme :
    #
    # contribution
    # sujet
    #
    # On conserve une seule ligne par bnf_id + ISBN et on fusionne
    # les relations sous la forme :
    #
    # contribution | sujet
    # ========================================================

    relations = (
        df_isbn
        .groupby(
            [
                "_bnf_id_requete",
                "isbn"
            ],
            dropna=False
        )["relation_bnf"]
        .agg(
            lambda valeurs:
                " | ".join(
                    sorted(
                        {
                            valeur
                            for valeur in valeurs
                            if pd.notna(valeur)
                            and str(valeur).strip()
                        }
                    )
                )
        )
        .reset_index()
    )

    df_isbn_unique = (
        df_isbn
        .drop(
            columns=[
                "relation_bnf"
            ]
        )
        .drop_duplicates(
            subset=[
                "_bnf_id_requete",
                "isbn"
            ],
            keep="first"
        )
    )

    df_isbn = (
        df_isbn_unique
        .merge(
            relations,
            on=[
                "_bnf_id_requete",
                "isbn"
            ],
            how="left",
            sort=False,
            copy=False
        )
    )

    nb_lignes_dedoublonnees = len(
        df_isbn
    )

    # ========================================================
    # MERGE AVEC LA TABLE ORIGINALE
    # ========================================================

    resultat = source.merge(

        df_isbn,

        on="_bnf_id_requete",

        how="inner",

        sort=False,

        copy=False
    )

    # ========================================================
    # SUPPRESSION COLONNE TECHNIQUE
    # ========================================================

    resultat.drop(

        columns=[
            "_bnf_id_requete"
        ],

        inplace=True
    )

    # ========================================================
    # ORDRE DES COLONNES
    # ========================================================

    resultat = resultat[

        colonnes_originales
        + colonnes_bnf
    ]

    # ========================================================
    # TYPES
    # ========================================================

    resultat[
        colonne_bnf
    ] = (

        resultat[
            colonne_bnf
        ]

        .astype(
            "string"
        )
    )

    resultat[
        "ark_bnf"
    ] = (

        resultat[
            "ark_bnf"
        ]

        .astype(
            "string"
        )
    )

    resultat[
        "isbn"
    ] = (

        resultat[
            "isbn"
        ]

        .astype(
            "string"
        )
    )

    resultat[
        "relation_bnf"
    ] = (
        resultat[
            "relation_bnf"
        ]
        .astype(
            "string"
        )
    )

    resultat[
        "annee"
    ] = (

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

    # --------------------------------------------------------
    # Si la colonne ppn existe, on la conserve explicitement
    # comme texte.
    #
    # Elle n'a PAS servi aux requêtes BnF.
    # --------------------------------------------------------

    if "ppn" in resultat.columns:

        resultat[
            "ppn"
        ] = (

            resultat[
                "ppn"
            ]

            .astype(
                "string"
            )
        )

    resultat.reset_index(

        drop=True,

        inplace=True
    )

    # ========================================================
    # TEMPS
    # ========================================================

    duree_dataframe = (
        time.perf_counter()
        - t2
    )

    duree_totale = (
        time.perf_counter()
        - debut_total
    )

    # ========================================================
    # STOCKER LES ERREURS
    # ========================================================

    resultat.attrs[
        "erreurs_bnf"
    ] = erreurs_bnf

    resultat.attrs[
        "nb_lignes_media_exclues"
    ] = nb_lignes_media_exclues

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
        f"SPARQL BnF        : "
        f"{duree_bnf:.1f} s"
    )

    print(
        f"DataFrame         : "
        f"{duree_dataframe:.1f} s"
    )

    print()

    print(
        f"DURÉE TOTALE      : "
        f"{duree_totale:.1f} s "
        f"({duree_totale / 60:.2f} min)"
    )

    print()

    print(
        f"BNF ID interrogés : "
        f"{len(liste_bnf_id):,}"
    )

    print(
        f"Bindings valides  : "
        f"{nb_lignes_brutes:,}"
    )

    print(
        f"Après dédoublonn. : "
        f"{nb_lignes_dedoublonnees:,}"
    )

    print(
        f"BNF ID avec ISBN  : "
        f"{resultat[colonne_bnf].nunique():,}"
    )

    print(
        f"ARK BnF uniques   : "
        f"{resultat['ark_bnf'].nunique():,}"
    )

    print(
        f"ISBN uniques      : "
        f"{resultat['isbn'].nunique():,}"
    )

    print(
        f"Lignes finales    : "
        f"{len(resultat):,}"
    )

    print(
        f"Exclues 182$c=c   : "
        f"{nb_lignes_media_exclues:,}"
    )

    print()

    print(
        f"Erreurs BnF       : "
        f"{len(erreurs_bnf):,}"
    )

    print("#" * 80)

    return resultat