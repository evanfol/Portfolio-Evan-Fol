import requests
import pandas as pd
import re
import time
import threading
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

SPARQL_URL = "https://sparql.dnb.de/api/dnbgnd"
IDREF_URL = "https://www.idref.fr/Sru/Solr"
DNB_SRU_URL = "https://services.dnb.de/sru/dnb"

TIMEOUT = 90
MAX_RETRIES = 4

MAX_WORKERS_SEARCH = 22
MAX_WORKERS_METADATA = 4
MAX_WORKERS_IDREF = 2
MAX_WORKERS_MARC = 26

# Valeurs retenues après benchmarks réels DNB :
# recherche=22 workers ; MARC=26 workers / lots de 100 ; metadata=lots de 2500.
METADATA_BATCH_SIZE = 2500
MARC_BATCH_SIZE = 100
IDREF_BATCH_SIZE = 10
IDREF_CONNECT_TIMEOUT = 5
IDREF_READ_TIMEOUT = 12
IDREF_FINAL_RETRIES = 2


# ============================================================
# COMMUNES / VARIANTES
# ============================================================

def charger_communes():
    """
    Charge automatiquement les communes depuis :

        Liste_communes/liste commune geneve.txt

    Formats acceptés dans le fichier :
        "Genf",
        "Genève",
        "Geneve",

    ou :
        Genf
        Genève
        Geneve

    Les lignes vides sont ignorées et les doublons exacts sont supprimés
    en conservant l'ordre d'origine.
    """
    
    # Compatible fichier .py ET notebook/Colab.
    if "__file__" in globals():
        dossier_base = Path(__file__).resolve().parent
    else:
        dossier_base = Path.cwd()

    # On teste les emplacements les plus probables.
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

            # Accepte :
            # "Genève",
            # "Genève"
            # Genève
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

    # Déduplication sans modifier l'ordre.
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
        respect_retry_after_header=True
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=20,
        pool_maxsize=20
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
        "Accept":
            "application/sparql-results+json",

        "User-Agent":
            "Geneve-Bibliographic-Research/1.0"
    })

    return session


def get_session():

    if not hasattr(
        thread_local,
        "session"
    ):
        thread_local.session = creer_session()

    return thread_local.session


# ============================================================
# UTILITAIRES
# ============================================================

def valeur(binding, nom):

    element = binding.get(nom)

    if not element:
        return None

    return element.get("value")


def concat_unique(valeurs):

    valeurs = {
        str(v).strip()
        for v in valeurs
        if (
            v is not None
            and str(v).strip()
        )
    }

    return " | ".join(
        sorted(valeurs)
    )


def creer_lots(
    valeurs,
    taille
):

    valeurs = list(valeurs)

    for i in range(
        0,
        len(valeurs),
        taille
    ):

        yield valeurs[
            i:i + taille
        ]


def echapper_litteral(texte):

    return (
        str(texte)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )


# ============================================================
# NORMALISATION TEXTE
# ============================================================

