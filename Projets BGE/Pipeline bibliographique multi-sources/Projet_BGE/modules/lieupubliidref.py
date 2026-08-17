# ============================================================
# PIPELINE HYBRIDE ULTRA-RAPIDE
# IdRef Solr -> SPARQL data.idref.fr -> XML Sudoc asynchrone
#
# Objectif :
# - chercher largement les autorités IdRef liées à Genève
# - remplacer /services/references/{ppn}.xml par SPARQL en lots
# - récupérer les documents Sudoc liés très rapidement
# - télécharger ensuite uniquement les XML bibliographiques utiles
# - produire :
#   ppn | isbn_normalise | titre | nomAuteur | editeur |
#   annee | sujet | lieuPublication | raisons
#
# La colonne ppn contient UN seul PPN IdRef auteur par ligne.
# L'année SPARQL est utilisée en priorité, puis fallback XML.
# ============================================================

# !pip install aiohttp lxml pandas -q

import asyncio
import aiohttp
import pandas as pd
import re
import unicodedata
import time
import json
from pathlib import Path

from lxml import etree


# ============================================================
# CONFIGURATION
# ============================================================

IDREF_SOLR_URL = "https://www.idref.fr/Sru/Solr"
SPARQL_URL = "https://data.idref.fr/sparql"
SUDOC_URL = "https://www.sudoc.fr"

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

    communes = list(dict.fromkeys(communes))

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

# Solr
MAX_CONCURRENT_IDREF = 10
SOLR_ROWS = 1000

# SPARQL
# 100 à 300 est généralement un bon compromis pour Virtuoso.
SPARQL_BATCH_SIZE = 200
MAX_CONCURRENT_SPARQL = 4

# XML Sudoc
MAX_CONCURRENT_SUDOC = 40

# HTTP
TIMEOUT_TOTAL = 60
TIMEOUT_CONNECT = 15
MAX_RETRIES = 5
BACKOFF_BASE = 0.5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; bibliographic-data-analysis/2.0)",
    "Accept": "*/*",
}


# ============================================================
# NORMALISATION TEXTE
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
        caractere
        for caractere in unicodedata.normalize("NFD", texte)
        if unicodedata.category(caractere) != "Mn"
    )

    return texte


def construire_variantes():
    variantes = {
        normaliser_texte(commune)
        for commune in COMMUNES_GENEVE
    }

    variantes.update({
        "geneva",
        "genf",
        "ginevra",
    })

    return sorted(variantes)


VARIANTES = construire_variantes()

COMMUNES_NORMALISEES = sorted(
    VARIANTES,
    key=len,
    reverse=True,
)

REGEX_COMMUNES = re.compile(
    "|".join(
        rf"(?<![a-z]){re.escape(commune)}(?![a-z])"
        for commune in COMMUNES_NORMALISEES
    ),
    re.IGNORECASE,
)


def contient_commune(texte):
    if not texte:
        return False

    return bool(
        REGEX_COMMUNES.search(
            normaliser_texte(texte)
        )
    )


# ============================================================
# ISBN
# ============================================================

ISBN_NETTOYAGE_REGEX = re.compile(r"[^0-9X]")


def normaliser_isbn(isbn):
    if isbn is None:
        return None

    isbn = ISBN_NETTOYAGE_REGEX.sub(
        "",
        str(isbn).upper().strip(),
    )

    # ISBN-13
    if len(isbn) == 13 and isbn.isdigit():
        total = sum(
            int(chiffre) * (1 if position % 2 == 0 else 3)
            for position, chiffre in enumerate(isbn[:12])
        )

        cle = (10 - (total % 10)) % 10

        if cle != int(isbn[12]):
            return None

        return isbn

    # ISBN-10 -> ISBN-13
    if len(isbn) == 10:
        if not isbn[:9].isdigit():
            return None

        if not (isbn[-1].isdigit() or isbn[-1] == "X"):
            return None

        total = 0

        for position, caractere in enumerate(isbn):
            valeur = 10 if caractere == "X" else int(caractere)
            total += (10 - position) * valeur

        if total % 11 != 0:
            return None

        base = "978" + isbn[:9]

        total = sum(
            int(chiffre) * (1 if position % 2 == 0 else 3)
            for position, chiffre in enumerate(base)
        )

        cle = (10 - (total % 10)) % 10

        return base + str(cle)

    return None


