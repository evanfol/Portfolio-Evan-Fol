# 📚 Automatisation du suivi des auteurs genevois

## 📖 Présentation

Ce projet a été développé dans le cadre d'un stage au sein de la Bibliothèque de Genève afin d'automatiser le suivi des auteurs genevois présents dans les catalogues bibliographiques.

L'objectif est de remplacer un processus manuel de contrôle par une solution automatisée permettant d'importer, comparer et analyser les données provenant de plusieurs services bibliographiques.

Le système détecte automatiquement les nouvelles notices ainsi que les modifications apportées aux auteurs déjà référencés tout en conservant les enrichissements réalisés par les bibliothécaires.

---

# 🎯 Objectifs

- Automatiser l'import des auteurs genevois depuis IdRef.
- Comparer les nouvelles données avec les précédentes importations.
- Identifier automatiquement :
  - les nouveaux auteurs ;
  - les auteurs modifiés.
- Conserver les annotations réalisées par les bibliothécaires.
- Fournir un outil simple d'utilisation pour les équipes métier.

---

# 🛠️ Technologies utilisées

- Microsoft Excel
- VBA
- Power Query (Langage M)
- API SRU
- API Solr IdRef
- XML
- XPath
- Tableaux structurés Excel

---

# ⚙️ Fonctionnement

Le projet suit un véritable processus ETL (Extract – Transform – Load).

## 1. Extraction

Les données sont récupérées automatiquement depuis les services web IdRef grâce à une requête API.

Les informations importées comprennent notamment :

- PPN
- Nom de l'auteur
- Date de naissance
- Date de décès
- Genre
- Date de dernière modification
- Notes

---

## 2. Transformation

Power Query transforme les données XML en tableaux exploitables.

Les traitements comprennent notamment :

- extraction des champs XML ;
- nettoyage des données ;
- conversion des types ;

---

## 3. Chargement

Les données sont automatiquement chargées dans Excel.

Le classeur est ensuite mis à jour sans intervention manuelle.

---

# 🔍 Comparaison automatique

Une macro VBA compare chaque nouvelle importation avec la précédente version.

Le système détecte automatiquement :

- les nouveaux auteurs ;
- les auteurs modifiés ;
- les auteurs inchangés.

Cette comparaison s'appuie principalement sur la date de dernière modification des notices.

---

# 💾 Conservation des données métier

L'une des principales difficultés du projet consistait à conserver les informations ajoutées manuellement par les bibliothécaires.

Lors de chaque actualisation, le système restaure automatiquement :

- les commentaires ;
- les notes internes ;
- les informations de collection ;
- les enrichissements réalisés sur les notices.

Ainsi, aucune donnée métier n'est perdue.

---

# 📊 Indicateurs générés

Après chaque actualisation, plusieurs indicateurs sont calculés automatiquement :

- nombre total d'auteurs ;
- nombre de nouveaux auteurs ;
- nombre de notices modifiées ;
- date de la dernière actualisation ;
- différences entre les localisations Swisscovery et Bibliothèque de Genève.

---

# 📂 Organisation du classeur

Le projet est composé de plusieurs feuilles spécialisées :

| Feuille | Description |
|----------|-------------|
| Source | URL d'import des données |
| Final | Données consolidées |
| Comparatif | Historique des données précédentes |
| Horodatage | Suivi des dates d'actualisation |
| Interactif | Tableau de bord utilisateur |
| Version filtres | Données copiées filtrables sur lesquelles l'utilisateur travaille |

---

# 🚀 Automatisations réalisées

Le projet automatise entièrement :

- actualisation des requêtes Power Query ;
- attente de la fin des traitements asynchrones ;
- comparaison des anciennes et nouvelles données ;
- conservation des commentaires ;
- mise à jour des indicateurs ;
- gestion des erreurs.

L'utilisateur lance une seule macro pour exécuter l'ensemble du processus.

---

# 📈 Résultats

La solution permet :

- de réduire fortement le temps consacré aux vérifications manuelles ;
- de fiabiliser le suivi des auteurs genevois ;
- d'éviter la perte d'informations métier ;
- d'automatiser plusieurs milliers de contrôles en quelques minutes ;
- d'améliorer la qualité des données utilisées par les bibliothécaires.

---

# 🎓 Compétences mobilisées

- Data Analysis
- ETL
- Data Cleaning
- Automatisation de processus
- Power Query
- Langage M
- VBA
- XML
- XPath
- API REST / SRU
- Contrôle qualité des données
- Conception d'outils métier
- Optimisation des performances Excel

---

# 👤 Auteur

**Evan Fol**

Projet réalisé dans le cadre d'un stage à la **Bibliothèque de Genève**.