def normaliser_texte(texte):

    if texte is None:
        return ""

    texte = str(
        texte
    ).strip()

    texte = (
        texte
        .replace("œ", "oe")
        .replace("Œ", "OE")
        .replace("æ", "ae")
        .replace("Æ", "AE")
    )

    texte = "".join(
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

    return (
        texte
        .strip()
        .lower()
    )


# ============================================================
# ISBN
#
# ISBN-13 -> valide -> conservé
# ISBN-10 -> valide -> converti ISBN-13
# ============================================================

def normaliser_isbn_vers_13(isbn):

    if isbn is None:
        return None

    isbn = re.sub(
        r"[^0-9Xx]",
        "",
        str(isbn)
    ).upper()


    # ========================================================
    # ISBN-13
    # ========================================================

    if len(isbn) == 13:

        if not isbn.isdigit():
            return None

        total = sum(
            int(chiffre)
            *
            (
                1
                if i % 2 == 0
                else 3
            )
            for i, chiffre
            in enumerate(
                isbn[:12]
            )
        )

        cle = (
            10
            -
            (total % 10)
        ) % 10

        if cle != int(
            isbn[12]
        ):
            return None

        return isbn


    # ========================================================
    # ISBN-10
    # ========================================================

    if len(isbn) == 10:

        if not isbn[:9].isdigit():
            return None

        if not (
            isbn[9].isdigit()
            or isbn[9] == "X"
        ):
            return None


        # ----------------------------------------------------
        # validation ISBN-10
        # ----------------------------------------------------

        total = 0

        for i, caractere in enumerate(
            isbn
        ):

            valeur_chiffre = (
                10
                if caractere == "X"
                else int(caractere)
            )

            total += (
                (10 - i)
                *
                valeur_chiffre
            )

        if total % 11 != 0:
            return None


        # ----------------------------------------------------
        # ISBN10 -> ISBN13
        # ----------------------------------------------------

        base = (
            "978"
            +
            isbn[:9]
        )

        total = sum(
            int(chiffre)
            *
            (
                1
                if i % 2 == 0
                else 3
            )
            for i, chiffre
            in enumerate(base)
        )

        cle = (
            10
            -
            (total % 10)
        ) % 10

        return (
            base
            +
            str(cle)
        )


    return None


# ============================================================
# REQUETE SPARQL
# ============================================================

def requete_sparql(
    query,
    method="POST"
):

    session = get_session()

    if method == "GET":

        response = session.get(
            SPARQL_URL,
            params={
                "query": query
            },
            timeout=TIMEOUT
        )

    else:

        response = session.post(
            SPARQL_URL,
            data={
                "query": query
            },
            timeout=TIMEOUT
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


# ============================================================
# RECHERCHE ISBN-13
# ============================================================

def rechercher_lieu_isbn13(
    lieu
):

    lieu_sparql = echapper_litteral(
        lieu
    )

    query = f"""
PREFIX rdau: <http://rdaregistry.info/Elements/u/>
PREFIX bibo: <http://purl.org/ontology/bibo/>

SELECT
    ?document
    ?isbn

WHERE {{

    ?document
        rdau:P60163
        "{lieu_sparql}" .

    ?document
        bibo:isbn13
        ?isbn .
}}
"""

    debut = time.perf_counter()

    bindings = requete_sparql(
        query,
        method="GET"
    )

    duree = (
        time.perf_counter()
        -
        debut
    )

    resultats = []

    for binding in bindings:

        document = valeur(
            binding,
            "document"
        )

        isbn = (
            normaliser_isbn_vers_13(
                valeur(
                    binding,
                    "isbn"
                )
            )
        )

        if (
            document
            and isbn
        ):

            resultats.append(
                (
                    document,
                    isbn
                )
            )

    return {
        "lieu": lieu,
        "type": "ISBN13",
        "resultats": resultats,
        "temps": duree
    }


# ============================================================
# RECHERCHE ISBN-10
# ============================================================

def rechercher_lieu_isbn10(
    lieu
):

    lieu_sparql = echapper_litteral(
        lieu
    )

    query = f"""
PREFIX rdau: <http://rdaregistry.info/Elements/u/>
PREFIX bibo: <http://purl.org/ontology/bibo/>

SELECT
    ?document
    ?isbn

WHERE {{

    ?document
        rdau:P60163
        "{lieu_sparql}" .

    ?document
        bibo:isbn10
        ?isbn .
}}
"""

    debut = time.perf_counter()

    bindings = requete_sparql(
        query,
        method="GET"
    )

    duree = (
        time.perf_counter()
        -
        debut
    )

    resultats = []

    for binding in bindings:

        document = valeur(
            binding,
            "document"
        )

        isbn = (
            normaliser_isbn_vers_13(
                valeur(
                    binding,
                    "isbn"
                )
            )
        )

        if (
            document
            and isbn
        ):

            resultats.append(
                (
                    document,
                    isbn
                )
            )

    return {
        "lieu": lieu,
        "type": "ISBN10",
        "resultats": resultats,
        "temps": duree
    }


# ============================================================
# RECHERCHE DNB
# ============================================================

def rechercher_documents_dnb():

    print()
    print("==========================================")
    print("DNB - RECHERCHE ISBN")
    print("==========================================")

    print(
        f"Termes : {len(COMMUNES_GENEVE)}"
    )

    print(
        f"Workers : {MAX_WORKERS_SEARCH}"
    )

    print(
        "ISBN recherchés : ISBN-10 + ISBN-13"
    )

    print(
        "Sortie : ISBN-13 uniquement"
    )

    print()

    debut_global = time.perf_counter()

    documents = {}

    taches = []

    for lieu in COMMUNES_GENEVE:

        taches.append(
            (
                rechercher_lieu_isbn13,
                lieu
            )
        )

        taches.append(
            (
                rechercher_lieu_isbn10,
                lieu
            )
        )


    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS_SEARCH
    ) as executor:

        futures = {

            executor.submit(
                fonction,
                lieu
            ):
            lieu

            for fonction, lieu
            in taches
        }


        for future in as_completed(
            futures
        ):

            lieu = futures[
                future
            ]

            try:

                resultat = (
                    future.result()
                )

                lignes = resultat[
                    "resultats"
                ]

                type_isbn = resultat[
                    "type"
                ]

                for (
                    document,
                    isbn
                ) in lignes:

                    if document not in documents:

                        documents[
                            document
                        ] = {
                            "isbn": set(),
                            "raisons": set()
                        }

                    documents[
                        document
                    ][
                        "isbn"
                    ].add(
                        isbn
                    )

                    documents[
                        document
                    ][
                        "raisons"
                    ].add(
                        "lieu_publication"
                    )


                print(
                    f"✓ "
                    f"{lieu:<18}"
                    f" {type_isbn:<6}"
                    f" : "
                    f"{len(lignes):>6,}"
                    f" | "
                    f"{resultat['temps']:.2f}s"
                )


            except Exception as erreur:

                print(
                    f"✗ "
                    f"{lieu:<18}"
                    f" : "
                    f"{type(erreur).__name__}"
                    f" - "
                    f"{erreur}"
                )


    duree = (
        time.perf_counter()
        -
        debut_global
    )


    nombre_isbn = sum(
        len(
            infos["isbn"]
        )
        for infos
        in documents.values()
    )


    print()

    print(
        "Documents uniques :",
        f"{len(documents):,}"
    )

    print(
        "ISBN-13 normalisés :",
        f"{nombre_isbn:,}"
    )

    print(
        "Temps recherche :",
        f"{duree:.2f}s"
    )


    return documents


# ============================================================
# FILTRE RESSOURCES NUMERIQUES — MARC 338 $b = cr
#
# Règle :
#   338 $b = cr  -> ressource numérique -> notice exclue
#
# Le contrôle se fait une seule fois par document DNB unique,
# par lots SRU et en parallèle. En cas d'échec définitif sur une
# notice, elle est conservée par sécurité afin de ne pas perdre
# un ISBN physique faute de réponse du serveur.
# ============================================================

MARC_NS = "http://www.loc.gov/MARC21/slim"
DNB_ID_RE = re.compile(
    r"https?://d-nb\.info/([^/?#]+)",
    re.I
)


def extraire_idn_dnb(document):

    if not document:
        return None

    match = DNB_ID_RE.search(
        str(document)
    )

    if not match:
        return None

    return (
        match
        .group(1)
        .strip()
    ) or None


def construire_requete_sru_idn(
    liste_idn
):

    return " OR ".join(
        f'dnb.idn="{idn}"'
        for idn in liste_idn
    )


def parser_numerique_marcxml(
    xml_bytes,
    idn_attendus
):
    """
    Retourne {idn: bool}.

    True uniquement si au moins un champ 338 contient
    exactement un sous-champ $b = cr.
    """

    root = ET.fromstring(
        xml_bytes
    )

    resultats = {}

    for record in root.findall(
        f".//{{{MARC_NS}}}record"
    ):

        idn = None

        for controlfield in record.findall(
            f"{{{MARC_NS}}}controlfield"
        ):

            if controlfield.get("tag") == "001":

                idn = (
                    controlfield.text
                    or ""
                ).strip()

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

                valeur_338b = (
                    subfield.text
                    or ""
                ).strip().lower()

                if valeur_338b == "cr":
                    numerique = True
                    break

            if numerique:
                break

        resultats[idn] = numerique

    return {
        idn: resultats[idn]
        for idn in idn_attendus
        if idn in resultats
    }


def executer_requete_marc_dnb(
    liste_idn
):

    session = get_session()

    response = session.get(
        DNB_SRU_URL,
        params={
            "version": "1.1",
            "operation": "searchRetrieve",
            "query": construire_requete_sru_idn(
                liste_idn
            ),
            "recordSchema": "MARC21-xml",
            "maximumRecords": len(
                liste_idn
            )
        },
        headers={
            "Accept":
                "application/xml, text/xml;q=0.9, */*;q=0.1"
        },
        timeout=TIMEOUT
    )

    response.raise_for_status()

    return parser_numerique_marcxml(
        response.content,
        liste_idn
    )


def verifier_lot_marc_robuste(
    liste_idn
):
    """
    Si un lot échoue ou si certaines notices ne sont pas renvoyées,
    le lot est divisé récursivement jusqu'à isoler l'IDN concerné.
    """

    try:

        resultats = (
            executer_requete_marc_dnb(
                liste_idn
            )
        )

        manquants = [
            idn
            for idn in liste_idn
            if idn not in resultats
        ]

        if not manquants:
            return resultats, []

        if len(liste_idn) <= 1:

            return (
                resultats,
                [{
                    "idn_dnb":
                        liste_idn[0],
                    "erreur":
                        "Notice MARC absente de la réponse SRU"
                }]
            )

    except Exception as erreur:

        if len(liste_idn) <= 1:

            return (
                {},
                [{
                    "idn_dnb":
                        liste_idn[0],
                    "erreur":
                        (
                            f"{type(erreur).__name__}: "
                            f"{erreur}"
                        )
                }]
            )

    milieu = (
        len(liste_idn)
        // 2
    )

    gauche, erreurs_gauche = (
        verifier_lot_marc_robuste(
            liste_idn[:milieu]
        )
    )

    droite, erreurs_droite = (
        verifier_lot_marc_robuste(
            liste_idn[milieu:]
        )
    )

    gauche.update(
        droite
    )

    return (
        gauche,
        erreurs_gauche
        + erreurs_droite
    )


def filtrer_documents_numeriques(
    documents,
    batch_size=MARC_BATCH_SIZE,
    max_workers=MAX_WORKERS_MARC
):
    """
    Supprime de `documents` toutes les notices DNB dont 338 $b = cr.

    Retour :
        documents_filtres,
        erreurs_marc,
        nb_documents_numeriques,
        duree
    """

    debut = time.perf_counter()

    if not documents:
        return documents, [], 0, 0.0

    document_par_idn = {}

    for document in documents.keys():

        idn = extraire_idn_dnb(
            document
        )

        if idn:

            document_par_idn[
                idn
            ] = document

    liste_idn = list(
        document_par_idn.keys()
    )

    lots = list(
        creer_lots(
            liste_idn,
            batch_size
        )
    )

    print()
    print("==========================================")
    print("DNB - FILTRE NUMERIQUE 338 $b = cr")
    print("==========================================")

    print(
        "Documents à contrôler :",
        f"{len(liste_idn):,}"
    )

    print(
        "Taille lot SRU :",
        batch_size
    )

    print(
        "Nombre lots :",
        len(lots)
    )

    print(
        "Workers :",
        max_workers
    )

    print()

    statut_numerique = {}
    erreurs = []
    termines = 0

    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:

        futures = {

            executor.submit(
                verifier_lot_marc_robuste,
                lot
            ):
            numero

            for numero, lot
            in enumerate(
                lots,
                start=1
            )
        }

        for future in as_completed(
            futures
        ):

            numero = futures[
                future
            ]

            try:

                resultats_lot, erreurs_lot = (
                    future.result()
                )

                statut_numerique.update(
                    resultats_lot
                )

                erreurs.extend(
                    erreurs_lot
                )

            except Exception as erreur:

                erreurs.append({
                    "lot": numero,
                    "erreur": (
                        f"{type(erreur).__name__}: "
                        f"{erreur}"
                    )
                })

            termines += 1

            if (
                termines == 1
                or termines == len(lots)
                or termines % 10 == 0
            ):

                print(
                    f"\rLots terminés : "
                    f"{termines}/"
                    f"{len(lots)}"
                    f" | IDN contrôlés : "
                    f"{len(statut_numerique):,}"
                    f" | erreurs : "
                    f"{len(erreurs):,}",
                    end=""
                )

    print()

    idn_numeriques = {
        idn
        for idn, numerique
        in statut_numerique.items()
        if numerique
    }

    documents_numeriques = {
        document_par_idn[idn]
        for idn in idn_numeriques
        if idn in document_par_idn
    }

    documents_filtres = {
        document: infos
        for document, infos
        in documents.items()
        if document not in documents_numeriques
    }

    duree = (
        time.perf_counter()
        - debut
    )

    print(
        "Notices numériques exclues :",
        f"{len(documents_numeriques):,}"
    )

    print(
        "Documents conservés :",
        f"{len(documents_filtres):,}"
    )

    print(
        "Erreurs MARC définitives :",
        f"{len(erreurs):,}"
    )

    print(
        "Temps filtre MARC :",
        f"{duree:.2f}s"
    )

    print("==========================================")

    return (
        documents_filtres,
        erreurs,
        len(documents_numeriques),
        duree
    )


# ============================================================
# METADONNEES SIMPLES
# ============================================================

def recuperer_metadata_lot(
    documents
):

    values = "\n".join(
        f"<{document}>"
        for document in documents
    )

    query = f"""
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX rdau: <http://rdaregistry.info/Elements/u/>

SELECT
    ?document
    ?type
    ?valeur

WHERE {{

    VALUES ?document {{
        {values}
    }}

    {{
        ?document
            dc:title
            ?valeur .

        BIND(
            "titre"
            AS ?type
        )
    }}

    UNION

    {{
        ?document
            dcterms:issued
            ?valeur .

        BIND(
            "annee"
            AS ?type
        )
    }}

    UNION

    {{
        ?document
            rdau:P60163
            ?valeur .

        BIND(
            "lieuPublication"
            AS ?type
        )
    }}

    UNION

    {{
        ?document
            dc:publisher
            ?valeur .

        BIND(
            "editeur"
            AS ?type
        )
    }}
}}
"""

    bindings = requete_sparql(
        query,
        method="POST"
    )

    resultats = {}

    for binding in bindings:

        document = valeur(
            binding,
            "document"
        )

        type_metadata = valeur(
            binding,
            "type"
        )

        valeur_metadata = valeur(
            binding,
            "valeur"
        )

        if not document:
            continue


        if document not in resultats:

            resultats[
                document
            ] = {
                "titre": set(),
                "annee": set(),
                "nomAuteur": set(),
                "lieuPublication": set(),
                "editeur": set(),
                "sujet": set()
            }


        if (
            type_metadata
            in resultats[
                document
            ]
            and valeur_metadata
        ):

            resultats[
                document
            ][
                type_metadata
            ].add(
                valeur_metadata.strip()
            )


    return resultats


# ============================================================
# AUTEURS
# ============================================================

def recuperer_auteurs_lot(
    documents
):

    values = "\n".join(
        f"<{document}>"
        for document in documents
    )

    query = f"""
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX rel: <http://id.loc.gov/vocabulary/relators/>
PREFIX gndo: <https://d-nb.info/standards/elementset/gnd#>

SELECT
    ?document
    ?nomAuteur

WHERE {{

    VALUES ?document {{
        {values}
    }}

    VALUES ?relation {{
        dcterms:creator
        rel:aut
    }}

    ?document
        ?relation
        ?agent .

    ?agent
        gndo:preferredName
        ?nomAuteur .

    FILTER(
        isLiteral(
            ?nomAuteur
        )
    )
}}
"""

    bindings = requete_sparql(
        query,
        method="POST"
    )

    resultats = {}

    for binding in bindings:

        document = valeur(
            binding,
            "document"
        )

        auteur = valeur(
            binding,
            "nomAuteur"
        )

        if (
            document
            and auteur
        ):

            resultats.setdefault(
                document,
                set()
            ).add(
                auteur.strip()
            )

    return resultats


# ============================================================
# SUJETS GND CORRIGES
# ============================================================

def recuperer_sujets_lot(
    documents
):

    values = "\n".join(
        f"<{document}>"
        for document in documents
    )

    query = f"""
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX gndo: <https://d-nb.info/standards/elementset/gnd#>

SELECT
    ?document
    ?sujet

WHERE {{

    VALUES ?document {{
        {values}
    }}

    ?document
        dcterms:subject
        ?sujetURI .

    FILTER(
        STRSTARTS(
            STR(?sujetURI),
            "https://d-nb.info/gnd/"
        )
    )

    VALUES ?predicatNom {{

        gndo:preferredNameForTheSubjectHeading
        gndo:preferredNameForThePlaceOrGeographicName
        gndo:preferredNameForTheCorporateBody
        gndo:preferredNameForThePerson
        gndo:preferredNameForTheConferenceOrEvent
        gndo:preferredNameForTheWork
        gndo:preferredName
    }}

    ?sujetURI
        ?predicatNom
        ?sujet .

    FILTER(
        isLiteral(
            ?sujet
        )
    )
}}
"""

    bindings = requete_sparql(
        query,
        method="POST"
    )

    resultats = {}

    for binding in bindings:

        document = valeur(
            binding,
            "document"
        )

        sujet = valeur(
            binding,
            "sujet"
        )

        if (
            document
            and sujet
        ):

            resultats.setdefault(
                document,
                set()
            ).add(
                sujet.strip()
            )

    return resultats


# ============================================================
# TRAITEMENT LOT
# ============================================================

def traiter_lot_metadata(
    documents
):

    metadata = recuperer_metadata_lot(
        documents
    )

    auteurs = recuperer_auteurs_lot(
        documents
    )

    sujets = recuperer_sujets_lot(
        documents
    )


    for document in documents:

        if document not in metadata:

            metadata[
                document
            ] = {
                "titre": set(),
                "annee": set(),
                "nomAuteur": set(),
                "lieuPublication": set(),
                "editeur": set(),
                "sujet": set()
            }


        metadata[
            document
        ][
            "nomAuteur"
        ].update(
            auteurs.get(
                document,
                set()
            )
        )


        metadata[
            document
        ][
            "sujet"
        ].update(
            sujets.get(
                document,
                set()
            )
        )


    return metadata


# ============================================================
# RECUPERATION METADONNEES
# ============================================================

def recuperer_metadonnees(
    documents
):

    liste_documents = list(
        documents.keys()
    )

    lots = list(
        creer_lots(
            liste_documents,
            METADATA_BATCH_SIZE
        )
    )


    print()
    print("==========================================")
    print("DNB - METADONNEES")
    print("==========================================")

    print(
        "Documents :",
        f"{len(liste_documents):,}"
    )

    print(
        "Taille lot :",
        METADATA_BATCH_SIZE
    )

    print(
        "Nombre lots :",
        len(lots)
    )

    print(
        "Workers :",
        MAX_WORKERS_METADATA
    )

    print()


    debut = time.perf_counter()

    metadata_final = {}

    termines = 0
    erreurs = 0


    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS_METADATA
    ) as executor:

        futures = {

            executor.submit(
                traiter_lot_metadata,
                lot
            ):
            numero

            for numero, lot
            in enumerate(
                lots,
                start=1
            )
        }


        for future in as_completed(
            futures
        ):

            numero = futures[
                future
            ]

            try:

                resultat = (
                    future.result()
                )

                metadata_final.update(
                    resultat
                )

            except Exception as erreur:

                erreurs += 1

                print()

                print(
                    f"✗ Lot {numero} : "
                    f"{type(erreur).__name__}"
                    f" - "
                    f"{erreur}"
                )


            termines += 1

            print(
                f"\rLots terminés : "
                f"{termines}/"
                f"{len(lots)}"
                f" | erreurs : "
                f"{erreurs}",
                end=""
            )


    print()


    duree = (
        time.perf_counter()
        -
        debut
    )

    print(
        "Temps métadonnées :",
        f"{duree:.2f}s"
    )


    return metadata_final