# ============================================================
# OUTILS
# ============================================================

PPN_REGEX = re.compile(
    r"(?<![0-9A-Z])([0-9]{8}[0-9X])(?![0-9A-Z])",
    re.IGNORECASE,
)

ANNEE_REGEX = re.compile(
    r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)"
)


def extraire_ppn_uri(uri):
    if not uri:
        return None

    match = re.search(
        r"(?:idref|sudoc)\.fr/([0-9]{8}[0-9X])",
        str(uri),
        flags=re.IGNORECASE,
    )

    return match.group(1).upper() if match else None


def extraire_annee(texte):
    if not texte:
        return None

    match = ANNEE_REGEX.search(str(texte))

    if not match:
        return None

    return int(match.group(1))


def concat_unique(valeurs):
    valeurs_uniques = {
        str(x).strip()
        for x in valeurs
        if x is not None and str(x).strip()
    }

    if not valeurs_uniques:
        return ""

    return " | ".join(sorted(valeurs_uniques))


def decouper_lots(liste, taille):
    for i in range(0, len(liste), taille):
        yield liste[i:i + taille]


# ============================================================
# HTTP ASYNCHRONE
# ============================================================

async def requete_http(
    session,
    url,
    *,
    params=None,
    data=None,
    semaphore=None,
    accepter_404=False,
):
    if semaphore is None:
        semaphore = asyncio.Semaphore(100)

    async with semaphore:
        for tentative in range(1, MAX_RETRIES + 1):
            try:
                if data is None:
                    contexte = session.get(
                        url,
                        params=params,
                    )
                else:
                    contexte = session.post(
                        url,
                        data=data,
                    )

                async with contexte as response:
                    if accepter_404 and response.status == 404:
                        return None

                    if response.status in {429, 500, 502, 503, 504}:
                        if tentative == MAX_RETRIES:
                            response.raise_for_status()

                        retry_after = response.headers.get("Retry-After")

                        try:
                            attente = (
                                float(retry_after)
                                if retry_after
                                else BACKOFF_BASE * (2 ** (tentative - 1))
                            )
                        except ValueError:
                            attente = BACKOFF_BASE * (2 ** (tentative - 1))

                        await asyncio.sleep(attente)
                        continue

                    response.raise_for_status()
                    return await response.read()

            except (aiohttp.ClientError, asyncio.TimeoutError):
                if tentative == MAX_RETRIES:
                    raise

                await asyncio.sleep(
                    BACKOFF_BASE * (2 ** (tentative - 1))
                )

    return None


# ============================================================
# ETAPE 1 — IDREF SOLR
# ============================================================

async def rechercher_idref_mot(
    session,
    semaphore,
    mot_cle,
):
    mot = normaliser_texte(mot_cle)

    start = 0
    lignes = []

    while True:
        params = {
            "q": f'all:"{mot}"',
            "wt": "json",
            "fl": "ppn_z,recordtype_z,affcourt_z",
            "start": start,
            "rows": SOLR_ROWS,
            "version": "2.2",
        }

        contenu = await requete_http(
            session,
            IDREF_SOLR_URL,
            params=params,
            semaphore=semaphore,
        )

        if not contenu:
            break

        data = json.loads(contenu)

        bloc = data.get("response", {})
        docs = bloc.get("docs", [])
        total = bloc.get("numFound", 0)

        if not docs:
            break

        for doc in docs:
            ppn = doc.get("ppn_z")

            if not ppn:
                continue

            lignes.append({
                "mot_cle": mot_cle,
                "ppn": str(ppn),
                "recordtype": doc.get("recordtype_z"),
                "libelle": doc.get("affcourt_z"),
            })

        start += len(docs)

        if start >= total:
            break

    return mot_cle, lignes


