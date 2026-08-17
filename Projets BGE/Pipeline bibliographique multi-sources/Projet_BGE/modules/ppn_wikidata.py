import requests
import pandas as pd
import time

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

URL = "https://query.wikidata.org/sparql"

TIMEOUT = (5, 60)  # (connexion, lecture)
TAILLE_LOT = 500
MAX_RETRIES = 4
PAUSE_ENTRE_LOTS = 0.0  # aucune pause après une requête réussie


# ============================================================
# SESSION HTTP
# ============================================================

session = requests.Session()

retry = Retry(
    total=MAX_RETRIES - 1,
    connect=MAX_RETRIES - 1,
    read=MAX_RETRIES - 1,
    status=MAX_RETRIES - 1,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"POST"}),
    respect_retry_after_header=True,
    raise_on_status=False,
)

adapter = HTTPAdapter(
    max_retries=retry,
    pool_connections=10,
    pool_maxsize=10,
)

session.mount("https://", adapter)

session.headers.update({
    "User-Agent": "Geneve-PPN-Extraction/2.1",
    "Accept": "application/sparql-results+json",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept-Encoding": "gzip, deflate",
})


# ============================================================
# REQUÊTE WIKIDATA
# ============================================================

def requete_wikidata(query, max_retries=MAX_RETRIES):
    # Les retries sont désormais gérés directement par urllib3/requests.
    # `max_retries` est conservé dans la signature pour compatibilité.
    debut = time.perf_counter()

    response = session.post(
        URL,
        data={"query": query},
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    duree = time.perf_counter() - debut
    print(f"✓ {duree:.2f} s")

    return response.json()


# ============================================================
# JSON -> DATAFRAME
# ============================================================

def resultat_vers_dataframe(resultat):

    variables = resultat["head"]["vars"]

    return pd.DataFrame([
        {
            var: binding.get(
                var,
                {}
            ).get(
                "value"
            )
            for var in variables
        }
        for binding
        in resultat["results"]["bindings"]
    ])


# ============================================================
# ÉTAPE 1
# TOUS LES PPN LIÉS À GENÈVE
# ============================================================

QUERY_PPN = """
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT
    ?item
    ?ppn
    ?p

WHERE {

    # PPN obligatoire
    ?item wdt:P269 ?ppn .

    # Raisons du lien avec Genève
    VALUES ?p {
        wdt:P19
        wdt:P20
        wdt:P69
        wdt:P108
        wdt:P551
        wdt:P937
    }

    ?item ?p ?lieu .

    # Lieu situé dans le canton de Genève
    ?lieu wdt:P131* wd:Q11917 .
}
"""


def recuperer_ppn_geneve():

    print()
    print("=" * 60)
    print("ÉTAPE 1 : PPN LIÉS À GENÈVE")
    print("=" * 60)

    resultat = requete_wikidata(
        QUERY_PPN
    )

    df = resultat_vers_dataframe(
        resultat
    )

    print()
    print(
        f"Lignes brutes : {len(df):,}"
    )

    print(
        f"PPN uniques : {df['ppn'].nunique():,}"
    )

    print(
        f"Items Wikidata uniques : {df['item'].nunique():,}"
    )

    return df


# ============================================================
# RAISONS
# ============================================================

RAISONS = {

    "http://www.wikidata.org/prop/direct/P19":
        "lieu de naissance",

    "http://www.wikidata.org/prop/direct/P20":
        "lieu de décès",

    "http://www.wikidata.org/prop/direct/P69":
        "scolarité",

    "http://www.wikidata.org/prop/direct/P108":
        "employeur",

    "http://www.wikidata.org/prop/direct/P551":
        "résidence",

    "http://www.wikidata.org/prop/direct/P937":
        "lieu de travail"
}


def regrouper_raisons(df):

    df = df.copy()

    df["Raison"] = (
        df["p"]
        .map(RAISONS)
    )

    df = (
        df
        .groupby(
            ["item", "ppn"],
            as_index=False
        )
        .agg(
            Raisons=(
                "Raison",
                lambda x:
                    " | ".join(
                        sorted(
                            set(
                                x.dropna()
                            )
                        )
                    )
            )
        )
    )

    return df


# ============================================================
# CONSTRUCTION REQUÊTE ENRICHISSEMENT
# ============================================================

def construire_query_enrichissement(items):

    values = "\n".join(
        f"<{item}>"
        for item in items
    )

    return f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT
    ?item
    ?ppn
    (SAMPLE(?nom) AS ?NomComplet)
    (GROUP_CONCAT(
        DISTINCT ?occupationLabel;
        separator=" | "
    ) AS ?Occupations)
    (SAMPLE(?bnf_id) AS ?bnf_id)
    (SAMPLE(?gnd_id) AS ?gnd_id)

WHERE {{

    VALUES ?item {{
        {values}
    }}

    ?item wdt:P269 ?ppn .


    # --------------------------------------------------------
    # NOM : français uniquement
    # --------------------------------------------------------

    OPTIONAL {{
        ?item rdfs:label ?nom .
        FILTER(LANG(?nom) = "fr")
    }}


    # --------------------------------------------------------
    # OCCUPATIONS
    # --------------------------------------------------------

    OPTIONAL {{

        ?item wdt:P106 ?occupation .

        ?occupation rdfs:label ?occupationLabel .

        FILTER(
            LANG(?occupationLabel) = "fr"
        )
    }}


    # --------------------------------------------------------
    # BNF
    # --------------------------------------------------------

    OPTIONAL {{
        ?item wdt:P268 ?bnf_id .
    }}


    # --------------------------------------------------------
    # DNB / GND
    # --------------------------------------------------------

    OPTIONAL {{
        ?item wdt:P227 ?gnd_id .
    }}

}}

GROUP BY
    ?item
    ?ppn
"""


# ============================================================
# ENRICHISSEMENT
# ============================================================

def enrichir_ppn(
    df_ppn,
    taille_lot=TAILLE_LOT
):

    items = (
        df_ppn["item"]
        .dropna()
        .drop_duplicates()
        .tolist()
    )

    total = len(items)

    nb_lots = (
        total
        + taille_lot
        - 1
    ) // taille_lot

    resultats = []

    print()
    print("=" * 60)
    print("ÉTAPE 2 : ENRICHISSEMENT")
    print("=" * 60)

    print(
        f"{total:,} items"
    )

    print(
        f"{nb_lots:,} lots"
    )

    print(
        f"Taille lot : {taille_lot}"
    )

    for debut in range(
        0,
        total,
        taille_lot
    ):

        numero = (
            debut // taille_lot
        ) + 1

        lot = items[
            debut:
            debut + taille_lot
        ]

        print()
        print(
            f"Lot {numero}/{nb_lots} "
            f"({len(lot)} items)"
        )

        query = construire_query_enrichissement(
            lot
        )

        try:

            resultat = requete_wikidata(
                query
            )

            df_lot = resultat_vers_dataframe(
                resultat
            )

            print(
                f"→ {len(df_lot):,} lignes"
            )

            if not df_lot.empty:

                resultats.append(
                    df_lot
                )

        except Exception as e:

            print(
                f"✗ Erreur lot {numero}: {e}"
            )

            # =================================================
            # FALLBACK : on coupe le lot en deux
            # =================================================

            if len(lot) > 50:

                milieu = len(lot) // 2

                sous_lots = [
                    lot[:milieu],
                    lot[milieu:]
                ]

                for j, sous_lot in enumerate(
                    sous_lots,
                    start=1
                ):

                    print(
                        f"  ↳ sous-lot {j}/2 "
                        f"({len(sous_lot)} items)"
                    )

                    try:

                        query2 = (
                            construire_query_enrichissement(
                                sous_lot
                            )
                        )

                        resultat2 = requete_wikidata(
                            query2
                        )

                        df2 = resultat_vers_dataframe(
                            resultat2
                        )

                        if not df2.empty:

                            resultats.append(
                                df2
                            )

                    except Exception as e2:

                        print(
                            f"  ✗ sous-lot échoué : {e2}"
                        )

        if PAUSE_ENTRE_LOTS > 0:
            time.sleep(PAUSE_ENTRE_LOTS)

    if not resultats:

        return pd.DataFrame()

    return pd.concat(
        resultats,
        ignore_index=True
    )


# ============================================================
# NETTOYAGE FINAL
# ============================================================

def nettoyer_resultat(
    df_ppn,
    df_enrichi
):

    df = df_ppn.merge(
        df_enrichi,
        on=[
            "item",
            "ppn"
        ],
        how="left"
    )


    # --------------------------------------------------------
    # PPN texte
    # --------------------------------------------------------

    df["ppn"] = (
        df["ppn"]
        .astype("string")
        .str.strip()
    )


    # --------------------------------------------------------
    # Une ligne par PPN
    # --------------------------------------------------------

    df = (
        df
        .drop_duplicates(
            subset=["ppn"]
        )
        .reset_index(
            drop=True
        )
    )


    # --------------------------------------------------------
    # Colonnes finales
    # --------------------------------------------------------

    colonnes = [
        "ppn",
        "NomComplet",
        "Occupations",
        "Raisons",
        "bnf_id",
        "gnd_id"
    ]

    return df[colonnes]


# ============================================================
# FONCTION PRINCIPALE
# ============================================================

def chercher_ppn_geneve():

    debut_total = time.time()


    # ========================================================
    # 1. PPN
    # ========================================================

    df_brut = recuperer_ppn_geneve()


    # ========================================================
    # 2. RAISONS
    # ========================================================

    df_ppn = regrouper_raisons(
        df_brut
    )


    # ========================================================
    # 3. ENRICHISSEMENT
    # ========================================================

    df_enrichi = enrichir_ppn(
        df_ppn,
        taille_lot=TAILLE_LOT
    )


    # ========================================================
    # 4. FINAL
    # ========================================================

    df_final = nettoyer_resultat(
        df_ppn,
        df_enrichi
    )


    # ========================================================
    # STATS
    # ========================================================

    duree = (
        time.time()
        - debut_total
    )

    print()
    print("=" * 60)
    print("TERMINÉ")
    print("=" * 60)

    print(
        f"PPN uniques : "
        f"{df_final['ppn'].nunique():,}"
    )

    print(
        f"Avec nom : "
        f"{df_final['NomComplet'].notna().sum():,}"
    )

    print(
        f"Avec occupations : "
        f"{df_final['Occupations'].notna().sum():,}"
    )

    print(
        f"Avec BnF : "
        f"{df_final['bnf_id'].notna().sum():,}"
    )

    print(
        f"Avec DNB/GND : "
        f"{df_final['gnd_id'].notna().sum():,}"
    )

    print(
        f"Durée totale : "
        f"{duree:.2f} secondes"
    )

    print("=" * 60)

    return df_final