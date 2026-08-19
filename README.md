# NDARA

### *l'enquêteur qui va là où personne ne peut aller*

Un agent conversationnel qui mène de **vraies enquêtes statistiques par simple appel
téléphonique** — en français d'Afrique, en khmer, puis en langues nationales. Pas
d'application, pas d'internet, pas de smartphone, **pas besoin de savoir lire**. Un
téléphone à touches suffit.

L'IA appelle, pose le questionnaire, relance quand la réponse est vague, code les
réponses — et rend un **jeu de données pondéré, contrôlé, accompagné de la mesure de
son erreur**.

---

## La thèse, en trois phrases

**La chaîne vocale s'achète. La validité statistique se construit.**
N'importe qui branche un modèle de langage sur une ligne téléphonique en un week-end.
Presque personne ne sait rendre les données **valides** : base de sondage, traitement
de la non-réponse, pondération, calage, contrôle qualité.

Ce dépôt contient la seconde partie.

> Chaque appel produit trois choses : **une réponse** pour le client qui paie,
> **une observation** pour un baromètre permanent, et **une seconde de parole
> annotée** dans une langue qui n'en a presque pas. Le client paie la première.
> Les deux autres s'accumulent.

---

## Démarrage — trente secondes, zéro installation

Aucune dépendance externe. Python 3.11+ et rien d'autre.

```bash
python -m unittest discover -s tests        # 30 tests
python scripts/simulate.py --n 500 --reset  # une vague simulée
python scripts/report.py                    # la note de résultats
python web/server.py                        # http://127.0.0.1:8000
```

* `/` — l'entretien tel qu'il se déroule pendant un appel
* `/dashboard` — terrain, estimations, auto-audit, corpus

---

## Ce qui est déjà branché, et ce qui ne l'est pas

L'interface affiche en permanence l'état réel des composants. **On ne simule jamais
une transcription** : sans clé configurée, le micro enregistre mais la réponse passe
par le clavier ou la saisie, et l'écran le dit.

| Composant | Sans clé | Avec clé |
|---|---|---|
| Moteur d'entretien, filtres, relances | ✅ complet | ✅ |
| Codage des réponses | ✅ règles déterministes | + modèle de langage à **sortie contrainte** |
| Transcription | ⛔ annoncée comme absente | ElevenLabs Scribe / Azure |
| Synthèse vocale | voix du navigateur | Azure `km-KH-SreymomNeural`, `fr-FR-DeniseNeural` |
| Téléphonie | ⛔ dormante | Twilio (TwiML généré) |
| Pondération, calage, jackknife, auto-audit | ✅ complet | ✅ |

Configuration : copier `.env.example`, remplir, exporter. Rien d'autre à changer.

---

## Les quatre décisions de conception qui portent le projet

### 1. Les libellés sont figés et pré-synthétisés

**Le modèle de langage ne rédige jamais une question, ni une relance.** Il ne fait que
*coder* une réponse, et sa sortie est contrainte à une liste de codes autorisés,
vérifiée à la réception.

Ce n'est pas une précaution technique, c'est une exigence méthodologique : un
enquêteur qui reformule introduit un biais d'enquêteur. Conséquence heureuse — une
question = un fichier audio, réutilisé par tous les appels, donc **coût de synthèse
nul en production** et stimulus rigoureusement identique pour tout l'échantillon.

### 2. L'instrument est conçu autour du taux d'erreur réel de la langue

Le meilleur modèle public annonce **20 à 50 % de taux d'erreur mot en khmer**. On ne
construit donc pas un questionnaire à réponses libres : réponses courtes, vocabulaire
fermé, confirmation, et **repli systématique sur les touches** dès la dernière
relance. La validité ne dépend jamais d'une transcription parfaite.

> *La méthode s'adapte à la ressource — pas l'inverse.*

### 3. Le double consentement est dans le code, pas dans la plaquette

```
1. ANNONCE          « je suis une intelligence artificielle »   ← jamais facultatif
2. CONSENTEMENT 1   participer à l'enquête
3. CONSENTEMENT 2   verser l'enregistrement au corpus public    ← refusable sans
                                                                  aucune conséquence
```

Le refus du consentement 2 a **un seul effet technique** : aucun fichier audio n'est
écrit sur le disque. L'entretien continue, l'incitation est identique. Deux tests
automatisés vérifient précisément cela (`test_no_audio_written_without_corpus_consent`,
`test_corpus_refusal_does_not_stop_interview`).

S'y ajoutent : numéros hachés (HMAC-SHA256 salé, le clair ne touche jamais la base),
transcriptions expurgées avant stockage (téléphone, courriel, nom déclaré), questions
sensibles marquées `corpus_eligible: false` et **exclues du corpus même en cas de
consentement**, et un **droit de retrait effectif** — un code, et les fichiers comme
les lignes de manifeste sont supprimés.

### 4. Le système contrôle ses propres entretiens

Chaque jeu de données part avec son **rapport de qualité** : durées anormales,
réponses en ligne droite, taux de « ne sait pas », relances, repli clavier, valeurs
implausibles, incohérences logiques (dont l'ordre de sévérité de l'échelle
alimentaire). L'audit ne produit jamais une accusation : une **priorité de
revérification**.

---

## La chaîne statistique

```
base RDD stratifiée par opérateur
   → poids de sondage et classes de non-réponse
   → calage sur marges (raking / IPF)
   → écrêtement des poids extrêmes
   → estimation
   → intervalle de confiance par jackknife par groupes, avec RECALAGE à chaque réplique
```