async def rechercher_idref_large(session):
    debut = time.perf_counter()

    print()
    print("=" * 70)
    print("1/4 - RECHERCHE LARGE IDREF")
    print("=" * 70)

    semaphore = asyncio.Semaphore(
        MAX_CONCURRENT_IDREF
    )

    taches = [
        rechercher_idref_mot(
            session,
            semaphore,
            mot,
        )
        for mot in VARIANTES
    ]

    toutes_lignes = []

    for future in asyncio.as_completed(taches):
        try:
            mot, lignes = await future
            toutes_lignes.extend(lignes)

            print(
                f"✓ {mot:<15} : "
                f"{len(lignes):,}"
            )

        except Exception as erreur:
            print(
                f"✗ Erreur IdRef : {erreur}"
            )

    if not toutes_lignes:
        return pd.DataFrame(
            columns=[
                "mot_cle",
                "ppn",
                "recordtype",
                "libelle",
            ]
        )

    df = (
        pd.DataFrame(toutes_lignes)
        .drop_duplicates(
            subset=[
                "ppn",
                "mot_cle",
            ]
        )
        .reset_index(drop=True)
    )

    print()
    print(
        f"PPN IdRef uniques : "
        f"{df['ppn'].nunique():,}"
    )

    print(
        f"Durée IdRef : "
        f"{time.perf_counter() - debut:.1f}s"
    )

    return df


# ============================================================
# ETAPE 2 — SPARQL EN LOTS
#
# Remplace complètement services/references.
#
# Résultat :
# ppn_document
# ppn_auteur_source
# relation
# date_rdf
# annee_rdf
# citation
# ============================================================

async def sparql_lot_auteurs(
    session,
    semaphore,
    lot_ppn,
):
    values = "\n".join(
        f"<http://www.idref.fr/{ppn}/id>"
        for ppn in lot_ppn
    )

    query = f"""
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX dcterms: <http://purl.org/dc/terms/>

SELECT DISTINCT
    ?auteur
    ?doc
    ?relation
    ?citation
    ?date
WHERE
{{
    VALUES ?auteur {{
        {values}
    }}

    ?doc ?relation ?auteur .

    FILTER(
        STRSTARTS(
            STR(?doc),
            "http://www.sudoc.fr/"
        )
    )

    OPTIONAL {{
        ?doc dcterms:bibliographicCitation ?citation .
    }}

    OPTIONAL {{
        ?doc dc:date ?date .
    }}
}}
"""

    contenu = await requete_http(
        session,
        SPARQL_URL,
        data={
            "query": query,
            "format": "application/sparql-results+json",
        },
        semaphore=semaphore,
    )

    if not contenu:
        return []

    data = json.loads(contenu)

    bindings = (
        data
        .get("results", {})
        .get("bindings", [])
    )

    lignes = []

    for ligne in bindings:
        auteur_uri = (
            ligne.get("auteur", {})
            .get("value")
        )

        doc_uri = (
            ligne.get("doc", {})
            .get("value")
        )

        relation = (
            ligne.get("relation", {})
            .get("value")
        )

        citation = (
            ligne.get("citation", {})
            .get("value")
        )

        date_rdf = (
            ligne.get("date", {})
            .get("value")
        )

        ppn_auteur_source = extraire_ppn_uri(
            auteur_uri
        )

        ppn_document = extraire_ppn_uri(
            doc_uri
        )

        if not ppn_document:
            continue

        lignes.append({
            "ppn_auteur_source": ppn_auteur_source,
            "ppn_document": ppn_document,
            "relation": relation,
            "citation": citation,
            "date_rdf": date_rdf,
            "annee_rdf": extraire_annee(date_rdf),
        })

    return lignes


