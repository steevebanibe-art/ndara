"""Import d'un questionnaire client, depuis un tableau vers l'instrument.

Ce que ce module rend possible : un institut, un ministère ou une ONG écrit
son questionnaire dans un tableur, le dépose, et NDARA sait le mener. Aucune
ligne de code à écrire, aucun format à apprendre.

CE QUE CE MODULE NE FAIT PAS, ET POURQUOI
-----------------------------------------
Il ne reformule jamais une question. Le libellé déposé est le libellé posé,
mot pour mot, à tout l'échantillon. Un instrument dont les mots changent
d'un répondant à l'autre ne mesure plus rien : c'est la règle qui tient tout
le projet, et l'import ne l'assouplit pas.

Il refuse plutôt que de deviner. Une colonne manquante, une modalité vide,
un filtre qui pointe vers une question inexistante : l'import échoue avec le
numéro de ligne et la correction à faire. Un questionnaire à moitié compris
produirait des données à moitié fausses, et personne ne le verrait.

LE FORMAT
---------
Une ligne d'en-tête, puis une question par ligne. Séparateur virgule ou
point-virgule, détecté tout seul. Les noms de colonnes sont tolérants aux
accents, aux majuscules et aux espaces.

    question   le libellé, tel qu'il sera prononcé          OBLIGATOIRE
    type       choix · oui_non · nombre · tranches · libre  OBLIGATOIRE
    modalites  les réponses possibles, séparées par |       si choix/tranches
    id         identifiant court ; déduit du libellé sinon
    unite      l'unité d'une question numérique
    min, max   bornes de refus : au-delà, on relance
    plausible  plage attendue « 300-2000 » : au-delà, on signale sans refuser
    filtre     « riz=oui » : la question n'est posée que dans ce cas
    sensible   « oui » : la réponse est exclue du corpus vocal même consenti
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .questionnaire import STEP_TYPES, YES_NO_OPTIONS, Questionnaire

# Types écrits en français, tels qu'un enquêteur les nommerait.
TYPES = {
    "choix": "single_choice", "choix unique": "single_choice", "qcm": "single_choice",
    "modalites": "single_choice", "liste": "single_choice",
    "oui non": "yes_no", "oui/non": "yes_no", "ouinon": "yes_no", "binaire": "yes_no",
    "nombre": "numeric", "numerique": "numeric", "montant": "numeric", "quantite": "numeric",
    "tranches": "single_choice", "tranche": "single_choice", "intervalle": "single_choice",
    "libre": "open_short", "texte": "open_short", "ouverte": "open_short",
}

COLONNES = {
    "question": "question", "libelle": "question", "intitule": "question", "texte": "question",
    "type": "type", "nature": "type",
    "modalites": "modalites", "reponses": "modalites", "options": "modalites",
    "choix": "modalites", "propositions": "modalites",
    "id": "id", "identifiant": "id", "code": "id", "variable": "id",
    "unite": "unite", "min": "min", "minimum": "min", "max": "max", "maximum": "max",
    "plausible": "plausible", "plage": "plausible", "attendu": "plausible",
    "filtre": "filtre", "condition": "filtre", "si": "filtre",
    "sensible": "sensible", "confidentiel": "sensible",
}

SEPARATEURS_MODALITES = re.compile(r"\s*[|/]\s*|\s*;\s*")


def _sans_accents(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t or "")
                   if unicodedata.category(c) != "Mn")


def _cle(t: str) -> str:
    """Nom de colonne ou de type, débarrassé de ce qui ne distingue rien.

    Un client écrit « Oui/Non », « oui_non », « OUI NON » : c'est le même
    type. Refuser sur une différence de ponctuation serait de la pédanterie
    facturée au client.
    """
    t = _sans_accents(t or "").strip().lower()
    t = re.sub(r"[_\-/]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def slug(t: str, secours: str = "q") -> str:
    """Un identifiant court et lisible, tiré du libellé."""
    base = re.sub(r"[^a-z0-9]+", "_", _sans_accents(t).lower()).strip("_")
    mots = [m for m in base.split("_") if m and m not in
            ("le", "la", "les", "un", "une", "des", "du", "de", "dans", "votre",
             "vous", "quel", "quelle", "est", "ce", "que", "combien", "avez", "etes")]
    court = "_".join(mots[:3]) or base[:24] or secours
    return court[:32]


@dataclass
class Probleme:
    ligne: int | None
    colonne: str | None
    message: str
    correction: str

    def to_dict(self) -> dict:
        return {"ligne": self.ligne, "colonne": self.colonne,
                "message": self.message, "correction": self.correction}


@dataclass
class Resultat:
    ok: bool
    questionnaire: dict | None = None
    problemes: list[Probleme] = field(default_factory=list)
    avertissements: list[str] = field(default_factory=list)
    resume: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "problemes": [p.to_dict() for p in self.problemes],
            "avertissements": self.avertissements,
            "resume": self.resume,
            "questionnaire": self.questionnaire,
        }


# --------------------------------------------------------------------------
# Messages système, dérivés de ce que le client déclare
# --------------------------------------------------------------------------

def messages_systeme(objet: str, duree_min: int, incitation: str) -> dict[str, dict[str, str]]:
    """Les phrases qui encadrent tout entretien.

    L'annonce d'intelligence artificielle et les deux consentements séparés
    ne sont pas des options offertes au client : ils sont écrits ici, une
    fois, et aucun import ne peut les retirer. Seuls l'objet de l'enquête,
    la durée et l'incitation viennent du client.
    """
    incit = incitation.strip() or "aucune compensation"
    return {
        "announce": {"fr":
            "Bonjour. Je suis un assistant vocal automatique, une intelligence "
            "artificielle. Je ne suis pas une personne. J'appelle pour une enquête "
            f"sur {objet}. L'entretien dure environ {duree_min} minutes. "
            "Vous pouvez raccrocher à tout moment."},
        "consent_survey": {"fr":
            "Acceptez-vous de répondre à quelques questions ? Vos réponses resteront "
            "anonymes. Dites oui ou non, ou tapez 1 pour oui, 2 pour non."},
        "consent_corpus": {"fr":
            "Deuxième question, différente de la première. Acceptez-vous que "
            "l'enregistrement de votre voix soit conservé pour aider à améliorer les "
            "technologies vocales dans nos langues ? C'est facultatif. Si vous refusez, "
            "vous participez quand même à l'enquête et vous recevez la même "
            f"compensation. Dites oui ou non, ou tapez 1 pour oui, 2 pour non."},
        "consent_corpus_ack": {"fr": "C'est noté. Nous commençons l'enquête."},
        "relance_unclear": {"fr": "Je n'ai pas bien compris. Pouvez-vous répéter ?"},
        "relance_dtmf": {"fr": "Je n'ai toujours pas compris. Utilisez les touches de votre téléphone."},
        "thanks": {"fr": f"Merci pour votre temps. {incit[0].upper() + incit[1:]}."},
        "refusal_ack": {"fr": "C'est entendu. Je vous remercie et je ne vous rappellerai pas. Bonne journée."},
        "withdrawal": {"fr":
            "Votre code de retrait est {code}. Donnez-le nous pour faire effacer "
            "votre enregistrement à tout moment."},
        "yes_label": {"fr": "Oui"},
        "no_label": {"fr": "Non"},
    }


# --------------------------------------------------------------------------
# Lecture du tableau
# --------------------------------------------------------------------------

def lire_tableau(texte: str) -> tuple[list[dict], list[Probleme]]:
    """Lit le tableau et renvoie des lignes aux colonnes normalisées."""
    problemes: list[Probleme] = []
    texte = (texte or "").lstrip("﻿").strip()
    if not texte:
        return [], [Probleme(None, None, "Le tableau est vide.",
                             "Collez au moins une ligne d'en-tête et une question.")]
    try:
        dialecte = csv.Sniffer().sniff(texte[:2000], delimiters=",;\t")
        sep = dialecte.delimiter
    except csv.Error:
        premiere = texte.splitlines()[0]
        sep = max(",;\t", key=premiere.count)

    lecteur = csv.reader(io.StringIO(texte), delimiter=sep)
    lignes = [l for l in lecteur if any((c or "").strip() for c in l)]
    if len(lignes) < 2:
        return [], [Probleme(None, None, "Aucune question trouvée.",
                             "Il faut une ligne d'en-tête, puis une ligne par question.")]

    entete = [_cle(c) for c in lignes[0]]
    mapping: dict[int, str] = {}
    inconnues: list[str] = []
    for i, nom in enumerate(entete):
        if not nom:
            continue
        if nom in COLONNES:
            mapping[i] = COLONNES[nom]
        else:
            inconnues.append(lignes[0][i])
    if "question" not in mapping.values():
        problemes.append(Probleme(1, None,
            "Aucune colonne « question » dans l'en-tête.",
            "Nommez la colonne des libellés « question » (ou « libellé », « intitulé »)."))
    if "type" not in mapping.values():
        problemes.append(Probleme(1, None,
            "Aucune colonne « type » dans l'en-tête.",
            "Ajoutez une colonne « type » valant choix, oui_non, nombre, tranches ou libre."))

    sorties: list[dict] = []
    for n, brut in enumerate(lignes[1:], start=2):
        ligne = {"_ligne": n}
        for i, val in enumerate(brut):
            if i in mapping:
                ligne[mapping[i]] = (val or "").strip()
        sorties.append(ligne)
    return sorties, problemes


# --------------------------------------------------------------------------
# Construction de l'instrument
# --------------------------------------------------------------------------

def _bornes(valeur: str) -> tuple[float, float] | None:
    nombres = re.findall(r"-?\d+(?:[.,]\d+)?", valeur or "")
    if len(nombres) < 2:
        return None
    a, b = (float(x.replace(",", ".")) for x in nombres[:2])
    return (min(a, b), max(a, b))


def construire(texte_tableau: str, meta: dict) -> Resultat:
    """Du tableau du client à un instrument que le moteur sait mener."""
    lignes, problemes = lire_tableau(texte_tableau)
    avertissements: list[str] = []

    titre = (meta.get("titre") or "").strip()
    objet = (meta.get("objet") or "").strip()
    pays = (meta.get("pays") or "CM").strip().upper()[:2]
    devise = (meta.get("devise") or "FCFA").strip()
    incitation = (meta.get("incitation") or "").strip()
    if not titre:
        problemes.append(Probleme(None, "titre", "Le titre de l'enquête est vide.",
                                  "Donnez un nom court, par exemple « Prix des denrées, août »."))
    if not objet:
        problemes.append(Probleme(None, "objet", "L'objet de l'enquête est vide.",
            "Écrivez de quoi parle l'enquête : il est annoncé au répondant dès la "
            "première phrase, et il n'a pas le droit d'être vague."))

    etapes: list[dict] = []
    vus: set[str] = set()

    for ligne in lignes:
        n = ligne["_ligne"]
        libelle = (ligne.get("question") or "").strip()
        type_brut = _cle(ligne.get("type") or "")
        if not libelle:
            problemes.append(Probleme(n, "question", "Question sans libellé.",
                                      "Écrivez la phrase exacte qui sera prononcée."))
            continue
        if type_brut not in TYPES:
            problemes.append(Probleme(n, "type",
                f"Type « {ligne.get('type', '')} » non reconnu.",
                "Utilisez choix, oui_non, nombre, tranches ou libre."))
            continue

        stype = TYPES[type_brut]
        tranches = type_brut.startswith("tranche") or type_brut == "intervalle"

        sid = slug(ligne.get("id") or libelle)
        if sid in vus:
            suffixe = 2
            while f"{sid}_{suffixe}" in vus:
                suffixe += 1
            sid = f"{sid}_{suffixe}"
        vus.add(sid)

        etape: dict[str, Any] = {"id": sid, "type": stype, "text": {"fr": libelle}}

        if stype == "yes_no":
            etape["options"] = json.loads(json.dumps(YES_NO_OPTIONS))
        elif stype == "single_choice":
            brut = (ligne.get("modalites") or "").strip()
            libelles = [m.strip() for m in SEPARATEURS_MODALITES.split(brut) if m.strip()]
            if len(libelles) < 2:
                problemes.append(Probleme(n, "modalites",
                    "Une question à choix a besoin d'au moins deux modalités.",
                    "Séparez-les par une barre verticale : Femme|Homme."))
                continue
            if len(libelles) > 10:
                problemes.append(Probleme(n, "modalites",
                    f"{len(libelles)} modalités : le clavier d'un téléphone en a dix.",
                    "Regroupez les modalités, ou découpez la question en deux."))
                continue
            touches = "1234567890"
            options = []
            for i, lab in enumerate(libelles):
                options.append({
                    "code": slug(lab, f"m{i+1}").upper()[:16] or f"M{i+1}",
                    "dtmf": touches[i],
                    "labels": {"fr": [lab.lower()]},
                })
            etape["options"] = options
            # Le libellé prononcé porte les touches : sans elles, le repli
            # clavier est impossible pour quelqu'un qui ne sait pas lire.
            if not re.search(r"tapez|appuyez|touche", _sans_accents(libelle).lower()):
                rappel = ". ".join(f"{lab}, tapez {touches[i]}"
                                   for i, lab in enumerate(libelles))
                etape["text"]["fr"] = f"{libelle} {rappel}."
                avertissements.append(
                    f"Ligne {n} : les touches ont été ajoutées au libellé de « {sid} », "
                    "sinon le repli clavier serait impossible.")
            if tranches:
                etape["raking_var"] = sid
        elif stype == "numeric":
            if ligne.get("unite"):
                etape["unit"] = ligne["unite"]
            for champ in ("min", "max"):
                val = (ligne.get(champ) or "").replace(",", ".").strip()
                if val:
                    try:
                        etape[champ] = float(val)
                    except ValueError:
                        problemes.append(Probleme(n, champ, f"« {val} » n'est pas un nombre.",
                                                  "Écrivez une valeur numérique, ou laissez vide."))
            plage = _bornes(ligne.get("plausible") or "")
            if plage:
                etape["plausible"] = [plage[0], plage[1]]
            if etape.get("min") is None and etape.get("max") is None:
                avertissements.append(
                    f"Ligne {n} : « {sid} » n'a pas de bornes. Sans bornes, une valeur "
                    "aberrante entre dans les données au lieu de déclencher une relance.")

        filtre = (ligne.get("filtre") or "").strip()
        if filtre:
            m = re.match(r"\s*([^=<>!]+?)\s*=\s*(.+?)\s*$", filtre)
            if not m:
                problemes.append(Probleme(n, "filtre", f"Filtre « {filtre} » illisible.",
                                          "Écrivez-le sous la forme question=réponse, par exemple riz=oui."))
            else:
                cible, valeur = slug(m.group(1)), m.group(2).strip().lower()
                etape["ask_if"] = {"step": cible, "equals": valeur}

        if _cle(ligne.get("sensible") or "") in ("oui", "yes", "1", "vrai", "true"):
            etape["corpus_eligible"] = False

        etape["expected_seconds"] = 8.0 if stype != "open_short" else 12.0
        etapes.append(etape)

    if not etapes and not problemes:
        problemes.append(Probleme(None, None, "Aucune question exploitable.",
                                  "Vérifiez que chaque ligne a un libellé et un type."))

    # Les filtres se vérifient une fois toutes les étapes connues.
    connus = {e["id"] for e in etapes}
    for e in etapes:
        cond = e.get("ask_if")
        if not cond:
            continue
        if cond["step"] not in connus:
            # Le client a désigné la question par son libellé ou par un
            # raccourci. On rattrape tant que la désignation est SANS
            # AMBIGUÏTÉ : deux candidats, on refuse, parce qu'un filtre posé
            # sur la mauvaise question saute des répondants en silence.
            # Le rattrapage doit rester exigeant : une ressemblance d'une ou
            # deux lettres n'est pas une désignation. Sans ce seuil, « fantome »
            # se laisserait rattacher à une question nommée « a ».
            vise = cond["step"]
            mots = {m for m in vise.split("_") if len(m) >= 3}
            candidats = [
                i for i in connus
                if (len(vise) >= 3 and len(i) >= 3 and (vise in i or i in vise))
                or (mots & {m for m in i.split("_") if len(m) >= 3})
            ]
            if len(candidats) == 1:
                cond["step"] = candidats[0]
                avertissements.append(
                    f"Le filtre de « {e['id']} » a été rattaché à « {candidats[0]} ».")
            else:
                dispo = ", ".join(sorted(connus))
                problemes.append(Probleme(None, "filtre",
                    f"Le filtre de « {e['id']} » désigne « {cond['step']} », "
                    + ("qui est ambigu." if candidats else "qui n'existe pas."),
                    f"Questions disponibles : {dispo}."))
                continue
        cible = next(x for x in etapes if x["id"] == cond["step"])
        codes = {o["code"].lower() for o in cible.get("options", [])}
        if codes and cond["equals"].lower() not in codes:
            proches = ", ".join(sorted(codes)[:6])
            problemes.append(Probleme(None, "filtre",
                f"« {cond['equals']} » n'est pas une réponse de « {cond['step']} ».",
                f"Réponses possibles : {proches}."))
        # L'ordre compte : filtrer sur une question posée plus tard n'a pas de sens.
        if [x["id"] for x in etapes].index(cond["step"]) > [x["id"] for x in etapes].index(e["id"]):
            problemes.append(Probleme(None, "filtre",
                f"« {e['id'] }» est filtrée par « {cond['step']} », posée après elle.",
                "Placez la question qui conditionne avant celle qu'elle conditionne."))

    if problemes:
        return Resultat(False, None, problemes, avertissements)

    duree = max(2, round((len(etapes) * 8 + 90) / 60))
    qid = slug(titre, "enquete")
    doc = {
        "id": qid,
        "version": "1.0",
        "country": pays,
        "currency": devise,
        "languages": ["fr"],
        "consent_version": "1.0",
        "_note": "Importé depuis un tableau client. Libellés figés : le moteur ne "
                 "reformule jamais une question.",
        "incentive": {"amount": 0, "currency": devise,
                      "label": {"fr": incitation or "aucune compensation"}},
        "prompts": messages_systeme(objet, duree, incitation),
        "steps": etapes,
        "checks": [],
    }

    # Dernier juge : le validateur du moteur lui-même. S'il refuse, personne
    # ne doit pouvoir déposer l'instrument.
    try:
        Questionnaire.from_dict(doc).validate()
    except Exception as exc:
        return Resultat(False, None,
                        [Probleme(None, None, f"L'instrument reste invalide : {exc}",
                                  "Corrigez le tableau et redéposez-le.")],
                        avertissements)

    return Resultat(True, doc, [], avertissements, {
        "id": qid,
        "questions": len(etapes),
        "duree_estimee_min": duree,
        "types": {t: sum(1 for e in etapes if e["type"] == t)
                  for t in sorted({e["type"] for e in etapes})},
        "filtres": sum(1 for e in etapes if e.get("ask_if")),
        "sensibles": sum(1 for e in etapes if e.get("corpus_eligible") is False),
    })


EXEMPLE_CSV = """id;question;type;modalites;unite;min;max;plausible;filtre;sensible
region;Dans quelle région habitez-vous ?;choix;Adamaoua|Centre|Est|Littoral|Ouest;;;;;;
sexe;Êtes-vous une femme ou un homme ?;choix;Femme|Homme;;;;;;
age;Quel âge avez-vous ?;tranches;18-24|25-34|35-49|50-64|65 ou plus;;;;;;
menage;Combien de personnes vivent dans votre ménage ?;nombre;;personnes;1;30;1-15;;
riz;Avez-vous acheté du riz hier ou aujourd'hui ?;oui_non;;;;;;;
prix_riz;Quel prix avez-vous payé le kilogramme de riz ?;nombre;;FCFA;100;10000;300-2000;riz=yes;
repas;Avez-vous dû réduire le nombre de repas cette semaine ?;oui_non;;;;;;;oui
"""
