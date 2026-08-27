"""La vague omnibus : plusieurs clients, un seul appel.

CE QUE C'EST
------------
Un client achète un créneau d'une à trois questions dans la vague du mois.
Toutes les questions achetées sont posées pendant le même appel, à la suite
du tronc commun, et chaque client ne reçoit que ses réponses à lui.

C'est le modèle qui fait vivre les instituts de sondage depuis quarante ans.
On ne l'invente pas : on l'apporte, automatisé et vocal, là où il n'existe
pas. Sa raison d'être tient en une ligne : **le coût d'un appel se partage,
celui d'une question de plus ne se partage pas.** Ce module calcule les deux,
et refuse de vendre un créneau qui ne tient pas dans l'appel.

CE QU'IL NE FAIT PAS
--------------------
Il ne reformule aucune question : le libellé acheté est le libellé prononcé.
Il ne laisse aucun client toucher à l'annonce d'intelligence artificielle ni
aux deux consentements, qui vivent dans le tronc commun et nulle part
ailleurs. Il ne vend pas un créneau qui ferait dépasser la durée de l'appel,
et il dit pourquoi plutôt que de tronquer en silence.

DEUX DÉCISIONS QUI SE DISCUTENT, ET LEUR RAISON
-----------------------------------------------
**Le tronc commun passe en premier.** En face-à-face, les questions de
classification se posent à la fin, pour ne pas lasser au démarrage. Au
téléphone, l'appel peut se couper à n'importe quelle seconde, et un entretien
sans ses variables de calage ne se pondère pas : il est perdu pour tout le
monde, y compris pour les clients dont les questions avaient été posées. On
paie donc un peu d'abandon précoce pour ne jamais perdre un entretien
exploitable.

**L'ordre des créneaux tourne d'un appel à l'autre.** Le dernier bloc d'un
questionnaire est répondu plus vite, plus souvent par « ne sait pas », et par
un échantillon déjà amputé de ceux qui ont raccroché. Laisser un client
toujours en dernier reviendrait à lui vendre une donnée de moins bonne
qualité au même prix. La rotation est cyclique : sur k appels, chaque bloc
occupe chaque position exactement une fois. Sa position est enregistrée avec
l'entretien, pour qu'un effet de position reste contrôlable a posteriori.

Limite connue, dite ici plutôt que découverte : une rotation cyclique
équilibre les positions, pas les voisinages. Un bloc a toujours les mêmes
voisins. Équilibrer aussi les paires demanderait un carré latin complet
(plan de Williams), et ce n'est pas fait.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .analysis import Indicator
from .questionnaire import Questionnaire, Step

# Durée de ce que NDARA dit avant et après les questions : annonce
# d'intelligence artificielle, deux consentements, remerciement et code de
# retrait. C'est incompressible et c'est facturé comme le reste.
DUREE_PROTOCOLE_S = 45.0


@dataclass(frozen=True)
class Tarif:
    """Ce que coûte une minute, et ce que coûte un appel quelle que soit sa durée.

    Séparer les deux n'est pas une coquetterie comptable : c'est ce qui rend
    calculable le coût d'une question de plus. Une question supplémentaire
    n'ajoute que des secondes ; elle n'ajoute ni incitation, ni quote-part
    d'appels échoués.
    """

    nom: str
    minute_usd: float
    fixe_usd: float          # incitation, transcription, codage, quote-part des échecs

    def cout_appel(self, duree_s: float) -> float:
        return self.fixe_usd + (duree_s / 60.0) * self.minute_usd


# Tarifs voix relevés le 25 août 2026 **sur le compte Twilio lui-même**, et non
# sur la page tarifaire publique. La distinction compte : ce n'est pas un prix
# unique mais une fourchette, parce que Twilio facture selon l'opérateur qui
# termine l'appel, et qu'on ne choisit pas cet opérateur.
#
#   Cameroun  +237 : de 0,410 à 0,787 $ la minute
#   Cambodge  +855 : de 0,112 à 0,132 $ la minute
#
# **Les tarifs retenus ici sont le haut de chaque fourchette**, par prudence :
# une économie unitaire qu'on présente à un jury doit se tromper du côté qui
# coûte, jamais du côté qui arrange. La thèse résiste d'ailleurs sur toute la
# plage, et `test_omnibus.py` le vérifie au tarif bas comme au tarif haut.
#
# La part fixe suit le détail publié dans le README : 0,03 de transcription,
# 0,01 de codage, 0,35 d'incitation, et 0,60 de quote-part des appels qui
# n'aboutissent pas. Elle est reprise telle quelle pour le Cambodge, faute de
# donnée propre : inventer une incitation cambodgienne plausible reviendrait à
# fabriquer le chiffre qui décide de la conclusion.
FOURCHETTE_TWILIO_CM = (0.410, 0.7873)
FOURCHETTE_TWILIO_KH = (0.112, 0.132)

TARIF_TWILIO_CM = Tarif("Twilio, mobile camerounais", minute_usd=0.7873, fixe_usd=0.99)

# Le Cambodge coûte environ six fois moins cher la minute que le Cameroun, et
# ce n'est pas un détail comptable : c'est ce qui fait passer une vague de
# 3 000 ménages de « impossible sans accord opérateur » à « presque à
# l'équilibre sans aucun accord ». Le même produit n'a donc pas le même modèle
# économique des deux côtés, et il vaut mieux le dire que le découvrir devant
# un jury cambodgien.
TARIF_TWILIO_KH = Tarif("Twilio, mobile cambodgien", minute_usd=0.132, fixe_usd=0.99)

# Hypothèse d'accord opérateur, tant qu'aucun accord n'est signé. La
# quote-part des échecs tombe aussi, parce qu'un appel qui ne décroche pas
# n'est pas facturé de la même façon sur un accord de gros.
TARIF_OPERATEUR = Tarif("accord opérateur (hypothèse)", minute_usd=0.152, fixe_usd=0.51)


class CreneauRefuse(Exception):
    """Un créneau qu'on refuse de vendre, avec la raison et la correction.

    Refuser tôt, en donnant la correction, vaut mieux que d'accepter puis de
    tronquer l'appel : le client découvrirait le problème dans ses données.
    """

    def __init__(self, raisons: list[str]) -> None:
        super().__init__(" ".join(raisons))
        self.raisons = raisons


@dataclass
class Creneau:
    """Ce qu'un client achète : une à trois questions dans la vague."""

    client: str
    intitule: str
    steps: list[Step]
    prix_question_usd: float = 500.0
    reference: str = ""
    position: int | None = None       # attribuée à la composition, pour l'appel courant

    def duree_s(self) -> float:
        return sum(s.expected_seconds for s in self.steps)

    def prix_usd(self) -> float:
        return len(self.steps) * self.prix_question_usd

    def step_ids(self) -> set[str]:
        return {s.id for s in self.steps}

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "intitule": self.intitule,
            "reference": self.reference,
            "questions": len(self.steps),
            "step_ids": sorted(self.step_ids()),
            "duree_s": round(self.duree_s(), 1),
            "prix_usd": round(self.prix_usd(), 2),
            "position": self.position,
        }