async def recuperer_documents_sparql(
    session,
    df_idref,
):
    debut = time.perf_counter()

    print()
    print("=" * 70)
    print("2/4 - DOCUMENTS SUDOC VIA SPARQL")
    print("=" * 70)

    auteurs = (
        df_idref["ppn"]
        .dropna()
        .astype(str)
        .drop_duplicates()
        .tolist()
    )

    if not auteurs:
        return pd.DataFrame(
            columns=[
                "ppn_auteur_source",
                "ppn_document",
                "relation",
                "citation",
                "date_rdf",
                "annee_rdf",
            ]
        )

    lots = list(
        decouper_lots(
            auteurs,
            SPARQL_BATCH_SIZE,
        )
    )

    print(
        f"Autorités : {len(auteurs):,}"
    )

    print(
        f"Lots SPARQL : {len(lots):,} "
        f"(taille={SPARQL_BATCH_SIZE})"
    )

    semaphore = asyncio.Semaphore(
        MAX_CONCURRENT_SPARQL
    )

    taches = [
        sparql_lot_auteurs(
            session,
            semaphore,
            lot,
        )
        for lot in lots
    ]

    toutes_lignes = []
    termines = 0

    for future in asyncio.as_completed(taches):
        try:
            lignes = await future
            toutes_lignes.extend(lignes)

        except Exception as erreur:
            print(
                f"\n✗ Erreur SPARQL : {erreur}"
            )

        termines += 1

        print(
            f"\rLots : {termines:,}/{len(lots):,} "
            f"| liens : {len(toutes_lignes):,}",
            end="",
        )

    print()

    if not toutes_lignes:
        return pd.DataFrame(
            columns=[
                "ppn_auteur_source",
                "ppn_document",
                "relation",
                "citation",
                "date_rdf",
                "annee_rdf",
            ]
        )

    df = (
        pd.DataFrame(toutes_lignes)
        .drop_duplicates()
        .reset_index(drop=True)
    )

    print(
        f"Documents Sudoc uniques : "
        f"{df['ppn_document'].nunique():,}"
    )

    print(
        f"Documents avec année RDF : "
        f"{df.drop_duplicates('ppn_document')['annee_rdf'].notna().sum():,}"
    )

    print(
        f"Durée SPARQL : "
        f"{time.perf_counter() - debut:.1f}s"
    )

    return df


# ============================================================
# XML UNIMARC
# ============================================================

def nom_local(element):
    try:
        return etree.QName(element).localname
    except Exception:
        return ""


def parser_unimarc(contenu_xml):
    try:
        root = etree.fromstring(contenu_xml)
    except Exception:
        return None, None

    index = {}
    champs_complets = []

    for element in root.iter():
        if nom_local(element) != "datafield":
            continue

        tag = element.get("tag")

        if not tag:
            continue

        sous_champs = {}

        for subfield in element:
            if nom_local(subfield) != "subfield":
                continue

            code = subfield.get("code")
            texte = subfield.text

            if not code or texte is None:
                continue

            texte = texte.strip()

            if not texte:
                continue

            sous_champs.setdefault(
                code,
                []
            ).append(texte)

            index.setdefault(
                tag,
                {}
            ).setdefault(
                code,
                []
            ).append(texte)

        if sous_champs:
            champs_complets.append(
                (
                    tag,
                    sous_champs,
                )
            )

    return index, champs_complets


def valeurs_champs(index, tags, codes):
    valeurs = []

    if not index:
        return valeurs

    for tag in tags:
        bloc = index.get(tag)

        if not bloc:
            continue

        for code in codes:
            valeurs.extend(
                bloc.get(
                    code,
                    []
                )
            )

    return valeurs


TAGS_TITRES = [
    "200",
    "500",
    "501",
    "503",
    "510",
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
    "545",
]

CODES_TITRES = [
    "a",
    "e",
    "h",
    "i",
]

TAGS_SUJETS = [
    str(tag)
    for tag in range(600, 700)
]

CODES_SUJETS = [
    "a", "b", "c", "d", "f", "g", "h", "j",
    "k", "l", "m", "n", "o", "p", "q", "r",
    "s", "t", "u", "v", "x", "y", "z",
]


# ============================================================
# AUTEURS + PPN IDREF
# ============================================================

def extraire_auteurs_et_ppn(
    champs_complets,
):
    noms = []
    ppn_auteurs = []

    tags_personnes = {
        "700",
        "701",
        "702",
    }

    tags_collectivites = {
        "710",
        "711",
        "712",
    }

    for tag, sous_champs in champs_complets:
        if tag in tags_personnes:
            parties = []

            parties.extend(
                sous_champs.get(
                    "a",
                    []
                )
            )

            parties.extend(
                sous_champs.get(
                    "b",
                    []
                )
            )

            if parties:
                nom = " ".join(
                    partie.strip()
                    for partie in parties
                    if partie.strip()
                )

                if nom:
                    noms.append(nom)

            for valeur_ppn in sous_champs.get(
                "3",
                []
            ):
                match = PPN_REGEX.search(
                    str(valeur_ppn)
                )

                if match:
                    ppn_auteurs.append(
                        match.group(1).upper()
                    )

        elif tag in tags_collectivites:
            parties = []

            for code in [
                "a",
                "b",
                "c",
            ]:
                parties.extend(
                    sous_champs.get(
                        code,
                        []
                    )
                )

            if parties:
                nom = " ".join(
                    partie.strip()
                    for partie in parties
                    if partie.strip()
                )

                if nom:
                    noms.append(nom)

            for valeur_ppn in sous_champs.get(
                "3",
                []
            ):
                match = PPN_REGEX.search(
                    str(valeur_ppn)
                )

                if match:
                    ppn_auteurs.append(
                        match.group(1).upper()
                    )

    return (
        concat_unique(noms),
        concat_unique(ppn_auteurs),
    )


