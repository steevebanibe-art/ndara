# Validation de l'auto-audit — prix_denrees_cm v1.0

Vague simulée de 900 numéros tirés, graine 7. Des entretiens dégradés (réponses en ligne droite, durées impossibles, violation de l'ordre de sévérité de l'échelle alimentaire) sont injectés à taux connu.

| Taux injecté | Entretiens | Sensibilité | Fausses alertes | Précision |
|---:|---:|---:|---:|---:|
| 2% | 217 | 60.0% | 0.0% | 100.0% |
| 5% | 209 | 77.8% | 0.0% | 100.0% |
| 10% | 207 | 61.1% | 0.0% | 100.0% |
| 20% | 218 | 66.7% | 0.0% | 100.0% |

## Détection par type de dégradation (taux injecté 20 %)

| Profil injecté | n | Signalés | Taux |
|---|---:|---:|---:|
| sains (fausses alertes) | 185 | 0 | 0.0% |
| incoherent | 8 | 8 | 100.0% |
| partial_straightliner | 5 | 0 | 0.0% |
| speeder | 6 | 6 | 100.0% |
| straightliner | 8 | 8 | 100.0% |
| subtle_speeder | 6 | 0 | 0.0% |

Le taux de fausses alertes est le chiffre qui décide de l'adoption : signaler à tort un entretien sain coûte une revérification inutile et détruit la confiance dans l'outil.
