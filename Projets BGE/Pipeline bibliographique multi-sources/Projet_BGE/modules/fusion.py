# -*- coding: utf-8 -*-

import pandas as pd


# ==============================================================
# COLONNES COMMUNES AUX RESULTATS "LIEU"
# ==============================================================

COLONNES_LIEU = [
    "ppn",
    "isbn_normalise",
    "titre",
    "annee",
    "nomAuteur",
    "lieuPublication",
    "editeur",
    "sujet",
    "raisons",
    "Source",
    "TypeRecherche"
]


# ==============================================================
# PREPARATION D'UN DATAFRAME
# ==============================================================

def preparer_dataframe(
    df,
    source=None,
    type_recherche=None
):
    """
    Uniformise un DataFrame avant concaténation.

    Paramètres
    ----------
    df : pandas.DataFrame
        DataFrame à préparer.

    source : str ou None
        Source bibliographique :
        NZ / SUDOC / BNF / DNB / IDREF

    type_recherche : str ou None
        Type de recherche :
        Auteur / Genevensia
    """

    # ==========================================================
    # DATAFRAME ABSENT
    # ==========================================================

    if df is None:
        return None


    # ==========================================================
    # VERIFICATION TYPE
    # ==========================================================

    if not isinstance(
        df,
        pd.DataFrame
    ):

        raise TypeError(
            f"Objet reçu invalide : {type(df)}"
        )


    # ==========================================================
    # DATAFRAME VIDE
    # ==========================================================

    if df.empty:
        return None


    # ==========================================================
    # COPIE
    # ==========================================================

    df = df.copy()


    # ==========================================================
    # UNIFORMISATION DES NOMS DE COLONNES
    # ==========================================================

    renommages = {

        "ISBN_normalise":
            "isbn_normalise",

        "isbnNormalise":
            "isbn_normalise",

        "Sources":
            "Source",

        "source":
            "Source"
    }

    df = df.rename(
        columns=renommages
    )


    # ==========================================================
    # SOURCE
    # ==========================================================

    if source is not None:

        df["Source"] = source


    # ==========================================================
    # TYPE DE RECHERCHE
    # ==========================================================

    if type_recherche is not None:

        df["TypeRecherche"] = (
            type_recherche
        )


    # ==========================================================
    # TYPES TEXTE
    # ==========================================================

    colonnes_texte = [

        "ppn",

        "isbn",

        "isbn_normalise",

        "titre",

        "nomAuteur",

        "lieuPublication",

        "editeur",

        "sujet",

        "raisons",

        "Source",

        "TypeRecherche"
    ]


    for colonne in colonnes_texte:

        if colonne in df.columns:

            df[colonne] = (
                df[colonne]
                .astype("string")
            )


    # ==========================================================
    # ANNEE
    # ==========================================================

    if "annee" in df.columns:

        df["annee"] = (
            pd.to_numeric(
                df["annee"],
                errors="coerce"
            )
            .astype("Int64")
        )


    # ==========================================================
    # NETTOYAGE ISBN
    #
    # Important :
    # on ne convertit PAS ici l'ISBN.
    # Les modules doivent déjà avoir produit isbn_normalise.
    # ==========================================================

    if "isbn_normalise" in df.columns:

        df["isbn_normalise"] = (
            df["isbn_normalise"]
            .str.strip()
        )


    # ==========================================================
    # NETTOYAGE PPN
    # ==========================================================

    if "ppn" in df.columns:

        df["ppn"] = (
            df["ppn"]
            .str.strip()
        )


    # ==========================================================
    # NETTOYAGE SOURCE
    # ==========================================================

    if "Source" in df.columns:

        df["Source"] = (
            df["Source"]
            .str.strip()
        )


    return df


# ==============================================================
# FUSION DES SOURCES
# ==============================================================