# ============================================================
# ANALYSE NOTICE
# ============================================================

def analyser_xml_sudoc(
    ppn_document,
    contenu_xml,
    annee_rdf=None,
):
    if not contenu_xml:
        return []

    index, champs_complets = parser_unimarc(
        contenu_xml
    )

    if not index:
        return []

    # ============================================================
    # SUPPORT ÉLECTRONIQUE
    #
    # UNIMARC 338 $b = cr
    #
    # Si au moins un 338$b contient exactement "cr"
    # (insensible à la casse et aux espaces), la notice correspond
    # à une ressource en ligne / numérique et est exclue entièrement.
    #
    # Ce contrôle est volontairement placé AVANT l'extraction ISBN
    # et tous les autres traitements afin d'éviter du travail inutile.
    # ============================================================

    supports_338 = valeurs_champs(
        index,
        ["338"],
        ["b"],
    )

    if any(
        str(code_support).strip().lower() == "cr"
        for code_support in supports_338
    ):
        return []

    # ISBN
    isbn_bruts = valeurs_champs(
        index,
        ["010"],
        ["a"],
    )

    if not isbn_bruts:
        return []

    isbn = {
        normaliser_isbn(x)
        for x in isbn_bruts
    }

    isbn.discard(None)

    if not isbn:
        return []

    # Détection titre
    titres_detection = valeurs_champs(
        index,
        TAGS_TITRES,
        CODES_TITRES,
    )

    titre_ok = any(
        contient_commune(x)
        for x in titres_detection
    )

    # Sujets
    sujets = valeurs_champs(
        index,
        TAGS_SUJETS,
        CODES_SUJETS,
    )

    sujet_ok = any(
        contient_commune(x)
        for x in sujets
    )

    # Lieu publication
    lieux = valeurs_champs(
        index,
        ["210", "214"],
        ["a"],
    )

    lieu_ok = any(
        contient_commune(x)
        for x in lieux
    )

    # Aucun critère Genève
    if not (
        titre_ok
        or sujet_ok
        or lieu_ok
    ):
        return []

    # Titre
    titre = concat_unique(
        valeurs_champs(
            index,
            ["200"],
            ["a", "e", "h", "i"],
        )
    )

    # Auteurs + PPN IdRef
    nom_auteur, ppn_auteurs = (
        extraire_auteurs_et_ppn(
            champs_complets
        )
    )

    # Editeur
    editeur = concat_unique(
        valeurs_champs(
            index,
            ["210", "214"],
            ["c"],
        )
    )

    # Année
    # Priorité SPARQL, fallback XML.
    annee = (
        int(annee_rdf)
        if annee_rdf is not None
        and not pd.isna(annee_rdf)
        else None
    )

    if annee is None:
        dates_xml = valeurs_champs(
            index,
            ["210", "214"],
            ["d"],
        )

        for valeur_date in dates_xml:
            annee = extraire_annee(
                valeur_date
            )

            if annee is not None:
                break

    raisons = []

    if titre_ok:
        raisons.append("titre")

    if sujet_ok:
        raisons.append("sujet")

    if lieu_ok:
        raisons.append(
            "lieu_publication"
        )

    sujet_final = concat_unique(
        sujets
    )

    lieu_final = concat_unique(
        lieux
    )

    raisons_final = " | ".join(
        raisons
    )

    return [
        {
            "ppn": ppn_auteurs,
            "isbn_normalise": numero,
            "titre": titre,
            "nomAuteur": nom_auteur,
            "editeur": editeur,
            "annee": annee,
            "sujet": sujet_final,
            "lieuPublication": lieu_final,
            "raisons": raisons_final,
            "_ppn_document": ppn_document,
        }
        for numero in sorted(isbn)
    ]


