# 📚 Pipeline bibliographique multi-sources pour l’aide aux acquisitions

## 📖 Présentation

Ce projet a été développé dans le cadre d'un stage au sein de la **Bibliothèque de Genève** afin de créer un outil automatisé d'aide aux acquisitions.

L'objectif est d'identifier des ouvrages liés à Genève ou à des auteurs genevois susceptibles d'être absents des collections de la Bibliothèque de Genève.

Pour cela, le système interroge et croise automatiquement plusieurs grandes bases bibliographiques afin de récupérer les œuvres associées aux auteurs, leurs identifiants et leurs ISBN, puis de les comparer avec les collections de la bibliothèque.

Le projet repose principalement sur un **pipeline Python automatisé et modulaire**, capable de traiter plusieurs milliers de notices bibliographiques.

---

# 🎯 Objectifs

* Constituer automatiquement une liste d'auteurs liés à Genève.
* Identifier leurs identifiants bibliographiques (PPN).
* Rechercher leurs publications dans plusieurs catalogues.
* Récupérer et normaliser les ISBN disponibles.
* Croiser les résultats provenant de différentes sources.
* Supprimer les doublons entre les bases.
* Comparer les ouvrages identifiés avec les collections de la Bibliothèque de Genève.
* Repérer les ouvrages potentiellement absents des collections.
* Fournir au pôle Acquisitions une liste exploitable pour faciliter la sélection documentaire.

---

# 🛠️ Technologies utilisées

* Python
* Pandas
* Requests
* lxml / XML
* API REST
* API SRU
* API Solr
* SPARQL
* Wikidata
* IdRef
* SUDOC
* Swisscovery
* Bibliothèque nationale de France (BnF)
* Deutsche Nationalbibliothek (DNB / GND)
* Programmation concurrente (`ThreadPoolExecutor`)
* Excel / CSV

---

# ⚙️ Fonctionnement

Le projet repose sur un pipeline de collecte, d'enrichissement et de comparaison des données bibliographiques.

## 1. Identification des auteurs

Une première phase permet d'identifier des personnes liées à Genève à partir de **Wikidata**.

Les informations disponibles sont exploitées afin de constituer une première liste d'auteurs et d'identifiants.

Cette liste constitue le point d'entrée du pipeline bibliographique.

---

## 2. Constitution de la liste de PPN

Les différents identifiants disponibles sont utilisés pour retrouver les **PPN**, identifiants permettant d'interroger les services bibliographiques français et suisses.

Les données provenant de plusieurs sources sont regroupées afin d'obtenir une liste de PPN aussi complète que possible.

---

# 🔎 Recherche bibliographique multi-sources

Une fois les auteurs identifiés, plusieurs moteurs bibliographiques sont interrogés automatiquement.

## Swisscovery / IdRef

Les PPN sont utilisés pour rechercher les notices bibliographiques associées aux auteurs dans le réseau Swisscovery.

Le système récupère notamment :

* ISBN ;
* titre ;
* auteur ;
* année de publication ;
* lieu de publication ;
* éditeur ;
* sujets.

La pagination est automatiquement gérée afin de récupérer l'ensemble des notices lorsqu'un auteur possède plusieurs centaines de références.

---

## SUDOC

Le catalogue SUDOC est interrogé automatiquement via son service **SRU**.

Les notices UNIMARC sont analysées afin d'identifier les publications associées aux auteurs et de récupérer leurs ISBN ainsi que leurs métadonnées bibliographiques.

---

## Bibliothèque nationale de France

Les données de la **BnF** sont également interrogées afin d'enrichir les résultats.

Le système exploite notamment les relations entre identifiants pour retrouver des notices bibliographiques supplémentaires et compléter les ISBN disponibles.

---

## Deutsche Nationalbibliothek

La **DNB / GND** constitue une source supplémentaire permettant d'élargir la recherche, notamment pour les publications présentes dans les catalogues germanophones.

---

# 🔗 Croisement des données

Les résultats provenant des différentes bases sont ensuite fusionnés dans un DataFrame commun.

Le système rapproche les notices grâce à plusieurs identifiants, principalement :

* PPN ;
* ISBN ;
* identifiants BnF ;
* identifiants GND / DNB.

Cette approche permet d'obtenir une vision bibliographique plus complète qu'avec l'utilisation d'une seule base.

---

# 🧹 Normalisation des ISBN

Les ISBN provenant de plusieurs catalogues peuvent être enregistrés sous différentes formes.

Une étape spécifique de normalisation permet notamment de :

* supprimer les espaces ;
* supprimer les tirets ;
* nettoyer les caractères parasites ;
* homogénéiser les formats ;
* faciliter les comparaisons entre bases ;
* éliminer les doublons.

L'ISBN normalisé devient ainsi l'une des principales clés de comparaison du pipeline.

---

# 📚 Comparaison avec les collections de la BGE

Une fois les publications consolidées, les ISBN sont comparés avec les notices présentes dans les collections de la **Bibliothèque de Genève via Swisscovery VGE**.

Le système peut ainsi distinguer :

* les ouvrages déjà présents dans les collections ;
* les ouvrages potentiellement absents ;
* les références nécessitant une vérification complémentaire.

Cette étape transforme les données bibliographiques collectées en véritable **outil d'aide à la décision pour les acquisitions**.

---

# ⚡ Optimisation des performances

Le traitement pouvant nécessiter plusieurs milliers de requêtes réseau, une attention particulière a été portée aux performances.

Le projet utilise notamment :

* exécution parallèle des requêtes ;
* `ThreadPoolExecutor` ;
* sessions HTTP ;
* pagination automatique ;
* gestion des timeouts ;
* mécanismes de retry ;
* backoff en cas d'erreur réseau ;
* traitement par lots ;
* limitation du nombre de requêtes simultanées selon les services.

Les différents moteurs bibliographiques peuvent également être exécutés en parallèle afin de réduire fortement la durée globale du pipeline.

---

# 🛡️ Gestion des erreurs

Les services bibliographiques externes peuvent temporairement être indisponibles ou limiter le nombre de connexions.

Le pipeline intègre donc plusieurs mécanismes de sécurisation :

* nouvelles tentatives automatiques en cas d'échec ;
* gestion des erreurs HTTP ;
* gestion des interruptions de connexion ;
* timeouts ;
* limitation de la concurrence ;
* conservation des résultats déjà obtenus ;
* fichiers intermédiaires permettant de contrôler les différentes étapes.

L'objectif est d'éviter qu'une erreur réseau ponctuelle compromette l'ensemble du traitement.

---

# 🧩 Architecture modulaire

Le projet a été conçu sous forme de plusieurs modules Python spécialisés.

Chaque module possède une responsabilité précise :

* récupération des auteurs et identifiants ;
* génération des PPN ;
* interrogation de Swisscovery ;
* interrogation du SUDOC ;
* interrogation de la BnF ;
* interrogation de la DNB ;
* nettoyage des données ;
* fusion des résultats ;
* comparaison avec les collections BGE.

Cette architecture facilite :

* la maintenance ;
* les tests ;
* l'évolution du projet ;
* l'identification des erreurs ;
* la réutilisation des différentes fonctions.

---

# 📊 Données finales

Le pipeline produit un tableau consolidé contenant notamment :

* PPN ;
* ISBN normalisé ;
* titre ;
* année ;
* auteur ;
* lieu de publication ;
* éditeur ;
* sujet ;
* source bibliographique ;
* critères ayant permis de sélectionner la notice ;
* informations complémentaires sur l'auteur.

Les données peuvent ensuite être exportées vers **Excel** pour être analysées ou utilisées par les équipes métier.

---

# 🚀 Automatisations réalisées

Le projet automatise une chaîne de traitement auparavant difficilement réalisable manuellement :

**Wikidata → identification des auteurs → génération des PPN → interrogation multi-sources → extraction des notices → normalisation des ISBN → fusion → déduplication → comparaison Swisscovery VGE → ouvrages potentiellement absents**

L'utilisateur peut ainsi lancer le pipeline sans devoir effectuer manuellement les recherches dans chaque catalogue.

---

# 📈 Résultats

La solution permet :

* d'interroger automatiquement plusieurs catalogues bibliographiques ;
* de traiter plusieurs milliers d'auteurs et de notices ;
* de centraliser des données provenant de sources hétérogènes ;
* de fiabiliser les comparaisons grâce à la normalisation des identifiants ;
* de réduire considérablement les recherches bibliographiques manuelles ;
* d'identifier des ouvrages potentiellement absents des collections ;
* de fournir une base structurée pour les décisions d'acquisition ;
* de rendre le processus reproductible et actualisable.

---

# 🎓 Compétences mobilisées

* Data Analysis
* Python
* Pandas
* Data Cleaning
* Data Wrangling
* ETL
* API REST
* API SRU
* SPARQL
* XML / UNIMARC / MARCXML
* Manipulation d'identifiants bibliographiques
* Fusion et rapprochement de données
* Déduplication
* Programmation concurrente
* Optimisation des performances
* Gestion des erreurs réseau
* Architecture Python modulaire
* Contrôle qualité des données
* Automatisation de processus
* Conception d'un outil d'aide à la décision

---

# 👤 Auteur

**Evan Fol**

Projet réalisé dans le cadre d'un stage à la **Bibliothèque de Genève**.
