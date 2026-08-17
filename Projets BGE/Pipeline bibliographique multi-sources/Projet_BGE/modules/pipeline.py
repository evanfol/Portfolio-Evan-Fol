# -*- coding: utf-8 -*-

import os
import asyncio
import pandas as pd

from ppn_wikidata import chercher_ppn_geneve
from ppn_gen_aut import construire_liste_ppn_complete

from sources import lancer_sources

# Ancienne recherche BNF par auteurs
from bnf import ajouter_isbn_bnf_ultra

# Nouvelles recherches Genevensia
from lieupublibnf import recherche_BNF
from lieupublisudoc import recherche_SUDOC
from lieupublidnb import recherche_DNB
from lieupubliidref import recherche_IDREF

from fusion import fusionner_sources
from comparaison_vge import isbn_absents_vge

# ============================================================
# DATES NAISSANCE / DECES IDREF
# ============================================================

from naissance_deces import ajouter_dates_auteurs

# ============================================================
# NETTOYAGE FINAL
# ============================================================

from nettoyage import nettoyage


# ============================================================
# CHEMINS DU PROJET — VERSION PC
#
# Structure attendue :
#
# Projet_BGE/
# ├── main.py
# ├── modules/
# │   ├── pipeline.py
# │   └── ...
# └── output/
# ============================================================

DOSSIER_MODULES = os.path.dirname(
    os.path.abspath(__file__)
)

DOSSIER_PROJET = os.path.dirname(
    DOSSIER_MODULES
)

DOSSIER_OUTPUT = os.path.join(
    DOSSIER_PROJET,
    "output"
)


# ============================================================
# CREATION DU DOSSIER OUTPUT
# ============================================================

os.makedirs(
    DOSSIER_OUTPUT,
    exist_ok=True
)


# ============================================================
# SAUVEGARDE DATAFRAME
# ============================================================

def sauvegarder_dataframe(
    df,
    nom_fichier
):
    """
    Sauvegarde un DataFrame dans le dossier output.
    """

    if df is None:
        return

    if not isinstance(
        df,
        pd.DataFrame
    ):
        return

    chemin = os.path.join(
        DOSSIER_OUTPUT,
        nom_fichier
    )

    df.to_csv(
        chemin,
        index=False
    )


# ============================================================
# DEDOUBLONNAGE FINAL
# ============================================================