# ============================================================
# TELECHARGEMENT XML
# ============================================================

async def analyser_notice_sudoc(
    session,
    semaphore,
    ppn_document,
    annee_rdf=None,
):
    url = (
        f"{SUDOC_URL}/"
        f"{ppn_document}.xml"
    )

    contenu = await requete_http(
        session,
        url,
        semaphore=semaphore,
        accepter_404=True,
    )

    if contenu is None:
        return ppn_document, []

    lignes = analyser_xml_sudoc(
        ppn_document,
        contenu,
        annee_rdf=annee_rdf,
    )

    return ppn_document, lignes


async def analyser_documents_sudoc(
    session,
    df_liens_sparql,
):
    debut = time.perf_counter()

    print()
    print("=" * 70)
    print("3/4 - XML SUDOC")
    print("=" * 70)

    if df_liens_sparql.empty:
        return pd.DataFrame()

    # Une ligne par document.
    # Si plusieurs dates RDF existent, on prend la première année non vide.
    meta_documents = (
        df_liens_sparql[
            [
                "ppn_document",
                "annee_rdf",
            ]
        ]
        .sort_values(
            by="annee_rdf",
            na_position="last",
        )
        .drop_duplicates(
            subset=[
                "ppn_document"
            ],
            keep="first",
        )
        .reset_index(drop=True)
    )

    total = len(
        meta_documents
    )

    print(
        f"Documents XML à télécharger : "
        f"{total:,}"
    )

    semaphore = asyncio.Semaphore(
        MAX_CONCURRENT_SUDOC
    )

    taches = [
        analyser_notice_sudoc(
            session,
            semaphore,
            ligne.ppn_document,
            ligne.annee_rdf,
        )
        for ligne in meta_documents.itertuples(
            index=False
        )
    ]

    toutes_lignes = []
    termines = 0
    erreurs = 0

    for future in asyncio.as_completed(
        taches
    ):
        try:
            _, lignes = await future

            if lignes:
                toutes_lignes.extend(
                    lignes
                )

        except Exception as erreur:
            erreurs += 1

        termines += 1

        if (
            termines % 100 == 0
            or termines == total
        ):
            print(
                f"\rNotices : "
                f"{termines:,}/{total:,} "
                f"({termines / total * 100:.1f}%) "
                f"| lignes : {len(toutes_lignes):,} "
                f"| erreurs : {erreurs:,}",
                end="",
            )

    print()

    print(
        f"Durée XML Sudoc : "
        f"{time.perf_counter() - debut:.1f}s"
    )

    if not toutes_lignes:
        return pd.DataFrame()

    return pd.DataFrame(
        toutes_lignes
    )


# ============================================================
# FUSION FINALE
# ============================================================