def fusionner_sources(
    resultats,
    resultats_lieu=None
):
    """
    Fusionne deux familles de résultats :

    1. Anciennes recherches par auteurs :
       - NETWORK
       - SUDOC
       - BNF
       - DNB

    2. Nouvelles recherches Genevensia :
       - BNF
       - IDREF
       - SUDOC
       - DNB
    """

    sources = []


    # ==========================================================
    # 1. ANCIENNES SOURCES PAR AUTEURS
    # ==========================================================

    correspondance = {

        "network":
            "NZ",

        "sudoc":
            "SUDOC",

        "bnf":
            "BNF",

        "dnb":
            "DNB"
    }


    for cle, nom_source in correspondance.items():

        df = resultats.get(
            cle
        )


        df = preparer_dataframe(
            df,
            source=nom_source,
            type_recherche="Auteur"
        )


        if df is not None:

            sources.append(
                df
            )


    # ==========================================================
    # 2. NOUVELLES RECHERCHES GENEVENSIA
    # ==========================================================

    if resultats_lieu is not None:

        correspondance_lieu = {

            "bnf":
                "BNF",

            "idref":
                "IDREF",

            "sudoc":
                "SUDOC",

            "dnb":
                "DNB"
        }


        for cle, nom_source in correspondance_lieu.items():

            df = resultats_lieu.get(
                cle
            )


            df = preparer_dataframe(
                df,
                source=nom_source,
                type_recherche="Genevensia"
            )


            if df is not None:

                sources.append(
                    df
                )


    # ==========================================================
    # AUCUN RESULTAT
    # ==========================================================

    if not sources:

        return pd.DataFrame()


    # ==========================================================
    # CONCATENATION
    # ==========================================================

    df_final = pd.concat(
        sources,
        ignore_index=True,
        sort=False
    )


    # ==========================================================
    # TYPES APRES CONCATENATION
    # ==============================================================

    colonnes_texte = [

        "ppn",

        "isbn",

        "isbn_normalise",

        "titre",

        "nomAuteur",

        "lieuPublication",

        "editeur",

        "sujet",

        "raisons",

        "Source",

        "TypeRecherche"
    ]


    for colonne in colonnes_texte:

        if colonne in df_final.columns:

            df_final[colonne] = (
                df_final[colonne]
                .astype("string")
            )


    if "annee" in df_final.columns:

        df_final["annee"] = (
            pd.to_numeric(
                df_final["annee"],
                errors="coerce"
            )
            .astype("Int64")
        )


    # ==========================================================
    # SUPPRESSION DES DOUBLONS STRICTS
    #
    # IMPORTANT :
    #
    # On ne dédoublonne PAS uniquement sur ISBN.
    #
    # Exemple :
    #
    # ISBN X
    # BNF Auteur
    #
    # ISBN X
    # BNF Genevensia
    #
    # doivent rester deux lignes différentes.
    #
    # Source + TypeRecherche permettent de conserver
    # la provenance.
    # ==========================================================

    df_final = (
        df_final
        .drop_duplicates()
        .reset_index(
            drop=True
        )
    )


    # ==========================================================
    # ORDRE DES COLONNES
    # ==========================================================

    colonnes_prioritaires = [

        "ppn",

        "isbn_normalise",

        "titre",

        "annee",

        "nomAuteur",

        "lieuPublication",

        "editeur",

        "sujet",

        "raisons",

        "Source",

        "TypeRecherche"
    ]


    colonnes_prioritaires = [

        colonne

        for colonne
        in colonnes_prioritaires

        if colonne
        in df_final.columns
    ]


    autres_colonnes = [

        colonne

        for colonne
        in df_final.columns

        if colonne
        not in colonnes_prioritaires
    ]


    df_final = df_final[

        colonnes_prioritaires

        +

        autres_colonnes
    ]


    # ==========================================================
    # TRI
    # ==========================================================

    colonnes_tri = [

        colonne

        for colonne in [

            "ppn",

            "isbn_normalise",

            "Source",

            "TypeRecherche"

        ]

        if colonne
        in df_final.columns
    ]


    if colonnes_tri:

        df_final = (
            df_final
            .sort_values(
                colonnes_tri,
                na_position="last"
            )
            .reset_index(
                drop=True
            )
        )


    # ==========================================================
    # RESUME
    # ==========================================================

    print()
    print("=" * 70)
    print("FUSION DE TOUTES LES SOURCES")
    print("=" * 70)


    # ----------------------------------------------------------
    # LIGNES
    # ----------------------------------------------------------

    print(
        f"Lignes finales : "
        f"{len(df_final):,}"
    )


    # ----------------------------------------------------------
    # ISBN
    # ----------------------------------------------------------

    if "isbn_normalise" in df_final.columns:

        print(
            f"ISBN uniques   : "
            f"{df_final['isbn_normalise'].nunique():,}"
        )


    # ----------------------------------------------------------
    # SOURCE
    # ----------------------------------------------------------

    if "Source" in df_final.columns:

        print()
        print(
            "Répartition par source :"
        )

        print(
            df_final[
                "Source"
            ]
            .value_counts(
                dropna=False
            )
            .to_string()
        )


    # ----------------------------------------------------------
    # TYPE DE RECHERCHE
    # ----------------------------------------------------------

    if "TypeRecherche" in df_final.columns:

        print()
        print(
            "Répartition par type de recherche :"
        )

        print(
            df_final[
                "TypeRecherche"
            ]
            .value_counts(
                dropna=False
            )
            .to_string()
        )


    # ----------------------------------------------------------
    # SOURCE + TYPE
    # ----------------------------------------------------------

    if (
        "Source" in df_final.columns
        and
        "TypeRecherche" in df_final.columns
    ):

        print()
        print(
            "Répartition Source / Type :"
        )

        resume_source_type = (

            df_final

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
        )


        print(
            resume_source_type
            .to_string()
        )


    print("=" * 70)


    # ==========================================================
    # RETURN
    # ==========================================================

    return df_final