def dedoublonner_ppn_isbn(df):
    """
    Déduplique le DataFrame selon les règles suivantes :

    - si le PPN est vide :
        toutes les lignes sont conservées

    - si le PPN est renseigné :
        une seule ligne est conservée pour un même couple
        ppn + isbn_normalise

    - si plusieurs lignes existent pour le même couple
      et qu'une ligne provient de NZ :
        la ligne NZ est prioritaire

    - sinon :
        la première ligne rencontrée est conservée
    """

    # ========================================================
    # VERIFICATIONS
    # ========================================================

    if df is None:
        return df

    if not isinstance(
        df,
        pd.DataFrame
    ):
        raise TypeError(
            f"Objet reçu invalide : {type(df)}"
        )

    if df.empty:
        return df.copy()


    # ========================================================
    # COPIE
    # ========================================================

    df = df.copy()


    # ========================================================
    # COLONNES REQUISES
    # ========================================================

    colonnes_requises = [
        "ppn",
        "isbn_normalise"
    ]

    colonnes_manquantes = [
        colonne
        for colonne in colonnes_requises
        if colonne not in df.columns
    ]

    if colonnes_manquantes:

        raise ValueError(
            "Colonnes manquantes pour le dédoublonnage : "
            f"{colonnes_manquantes}"
        )


    # ========================================================
    # NETTOYAGE PPN
    # ========================================================

    ppn_nettoye = (
        df["ppn"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    masque_ppn_present = (
        ppn_nettoye.ne("")
    )


    # ========================================================
    # LIGNES SANS PPN
    #
    # Elles sont toutes conservées.
    # ========================================================

    df_sans_ppn = (
        df.loc[
            ~masque_ppn_present
        ]
        .copy()
    )


    # ========================================================
    # LIGNES AVEC PPN
    # ========================================================

    df_avec_ppn = (
        df.loc[
            masque_ppn_present
        ]
        .copy()
    )


    # ========================================================
    # AUCUNE LIGNE AVEC PPN
    # ========================================================

    if df_avec_ppn.empty:

        return (
            df_sans_ppn
            .reset_index(drop=True)
        )


    # ========================================================
    # PRIORITE NZ
    #
    # NZ = 0
    # autres sources = 1
    #
    # Donc NZ arrive toujours avant les autres.
    # ========================================================

    if "Source" in df_avec_ppn.columns:

        df_avec_ppn["_priorite_source"] = (
            df_avec_ppn["Source"]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.upper()
            .ne("NZ")
            .astype(int)
        )

    else:

        df_avec_ppn[
            "_priorite_source"
        ] = 1


    # ========================================================
    # ORDRE INITIAL
    #
    # Si aucune ligne NZ n'existe :
    # on conserve simplement la première ligne rencontrée.
    # ========================================================

    df_avec_ppn["_ordre_initial"] = (
        range(
            len(df_avec_ppn)
        )
    )


    # ========================================================
    # TRI
    # ========================================================

    df_avec_ppn = (
        df_avec_ppn
        .sort_values(
            by=[
                "ppn",
                "isbn_normalise",
                "_priorite_source",
                "_ordre_initial"
            ],
            na_position="last"
        )
    )


    # ========================================================
    # DEDOUBLONNAGE PPN + ISBN
    # ========================================================

    df_avec_ppn = (
        df_avec_ppn
        .drop_duplicates(
            subset=[
                "ppn",
                "isbn_normalise"
            ],
            keep="first"
        )
    )


    # ========================================================
    # SUPPRESSION DES COLONNES TECHNIQUES
    # ========================================================

    df_avec_ppn = (
        df_avec_ppn
        .drop(
            columns=[
                "_priorite_source",
                "_ordre_initial"
            ]
        )
    )


    # ========================================================
    # REASSEMBLAGE
    # ========================================================

    df_final = pd.concat(
        [
            df_avec_ppn,
            df_sans_ppn
        ],
        ignore_index=True,
        sort=False
    )

    df_final = (
        df_final
        .reset_index(drop=True)
    )

    return df_final


# ============================================================
# PIPELINE PRINCIPAL
# ============================================================

async def lancer_pipeline(
    limite_sources=None
):

    print()
    print("=" * 80)
    print("PIPELINE BGE")
    print("=" * 80)


    # ========================================================
    # 1. WIKIDATA
    # ========================================================

    print()
    print("ÉTAPE 1/8 — WIKIDATA")
    print()

    df_wikidata = chercher_ppn_geneve()


    # ========================================================
    # 2. GEN_AUT
    # ========================================================

    print()
    print("ÉTAPE 2/8 — GEN_AUT")
    print()

    df_ppn_global = (
        construire_liste_ppn_complete(
            df_wikidata
        )
    )


    # ========================================================
    # MODE TEST OPTIONNEL
    # ========================================================

    df_ppn_sources = (
        df_ppn_global
    )

    if limite_sources is not None:

        df_ppn_sources = (
            df_ppn_global
            .head(
                limite_sources
            )
            .copy()
        )

        print()
        print(
            f"MODE TEST : "
            f"{len(df_ppn_sources):,} auteurs utilisés "
            f"pour les recherches par auteurs"
        )


    # ========================================================
    # 3. SOURCES PAR AUTEURS
    # ========================================================

    print()
    print(
        "ÉTAPE 3/8 — SOURCES PAR AUTEURS"
    )
    print()

    print(
        "Lancement parallèle : "
        "NETWORK / SUDOC / DNB / BNF"
    )


    # --------------------------------------------------------
    # NETWORK / SUDOC / DNB
    # --------------------------------------------------------

    tache_autres_sources = (
        asyncio.to_thread(
            lancer_sources,
            df_ppn_sources
        )
    )


    # --------------------------------------------------------
    # BNF PAR AUTEURS
    # --------------------------------------------------------

    tache_bnf_auteurs = (
        asyncio.to_thread(
            ajouter_isbn_bnf_ultra,
            df_ppn_sources,
            "bnf_id"
        )
    )


    # --------------------------------------------------------
    # EXECUTION SIMULTANEE
    # --------------------------------------------------------

    (
        resultat_autres_sources,
        df_bnf_auteurs
    ) = await asyncio.gather(
        tache_autres_sources,
        tache_bnf_auteurs
    )


    # --------------------------------------------------------
    # RECUPERATION RESULTATS
    # --------------------------------------------------------

    (
        resultats_sources,
        erreurs_sources
    ) = resultat_autres_sources


    # --------------------------------------------------------
    # AJOUT BNF
    # --------------------------------------------------------

    resultats_sources[
        "bnf"
    ] = df_bnf_auteurs


    # --------------------------------------------------------
    # ERREURS BNF
    # --------------------------------------------------------

    erreurs_bnf = (
        df_bnf_auteurs
        .attrs
        .get(
            "erreurs_bnf",
            []
        )
    )

    erreurs_sources[
        "bnf"
    ] = erreurs_bnf


    # ========================================================
    # SAUVEGARDE SOURCES PAR AUTEURS
    # ========================================================

    print()
    print(
        "Sauvegarde des sources par auteurs..."
    )

    for (
        nom,
        df_source
    ) in resultats_sources.items():

        if not isinstance(
            df_source,
            pd.DataFrame
        ):
            continue


    # --------------------------------------------------------
    # SAUVEGARDE ERREURS BNF
    # --------------------------------------------------------

    if erreurs_bnf:

        df_erreurs_bnf = (
            pd.DataFrame(
                erreurs_bnf
            )
        )


    # ========================================================
    # 4. RECHERCHES GENEVENSIA
    # ========================================================

    print()
    print("=" * 80)
    print(
        "ÉTAPE 4/8 — RECHERCHES GENEVENSIA"
    )
    print("=" * 80)
    print()

    print(
        "Lancement parallèle : "
        "BNF / SUDOC / DNB / IDREF"
    )


    # --------------------------------------------------------
    # BNF
    # --------------------------------------------------------

    tache_lieu_bnf = (
        asyncio.to_thread(
            recherche_BNF
        )
    )


    # --------------------------------------------------------
    # SUDOC
    # --------------------------------------------------------

    tache_lieu_sudoc = (
        asyncio.to_thread(
            recherche_SUDOC
        )
    )


    # --------------------------------------------------------
    # DNB
    # --------------------------------------------------------

    tache_lieu_dnb = (
        asyncio.to_thread(
            recherche_DNB
        )
    )


    # --------------------------------------------------------
    # IDREF
    # Fonction déjà asynchrone
    # --------------------------------------------------------

    tache_lieu_idref = (
        recherche_IDREF()
    )


    # --------------------------------------------------------
    # EXECUTION SIMULTANEE
    # --------------------------------------------------------

    (
        df_lieu_bnf,
        df_lieu_sudoc,
        df_lieu_dnb,
        df_lieu_idref
    ) = await asyncio.gather(
        tache_lieu_bnf,
        tache_lieu_sudoc,
        tache_lieu_dnb,
        tache_lieu_idref
    )


    # ========================================================
    # RESULTATS LIEU
    # ========================================================

    resultats_lieu = {

        "bnf":
            df_lieu_bnf,

        "idref":
            df_lieu_idref,

        "sudoc":
            df_lieu_sudoc,

        "dnb":
            df_lieu_dnb
    }


    # ========================================================
    # SAUVEGARDE RECHERCHES GENEVENSIA
    # ========================================================

    print()
    print(
        "Sauvegarde des recherches Genevensia..."
    )

    for (
        nom,
        df_source
    ) in resultats_lieu.items():

        if not isinstance(
            df_source,
            pd.DataFrame
        ):
            continue


    # ========================================================
    # RESUME GENEVENSIA
    # ========================================================

    print()
    print("=" * 70)
    print("RÉSUMÉ GENEVENSIA")
    print("=" * 70)

    for nom in [
        "bnf",
        "idref",
        "sudoc",
        "dnb"
    ]:

        df_source = (
            resultats_lieu.get(
                nom
            )
        )

        if (
            isinstance(
                df_source,
                pd.DataFrame
            )
            and
            not df_source.empty
        ):

            print(
                f"{nom.upper():<10} : "
                f"{len(df_source):,} lignes"
            )

        elif isinstance(
            df_source,
            pd.DataFrame
        ):

            print(
                f"{nom.upper():<10} : "
                f"0 ligne"
            )

        else:

            print(
                f"{nom.upper():<10} : "
                f"ABSENT"
            )

    print("=" * 70)


    # ========================================================
    # 5. FUSION
    # ========================================================

    print()
    print("ÉTAPE 5/8 — FUSION")
    print()

    df_final = fusionner_sources(
        resultats=resultats_sources,
        resultats_lieu=resultats_lieu
    )


    # ========================================================
    # 6. COMPARAISON VGE
    # ========================================================

    print()
    print(
        "ÉTAPE 6/8 — COMPARAISON VGE"
    )
    print()

    # IMPORTANT :
    #
    # On ne force plus batch_size=20 / max_concurrent=20.
    #
    # comparaison_vge.py utilise désormais directement :
    #
    # batch_size      = 175
    # max_concurrent  = 12
    # page_size       = 50
    #
    # Ces valeurs ont été validées par benchmark A/B.

    df_absents_vge = (
        await isbn_absents_vge(
            df_final
        )
    )


    # ========================================================
    # DEDOUBLONNAGE APRES COMPARAISON VGE
    # ========================================================

    nombre_avant_dedoublonnage = (
        len(
            df_absents_vge
        )
    )

    df_absents_vge = (
        dedoublonner_ppn_isbn(
            df_absents_vge
        )
    )

    nombre_apres_dedoublonnage = (
        len(
            df_absents_vge
        )
    )


    print()
    print("=" * 70)
    print("DEDOUBLONNAGE FINAL")
    print("=" * 70)

    print(
        f"Lignes avant      : "
        f"{nombre_avant_dedoublonnage:,}"
    )

    print(
        f"Lignes après      : "
        f"{nombre_apres_dedoublonnage:,}"
    )

    print(
        f"Lignes supprimées : "
        f"{nombre_avant_dedoublonnage - nombre_apres_dedoublonnage:,}"
    )

    print("=" * 70)


    # ========================================================
    # 7. DATES DE NAISSANCE ET DE DECES
    # ========================================================

    print()
    print("=" * 80)
    print(
        "ÉTAPE 7/8 — DATES DE NAISSANCE / DECES"
    )
    print("=" * 80)
    print()


    # --------------------------------------------------------
    # Nombre de PPN exploitables
    # --------------------------------------------------------

    if "ppn" in df_absents_vge.columns:

        nombre_ppn_dates = (
            df_absents_vge["ppn"]
            .dropna()
            .astype(str)
            .str.strip()
        )

        nombre_ppn_dates = (
            nombre_ppn_dates[
                nombre_ppn_dates.ne("")
            ]
            .nunique()
        )

    else:

        nombre_ppn_dates = 0


    print(
        f"PPN uniques susceptibles d'être interrogés : "
        f"{nombre_ppn_dates:,}"
    )

    print()


    # --------------------------------------------------------
    # Récupération SPARQL IdRef
    #
    # IMPORTANT :
    # - seulement les PPN présents
    # - PPN dédupliqués dans Naissance_deces.py
    # - traitement par lots
    # - uniquement les années sont conservées
    # --------------------------------------------------------

    df_avec_dates = (
        ajouter_dates_auteurs(
            df_absents_vge,
            taille_lot=100,
            timeout=30,
            afficher_progression=True
        )
    )


    # --------------------------------------------------------
    # Statistiques dates trouvées
    # --------------------------------------------------------

    nombre_naissances = 0
    nombre_deces = 0

    if (
        "date_naissance"
        in df_avec_dates.columns
    ):

        nombre_naissances = (
            df_avec_dates[
                "date_naissance"
            ]
            .notna()
            .sum()
        )

    if (
        "date_deces"
        in df_avec_dates.columns
    ):

        nombre_deces = (
            df_avec_dates[
                "date_deces"
            ]
            .notna()
            .sum()
        )


    print()
    print("=" * 70)
    print("DATES AUTEURS IDREF")
    print("=" * 70)

    print(
        f"Naissances renseignées : "
        f"{nombre_naissances:,}"
    )

    print(
        f"Décès renseignés       : "
        f"{nombre_deces:,}"
    )

    print("=" * 70)


    # ========================================================
    # 8. NETTOYAGE FINAL
    # ========================================================

    print()
    print("=" * 80)
    print(
        "ÉTAPE 8/8 — NETTOYAGE FINAL"
    )
    print("=" * 80)
    print()


    df_resultat_final = nettoyage(
        df_avec_dates,
        DOSSIER_OUTPUT
    )


    # ========================================================
    # RESULTATS FINAUX
    # ========================================================

    print()
    print("=" * 80)
    print("PIPELINE TERMINÉ")
    print("=" * 80)


    # --------------------------------------------------------
    # WIKIDATA
    # --------------------------------------------------------

    print(
        f"PPN Wikidata              : "
        f"{len(df_wikidata):,}"
    )


    # --------------------------------------------------------
    # PPN GLOBAL
    # --------------------------------------------------------

    print(
        f"PPN globaux               : "
        f"{len(df_ppn_global):,}"
    )


    # --------------------------------------------------------
    # BNF PAR AUTEURS
    # --------------------------------------------------------

    print(
        f"BNF auteurs — lignes      : "
        f"{len(df_bnf_auteurs):,}"
    )

    if (
        "isbn_normalise"
        in df_bnf_auteurs.columns
    ):

        print(
            f"BNF auteurs — ISBN uniques: "
            f"{df_bnf_auteurs['isbn_normalise'].nunique():,}"
        )

    elif (
        "isbn"
        in df_bnf_auteurs.columns
    ):

        print(
            f"BNF auteurs — ISBN uniques: "
            f"{df_bnf_auteurs['isbn'].nunique():,}"
        )

    print(
        f"BNF auteurs — erreurs     : "
        f"{len(erreurs_bnf):,}"
    )


    # --------------------------------------------------------
    # GENEVENSIA
    # --------------------------------------------------------

    print()
    print("Genevensia :")

    for (
        nom,
        df_source
    ) in resultats_lieu.items():

        if isinstance(
            df_source,
            pd.DataFrame
        ):

            print(
                f"  {nom.upper():<8} : "
                f"{len(df_source):,} lignes"
            )


    # --------------------------------------------------------
    # FUSION
    # --------------------------------------------------------

    print()

    print(
        f"Lignes fusionnées         : "
        f"{len(df_final):,}"
    )

    if (
        "isbn_normalise"
        in df_final.columns
    ):

        print(
            f"ISBN fusionnés uniques    : "
            f"{df_final['isbn_normalise'].nunique():,}"
        )


    # --------------------------------------------------------
    # ABSENTS VGE
    # --------------------------------------------------------

    print(
        f"Lignes absentes VGE       : "
        f"{len(df_absents_vge):,}"
    )

    if (
        "isbn_normalise"
        in df_absents_vge.columns
    ):

        print(
            f"ISBN absents uniques      : "
            f"{df_absents_vge['isbn_normalise'].nunique():,}"
        )


    # --------------------------------------------------------
    # DATES
    # --------------------------------------------------------

    print(
        f"Dates naissance trouvées  : "
        f"{nombre_naissances:,}"
    )

    print(
        f"Dates décès trouvées      : "
        f"{nombre_deces:,}"
    )


    # --------------------------------------------------------
    # RESULTAT FINAL NETTOYE
    # --------------------------------------------------------

    print(
        f"Lignes résultat final     : "
        f"{len(df_resultat_final):,}"
    )

    if (
        "isbn_normalise"
        in df_resultat_final.columns
    ):

        print(
            f"ISBN finaux uniques       : "
            f"{df_resultat_final['isbn_normalise'].nunique():,}"
        )


    # --------------------------------------------------------
    # Vérification colonnes dates dans résultat final
    # --------------------------------------------------------

    if (
        "date_naissance"
        in df_resultat_final.columns
    ):

        print(
            f"Naissances résultat final : "
            f"{df_resultat_final['date_naissance'].notna().sum():,}"
        )

    else:

        print(
            "ATTENTION : colonne date_naissance absente "
            "du résultat final."
        )


    if (
        "date_deces"
        in df_resultat_final.columns
    ):

        print(
            f"Décès résultat final      : "
            f"{df_resultat_final['date_deces'].notna().sum():,}"
        )

    else:

        print(
            "ATTENTION : colonne date_deces absente "
            "du résultat final."
        )


    print()

    print(
        "Fichier final             : "
        f"{os.path.join(DOSSIER_OUTPUT, 'resultat_final.csv')}"
    )

    print("=" * 80)


    # ========================================================
    # RETURN
    # ========================================================

    return {

        # ----------------------------------------------------
        # PREPARATION
        # ----------------------------------------------------

        "wikidata":
            df_wikidata,

        "ppn_global":
            df_ppn_global,


        # ----------------------------------------------------
        # SOURCES PAR AUTEURS
        # ----------------------------------------------------

        "sources":
            resultats_sources,

        "erreurs_sources":
            erreurs_sources,

        "bnf":
            df_bnf_auteurs,

        "erreurs_bnf":
            erreurs_bnf,


        # ----------------------------------------------------
        # GENEVENSIA
        # ----------------------------------------------------

        "sources_lieu":
            resultats_lieu,

        "lieu_bnf":
            df_lieu_bnf,

        "lieu_idref":
            df_lieu_idref,

        "lieu_sudoc":
            df_lieu_sudoc,

        "lieu_dnb":
            df_lieu_dnb,


        # ----------------------------------------------------
        # RESULTATS INTERMEDIAIRES
        # ----------------------------------------------------

        "final":
            df_final,

        "absents_vge":
            df_absents_vge,

        "absents_vge_avec_dates":
            df_avec_dates,


        # ----------------------------------------------------
        # RESULTAT FINAL NETTOYE
        # ----------------------------------------------------

        "resultat_final":
            df_resultat_final
    }