@dataclass
class Vague:
    """Une vague omnibus : un tronc commun, des créneaux, un budget de durée."""

    id: str
    tronc: Questionnaire
    creneaux: list[Creneau] = field(default_factory=list)
    duree_max_s: float = 150.0        # 2 min 30, au-delà l'abandon décolle
    questions_max_par_client: int = 3
    tarif: Tarif = TARIF_TWILIO_CM

    # ------------------------------------------------------------------
    # Durées
    # ------------------------------------------------------------------

    def duree_tronc_s(self) -> float:
        return DUREE_PROTOCOLE_S + sum(s.expected_seconds for s in self.tronc.steps)

    def duree_creneaux_s(self) -> float:
        return sum(c.duree_s() for c in self.creneaux)

    def duree_engagee_s(self) -> float:
        return self.duree_tronc_s() + self.duree_creneaux_s()

    def duree_restante_s(self) -> float:
        return self.duree_max_s - self.duree_engagee_s()

    # ------------------------------------------------------------------
    # Vente d'un créneau
    # ------------------------------------------------------------------

    def verifier(self, creneau: Creneau) -> list[str]:
        """Tout ce qui empêche de vendre ce créneau. Liste vide : c'est vendable."""
        raisons: list[str] = []

        if not creneau.steps:
            raisons.append("Créneau vide : un créneau contient au moins une question.")
        if len(creneau.steps) > self.questions_max_par_client:
            raisons.append(
                f"{len(creneau.steps)} questions pour un maximum de "
                f"{self.questions_max_par_client}. Découpez sur deux vagues, ou "
                f"achetez une enquête dédiée.")

        if creneau.duree_s() > self.duree_restante_s():
            raisons.append(
                f"Il reste {self.duree_restante_s():.0f} secondes dans cet appel et "
                f"ce créneau en demande {creneau.duree_s():.0f}. Au-delà de "
                f"{self.duree_max_s:.0f} secondes, le taux d'abandon dégrade les "
                f"données de tous les clients de la vague.")

        occupes = {s.id for s in self.tronc.steps}
        for c in self.creneaux:
            occupes |= c.step_ids()
        for s in creneau.steps:
            if s.id in occupes:
                raisons.append(
                    f"L'identifiant « {s.id} » est déjà pris dans cette vague. "
                    f"Renommez la question.")

        for lang in self.tronc.languages:
            for s in creneau.steps:
                if lang not in s.text:
                    raisons.append(
                        f"La question « {s.id} » n'est pas traduite en « {lang} », "
                        f"qui est une langue de la vague.")

        # Un filtre ne peut pointer que dans son propre bloc ou dans le tronc :
        # les blocs tournent d'un appel à l'autre, et un filtre qui traverserait
        # deux blocs se retrouverait un appel sur deux à interroger une réponse
        # pas encore donnée.
        internes = creneau.step_ids()
        tronc_ids = {s.id for s in self.tronc.steps}
        for s in creneau.steps:
            if s.ask_if:
                cible = s.ask_if.get("step")
                if cible not in internes and cible not in tronc_ids:
                    raisons.append(
                        f"Le filtre de « {s.id} » pointe vers « {cible} », qui n'est "
                        f"ni dans ce créneau ni dans le tronc commun. Les blocs "
                        f"changent d'ordre d'un appel à l'autre : un filtre ne peut "
                        f"pas traverser deux créneaux.")

        return raisons

    def ajouter(self, creneau: Creneau) -> Creneau:
        raisons = self.verifier(creneau)
        if raisons:
            raise CreneauRefuse(raisons)
        self.creneaux.append(creneau)
        return creneau

    def proprietaire(self, step_id: str) -> str | None:
        """À qui appartient une question. « __tronc__ » pour le tronc commun."""
        if any(s.id == step_id for s in self.tronc.steps):
            return "__tronc__"
        for c in self.creneaux:
            if step_id in c.step_ids():
                return c.client
        return None

    # ------------------------------------------------------------------
    # Composition d'un appel
    # ------------------------------------------------------------------

    def rotation(self, rang: int) -> list[int]:
        """Ordre des créneaux pour le rang-ième appel de la vague.

        Rotation cyclique : sur k appels consécutifs, chaque créneau occupe
        chaque position exactement une fois.
        """
        k = len(self.creneaux)
        if k == 0:
            return []
        depart = rang % k
        return [(depart + i) % k for i in range(k)]

    def composer(self, rang: int = 0) -> Questionnaire:
        """L'instrument tel qu'il sera mené pour le rang-ième appel.

        Le questionnaire composé emprunte le répertoire audio du tronc, parce
        que ce sont ses libellés qui ont été pré-synthétisés. Quand des
        créneaux viendront de questionnaires synthétisés séparément, il faudra
        porter l'identité audio question par question. Ce n'est pas fait.
        """
        ordre = self.rotation(rang)
        steps = list(self.tronc.steps)
        for position, idx in enumerate(ordre):
            creneau = self.creneaux[idx]
            creneau.position = position
            steps.extend(creneau.steps)

        presents = {s.id for s in steps}
        checks = [c for c in self.tronc.checks
                  if _cibles_du_controle(c) <= presents]

        q = Questionnaire(
            id=f"{self.id}__r{rang % max(1, len(self.creneaux))}",
            version=self.tronc.version,
            country=self.tronc.country,
            currency=self.tronc.currency,
            languages=list(self.tronc.languages),
            consent_version=self.tronc.consent_version,
            incentive=dict(self.tronc.incentive),
            prompts=self.tronc.prompts,
            steps=steps,
            checks=checks,
            audio_id=self.tronc.audio_id or self.tronc.id,
        )
        q.validate()
        _verifier_ordre_des_filtres(q)
        return q

    def marque_entretien(self, rang: int) -> dict[str, Any]:
        """Ce qu'on écrit dans l'entretien pour retrouver la vague et les positions.

        Sans cette trace, un effet de position resterait invisible dans les
        données, et la rotation ne servirait à rien puisqu'on ne pourrait pas
        la contrôler.
        """
        ordre = self.rotation(rang)
        return {
            "vague": self.id,
            "rang": rang,
            "positions": {self.creneaux[idx].client: position
                          for position, idx in enumerate(ordre)},
        }

    # ------------------------------------------------------------------
    # Facturation
    # ------------------------------------------------------------------

    def facture(self, n_aboutis: int, tarif: Tarif | None = None) -> dict[str, Any]:
        """Recette, coût imputé et marge, par client et pour la vague.

        Règle de répartition : le coût d'un appel abouti est réparti entre les
        clients au prorata des secondes que leur créneau occupe. Le tronc
        commun et le protocole servent tout le monde, donc ils se répartissent
        de la même façon. Un client qui achète une question longue paie plus
        qu'un client qui achète une question courte, ce qui est la seule
        répartition qu'on puisse défendre devant les deux.
        """
        tarif = tarif or self.tarif
        duree = self.duree_engagee_s()
        cout_unitaire = tarif.cout_appel(duree)
        cout_total = cout_unitaire * n_aboutis
        secondes_vendues = self.duree_creneaux_s()

        lignes = []
        for c in self.creneaux:
            part = (c.duree_s() / secondes_vendues) if secondes_vendues else 0.0
            cout = cout_total * part
            recette = c.prix_usd()
            lignes.append({
                "client": c.client,
                "intitule": c.intitule,
                "questions": len(c.steps),
                "duree_s": round(c.duree_s(), 1),
                "part": round(part, 4),
                "recette_usd": round(recette, 2),
                "cout_impute_usd": round(cout, 2),
                "marge_usd": round(recette - cout, 2),
                "cout_par_entretien_usd": round(cout / n_aboutis, 4) if n_aboutis else 0.0,
            })

        recette_totale = sum(l["recette_usd"] for l in lignes)
        return {
            "vague": self.id,
            "tarif": tarif.nom,
            "n_aboutis": n_aboutis,
            "duree_appel_s": round(duree, 1),
            "cout_par_entretien_usd": round(cout_unitaire, 4),
            "cout_total_usd": round(cout_total, 2),
            "recette_totale_usd": round(recette_totale, 2),
            "marge_totale_usd": round(recette_totale - cout_total, 2),
            "lignes": lignes,
        }

    def cout_question_supplementaire(self, duree_s: float, n_aboutis: int,
                                     tarif: Tarif | None = None) -> dict[str, Any]:
        """Ce que coûte vraiment une question de plus dans une vague déjà lancée.

        C'est le chiffre qui décide si le modèle omnibus tient. Une question
        de plus n'ajoute ni incitation, ni quote-part d'appels échoués, ni
        recrutement : elle n'ajoute que des secondes de voix. À tarif de gros
        public elles coûtent cher ; sous accord opérateur elles ne coûtent
        presque rien, et c'est là que le modèle devient un modèle.
        """
        tarif = tarif or self.tarif
        cout_unitaire = (duree_s / 60.0) * tarif.minute_usd
        return {
            "tarif": tarif.nom,
            "duree_s": duree_s,
            "cout_par_entretien_usd": round(cout_unitaire, 4),
            "cout_total_usd": round(cout_unitaire * n_aboutis, 2),
            "tient_dans_l_appel": duree_s <= self.duree_restante_s(),
        }

    # ------------------------------------------------------------------
    # Restitution
    # ------------------------------------------------------------------

    def indicateurs(self, client: str) -> list[Indicator]:
        """Les indicateurs d'un client, et rien que les siens."""
        creneau = next((c for c in self.creneaux if c.client == client), None)
        if creneau is None:
            return []
        out: list[Indicator] = []
        for s in creneau.steps:
            out.extend(indicateurs_de_la_question(s))
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tronc": {"id": self.tronc.id, "questions": len(self.tronc.steps),
                      "duree_s": round(self.duree_tronc_s(), 1)},
            "duree_max_s": self.duree_max_s,
            "duree_engagee_s": round(self.duree_engagee_s(), 1),
            "duree_restante_s": round(self.duree_restante_s(), 1),
            "creneaux": [c.as_dict() for c in self.creneaux],
            "questions_vendues": sum(len(c.steps) for c in self.creneaux),
        }


