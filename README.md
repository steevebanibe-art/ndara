---
title: NDARA
emoji: "📞"
colorFrom: gray
colorTo: red
sdk: docker
app_port: 7860
pinned: false
license: other
short_description: Enquetes statistiques par simple appel telephonique
---

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
python -m unittest discover -s tests        # 115 tests
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

## La vague omnibus : plusieurs clients, un seul appel

Un client achète un créneau d'une à trois questions dans la vague du mois.
Toutes les questions achetées sont posées pendant le même appel, à la suite du
tronc commun, et chaque client ne reçoit que ses réponses à lui.

Ce n'est pas une idée neuve : c'est le modèle qui fait vivre les instituts de
sondage depuis quarante ans. Ce qui est neuf, c'est de l'apporter automatisé et
vocal là où il n'existe pas. Sa raison d'être tient en une ligne : **le coût
d'un appel se partage, celui d'une question de plus ne se partage pas.**

Trois décisions, et chacune répond à une objection.

**Un créneau qui ne tient pas dans l'appel est refusé avant la vente**, avec le
nombre de secondes restantes et la correction à faire. Au-delà de deux minutes
trente, l'abandon dégrade les données de tous les clients de la vague, pas
seulement celles du dernier arrivé.

**L'ordre des créneaux tourne d'un appel à l'autre.** Le dernier bloc d'un
questionnaire est répondu plus vite, plus souvent par « ne sait pas », et par
un échantillon déjà amputé de ceux qui ont raccroché. Vendre la dernière place
au même prix que la première ne se défend pas. La rotation est cyclique : sur k
appels, chaque bloc occupe chaque position exactement une fois, et sa position
est enregistrée avec l'entretien pour qu'un effet de position reste
contrôlable. Limite connue : une rotation cyclique équilibre les positions,
pas les voisinages ; un plan de Williams le ferait, il n'est pas fait.

**Le tronc commun passe en premier**, à rebours de l'usage en face-à-face. Au
téléphone l'appel peut se couper à n'importe quelle seconde, et un entretien
sans ses variables de calage ne se pondère pas : il est perdu pour tous les
clients, y compris ceux dont les questions avaient déjà été posées.

### Ce que le calcul dit du modèle économique

Vague de démonstration, 137 secondes d'appel, trois commanditaires, six
questions vendues à 500 dollars, 3 000 entretiens aboutis :

| | Twilio, Cameroun | Accord opérateur |
|---|---|---|
| Coût par entretien | 2,79 $ | 0,86 $ |
| Coût de la vague | 8 363 $ | 2 571 $ |
| Recette | 3 000 $ | 3 000 $ |
| **Marge** | **moins 5 363 $** | **429 $** |
| Une question de plus (10 s) | 394 $ | 76 $ |

La dernière ligne est celle qui décide. Une question de plus dans une vague
déjà lancée n'ajoute ni incitation, ni quote-part d'appels échoués, ni
recrutement : elle n'ajoute que des secondes de voix. À tarif de gros public
ces secondes coûtent presque autant que la question se vend ; sous accord
opérateur elles ne coûtent presque rien. **C'est là, et nulle part ailleurs,
que l'omnibus devient un modèle économique**, et c'est pourquoi l'accord de
minutes n'est pas un confort mais la condition de viabilité.

La section « La vague du mois, et qui la paie » du tableau de bord affiche ces
chiffres, calculés et non recopiés.

---

## L'appel, tel qu'il s'entend

Trois réglages décident si un appel automatisé passe pour un entretien ou pour
un robot. Aucun n'est visible dans une capture d'écran, et tous les trois
s'entendent au premier appel.

**On peut couper une question, jamais un consentement.** La lecture de la
question se fait *dans* l'écoute : le répondant peut répondre avant la fin,
comme il le ferait avec un enquêteur. Sans cela, l'écoute ne s'ouvre qu'une
fois la phrase terminée, la première syllabe de la réponse se perd, et chaque
tour porte un blanc qui trahit la machine. Sur l'annonce d'intelligence
artificielle et sur les deux consentements, c'est l'inverse et ce n'est pas
négociable : un « oui » lâché à la moitié d'une phrase n'est pas un
consentement, donc la phrase se dit en entier avant que l'écoute s'ouvre.

**La relance précède la question qu'elle relance, et garde la même voix.**
« Je n'ai pas bien compris », puis la question à nouveau, dans la voix de
studio. Les relances font partie des libellés pré-synthétisés : les faire dire
par la voix de secours du canal reviendrait à changer de locuteur à l'instant
précis où le répondant hésite déjà.

**La reconnaissance sait ce qu'elle doit entendre.** Le moteur connaît les
seules réponses recevables, ce sont les modalités de la question, et il les
passe en indices. C'est gratuit et c'est là que la reconnaissance se trompe le
plus : sur les noms de lieux et les mots régionaux. Le modèle est celui des
réponses brèves, pas celui de la dictée, et le filtre de grossièretés est
désactivé parce qu'une réponse d'enquête censurée en astérisques n'est plus
une donnée.

### Le répondeur, qui est un poste de coût avant d'être un cas statistique

