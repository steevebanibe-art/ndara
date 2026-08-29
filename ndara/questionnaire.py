"""Chargement et validation du questionnaire.

RÈGLE MÉTHODOLOGIQUE FONDAMENTALE
---------------------------------
Le libellé d'une question est FIXE et identique pour tous les répondants.
Le modèle de langage ne rédige jamais une question, jamais une relance :
il ne fait que *coder* une réponse. Toute reformulation à la volée
introduirait un biais d'enquêteur et rendrait les données inexploitables.

Conséquence technique heureuse : les libellés sont pré-synthétisés une
seule fois (scripts/build_audio.py), donc le coût de synthèse vocale est
nul en production et le stimulus est rigoureusement identique.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STEP_TYPES = {"single_choice", "yes_no", "numeric", "open_short"}

YES_NO_OPTIONS = [
    {
        "code": "yes",
        "dtmf": "1",
        "labels": {
            "fr": ["oui", "ouais", "bien sur", "bien sûr", "affirmatif", "d'accord", "voila", "voilà"],
            "en": ["yes", "yeah", "yep", "correct", "sure"],
            "km": ["បាទ", "ចាស", "បាទ/ចាស", "យល់ព្រម"],
        },
    },
    {
        "code": "no",
        "dtmf": "2",
        "labels": {
            "fr": ["non", "nan", "pas du tout", "negatif", "négatif", "jamais"],
            "en": ["no", "nope", "not at all", "never"],
            "km": ["ទេ", "អត់ទេ", "មិន"],
        },
    },
]


@dataclass
class Option:
    code: str
    dtmf: str | None
    labels: dict[str, list[str]]

    def label_for(self, lang: str) -> str:
        vals = self.labels.get(lang) or next(iter(self.labels.values()), [self.code])
        return vals[0]


@dataclass
class Step:
    id: str
    type: str
    text: dict[str, str]
    text_court: dict[str, str] | None = None
    """La même question, réduite à ce qu'il faut pour y répondre.

    Une relance qui reprend tout depuis le début est la façon la plus sûre de
    faire raccrocher quelqu'un qui vient déjà de parler pour rien. Elle n'a
    pourtant aucune raison d'être longue : le répondant a entendu le préambule,
    il lui manque seulement les modalités.

    Ce libellé est FACULTATIF, et c'est délibéré. Tant qu'un questionnaire ne
    le déclare pas, la relance rejoue la question entière, exactement comme
    avant : aucun questionnaire existant ne devient muet, aucun fichier audio
    n'est exigé rétroactivement. Quand il est déclaré, la pré-synthèse produit
    `{id}_court.mp3` d'elle-même.
    """
    options: list[Option] = field(default_factory=list)
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    plausible: list[float] | None = None
    expected_seconds: float = 8.0
    min_seconds: float = 1.5
    max_relances: int = 2
    ask_if: dict[str, Any] | None = None
    raking_var: str | None = None
    corpus_eligible: bool = True     # False pour les questions sensibles
    indicator: str | None = None     # nom de l'indicateur publié

    def prompt(self, lang: str) -> str:
        return self.text.get(lang) or self.text.get("fr") or next(iter(self.text.values()))

    def prompt_court(self, lang: str) -> str | None:
        """La forme brève, si et seulement si elle existe DANS CETTE LANGUE.

        Pas de repli sur le français : faire entendre une relance française à
        un répondant khmer serait pire que de rejouer la question entière.
        """
        if not self.text_court:
            return None
        return self.text_court.get(lang) or None

    def option_by_code(self, code: str) -> Option | None:
        for o in self.options:
            if o.code == code:
                return o
        return None

    def option_by_dtmf(self, digit: str) -> Option | None:
        for o in self.options:
            if o.dtmf == digit:
                return o
        return None


@dataclass
class Questionnaire:
    id: str
    version: str
    country: str
    currency: str
    languages: list[str]
    consent_version: str
    incentive: dict[str, Any]
    prompts: dict[str, dict[str, str]]
    steps: list[Step]
    checks: list[dict[str, Any]] = field(default_factory=list)
    audio_id: str | None = None      # répertoire audio emprunté (vague composée)

    # ---------- accès ----------

    def prompt_keys(self) -> list[str]:
        """Les libellés système, dans l'ordre du fichier.

        Utilisé par la pré-synthèse, qui doit couvrir tout ce que NDARA dira,
        pas une liste tenue à la main qui prend du retard sur le questionnaire.
        """
        return list(self.prompts)

    def prompt(self, key: str, lang: str) -> str:
        block = self.prompts.get(key, {})
        return block.get(lang) or block.get("fr") or next(iter(block.values()), f"[{key}]")

    def prompt_optionnel(self, key: str, lang: str) -> str | None:
        """Un libellé système FACULTATIF, ou rien du tout.

        `prompt()` ne rend jamais rien : à défaut il rend `[clé]`, et à défaut
        de la langue demandée il rend le français. C'est le bon comportement
        pour les libellés obligatoires, et le pire possible pour ceux qui ne le
        sont pas : un questionnaire qui n'a pas encore été retraduit ferait
        prononcer « crochet invite touches crochet » à la voix de studio, ou
        une phrase française à un répondant khmer.

        Les conduites du tour de parole (les invites « appuyez sur… ou parlez
        après le signal », le tour de calibrage) sont exactement de ce genre :
        elles améliorent l'appel là où elles ont été traduites et synthétisées,
        et elles se taisent partout ailleurs. Le khmer de `prix_denrees_kh`
        attend une relecture par un locuteur natif du CADT : tant qu'elle n'a
        pas eu lieu, ces libellés n'existent pas en khmer, et NDARA ne les
        invente pas.
        """
        texte = (self.prompts.get(key) or {}).get(lang)
        return texte.strip() if texte and texte.strip() else None

    def step(self, step_id: str) -> Step | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def expected_duration_seconds(self) -> float:
        return sum(s.expected_seconds for s in self.steps)

    def audio_dir_id(self) -> str:
        """Le répertoire où chercher les libellés pré-synthétisés.

        Une vague omnibus est un questionnaire composé à la volée : ses
        questions ont été synthétisées sous l'identité du questionnaire
        d'origine, et c'est là qu'il faut aller les chercher. Sans cette
        indirection, une vague composée serait muette alors que les fichiers
        existent.
        """
        return self.audio_id or self.id

    # ---------- chargement ----------

    @classmethod
    def load(cls, path: str | Path) -> "Questionnaire":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: dict) -> "Questionnaire":
        """Même construction, depuis un dictionnaire déjà en mémoire.

        Sert à l'import d'un questionnaire client : celui-ci doit passer
        exactement le même validateur qu'un fichier du dépôt, sans exception
        et sans chemin allégé.
        """
        steps: list[Step] = []
        for sd in raw["steps"]:
            stype = sd["type"]
            if stype not in STEP_TYPES:
                raise ValueError(f"Type d'étape inconnu : {stype} (étape {sd['id']})")
            opts_raw = sd.get("options")
            if stype == "yes_no" and not opts_raw:
                opts_raw = YES_NO_OPTIONS
            options = [
                Option(code=o["code"], dtmf=o.get("dtmf"), labels=o.get("labels", {}))
                for o in (opts_raw or [])
            ]
            steps.append(
                Step(
                    id=sd["id"],
                    type=stype,
                    text=sd["text"],
                    text_court=sd.get("text_court"),
                    options=options,
                    unit=sd.get("unit"),
                    min=sd.get("min"),
                    max=sd.get("max"),
                    plausible=sd.get("plausible"),
                    expected_seconds=float(sd.get("expected_seconds", 8.0)),
                    min_seconds=float(sd.get("min_seconds", 1.5)),
                    max_relances=int(sd.get("max_relances", 2)),
                    ask_if=sd.get("ask_if"),
                    raking_var=sd.get("raking_var"),
                    corpus_eligible=bool(sd.get("corpus_eligible", True)),
                    indicator=sd.get("indicator"),
                )
            )
        q = cls(
            id=raw["id"],
            version=raw["version"],
            country=raw["country"],
            currency=raw["currency"],
            languages=raw["languages"],
            consent_version=raw.get("consent_version", "1.0"),
            incentive=raw.get("incentive", {}),
            prompts=raw["prompts"],
            steps=steps,
            checks=raw.get("checks", []),
            audio_id=raw.get("audio_id"),
        )
        q.validate()
        return q

    def validate(self) -> None:
        """Refuse un questionnaire incomplet — mieux vaut échouer ici qu'en appel."""
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Identifiants d'étape dupliqués")
        required_prompts = [
            "announce", "consent_survey", "consent_corpus", "consent_corpus_ack",
            "relance_unclear", "relance_dtmf", "thanks", "refusal_ack", "withdrawal",
        ]
        for key in required_prompts:
            if key not in self.prompts:
                raise ValueError(f"Message système manquant : {key}")
        for lang in self.languages:
            for key in required_prompts:
                if lang not in self.prompts[key]:
                    raise ValueError(f"Message '{key}' non traduit en '{lang}'")
            for s in self.steps:
                if lang not in s.text:
                    raise ValueError(f"Étape '{s.id}' non traduite en '{lang}'")
        for s in self.steps:
            if s.type in ("single_choice", "yes_no") and not s.options:
                raise ValueError(f"Étape '{s.id}' sans modalités")
            if s.ask_if and not self.step(s.ask_if["step"]):
                raise ValueError(f"Filtre de '{s.id}' pointe vers une étape inexistante")


def default_questionnaire_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "questionnaires"


def load_by_id(qid: str) -> Questionnaire:
    return Questionnaire.load(default_questionnaire_dir() / f"{qid}.json")
