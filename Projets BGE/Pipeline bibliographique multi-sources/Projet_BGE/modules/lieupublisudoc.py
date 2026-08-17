import requests
from lxml import etree
import pandas as pd
import re
import unicodedata
import threading
import time
from pathlib import Path

from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed
)

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

SRU_URL = "https://www.sudoc.abes.fr/cbs/sru/"

NS = {
    "srw": "http://www.loc.gov/zing/srw/"
}

HEADERS = {
    "Accept": "application/xml",
    "User-Agent": "Mozilla/5.0"
}


# ------------------------------------------------------------
# RECHERCHE UNIMARC
# ------------------------------------------------------------

MAX_RECORDS = 1000
MAX_WORKERS = 10


# ------------------------------------------------------------
# CONTRÔLE PICA CEB
# ------------------------------------------------------------

PICA_BATCH_SIZE = 20
PICA_WORKERS = 10


# ------------------------------------------------------------
# RÉSEAU
# ------------------------------------------------------------

TIMEOUT = 90
MAX_RETRIES = 5


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
        dict.fromkeys(
            communes
        )
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
        backoff_factor=0.7,
        status_forcelist=[
            429,
            500,
            502,
            503,
            504
        ],
        allowed_methods=[
            "GET"
        ],
        respect_retry_after_header=True
    )

    pool_size = max(
        MAX_WORKERS,
        PICA_WORKERS
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=pool_size,
        pool_maxsize=pool_size
    )

    session.mount(
        "https://",
        adapter
    )

    session.mount(
        "http://",
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

        thread_local.session = (
            creer_session()
        )

    return thread_local.session


# ============================================================
# NORMALISATION TEXTE
# ============================================================

def normaliser_texte(
    texte
):

    if texte is None:
        return ""

    texte = (
        str(texte)
        .lower()
        .strip()
    )

    texte = (
        texte
        .replace("œ", "oe")
        .replace("æ", "ae")
        .replace("’", "'")
    )

    return "".join(
        caractere
        for caractere
        in unicodedata.normalize(
            "NFD",
            texte
        )
        if unicodedata.category(
            caractere
        ) != "Mn"
    )


# ============================================================
# MOTS-CLÉS
# ============================================================

def construire_variantes():

    variantes = {
        normaliser_texte(
            commune
        )
        for commune
        in COMMUNES_GENEVE
    }

    variantes.update([
        "geneva",
        "genf",
        "ginevra"
    ])

    return sorted(
        variantes,
        key=str.casefold
    )


VARIANTES = construire_variantes()


# ============================================================
# COMMUNES NORMALISÉES
# ============================================================

COMMUNES_NORMALISEES = sorted(
    {
        normaliser_texte(x)
        for x
        in COMMUNES_GENEVE
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


def contient_commune(
    texte
):

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

        elif commune in texte:

            return True

    return False


# ============================================================
# ISBN
# ============================================================

def normaliser_isbn(
    isbn
):

    if isbn is None:
        return None

    isbn = re.sub(
        r"[^0-9X]",
        "",
        str(isbn)
        .upper()
        .strip()
    )

    if (
        len(isbn) == 13
        and isbn.isdigit()
    ):

        return isbn

    if len(isbn) == 10:

        if not isbn[:9].isdigit():
            return None

        if not (
            isbn[-1].isdigit()
            or isbn[-1] == "X"
        ):

            return None

        base = (
            "978"
            + isbn[:9]
        )

        somme = sum(
            int(chiffre)
            *
            (
                1
                if position % 2 == 0
                else 3
            )
            for position, chiffre
            in enumerate(base)
        )

        cle = (
            10
            - (
                somme % 10
            )
        ) % 10

        return (
            base
            + str(cle)
        )

    return None


# ============================================================
# PPN
# ============================================================

def normaliser_ppn(
    valeur
):

    if valeur is None:
        return None

    texte = (
        str(valeur)
        .strip()
        .upper()
    )

    match = re.search(
        r"(?<![0-9A-Z])([0-9]{8}[0-9X])(?![0-9A-Z])",
        texte
    )

    if match:

        return match.group(1)

    texte_nettoye = re.sub(
        r"[^0-9X]",
        "",
        texte
    )

    if (
        len(texte_nettoye) == 9
        and texte_nettoye[:8].isdigit()
        and (
            texte_nettoye[-1].isdigit()
            or texte_nettoye[-1] == "X"
        )
    ):

        return texte_nettoye

    return None


# ============================================================
# OUTILS COMMUNS
# ============================================================

def concat_unique(
    valeurs
):

    valeurs_nettoyees = []
    vus = set()

    for valeur in valeurs:

        if valeur is None:
            continue

        valeur = (
            str(valeur)
            .strip()
        )

        if not valeur:
            continue

        if valeur in vus:
            continue

        vus.add(
            valeur
        )

        valeurs_nettoyees.append(
            valeur
        )

    return " | ".join(
        valeurs_nettoyees
    )


def datafields(
    record
):

    return record.xpath(
        "./*[local-name()='datafield']"
    )


def controlfield(
    record,
    tag
):

    valeurs = record.xpath(
        f"./*[local-name()='controlfield'][@tag='{tag}']/text()"
    )

    if valeurs:

        return (
            valeurs[0]
            .strip()
        )

    return None


def sous_champs(
    record,
    tags,
    codes
):

    tags = {
        str(tag)
        for tag
        in tags
    }

    codes = set(
        codes
    )

    valeurs = []

    for champ in datafields(
        record
    ):

        if (
            champ.get("tag")
            not in tags
        ):

            continue

        for sous in champ.xpath(
            "./*[local-name()='subfield']"
        ):

            if (
                sous.get("code")
                not in codes
            ):

                continue

            if sous.text:

                valeur = (
                    sous.text
                    .strip()
                )

                if valeur:

                    valeurs.append(
                        valeur
                    )

    return valeurs


# ============================================================
# PPN NOTICE SUDOC
# ============================================================

def extraire_ppn_notice_sudoc(
    record
):

    return controlfield(
        record,
        "001"
    )


# ============================================================
# EXTRACTION DES AUTEURS
# ============================================================

def extraire_auteurs_structures(
    record
):

    auteurs = []

    for champ in datafields(
        record
    ):

        tag = champ.get(
            "tag"
        )

        if tag not in {
            "700",
            "701",
            "702"
        }:

            continue

        sous_champs_dict = {}

        for sous in champ.xpath(
            "./*[local-name()='subfield']"
        ):

            code = sous.get(
                "code"
            )

            if not sous.text:
                continue

            valeur = (
                sous.text
                .strip()
            )

            if not valeur:
                continue

            sous_champs_dict.setdefault(
                code,
                []
            ).append(
                valeur
            )

        ppn = None

        for valeur in sous_champs_dict.get(
            "3",
            []
        ):

            ppn_test = normaliser_ppn(
                valeur
            )

            if ppn_test:

                ppn = ppn_test
                break

        nom = " ".join(
            sous_champs_dict.get(
                "a",
                []
            )
        ).strip()

        prenom = " ".join(
            sous_champs_dict.get(
                "b",
                []
            )
        ).strip()

        qualificatif = " ".join(
            sous_champs_dict.get(
                "c",
                []
            )
        ).strip()

        numerotation = " ".join(
            sous_champs_dict.get(
                "d",
                []
            )
        ).strip()

        date = " ".join(
            sous_champs_dict.get(
                "f",
                []
            )
        ).strip()

        developpement = " ".join(
            sous_champs_dict.get(
                "g",
                []
            )
        ).strip()

        if nom and prenom:

            nom_complet = (
                f"{nom}, {prenom}"
            )

        elif nom:

            nom_complet = nom

        elif prenom:

            nom_complet = prenom

        else:

            nom_complet = ""

        if developpement:

            if nom_complet:

                nom_complet += (
                    f" {developpement}"
                )

            else:

                nom_complet = (
                    developpement
                )

        if numerotation:

            if nom_complet:

                nom_complet += (
                    f" {numerotation}"
                )

            else:

                nom_complet = (
                    numerotation
                )

        if qualificatif:

            if nom_complet:

                nom_complet += (
                    f" ({qualificatif})"
                )

            else:

                nom_complet = (
                    qualificatif
                )

        if date:

            if nom_complet:

                nom_complet += (
                    f" ({date})"
                )

            else:

                nom_complet = date

        nom_complet = re.sub(
            r"\s+",
            " ",
            nom_complet
        ).strip()

        if not nom_complet:
            continue

        auteurs.append({
            "ppn":
                ppn,

            "nom":
                nom_complet
        })

    resultat = []
    vus = set()

    for auteur in auteurs:

        cle = (
            auteur["ppn"],
            auteur["nom"]
        )

        if cle in vus:
            continue

        vus.add(
            cle
        )

        resultat.append(
            auteur
        )

    return resultat


# ============================================================
# ISBN
# ============================================================

def extraire_isbn(
    record
):

    valeurs = sous_champs(
        record,
        tags=[
            "010"
        ],
        codes=[
            "a"
        ]
    )

    resultat = set()

    for valeur in valeurs:

        isbn = normaliser_isbn(
            valeur
        )

        if isbn:

            resultat.add(
                isbn
            )

    return sorted(
        resultat
    )


# ============================================================
# TITRE
# ============================================================

def extraire_titre(
    record
):

    return concat_unique(
        sous_champs(
            record,
            tags=[
                "200"
            ],
            codes=[
                "a",
                "e",
                "h",
                "i"
            ]
        )
    )


def extraire_textes_titre(
    record
):

    return sous_champs(
        record,
        tags=[
            "200",
            "423",
            "424",
            "425",
            "430",
            "431",
            "432",
            "433",
            "434",
            "435",
            "436",
            "437",
            "440",
            "441",
            "442",
            "443",
            "444",
            "445",
            "446",
            "447",
            "451",
            "452",
            "453",
            "454",
            "455",
            "456",
            "461",
            "462",
            "463",
            "464",
            "470",
            "481",
            "482",
            "488",
            "500",
            "501",
            "503",
            "510",
            "511",
            "512",
            "513",
            "514",
            "515",
            "516",
            "517",
            "518",
            "520",
            "530",
            "531",
            "532",
            "540",
            "541",
            "545"
        ],
        codes=[
            "a",
            "e",
            "h",
            "i",
            "t"
        ]
    )


# ============================================================
# PUBLICATION
# ============================================================

def extraire_lieux_publication(
    record
):

    return sous_champs(
        record,
        tags=[
            "210",
            "214"
        ],
        codes=[
            "a"
        ]
    )


def extraire_editeurs(
    record
):

    return concat_unique(
        sous_champs(
            record,
            tags=[
                "210",
                "214"
            ],
            codes=[
                "c"
            ]
        )
    )


# ============================================================
# ANNÉE
# ============================================================

def extraire_annee(
    record
):

    valeurs = sous_champs(
        record,
        tags=[
            "210",
            "214"
        ],
        codes=[
            "d"
        ]
    )

    for valeur in valeurs:

        match = re.search(
            r"(?<!\d)(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})(?!\d)",
            str(valeur)
        )

        if match:

            return match.group(1)

    champ100 = sous_champs(
        record,
        tags=[
            "100"
        ],
        codes=[
            "a"
        ]
    )

    for valeur in champ100:

        match = re.search(
            r"(?<!\d)(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})(?!\d)",
            str(valeur)
        )

        if match:

            return match.group(1)

    return None


# ============================================================
# SUJETS
# ============================================================

def extraire_textes_sujets(
    record
):

    valeurs = []

    for champ in datafields(
        record
    ):

        tag = champ.get(
            "tag",
            ""
        )

        if not (
            len(tag) == 3
            and tag.isdigit()
            and 600 <= int(tag) <= 609
        ):

            continue

        for sous in champ.xpath(
            "./*[local-name()='subfield']"
        ):

            if sous.get(
                "code"
            ) in {
                "2",
                "3"
            }:

                continue

            if sous.text:

                valeur = (
                    sous.text
                    .strip()
                )

                if valeur:

                    valeurs.append(
                        valeur
                    )

    return valeurs


def extraire_sujets(
    record
):

    return concat_unique(
        extraire_textes_sujets(
            record
        )
    )


# ============================================================
# RAISONS
# ============================================================

def detecter_raisons(
    record
):

    raisons = []

    if any(
        contient_commune(
            x
        )
        for x
        in extraire_textes_titre(
            record
        )
    ):

        raisons.append(
            "titre"
        )

    if any(
        contient_commune(
            x
        )
        for x
        in extraire_lieux_publication(
            record
        )
    ):

        raisons.append(
            "lieu_publication"
        )

    if any(
        contient_commune(
            x
        )
        for x
        in extraire_textes_sujets(
            record
        )
    ):

        raisons.append(
            "sujet"
        )

    return raisons


# ============================================================
# ANALYSE D'UNE NOTICE UNIMARC
# ============================================================

def analyser_record(
    record
):

    ppn_notice_sudoc = (
        extraire_ppn_notice_sudoc(
            record
        )
    )

    if not ppn_notice_sudoc:
        return None

    isbn = extraire_isbn(
        record
    )

    if not isbn:
        return None

    raisons = detecter_raisons(
        record
    )

    if not raisons:
        return None

    auteurs_structures = (
        extraire_auteurs_structures(
            record
        )
    )

    ppn_idref = []
    noms_auteurs = []

    vus_ppn = set()
    vus_noms = set()

    for auteur in auteurs_structures:

        ppn = auteur[
            "ppn"
        ]

        nom = auteur[
            "nom"
        ]

        if (
            ppn
            and ppn not in vus_ppn
        ):

            vus_ppn.add(
                ppn
            )

            ppn_idref.append(
                ppn
            )

        if (
            nom
            and nom not in vus_noms
        ):

            vus_noms.add(
                nom
            )

            noms_auteurs.append(
                nom
            )

    return {
        "ppn_notice_sudoc":
            ppn_notice_sudoc,

        "ppn":
            ppn_idref,

        "isbn":
            isbn,

        "titre":
            extraire_titre(
                record
            ),

        "annee":
            extraire_annee(
                record
            ),

        "nomAuteur":
            " | ".join(
                noms_auteurs
            ),

        "lieuPublication":
            concat_unique(
                extraire_lieux_publication(
                    record
                )
            ),

        "editeur":
            extraire_editeurs(
                record
            ),

        "sujet":
            extraire_sujets(
                record
            ),

        "raisons":
            set(
                raisons
            )
    }


# ============================================================
# REQUÊTE SRU UNIMARC
# ============================================================

def requete_sru(
    query,
    start_record=1
):

    session = get_session()

    params = {
        "version":
            "1.1",

        "operation":
            "searchRetrieve",

        "recordSchema":
            "unimarc",

        "recordPacking":
            "xml",

        "query":
            query,

        "startRecord":
            start_record,

        "maximumRecords":
            MAX_RECORDS
    }

    response = session.get(
        SRU_URL,
        params=params,
        timeout=TIMEOUT
    )

    response.raise_for_status()

    return etree.fromstring(
        response.content
    )


def nombre_resultats(
    root
):

    valeur = root.xpath(
        "string(.//srw:numberOfRecords)",
        namespaces=NS
    )

    try:

        return int(
            valeur
        )

    except Exception:

        return 0


def records_unimarc(
    root
):

    return root.xpath(
        ".//srw:recordData/*[local-name()='record']",
        namespaces=NS
    )


# ============================================================
# CONSTRUCTION REQUÊTE
# ============================================================

def construire_requete(
    mot_cle
):

    mot = (
        str(mot_cle)
        .strip()
    )

    if " " in mot:

        mot = (
            '"'
            +
            mot.replace(
                '"',
                '\\"'
            )
            +
            '"'
        )

    return (
        f"tou={mot}"
    )


# ============================================================
# FUSION NOTICE
# ============================================================

def fusionner_notice(
    notices,
    notice
):

    if notice is None:
        return

    ppn_notice = (
        notice[
            "ppn_notice_sudoc"
        ]
    )

    if ppn_notice not in notices:

        notices[
            ppn_notice
        ] = notice

        return

    existante = notices[
        ppn_notice
    ]

    existante[
        "raisons"
    ].update(
        notice[
            "raisons"
        ]
    )

    existante[
        "ppn"
    ] = list(
        dict.fromkeys(
            existante[
                "ppn"
            ]
            +
            notice[
                "ppn"
            ]
        )
    )

    existante[
        "isbn"
    ] = list(
        dict.fromkeys(
            existante.get(
                "isbn",
                []
            )
            +
            notice.get(
                "isbn",
                []
            )
        )
    )

    auteurs_existants = [
        x.strip()
        for x
        in existante[
            "nomAuteur"
        ].split("|")
        if x.strip()
    ]

    nouveaux_auteurs = [
        x.strip()
        for x
        in notice[
            "nomAuteur"
        ].split("|")
        if x.strip()
    ]

    tous_auteurs = list(
        dict.fromkeys(
            auteurs_existants
            +
            nouveaux_auteurs
        )
    )

    existante[
        "nomAuteur"
    ] = " | ".join(
        tous_auteurs
    )


# ============================================================
# RECHERCHE SUDOC GENEVENSIA
# ============================================================

def rechercher_geneve_sudoc():

    debut = (
        time.perf_counter()
    )

    print()
    print(
        "=========================================="
    )
    print(
        "RECHERCHE SUDOC - GENEVENSIA"
    )
    print(
        "=========================================="
    )

    print(
        f"Mots-clés : "
        f"{len(VARIANTES)}"
    )

    print(
        f"Workers : "
        f"{MAX_WORKERS}"
    )

    print(
        f"Notices par page : "
        f"{MAX_RECORDS}"
    )

    print(
        "ISBN normalisés : ISBN-13 uniquement"
    )

    print()

    requetes = {
        mot_cle:
            construire_requete(
                mot_cle
            )
        for mot_cle
        in VARIANTES
    }

    totaux = {}
    premieres_pages = {}

    print(
        "Phase 1/2 - premières pages..."
    )

    with ThreadPoolExecutor(
        max_workers=
            MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                requete_sru,
                query,
                1
            ):
                mot_cle
            for mot_cle, query
            in requetes.items()
        }

        for future in as_completed(
            futures
        ):

            mot_cle = futures[
                future
            ]

            try:

                root = (
                    future.result()
                )

                total = (
                    nombre_resultats(
                        root
                    )
                )

                totaux[
                    mot_cle
                ] = total

                premieres_pages[
                    mot_cle
                ] = root

                print(
                    f"✓ {mot_cle:<25} : "
                    f"{total:,}"
                )

            except Exception as erreur:

                totaux[
                    mot_cle
                ] = 0

                print(
                    f"✗ {mot_cle:<25} : "
                    f"{type(erreur).__name__} - "
                    f"{erreur}"
                )

    notices = {}

    for root in premieres_pages.values():

        for record in records_unimarc(
            root
        ):

            fusionner_notice(
                notices,
                analyser_record(
                    record
                )
            )

    premieres_pages.clear()

    taches = []

    for mot_cle, total in totaux.items():

        if total <= MAX_RECORDS:
            continue

        query = requetes[
            mot_cle
        ]

        for start_record in range(
            MAX_RECORDS + 1,
            total + 1,
            MAX_RECORDS
        ):

            taches.append(
                (
                    mot_cle,
                    query,
                    start_record
                )
            )

    total_pages = len(
        taches
    )

    print()

    print(
        f"Phase 2/2 - pages supplémentaires : "
        f"{total_pages:,}"
    )

    if total_pages > 0:

        terminees = 0
        erreurs = 0

        with ThreadPoolExecutor(
            max_workers=
                MAX_WORKERS
        ) as executor:

            futures = {
                executor.submit(
                    requete_sru,
                    query,
                    start_record
                ):
                    (
                        mot_cle,
                        start_record
                    )
                for (
                    mot_cle,
                    query,
                    start_record
                )
                in taches
            }

            for future in as_completed(
                futures
            ):

                (
                    mot_cle,
                    start_record
                ) = futures[
                    future
                ]

                try:

                    root = (
                        future.result()
                    )

                    for record in records_unimarc(
                        root
                    ):

                        fusionner_notice(
                            notices,
                            analyser_record(
                                record
                            )
                        )

                except Exception as erreur:

                    erreurs += 1

                    print()

                    print(
                        f"✗ {mot_cle} "
                        f"startRecord={start_record} : "
                        f"{type(erreur).__name__} - "
                        f"{erreur}"
                    )

                terminees += 1

                if (
                    terminees % 10 == 0
                    or terminees == total_pages
                ):

                    print(
                        f"\rPages terminées : "
                        f"{terminees:,}/"
                        f"{total_pages:,} "
                        f"({terminees / total_pages * 100:.1f} %) "
                        f"| notices utiles uniques : "
                        f"{len(notices):,} "
                        f"| erreurs : "
                        f"{erreurs}",
                        end=""
                    )

        print()

    duree = (
        time.perf_counter()
        -
        debut
    )

    print()

    print(
        f"Recherche terminée en "
        f"{duree:.1f}s "
        f"({duree / 60:.1f} min)"
    )

    print(
        f"Notices utiles uniques avant CEB : "
        f"{len(notices):,}"
    )

    return notices


# ============================================================
# PICA
# ============================================================

def records_pica(
    root
):

    return root.xpath(
        ".//srw:recordData/*[local-name()='record']",
        namespaces=NS
    )


# ============================================================
# PPN PICA : 003@ $0
# ============================================================

def extraire_ppn_pica(
    record
):

    valeurs = sous_champs(
        record,
        tags=[
            "003@"
        ],
        codes=[
            "0"
        ]
    )

    if not valeurs:
        return None

    return normaliser_ppn(
        valeurs[0]
    )


# ============================================================
# TEST 012V$c = ceb
# ============================================================

def record_pica_est_ceb(
    record
):

    valeurs = sous_champs(
        record,
        tags=[
            "012V"
        ],
        codes=[
            "c"
        ]
    )

    for valeur in valeurs:

        if (
            str(valeur)
            .strip()
            .lower()
            == "ceb"
        ):

            return True

    return False


# ============================================================
# REQUÊTE PICA PAR LOT
# ============================================================

def requete_pica_lot(
    lot
):

    query = " or ".join(
        f"pica.ppn={ppn}"
        for ppn
        in lot
    )

    params = {
        "version":
            "1.1",

        "operation":
            "searchRetrieve",

        "recordSchema":
            "pica",

        "recordPacking":
            "xml",

        "query":
            query,

        "startRecord":
            1,

        "maximumRecords":
            len(lot)
    }

    session = get_session()

    response = session.get(
        SRU_URL,
        params=params,
        timeout=TIMEOUT
    )

    response.raise_for_status()

    return etree.fromstring(
        response.content
    )


# ============================================================
# FALLBACK PICA
# ============================================================

def requete_pica_robuste(
    lot
):

    try:

        return [
            requete_pica_lot(
                lot
            )
        ]

    except Exception:

        if len(lot) <= 1:
            raise

        milieu = (
            len(lot)
            // 2
        )

        return (
            requete_pica_robuste(
                lot[
                    :milieu
                ]
            )
            +
            requete_pica_robuste(
                lot[
                    milieu:
                ]
            )
        )


# ============================================================
# TRAITEMENT LOT PICA
# ============================================================

def traiter_lot_pica_ceb(
    lot
):

    ppn_ceb = set()
    ppn_recus = set()
    erreurs = []

    try:

        roots = (
            requete_pica_robuste(
                lot
            )
        )

        for root in roots:

            for record in records_pica(
                root
            ):

                ppn = (
                    extraire_ppn_pica(
                        record
                    )
                )

                if not ppn:
                    continue

                ppn_recus.add(
                    ppn
                )

                if record_pica_est_ceb(
                    record
                ):

                    ppn_ceb.add(
                        ppn
                    )

    except Exception:

        erreurs.extend(
            lot
        )

    return (
        ppn_ceb,
        ppn_recus,
        erreurs
    )


# ============================================================
# IDENTIFICATION PARALLÈLE DES CEB
# ============================================================

def recuperer_ppn_ceb(
    liste_ppn
):

    liste_ppn = [
        ppn_normalise
        for valeur
        in liste_ppn
        if (
            ppn_normalise :=
            normaliser_ppn(
                valeur
            )
        )
    ]

    liste_ppn = list(
        dict.fromkeys(
            liste_ppn
        )
    )

    if not liste_ppn:

        return (
            set(),
            set(),
            [],
            0.0
        )

    lots = [
        liste_ppn[
            i:
            i + PICA_BATCH_SIZE
        ]
        for i in range(
            0,
            len(liste_ppn),
            PICA_BATCH_SIZE
        )
    ]

    ppn_ceb = set()
    ppn_recus = set()
    erreurs = []

    debut = (
        time.perf_counter()
    )

    print()
    print(
        "=========================================="
    )
    print(
        "CONTRÔLE PICA - 012V$c = ceb"
    )
    print(
        "=========================================="
    )

    print(
        f"PPN à contrôler : "
        f"{len(liste_ppn):,}"
    )

    print(
        f"Lots : "
        f"{len(lots):,}"
    )

    print(
        f"Batch : "
        f"{PICA_BATCH_SIZE}"
    )

    print(
        f"Workers : "
        f"{PICA_WORKERS}"
    )

    with ThreadPoolExecutor(
        max_workers=
            PICA_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                traiter_lot_pica_ceb,
                lot
            ):
                lot
            for lot
            in lots
        }

        termines = 0

        for future in as_completed(
            futures
        ):

            termines += 1

            try:

                (
                    ceb_lot,
                    recus_lot,
                    erreurs_lot
                ) = (
                    future.result()
                )

                ppn_ceb.update(
                    ceb_lot
                )

                ppn_recus.update(
                    recus_lot
                )

                erreurs.extend(
                    erreurs_lot
                )

            except Exception:

                erreurs.extend(
                    futures[
                        future
                    ]
                )

            if (
                termines % 10 == 0
                or termines == len(lots)
            ):

                print(
                    f"\rLots terminés : "
                    f"{termines:,}/"
                    f"{len(lots):,} "
                    f"| contrôlés="
                    f"{len(ppn_recus):,} "
                    f"| CEB="
                    f"{len(ppn_ceb):,} "
                    f"| erreurs="
                    f"{len(erreurs):,}",
                    end=""
                )

    print()

    duree = (
        time.perf_counter()
        -
        debut
    )

    ppn_absents = (
        set(liste_ppn)
        -
        ppn_recus
    )

    print()

    print(
        f"PPN contrôlés : "
        f"{len(ppn_recus):,}"
    )

    print(
        f"Notices CEB : "
        f"{len(ppn_ceb):,}"
    )

    print(
        f"PPN PICA absents : "
        f"{len(ppn_absents):,}"
    )

    print(
        f"Erreurs PICA : "
        f"{len(erreurs):,}"
    )

    print(
        f"Durée contrôle CEB : "
        f"{duree:.1f} s"
    )

    return (
        ppn_ceb,
        ppn_absents,
        erreurs,
        duree
    )


# ============================================================
# TABLEAU DES ISBN EXCLUS
# ============================================================

def construire_isbn_ceb_exclus(
    notices,
    ppn_ceb
):

    lignes = []

    for ppn_sudoc in ppn_ceb:

        notice = notices.get(
            ppn_sudoc
        )

        if not notice:
            continue

        for isbn in notice.get(
            "isbn",
            []
        ):

            lignes.append({
                "ppn_sudoc":
                    ppn_sudoc,

                "isbn_normalise":
                    isbn,

                "titre":
                    notice.get(
                        "titre"
                    ),

                "nomAuteur":
                    notice.get(
                        "nomAuteur"
                    ),

                "raison_exclusion":
                    "012V$c = ceb"
            })

    colonnes = [
        "ppn_sudoc",
        "isbn_normalise",
        "titre",
        "nomAuteur",
        "raison_exclusion"
    ]

    if not lignes:

        return pd.DataFrame(
            columns=colonnes
        )

    df = pd.DataFrame(
        lignes
    )[
        colonnes
    ]

    df = (
        df
        .drop_duplicates()
        .reset_index(
            drop=True
        )
    )

    df[
        "ppn_sudoc"
    ] = (
        df[
            "ppn_sudoc"
        ]
        .astype(
            "string"
        )
    )

    df[
        "isbn_normalise"
    ] = (
        df[
            "isbn_normalise"
        ]
        .astype(
            "string"
        )
    )

    return df


# ============================================================
# LISTE DES PPN IDREF
# ============================================================

def extraire_liste_ppn_idref(
    notices
):

    liste = []
    vus = set()

    for notice in notices.values():

        for ppn in notice.get(
            "ppn",
            []
        ):

            if ppn is None:
                continue

            ppn = (
                str(ppn)
                .strip()
            )

            if not ppn:
                continue

            if ppn in vus:
                continue

            vus.add(
                ppn
            )

            liste.append(
                ppn
            )

    return liste


# ============================================================
# DATAFRAME FINAL
# ============================================================

def notices_vers_dataframe(
    notices,
    ppn_ceb=None
):

    if ppn_ceb is None:

        ppn_ceb = set()

    colonnes_finales = [
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

    lignes = []

    for notice in notices.values():

        ppn_notice_sudoc = (
            notice.get(
                "ppn_notice_sudoc"
            )
        )

        if (
            ppn_notice_sudoc
            in ppn_ceb
        ):

            continue

        raisons_txt = " | ".join(
            sorted(
                notice[
                    "raisons"
                ]
            )
        )

        ppns = notice.get(
            "ppn",
            []
        )

        ppns = [
            str(ppn)
            .strip()
            for ppn
            in ppns
            if (
                ppn is not None
                and str(ppn).strip()
            )
        ]

        ppns = list(
            dict.fromkeys(
                ppns
            )
        )

        isbns = notice.get(
            "isbn",
            []
        )

        isbns = [
            str(isbn)
            .strip()
            for isbn
            in isbns
            if (
                isbn is not None
                and str(isbn).strip()
            )
        ]

        isbns = list(
            dict.fromkeys(
                isbns
            )
        )

        if not isbns:
            continue

        if ppns:

            for ppn in ppns:

                for isbn_normalise in isbns:

                    lignes.append({
                        "ppn":
                            ppn,

                        "isbn_normalise":
                            isbn_normalise,

                        "titre":
                            notice.get(
                                "titre"
                            ),

                        "annee":
                            notice.get(
                                "annee"
                            ),

                        "nomAuteur":
                            notice.get(
                                "nomAuteur"
                            ),

                        "lieuPublication":
                            notice.get(
                                "lieuPublication"
                            ),

                        "editeur":
                            notice.get(
                                "editeur"
                            ),

                        "sujet":
                            notice.get(
                                "sujet"
                            ),

                        "raisons":
                            raisons_txt
                    })

        else:

            for isbn_normalise in isbns:

                lignes.append({
                    "ppn":
                        None,

                    "isbn_normalise":
                        isbn_normalise,

                    "titre":
                        notice.get(
                            "titre"
                        ),

                    "annee":
                        notice.get(
                            "annee"
                        ),

                    "nomAuteur":
                        notice.get(
                            "nomAuteur"
                        ),

                    "lieuPublication":
                        notice.get(
                            "lieuPublication"
                        ),

                    "editeur":
                        notice.get(
                            "editeur"
                        ),

                    "sujet":
                        notice.get(
                            "sujet"
                        ),

                    "raisons":
                        raisons_txt
                })

    if not lignes:

        return pd.DataFrame(
            columns=
                colonnes_finales
        )

    df = pd.DataFrame(
        lignes
    )[
        colonnes_finales
    ]

    df = (
        df
        .drop_duplicates()
    )

    df[
        "ppn"
    ] = (
        df[
            "ppn"
        ]
        .astype(
            "string"
        )
    )

    df[
        "isbn_normalise"
    ] = (
        df[
            "isbn_normalise"
        ]
        .astype(
            "string"
        )
    )

    df[
        "annee"
    ] = (
        pd.to_numeric(
            df[
                "annee"
            ],
            errors="coerce"
        )
        .astype(
            "Int64"
        )
    )

    df = (
        df
        .sort_values(
            [
                "ppn",
                "isbn_normalise",
                "titre"
            ],
            na_position="last"
        )
        .reset_index(
            drop=True
        )
    )

    return df


# ============================================================
# FONCTION PUBLIQUE UTILISÉE PAR pipeline.py
# ============================================================

def recherche_SUDOC():

    debut_global = (
        time.perf_counter()
    )

    print()
    print("#" * 80)
    print(
        "SUDOC GENEVENSIA"
    )
    print("#" * 80)

    notices_sudoc = (
        rechercher_geneve_sudoc()
    )

    liste_ppn_idref = (
        extraire_liste_ppn_idref(
            notices_sudoc
        )
    )

    (
        ppn_ceb,
        ppn_pica_absents,
        erreurs_pica,
        duree_pica
    ) = (
        recuperer_ppn_ceb(
            list(
                notices_sudoc.keys()
            )
        )
    )

    df_isbn_ceb_exclus = (
        construire_isbn_ceb_exclus(
            notices_sudoc,
            ppn_ceb
        )
    )

    df_sudoc_geneve = (
        notices_vers_dataframe(
            notices_sudoc,
            ppn_ceb=
                ppn_ceb
        )
    )

    if df_isbn_ceb_exclus.empty:

        nombre_isbn_ceb_uniques = 0
        nombre_lignes_ceb = 0

    else:

        nombre_lignes_ceb = (
            len(
                df_isbn_ceb_exclus
            )
        )

        nombre_isbn_ceb_uniques = (
            df_isbn_ceb_exclus[
                "isbn_normalise"
            ]
            .dropna()
            .nunique()
        )

    nombre_isbn_conserves = 0

    if (
        "isbn_normalise"
        in df_sudoc_geneve.columns
    ):

        nombre_isbn_conserves = (
            df_sudoc_geneve[
                "isbn_normalise"
            ]
            .dropna()
            .nunique()
        )

    duree_totale = (
        time.perf_counter()
        -
        debut_global
    )

    print()
    print("=" * 80)
    print(
        "RÉSULTATS SUDOC GENEVENSIA"
    )
    print("=" * 80)

    print(
        f"Notices utiles avant CEB      : "
        f"{len(notices_sudoc):,}"
    )

    print(
        f"PPN IdRef auteurs conservés   : "
        f"{len(liste_ppn_idref):,}"
    )

    print(
        f"Notices CEB détectées         : "
        f"{len(ppn_ceb):,}"
    )

    print(
        f"ISBN exclus - lignes          : "
        f"{nombre_lignes_ceb:,}"
    )

    print(
        f"ISBN CEB uniques exclus       : "
        f"{nombre_isbn_ceb_uniques:,}"
    )

    print(
        f"ISBN uniques conservés        : "
        f"{nombre_isbn_conserves:,}"
    )

    print(
        f"Lignes finales Genevensia     : "
        f"{len(df_sudoc_geneve):,}"
    )

    print(
        f"PPN PICA absents              : "
        f"{len(ppn_pica_absents):,}"
    )

    print(
        f"Erreurs PICA                  : "
        f"{len(erreurs_pica):,}"
    )

    print()

    print(
        f"Durée contrôle PICA           : "
        f"{duree_pica:.1f} s"
    )

    print(
        f"Durée totale SUDOC Genevensia : "
        f"{duree_totale:.1f} s "
        f"({duree_totale / 60:.2f} min)"
    )

    print("=" * 80)

    df_sudoc_geneve.attrs[
        "ppn_ceb"
    ] = sorted(
        ppn_ceb
    )

    df_sudoc_geneve.attrs[
        "ppn_pica_absents"
    ] = sorted(
        ppn_pica_absents
    )

    df_sudoc_geneve.attrs[
        "erreurs_pica"
    ] = erreurs_pica

    df_sudoc_geneve.attrs[
        "nombre_isbn_ceb_exclus"
    ] = nombre_isbn_ceb_uniques

    df_sudoc_geneve.attrs[
        "isbn_ceb_exclus"
    ] = (
        df_isbn_ceb_exclus
        .to_dict(
            orient="records"
        )
    )

    df_sudoc_geneve.attrs[
        "liste_ppn_idref"
    ] = liste_ppn_idref

    df_sudoc_geneve.attrs[
        "duree_pica"
    ] = duree_pica

    df_sudoc_geneve.attrs[
        "duree_totale"
    ] = duree_totale

    return df_sudoc_geneve