**Un point que seuls les statisticiens d'enquête connaissent** : au Cameroun comme au
Cambodge, les préfixes mobiles ne sont pas géographiques. Une base RDD mobile **ne
peut pas** être stratifiée par région. Les strates sont donc les **opérateurs**, la
région est une **question de filtrage**, et la représentativité géographique est
rétablie **a posteriori** par calage. C'est écrit dans le code, pas seulement dans le
dossier.

Et aucune donnée d'abonné n'est utilisée : on tire des numéros au hasard dans les
plages publiées par le régulateur. C'est la réponse à « vous exploitez le fichier
client de l'opérateur ? » — non.

---

## Validation de l'auto-audit

`python scripts/simulate.py --n 900 --sweep --seed 7`

On injecte des entretiens dégradés **à taux connu**, dont deux profils volontairement
difficiles, et on publie ce qui est attrapé — **y compris ce qui ne l'est pas**.

| Profil injecté | Détection |
|---|---|
| Réponses en ligne droite | 100 % |
| Durées impossibles | 100 % |
| Incohérence logique (ordre de sévérité) | 100 % |
| **Accélération discrète** (55 % du temps attendu) | **0 %** |
| **Ligne droite partielle** (3 items sur 5) | **0 %** |
| **Entretiens sains signalés à tort** | **0 %** |

Lecture honnête : **le système attrape la fraude grossière, ne voit pas la fraude
discrète, et n'accuse jamais un entretien sain.** C'est le second chiffre qui décide
de l'adoption — un contrôleur ne rouvre pas un dossier sur une accusation infondée.
Les profils discrets exigent une détection multivariée au niveau du lot, pas de
l'entretien : c'est la feuille de route, pas une promesse.

> ⚠️ Les seuils de l'audit sont fixés par jugement méthodologique, **pas ajustés sur
> la simulation** — les ajuster sur ses propres données synthétiques reviendrait à
> mesurer sa propre invention. Ils devront être recalibrés sur la première vague
> réelle, contre un sous-échantillon réécouté à la main.

---

## Ce qui n'est pas fait, et qui doit l'être

Dit ici plutôt que découvert par un évaluateur :

- **Le questionnaire khmer n'est pas validé** par un locuteur natif. Il est bloqué en
  version `0.9-draft` et l'interface l'affiche comme brouillon. Relecture à demander
  au CADT.
- **Les marges de calage sont provisoires** (`data/margins/cm_margins.json`). À
  remplacer par les marges du RGPH/BUCREP avant toute publication.
- **Les plages de numérotation** doivent être vérifiées auprès du régulateur (ART,
  TRC) avant toute collecte réelle.
- **L'accord de codage n'est pas publiable** tant qu'aucun sous-échantillon n'a été
  recodé à la main. Le rapport le dit à chaque édition, au lieu d'afficher un chiffre
  flatteur.
- **La téléphonie n'a jamais été testée contre un vrai réseau** : l'adaptateur est
  écrit, pas éprouvé.
- **Le corpus est vide** — et il le restera tant qu'il n'y aura pas de parole réelle
  consentie. La simulation ne fabrique aucun audio, par construction : ainsi rien de
  synthétique ne peut contaminer un export.

---

## Coût réel d'un entretien complété (2 min 30)

| | Twilio, Cameroun | Partenariat opérateur |
|---|---|---|
| Minutes voix | ~1,38 $ | ~0,15 $ |
| Transcription | ~0,03 $ | ~0,03 $ |
| Codage | ~0,01 $ | ~0,01 $ |
| Synthèse vocale | ~0 $ *(pré-synthétisée)* | ~0 $ |
| Incitation au répondant | ~0,35 $ | ~0,35 $ |
| **Total** | **≈ 1,80 $** | **≈ 0,55 $** |

Points de comparaison : **20 à 60 $** en face-à-face, **5 à 15 $** en centre d'appels
humain. Le partenariat opérateur divise le coût par plus de trois — ce n'est pas un
logo sur une diapositive, c'est la viabilité du modèle. Et un identifiant d'appelant
local n'est pas un confort : c'est une variable de la qualité statistique, parce qu'un
numéro étranger fait chuter le taux de décrochage.

---

## Arborescence

```
ndara/
├── ndara/
│   ├── questionnaire.py   libellés FIXES, validation stricte au chargement
│   ├── engine.py          machine à états, double consentement, relances
│   ├── coding.py          transcription → modalité (règles, puis LLM contraint)
│   ├── sampling.py        base RDD, strates opérateur, taux AAPOR (RR2/RR3, coopération)
│   ├── weighting.py       poids, calage IPF, écrêtement, effet de plan, jackknife
│   ├── audit.py           auto-contrôle, rapport de qualité, accord de codage (kappa)
│   ├── corpus.py          corpus consenti, expurgation, retrait, fiche descriptive
│   ├── analysis.py        estimations publiées + limites publiées
│   └── providers/         ASR · TTS · téléphonie — tous optionnels
├── data/questionnaires/   prix_denrees_cm (fr/en) · prix_denrees_kh (km/en, brouillon)
├── data/margins/          marges de calage
├── web/                   serveur stdlib + interface entretien + tableau de bord
├── scripts/               simulate · report · build_audio
└── tests/                 30 tests, stdlib
```

---

## Licence et statut

Prototype de recherche. Le **corpus vocal** produit par ce système n'est pas destiné à
la vente : publication prévue sous licence ouverte, copubliée avec l'institution
partenaire du pays d'enquête.