def fusionner_resultats(df):
    if df.empty:
        return df

    # ========================================================
    # 1 — Une notice bibliographique + un ISBN = une ligne
    #     AVANT éclatement des PPN auteurs.
    #
    # Le PPN auteur n'est PAS utilisé pour dédupliquer ici,
    # sinon une même notice avec plusieurs auteurs pourrait
    # être traitée incorrectement.
    # ========================================================

    df = (
        df
        .drop_duplicates(
            subset=[
                "_ppn_document",
                "isbn_normalise",
            ]
        )
        .reset_index(drop=True)
    )

    # ========================================================
    # 2 — Une ligne par PPN IdRef auteur
    #
    # Exemple :
    #
    # 028805542 | 076535339
    #
    # devient :
    #
    # 028805542
    # 076535339
    #
    # Toutes les autres informations sont dupliquées
    # à l’identique sur chaque nouvelle ligne.
    #
    # Si aucun PPN n’existe, la ligne est conservée avec
    # une valeur vide dans la colonne ppn.
    # ========================================================

    df["ppn"] = (
        df["ppn"]
        .fillna("")
        .astype(str)
        .str.split(r"\s*\|\s*", regex=True)
    )

    df = (
        df
        .explode(
            "ppn",
            ignore_index=True,
        )
    )

    df["ppn"] = (
        df["ppn"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    # ========================================================
    # 3 — Suppression d’éventuels doublons créés après explode
    #
    # Une même notice + ISBN + PPN auteur ne doit apparaître
    # qu’une seule fois.
    # ========================================================

    df = (
        df
        .drop_duplicates(
            subset=[
                "_ppn_document",
                "isbn_normalise",
                "ppn",
            ]
        )
        .reset_index(drop=True)
    )

    # ========================================================
    # 4 — Colonnes finales
    # ========================================================

    colonnes = [
        "ppn",
        "isbn_normalise",
        "titre",
        "nomAuteur",
        "editeur",
        "annee",
        "sujet",
        "lieuPublication",
        "raisons",
    ]

    df = df[colonnes]

    # ========================================================
    # 5 — Année en entier nullable
    # ========================================================

    df["annee"] = pd.to_numeric(
        df["annee"],
        errors="coerce",
    ).astype("Int64")

    # ========================================================
    # 6 — Tri final
    # ========================================================

    df = (
        df
        .sort_values(
            by=[
                "isbn_normalise",
                "titre",
                "ppn",
            ],
            na_position="last",
        )
        .reset_index(drop=True)
    )

    return df


# ============================================================
# PIPELINE
# ============================================================

async def executer_pipeline_hybride():
    debut_global = time.perf_counter()

    timeout = aiohttp.ClientTimeout(
        total=TIMEOUT_TOTAL,
        connect=TIMEOUT_CONNECT,
    )

    connector = aiohttp.TCPConnector(
        limit=80,
        limit_per_host=45,
        ttl_dns_cache=600,
        enable_cleanup_closed=True,
    )

    async with aiohttp.ClientSession(
        headers=HEADERS,
        timeout=timeout,
        connector=connector,
    ) as session:

        # 1 — Autorités IdRef
        df_idref_brut = await rechercher_idref_large(
            session
        )

        if df_idref_brut.empty:
            print(
                "Aucun résultat IdRef."
            )

            return (
                pd.DataFrame(),
                [],
                df_idref_brut,
                pd.DataFrame(),
            )

        # 2 — Documents Sudoc via SPARQL
        df_liens_sparql = (
            await recuperer_documents_sparql(
                session,
                df_idref_brut,
            )
        )

        if df_liens_sparql.empty:
            print(
                "Aucun document Sudoc trouvé via SPARQL."
            )

            return (
                pd.DataFrame(),
                [],
                df_idref_brut,
                df_liens_sparql,
            )

        # 3 — XML Sudoc
        df_sudoc_brut = (
            await analyser_documents_sudoc(
                session,
                df_liens_sparql,
            )
        )

    # 4 — Final
    df_final = fusionner_resultats(
        df_sudoc_brut
    )

    if not df_final.empty:
        liste_isbn = (
            df_final["isbn_normalise"]
            .dropna()
            .drop_duplicates()
            .tolist()
        )
    else:
        liste_isbn = []

    duree = (
        time.perf_counter()
        - debut_global
    )

    print()
    print("=" * 70)
    print("4/4 - RESULTATS")
    print("=" * 70)

    print(
        f"Autorités IdRef : "
        f"{df_idref_brut['ppn'].nunique():,}"
    )

    print(
        f"Documents Sudoc candidats : "
        f"{df_liens_sparql['ppn_document'].nunique():,}"
    )

    print(
        f"Lignes finales : "
        f"{len(df_final):,}"
    )

    print(
        f"ISBN uniques : "
        f"{len(liste_isbn):,}"
    )

    print(
        f"Durée totale : "
        f"{duree:.1f}s "
        f"({duree / 60:.1f} min)"
    )

    return (
        df_final,
        liste_isbn,
        df_idref_brut,
        df_liens_sparql,
    )


# ============================================================
# INTERFACE POUR PIPELINE.PY
# ============================================================

async def recherche_IDREF():
    """
    Interface utilisée par pipeline.py.

    Lance le pipeline hybride IdRef / SPARQL / Sudoc
    et retourne uniquement le DataFrame final Genevensia.
    """

    (
        df_final,
        liste_isbn,
        df_idref_brut,
        df_liens_sparql,
    ) = await executer_pipeline_hybride()

    return df_final