La détection de répondeur ne bloque pas l'appel. En mode bloquant, l'opérateur
retient la ligne le temps de décider si c'est une machine qui a décroché, et
pendant ce verdict la personne dit « allô » dans le vide. Le premier contact
est alors un silence, et c'est ce qui fait dire d'un dispositif qui marche
qu'il ne marche pas.

Le verdict arrive donc de façon asynchrone, sur sa propre route. S'il dit
« machine », l'entretien est classé non-contact et **l'appel est raccroché** :
sans cela un répondeur écoute deux minutes trente de questionnaire, facturées
comme un entretien. Sa route est séparée de celle de la fin d'appel, sans quoi
le compteur d'appels simultanés serait libéré deux fois et la campagne
composerait plus de numéros que son plafond.

### Éprouver tout cela sans compte et sans dépenser une minute

```bash
TWILIO_AUTH_TOKEN=jeton_de_test_1234567890 NDARA_PUBLIC_URL=http://127.0.0.1:8170   python web/server.py --port 8170
python scripts/appel_simule.py http://127.0.0.1:8170 http://127.0.0.1:8170
```

Le script rejoue l'opérateur, signatures HMAC comprises : requête forgée
refusée, appel décroché, entretien complet tour par tour, répondeur détecté en
cours d'appel, dispositions posées. Ce qu'il affiche entre parenthèses,
« coupable », dit quels tours acceptent d'être interrompus.

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

Tarifs voix relevés **sur le compte Twilio lui-même** le 25 août 2026, et non sur la
page tarifaire publique. Ce ne sont pas des prix uniques mais des fourchettes, parce que
la facturation dépend de l'opérateur qui termine l'appel, et qu'on ne le choisit pas :

| Destination | Tarif à la minute |
|---|---|
| Cameroun `+237` | de **0,410 à 0,787 $** |
| Cambodge `+855` | de **0,112 à 0,132 $** |

Tout ce qui suit retient le **haut** de chaque fourchette. Une économie unitaire qu'on
présente à un jury doit se tromper du côté qui coûte, jamais du côté qui arrange, et la
conclusion ne doit pas dépendre de l'endroit où l'on se place dans la plage.
`tests/test_omnibus.py` refait le calcul au tarif bas et vérifie qu'elle tient.

| | Twilio, Cameroun | Twilio, Cambodge | Partenariat opérateur |
|---|---|---|---|
| Minutes voix (2 min 30) | **1,97 $** | **0,33 $** | ~0,38 $ *(hypothèse)* |
| Transcription | ~0,03 $ | ~0,03 $ | ~0,03 $ |
| Codage | ~0,01 $ | ~0,01 $ | ~0,01 $ |
| Synthèse vocale | 0 $ *(pré-synthétisée)* | 0 $ | 0 $ |
| Incitation au répondant | ~0,35 $ | ~0,35 $ | ~0,35 $ |
| Quote-part des appels qui n'aboutissent pas | ~0,60 $ | ~0,60 $ | ~0,12 $ |
| **Coût par entretien complété** | **≈ 3,00 $** | **≈ 1,32 $** | **≈ 0,90 $** |

La colonne cambodgienne reprend telle quelle la part fixe camerounaise, faute de donnée
propre. Inventer une incitation cambodgienne plausible reviendrait à fabriquer le chiffre
qui décide de la conclusion, et c'est précisément ce que ce projet refuse de faire.

La dernière ligne avant le total est celle que personne ne publie : un répondeur qui
décroche est facturé, un refus qui décroche aussi. Avec un taux d'aboutissement autour
de 20 %, chaque entretien complété porte la facture de ceux qui ont échoué.

Points de comparaison : **20 à 60 $** en face-à-face, **5 à 15 $** en centre d'appels
humain. Le tarif opérateur reste une hypothèse tant qu'aucun accord n'est signé, et
c'est lui qui décide de la viabilité **au Cameroun** : sans accord de minutes, une vague
mensuelle de 3 000 ménages y coûte 8 363 $ pour 3 000 $ de recette.

**Le pays appelé décide autant que l'accord.** La même vague, le même code, le même
opérateur, mais composée au Cambodge, coûte 3 874 $ : elle est presque à l'équilibre
sans qu'aucun accord n'ait été signé, et elle dégage 926 $ de marge dès que la question
se vend 800 $ au lieu de 500. Ces trois chiffres ne sont pas recopiés ici, ils sortent
de `ndara/omnibus.py` et trois tests les tiennent. Un identifiant d'appelant local n'est pas
un confort non plus : c'est une variable de la qualité statistique, parce qu'un numéro
étranger fait chuter le taux de décrochage.

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
│   ├── omnibus.py         vague omnibus : créneaux, rotation, facturation
│   └── providers/         ASR · TTS · téléphonie — tous optionnels
├── data/questionnaires/   prix_denrees_cm (fr/en) · prix_denrees_kh (km/en, brouillon)
├── data/margins/          marges de calage
├── web/                   serveur stdlib + interface entretien + tableau de bord
├── scripts/               simulate · report · build_audio · appel_simule · fiche_relecture
└── tests/                 115 tests, stdlib
```

---

## Licence et statut

Prototype de recherche. Le **corpus vocal** produit par ce système n'est pas destiné à
la vente : publication prévue sous licence ouverte, copubliée avec l'institution
partenaire du pays d'enquête.