# --------------------------------------------------------------------------
# Indicateurs dérivés d'une question
# --------------------------------------------------------------------------

def indicateurs_de_la_question(step: Step, lang: str = "fr") -> list[Indicator]:
    """Ce qu'on publie pour une question, selon son type.

    Un client qui achète une question veut un chiffre et son intervalle, pas
    un fichier de réponses brutes. Les libellés sont repris tels quels : ils
    ont été achetés tels quels.
    """
    libelle = step.prompt(lang)
    court = libelle if len(libelle) <= 90 else libelle[:87] + "..."

    if step.type == "numeric":
        unite = step.unit
        return [
            Indicator(f"{step.id}_moyenne", f"{court} (moyenne)", "mean", step.id,
                      unit=unite, decimals=1),
            Indicator(f"{step.id}_mediane", f"{court} (médiane)", "median", step.id,
                      unit=unite, decimals=1),
        ]
    if step.type in ("yes_no", "single_choice"):
        return [
            Indicator(f"{step.id}_{o.code}", f"{court} : {o.label_for(lang)}",
                      "proportion", step.id, code=o.code, unit="%", decimals=1)
            for o in step.options
        ]
    return []


def restitution(vague: Vague, client: str, store, margins: dict,
                groups: int = 10) -> dict[str, Any]:
    """Ce que reçoit un client : ses chiffres, et la qualité de la vague entière.

    Un client ne voit jamais les questions d'un autre client. Il voit en
    revanche le taux de réponse, l'effet de plan et le rapport d'auto-audit de
    la vague, parce que ce sont les siens aussi : ce sont les mêmes appels.
    """
    from .analysis import estimate_all

    creneau = next((c for c in vague.creneaux if c.client == client), None)
    if creneau is None:
        return {"client": client, "erreur": "aucun créneau pour ce client"}

    q = vague.composer(0)
    res = estimate_all(store, q, margins,
                       indicators=vague.indicateurs(client), groups=groups)
    res["client"] = client
    res["vague"] = vague.id
    res["creneau"] = creneau.as_dict()
    res["confidentialite"] = (
        "Ce document ne contient que les questions achetées par ce client. Les "
        "autres questions de la vague, et leurs commanditaires, ne sont ni "
        "nommés ni déductibles de ces chiffres.")
    return res