# ============================================================
# NOM / PRENOM
# ============================================================

def separer_nom_prenom(
    nom_complet
):

    if not nom_complet:
        return None, None

    texte = str(
        nom_complet
    ).strip()

    if "," not in texte:
        return None, None

    parties = texte.split(
        ",",
        1
    )

    nom = (
        parties[0]
        .strip()
    )

    prenom = (
        parties[1]
        .strip()
    )


    if "," in prenom:

        prenom = (
            prenom
            .split(
                ",",
                1
            )[0]
            .strip()
        )


    if not nom or not prenom:
        return None, None

    return nom, prenom


# ============================================================
# IDREF — SESSION HTTP DEDIEE PAR THREAD
#
# Optimisation validée par benchmark :
#   - lots de 10 auteurs
#   - 2 workers
#   - pas de retry automatique urllib3 (évite les blocages longs)
#   - fallback récursif si un lot échoue
#   - retry final uniquement pour un auteur isolé
#
# IMPORTANT :
# une erreur réseau n'est jamais interprétée comme "auteur absent".
# ============================================================

thread_local_idref = threading.local()


def creer_session_idref():

    session = requests.Session()

    retry = Retry(
        total=0,
        connect=0,
        read=0,
        status=0,
        redirect=0,
        allowed_methods=["GET"],
        raise_on_status=False
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=4
    )

    session.mount(
        "https://",
        adapter
    )

    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "Geneve-Bibliographic-Research/1.0"
    })

    return session


def get_session_idref():

    if not hasattr(
        thread_local_idref,
        "session"
    ):
        thread_local_idref.session = creer_session_idref()

    return thread_local_idref.session


# ============================================================
# IDREF — ECHAPPEMENT SOLR/LUCENE
# ============================================================

def echapper_solr(texte):

    return re.sub(
        r'([+\-!(){}\[\]^"~*?:\\/])',
        r'\\\1',
        str(texte)
    )


# ============================================================
# IDREF — CONSTRUCTION D'UNE REQUETE GROUPEE
# ============================================================

def construire_requete_idref(auteurs):

    conditions = []

    for auteur in auteurs:

        nom, prenom = separer_nom_prenom(
            auteur
        )

        if not nom or not prenom:
            continue

        nom = echapper_solr(
            nom
        )

        prenom = echapper_solr(
            prenom
        )

        conditions.append(
            "("
            f'nom_t:"{nom}" '
            "AND "
            f'prenom_t:"{prenom}"'
            ")"
        )

    if not conditions:
        return None

    return (
        "("
        + " OR ".join(conditions)
        + ") AND recordtype_z:a"
    )


# ============================================================
# IDREF — EXECUTION D'UN LOT
# ============================================================

def requete_lot_idref(auteurs):

    query = construire_requete_idref(
        auteurs
    )

    if query is None:

        return {
            auteur: {
                "statut": "INVALID",
                "ppn": ""
            }
            for auteur in auteurs
        }

    session = get_session_idref()

    params = {
        "wt": "json",
        "rows": max(
            10,
            len(auteurs) * 10
        ),
        "fl": (
            "ppn_z,"
            "recordtype_z,"
            "nom_s,"
            "prenom_s,"
            "bestnom_s,"
            "bestprenom_s"
        ),
        "q": query
    }

    response = session.get(
        IDREF_URL,
        params=params,
        timeout=(
            IDREF_CONNECT_TIMEOUT,
            IDREF_READ_TIMEOUT
        )
    )

    response.raise_for_status()

    docs = (
        response
        .json()
        .get(
            "response",
            {}
        )
        .get(
            "docs",
            []
        )
    )

    # --------------------------------------------------------
    # Index exact des auteurs demandés
    # --------------------------------------------------------

    index_auteurs = {}

    for auteur in auteurs:

        nom, prenom = separer_nom_prenom(
            auteur
        )

        if not nom or not prenom:
            continue

        cle = (
            normaliser_texte(nom),
            normaliser_texte(prenom)
        )

        index_auteurs.setdefault(
            cle,
            []
        ).append(
            auteur
        )

    candidats = {
        auteur: set()
        for auteur in auteurs
    }

    # --------------------------------------------------------
    # Association des résultats Solr aux auteurs du lot
    # --------------------------------------------------------

    for doc in docs:

        ppn = doc.get(
            "ppn_z"
        )

        if not ppn:
            continue

        noms = (
            doc.get(
                "bestnom_s"
            )
            or
            doc.get(
                "nom_s"
            )
            or
            []
        )

        prenoms = (
            doc.get(
                "bestprenom_s"
            )
            or
            doc.get(
                "prenom_s"
            )
            or
            []
        )

        if isinstance(
            noms,
            str
        ):
            noms = [noms]

        if isinstance(
            prenoms,
            str
        ):
            prenoms = [prenoms]

        noms_normalises = {
            normaliser_texte(x)
            for x in noms
        }

        prenoms_normalises = {
            normaliser_texte(x)
            for x in prenoms
        }

        for (
            nom_normalise,
            prenom_normalise
        ), auteurs_correspondants in index_auteurs.items():

            if (
                nom_normalise in noms_normalises
                and
                prenom_normalise in prenoms_normalises
            ):

                for auteur in auteurs_correspondants:

                    candidats[
                        auteur
                    ].add(
                        str(ppn).strip()
                    )

    # --------------------------------------------------------
    # Même règle métier que l'ancien code :
    # exactement un PPN => trouvé ; sinon => pas de PPN retenu.
    # --------------------------------------------------------

    resultats = {}

    for auteur in auteurs:

        ppns = sorted(
            candidats.get(
                auteur,
                set()
            )
        )

        if len(ppns) == 1:

            resultats[
                auteur
            ] = {
                "statut": "FOUND",
                "ppn": ppns[0]
            }

        else:

            resultats[
                auteur
            ] = {
                "statut": "NOT_FOUND",
                "ppn": ""
            }

    return resultats


# ============================================================
# IDREF — FALLBACK ROBUSTE
#
# Si un lot de 10 échoue : 5+5, puis subdivision récursive.
# Pour un auteur isolé, on retente quelques fois avant de
# déclarer une erreur finale. Une erreur finale reste distincte
# d'un vrai NOT_FOUND.
# ============================================================

def requete_lot_idref_robuste(
    auteurs,
    stats
):

    stats[
        "requetes_http"
    ] += 1

    try:

        return requete_lot_idref(
            auteurs
        )

    except Exception as erreur:

        stats[
            "lots_echoues"
        ] += 1

        # ----------------------------------------------------
        # Dernier niveau : un seul auteur
        # ----------------------------------------------------

        if len(auteurs) == 1:

            auteur = auteurs[0]

            # Le premier essai vient déjà d'échouer.
            # On ajoute seulement quelques tentatives ciblées.
            for tentative in range(
                IDREF_FINAL_RETRIES
            ):

                stats[
                    "requetes_http"
                ] += 1

                try:

                    # Petit délai uniquement sur une erreur finale,
                    # jamais sur le chemin normal.
                    if tentative > 0:
                        time.sleep(0.20)

                    return requete_lot_idref(
                        auteurs
                    )

                except Exception:
                    continue

            stats[
                "erreurs_finales"
            ] += 1

            stats[
                "auteurs_en_erreur"
            ].append(
                {
                    "auteur": auteur,
                    "erreur": (
                        f"{type(erreur).__name__}: "
                        f"{erreur}"
                    )
                }
            )

            return {
                auteur: {
                    "statut": "ERROR",
                    "ppn": ""
                }
            }

        # ----------------------------------------------------
        # Division récursive du lot
        # ----------------------------------------------------

        stats[
            "divisions"
        ] += 1

        milieu = (
            len(auteurs)
            // 2
        )

        gauche = auteurs[
            :milieu
        ]

        droite = auteurs[
            milieu:
        ]

        resultats_gauche = (
            requete_lot_idref_robuste(
                gauche,
                stats
            )
        )

        resultats_droite = (
            requete_lot_idref_robuste(
                droite,
                stats
            )
        )

        resultats_gauche.update(
            resultats_droite
        )

        return resultats_gauche


