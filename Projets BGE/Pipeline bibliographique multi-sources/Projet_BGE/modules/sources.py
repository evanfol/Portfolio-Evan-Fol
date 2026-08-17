# -*- coding: utf-8 -*-

from concurrent.futures import ThreadPoolExecutor, as_completed

from idref import ajouter_isbn_network_ultra
from sudoc import ajouter_isbn_sudoc_ultra
from dnb import ajouter_isbn_dnb_ultra


def lancer_sources(df):

    print()
    print("=" * 70)
    print("LANCEMENT DES SOURCES PAR AUTEURS")
    print("=" * 70)

    resultats = {}
    erreurs = {}

    # ==========================================================
    # SOURCES
    #
    # BNF volontairement absente :
    # elle est lancée séparément dans pipeline.py.
    # ==========================================================

    traitements = {

        "network": lambda: ajouter_isbn_network_ultra(
            df,
            colonne_ppn="ppn"
        ),

        "sudoc": lambda: ajouter_isbn_sudoc_ultra(
            df,
            colonne_ppn="ppn"
        ),

        "dnb": lambda: ajouter_isbn_dnb_ultra(
            df,
            colonne_gnd="gnd_id"
        ),
    }

    # ==========================================================
    # LANCEMENT PARALLELE
    # ==========================================================

    with ThreadPoolExecutor(
        max_workers=len(traitements)
    ) as executor:

        futures = {
            executor.submit(fonction): nom
            for nom, fonction
            in traitements.items()
        }

        for future in as_completed(futures):

            nom = futures[future]

            try:

                resultat = future.result()

                resultats[nom] = resultat

                print(
                    f"\n✓ {nom.upper()} terminé"
                )

            except Exception as erreur:

                erreurs[nom] = erreur

                print(
                    f"\n✗ {nom.upper()} a échoué : "
                    f"{type(erreur).__name__}: "
                    f"{erreur}"
                )

    # ==========================================================
    # RESUME
    # ==========================================================

    print()
    print("=" * 70)
    print("RÉSUMÉ SOURCES PAR AUTEURS")
    print("=" * 70)

    for nom in [
        "network",
        "sudoc",
        "dnb"
    ]:

        if nom in resultats:

            print(
                f"{nom.upper():<10} : "
                f"{len(resultats[nom]):,} lignes"
            )

        else:

            print(
                f"{nom.upper():<10} : ÉCHEC"
            )

    print("=" * 70)

    return resultats, erreurs