# --------------------------------------------------------------------------
# Vérifications internes
# --------------------------------------------------------------------------

def _cibles_du_controle(check: dict) -> set[str]:
    cibles = set()
    for cle in ("if", "then"):
        bloc = check.get(cle) or {}
        if isinstance(bloc, dict) and bloc.get("step"):
            cibles.add(bloc["step"])
    return cibles


def _verifier_ordre_des_filtres(q: Questionnaire) -> None:
    """Un filtre doit toujours suivre la question qu'il interroge.

    La rotation des blocs déplace des questions : cette vérification tourne
    après chaque composition, pour qu'un ordre impossible échoue ici plutôt
    qu'au milieu d'un appel réel.
    """
    rang = {s.id: i for i, s in enumerate(q.steps)}
    for s in q.steps:
        if not s.ask_if:
            continue
        cible = s.ask_if.get("step")
        if cible not in rang:
            raise ValueError(
                f"Le filtre de « {s.id} » pointe vers « {cible} », absent de la vague.")
        if rang[cible] >= rang[s.id]:
            raise ValueError(
                f"Le filtre de « {s.id} » interroge « {cible} », qui est posée après. "
                f"Vérifiez l'ordre des créneaux.")


# --------------------------------------------------------------------------
# La vague de démonstration
# --------------------------------------------------------------------------

def vague_de_demonstration(tronc_source: Questionnaire,
                           identifiant: str = "vague_demonstration") -> Vague:
    """Une vague montée à partir d'un questionnaire déjà synthétisé.

    Elle n'invente aucune question : elle redécoupe un questionnaire existant
    en un tronc commun et trois créneaux, comme si trois commanditaires
    avaient acheté leurs places dans le même appel. C'est délibéré, et pour
    deux raisons. Les libellés sont déjà pré-synthétisés, donc la vague
    **parle** au lieu d'être une maquette. Et rien n'est fabriqué : ce sont
    les mêmes questions, les mêmes voix, le même moteur.

    Le tronc garde les quatre variables de classification, qui servent au
    calage et donc à tout le monde. Les trois créneaux se partagent le reste.
    """
    par_id = {s.id: s for s in tronc_source.steps}
    manquants = [i for i in ("region", "sex", "age_group", "hh_size") if i not in par_id]
    if manquants:
        raise ValueError(f"Tronc impossible, questions absentes : {manquants}")

    tronc = Questionnaire(
        id=f"{identifiant}_tronc",
        version=tronc_source.version,
        country=tronc_source.country,
        currency=tronc_source.currency,
        languages=list(tronc_source.languages),
        consent_version=tronc_source.consent_version,
        incentive=dict(tronc_source.incentive),
        prompts=tronc_source.prompts,
        steps=[par_id[i] for i in ("region", "sex", "age_group", "hh_size")],
        checks=list(tronc_source.checks),
        audio_id=tronc_source.audio_dir_id(),
    )

    vague = Vague(id=identifiant, tronc=tronc)
    vague.ajouter(Creneau(
        client="Programme alimentaire mondial",
        intitule="Sécurité alimentaire des ménages",
        steps=[par_id["reduced_meals"], par_id["skipped_day"]],
        reference="PAM-2026-09"))
    vague.ajouter(Creneau(
        client="Institut national de la statistique",
        intitule="Prix du riz à la consommation",
        steps=[par_id["bought_rice"], par_id["rice_price"]],
        reference="INS-2026-09"))
    vague.ajouter(Creneau(
        client="Observatoire des prix",
        intitule="Huile et direction perçue des prix",
        steps=[par_id["oil_price"], par_id["price_direction"]],
        reference="OBS-2026-09"))
    return vague