# ============================================================
# RESOLUTION IDREF GLOBALE
# ============================================================

def recuperer_ppn_idref(
    metadata
):

    print()
    print("==========================================")
    print("IDREF - RESOLUTION DES AUTEURS")
    print("==========================================")
    print()

    auteurs_uniques = set()

    for meta in metadata.values():

        auteurs_uniques.update(
            meta.get(
                "nomAuteur",
                set()
            )
        )

    auteurs_uniques = sorted(
        auteur
        for auteur in auteurs_uniques
        if auteur
    )

    lots = list(
        creer_lots(
            auteurs_uniques,
            IDREF_BATCH_SIZE
        )
    )

    print(
        "Auteurs uniques :",
        f"{len(auteurs_uniques):,}"
    )

    print(
        "Taille lot IdRef :",
        IDREF_BATCH_SIZE
    )

    print(
        "Lots initiaux :",
        len(lots)
    )

    print(
        "Workers IdRef :",
        MAX_WORKERS_IDREF
    )

    print()

    debut = time.perf_counter()

    ppn_par_auteur = {}

    statuts_par_auteur = {}

    stats_globales = {
        "requetes_http": 0,
        "lots_echoues": 0,
        "divisions": 0,
        "erreurs_finales": 0,
        "auteurs_en_erreur": []
    }

    lots_termines = 0

    def traiter_lot(lot):

        stats_locales = {
            "requetes_http": 0,
            "lots_echoues": 0,
            "divisions": 0,
            "erreurs_finales": 0,
            "auteurs_en_erreur": []
        }

        resultat = (
            requete_lot_idref_robuste(
                lot,
                stats_locales
            )
        )

        return (
            resultat,
            stats_locales
        )

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS_IDREF
    ) as executor:

        futures = [
            executor.submit(
                traiter_lot,
                lot
            )
            for lot in lots
        ]

        for future in as_completed(
            futures
        ):

            (
                resultat_lot,
                stats_lot
            ) = future.result()

            for auteur, resultat in resultat_lot.items():

                statut = resultat[
                    "statut"
                ]

                ppn_par_auteur[
                    auteur
                ] = resultat[
                    "ppn"
                ]

                statuts_par_auteur[
                    auteur
                ] = statut

            for cle in (
                "requetes_http",
                "lots_echoues",
                "divisions",
                "erreurs_finales"
            ):

                stats_globales[
                    cle
                ] += stats_lot[
                    cle
                ]

            stats_globales[
                "auteurs_en_erreur"
            ].extend(
                stats_lot[
                    "auteurs_en_erreur"
                ]
            )

            lots_termines += 1

            if (
                lots_termines == 1
                or
                lots_termines == len(lots)
                or
                lots_termines % 10 == 0
            ):

                trouves_temp = sum(
                    statut == "FOUND"
                    for statut in statuts_par_auteur.values()
                )

                print(
                    f"\rLots terminés : "
                    f"{lots_termines}/{len(lots)}"
                    f" | auteurs traités : "
                    f"{len(statuts_par_auteur):,}/"
                    f"{len(auteurs_uniques):,}"
                    f" | PPN trouvés : "
                    f"{trouves_temp:,}"
                    f" | erreurs finales : "
                    f"{stats_globales['erreurs_finales']}",
                    end=""
                )

    print()

    duree = (
        time.perf_counter()
        - debut
    )

    trouves = sum(
        statut == "FOUND"
        for statut in statuts_par_auteur.values()
    )

    non_trouves = sum(
        statut == "NOT_FOUND"
        for statut in statuts_par_auteur.values()
    )

    invalides = sum(
        statut == "INVALID"
        for statut in statuts_par_auteur.values()
    )

    erreurs_finales = sum(
        statut == "ERROR"
        for statut in statuts_par_auteur.values()
    )

    print()
    print(
        "Temps IdRef :",
        f"{duree:.2f}s"
    )

    print(
        "PPN trouvés :",
        f"{trouves:,}"
    )

    print(
        "Non trouvés :",
        f"{non_trouves:,}"
    )

    print(
        "Requêtes HTTP :",
        f"{stats_globales['requetes_http']:,}"
    )

    print(
        "Lots échoués :",
        f"{stats_globales['lots_echoues']:,}"
    )

    print(
        "Divisions fallback :",
        f"{stats_globales['divisions']:,}"
    )

    print(
        "Erreurs finales :",
        f"{erreurs_finales:,}"
    )

    if invalides:
        print(
            "Auteurs invalides :",
            f"{invalides:,}"
        )

    if stats_globales[
        "auteurs_en_erreur"
    ]:

        print()
        print(
            "Auteurs encore en erreur après fallback :"
        )

        for item in stats_globales[
            "auteurs_en_erreur"
        ]:

            print(
                " -",
                item[
                    "auteur"
                ],
                "|",
                item[
                    "erreur"
                ]
            )

    return ppn_par_auteur


