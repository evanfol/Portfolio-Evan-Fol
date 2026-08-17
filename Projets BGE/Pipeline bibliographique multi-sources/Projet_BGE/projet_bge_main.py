# -*- coding: utf-8 -*-

import os
import sys
import asyncio
import argparse


# ============================================================
# CHEMINS DU PROJET
# ============================================================

DOSSIER_PROJET = os.path.dirname(
    os.path.abspath(__file__)
)

DOSSIER_MODULES = os.path.join(
    DOSSIER_PROJET,
    "modules"
)

DOSSIER_OUTPUT = os.path.join(
    DOSSIER_PROJET,
    "output"
)


# ============================================================
# VÉRIFICATION / CRÉATION DES DOSSIERS
# ============================================================

if not os.path.isdir(
    DOSSIER_MODULES
):

    raise FileNotFoundError(
        "Le dossier 'modules' est introuvable : "
        f"{DOSSIER_MODULES}"
    )


os.makedirs(
    DOSSIER_OUTPUT,
    exist_ok=True
)


# ============================================================
# AJOUT DU DOSSIER MODULES AU PATH PYTHON
# ============================================================

if DOSSIER_MODULES not in sys.path:

    sys.path.insert(
        0,
        DOSSIER_MODULES
    )


# ============================================================
# IMPORT PIPELINE
# ============================================================

from pipeline import lancer_pipeline


# ============================================================
# ARGUMENTS DE LIGNE DE COMMANDE
# ============================================================

def lire_arguments():

    parser = argparse.ArgumentParser(
        description="Pipeline Projet BGE"
    )


    parser.add_argument(
        "--test",
        type=int,
        default=None,
        help=(
            "Limite le nombre d'auteurs utilisés "
            "pour les recherches par auteurs. "
            "Exemple : python main.py --test 5"
        )
    )


    return parser.parse_args()


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

async def main():

    args = lire_arguments()


    print()
    print("=" * 80)
    print("PROJET BGE")
    print("=" * 80)

    print(
        f"Dossier projet  : {DOSSIER_PROJET}"
    )

    print(
        f"Dossier modules : {DOSSIER_MODULES}"
    )

    print(
        f"Dossier output  : {DOSSIER_OUTPUT}"
    )

    print()


    # ========================================================
    # MODE TEST
    # ========================================================

    if args.test is not None:

        if args.test <= 0:

            raise ValueError(
                "--test doit être supérieur à 0."
            )


        print(
            f"MODE TEST : "
            f"{args.test} auteurs"
        )

        print()


        resultats = await lancer_pipeline(
            limite_sources=args.test
        )


    # ========================================================
    # MODE COMPLET
    # ========================================================

    else:

        print(
            "MODE COMPLET"
        )

        print()


        resultats = await lancer_pipeline()


    # ========================================================
    # RÉCUPÉRATION DES DATAFRAMES
    # ========================================================

    df_wikidata = resultats.get(
        "wikidata"
    )

    df_ppn_global = resultats.get(
        "ppn_global"
    )

    df_final = resultats.get(
        "final"
    )

    df_absents_vge = resultats.get(
        "absents_vge"
    )

    df_resultat_final = resultats.get(
        "resultat_final"
    )


    # ========================================================
    # RÉSUMÉ
    # ========================================================

    print()
    print("=" * 80)
    print("RÉSUMÉ MAIN")
    print("=" * 80)


    if df_wikidata is not None:

        print(
            f"PPN Wikidata        : "
            f"{len(df_wikidata):,}"
        )


    if df_ppn_global is not None:

        print(
            f"PPN globaux         : "
            f"{len(df_ppn_global):,}"
        )


    if df_final is not None:

        print(
            f"Lignes fusionnées   : "
            f"{len(df_final):,}"
        )


    if df_absents_vge is not None:

        print(
            f"Lignes absentes VGE : "
            f"{len(df_absents_vge):,}"
        )


    if df_resultat_final is not None:

        print(
            f"Résultat final      : "
            f"{len(df_resultat_final):,}"
        )


        if (
            "isbn_normalise"
            in df_resultat_final.columns
        ):

            print(
                f"ISBN finaux uniques : "
                f"{df_resultat_final['isbn_normalise'].nunique():,}"
            )


    print()
    print(
        f"Résultats enregistrés dans : "
        f"{DOSSIER_OUTPUT}"
    )

    print("=" * 80)

    print()
    print(
        "TRAITEMENT TERMINÉ"
    )


    return resultats


# ============================================================
# LANCEMENT
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )