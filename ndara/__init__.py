"""NDARA — l'enquêteur qui va là où personne ne peut aller.

Moteur d'enquête statistique par agent vocal, conçu pour les langues peu
dotées et les lignes 2G.

Conception en une phrase : la chaîne vocale s'achète, la **validité
statistique** se construit. Ce paquet contient la seconde.

Modules
-------
``questionnaire``  libellés FIXES, jamais générés à la volée
``engine``         machine à états de l'entretien, double consentement
``coding``         transcription → modalité (règles, puis LLM à sortie contrainte)
``sampling``       base RDD, strates opérateur, taux de réponse AAPOR
``weighting``      poids, calage sur marges, écrêtement, jackknife
``audit``          auto-contrôle des entretiens, rapport de qualité publié
``corpus``         corpus vocal consenti, expurgé, retirable
``analysis``       estimations publiées avec intervalles et limites
"""

__version__ = "0.1.0"