# ============================================================
# DATAFRAME FINAL
#
# IMPORTANT :
#
# 1 PPN PAR LIGNE
#
# Si :
#
# PPN = A, B
# ISBN = X, Y
#
# sortie :
#
# A X
# A Y
# B X
# B Y
#
# Si aucun PPN :
#
# "" X
# "" Y
# ============================================================

def construire_dataframe(
    documents,
    metadata,
    ppn_par_auteur
):

    lignes = []


    for (
        document,
        infos
    ) in documents.items():


        meta = metadata.get(
            document,
            {}
        )


        # ====================================================
        # AUTEURS
        # ====================================================

        auteurs = sorted(
            meta.get(
                "nomAuteur",
                set()
            )
        )


        nom_auteur = concat_unique(
            auteurs
        )


        # ====================================================
        # PPN
        #
        # Un set de PPN distincts
        # ====================================================

        ppns = {
            ppn_par_auteur.get(
                auteur,
                ""
            )

            for auteur
            in auteurs

            if ppn_par_auteur.get(
                auteur,
                ""
            )
        }


        # ----------------------------------------------------
        # Aucun PPN trouvé :
        # on conserve quand même la notice
        # ----------------------------------------------------

        if not ppns:

            ppns = {
                ""
            }


        # ====================================================
        # METADATA
        # ====================================================

        titre = concat_unique(
            meta.get(
                "titre",
                set()
            )
        )


        annee = concat_unique(
            meta.get(
                "annee",
                set()
            )
        )


        lieu = concat_unique(
            meta.get(
                "lieuPublication",
                set()
            )
        )


        editeur = concat_unique(
            meta.get(
                "editeur",
                set()
            )
        )


        sujet = concat_unique(
            meta.get(
                "sujet",
                set()
            )
        )


        raisons = concat_unique(
            infos.get(
                "raisons",
                set()
            )
        )


        # ====================================================
        # ISBN NORMALISES
        # ====================================================

        isbns = set()


        for isbn in infos[
            "isbn"
        ]:

            isbn_normalise = (
                normaliser_isbn_vers_13(
                    isbn
                )
            )

            if isbn_normalise:

                isbns.add(
                    isbn_normalise
                )


        # ====================================================
        # PRODUIT :
        #
        # PPN × ISBN
        # ====================================================

        for ppn in sorted(
            ppns
        ):

            for isbn_normalise in sorted(
                isbns
            ):


                lignes.append({

                    "ppn":
                        ppn,

                    "isbn_normalise":
                        isbn_normalise,

                    "titre":
                        titre,

                    "annee":
                        annee,

                    "nomAuteur":
                        nom_auteur,

                    "lieuPublication":
                        lieu,

                    "editeur":
                        editeur,

                    "sujet":
                        sujet,

                    "raisons":
                        raisons

                })


    # ========================================================
    # ORDRE COLONNES
    # ========================================================

    colonnes = [

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


    df = pd.DataFrame(
        lignes,
        columns=colonnes
    )


    if df.empty:

        return df


    # ========================================================
    # DEDUPLICATION
    # ========================================================

    df = (
        df
        .drop_duplicates()
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
# PROGRAMME PRINCIPAL
# ============================================================

def recherche_DNB():

    debut_global = time.perf_counter()


    # ========================================================
    # 1. RECHERCHE ISBN
    # ========================================================

    documents = (
        rechercher_documents_dnb()
    )


    if not documents:

        return pd.DataFrame(
            columns=[
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
        )


    # ========================================================
    # 2. FILTRE NUMERIQUE MARC 338 $b = cr
    # ========================================================

    (
        documents,
        erreurs_marc,
        nb_documents_numeriques,
        duree_marc
    ) = filtrer_documents_numeriques(
        documents
    )


    # --------------------------------------------------------
    # Tous les documents trouvés étaient numériques
    # --------------------------------------------------------

    if not documents:

        df_vide = pd.DataFrame(
            columns=[
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
        )

        df_vide.attrs[
            "erreurs_marc_dnb"
        ] = erreurs_marc

        return df_vide


    # ========================================================
    # 3. METADONNEES
    # ========================================================

    metadata = (
        recuperer_metadonnees(
            documents
        )
    )


    # ========================================================
    # 4. IDREF
    # ========================================================

    ppn_par_auteur = (
        recuperer_ppn_idref(
            metadata
        )
    )


    # ========================================================
    # 5. DATAFRAME
    # ========================================================

    df = construire_dataframe(
        documents,
        metadata,
        ppn_par_auteur
    )

    df.attrs[
        "erreurs_marc_dnb"
    ] = erreurs_marc

    df.attrs[
        "notices_numeriques_dnb_exclues"
    ] = nb_documents_numeriques


    # ========================================================
    # STATISTIQUES
    # ========================================================

    duree = (
        time.perf_counter()
        -
        debut_global
    )


    if not df.empty:

        nombre_isbn = (
            df[
                "isbn_normalise"
            ]
            .nunique()
        )


        lignes_ppn = (
            df[
                "ppn"
            ]
            .ne("")
            .sum()
        )


        ppn_uniques = (
            df.loc[
                df["ppn"].ne(""),
                "ppn"
            ]
            .nunique()
        )


        lignes_sujet = (
            df[
                "sujet"
            ]
            .ne("")
            .sum()
        )


    else:

        nombre_isbn = 0
        lignes_ppn = 0
        ppn_uniques = 0
        lignes_sujet = 0


    print()
    print("==========================================")
    print("RESULTATS DNB")
    print("==========================================")


    print(
        "Documents DNB uniques :",
        f"{len(documents):,}"
    )


    print(
        "Lignes finales :",
        f"{len(df):,}"
    )


    print(
        "ISBN-13 uniques :",
        f"{nombre_isbn:,}"
    )


    print(
        "PPN IdRef uniques :",
        f"{ppn_uniques:,}"
    )


    print(
        "Lignes avec PPN :",
        f"{lignes_ppn:,}"
    )


    print(
        "Lignes avec sujet :",
        f"{lignes_sujet:,}"
    )


    print(
        "Notices numériques exclues :",
        f"{nb_documents_numeriques:,}"
    )


    print(
        "Erreurs contrôle MARC :",
        f"{len(erreurs_marc):,}"
    )


    print(
        "Temps contrôle MARC :",
        f"{duree_marc:.2f}s"
    )


    print(
        "Durée totale :",
        f"{duree:.2f}s"
    )


    print(
        "Soit :",
        f"{duree / 60:.2f} min"
    )


    print(
        "=========================================="
    )


    return df


# ============================================================
# LANCEMENT DIRECT UNIQUEMENT
#
# IMPORTANT POUR LE PIPELINE :
# quand ce fichier est importé avec
#     from lieupublidnb import recherche_DNB
# aucune requête n'est lancée automatiquement.
# ============================================================

if __name__ == "__main__":

    df_dnb = recherche_DNB()

    liste_isbn_dnb = (
        df_dnb[
            "isbn_normalise"
        ]
        .dropna()
        .drop_duplicates()
        .tolist()
    )