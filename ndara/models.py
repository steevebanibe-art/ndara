"""Modèles de données NDARA.

Aucune dépendance externe : stdlib uniquement.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> str:
    """Horodatage ISO 8601 en UTC, à la seconde."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def hash_msisdn(msisdn: str, salt: str | None = None) -> str:
    """Pseudonymise un numéro de téléphone.

    Le numéro en clair ne doit JAMAIS être écrit en base ni dans le corpus.
    Le sel vit dans l'environnement (NDARA_SALT) et n'est pas versionné.
    """
    salt = salt or os.environ.get("NDARA_SALT", "ndara-dev-salt-a-remplacer")
    digest = hmac.new(salt.encode("utf-8"), msisdn.encode("utf-8"), hashlib.sha256)
    return "r_" + digest.hexdigest()[:20]


class Disposition(str, Enum):
    """Codes de disposition (inspirés des standards AAPOR).

    Ce sont eux qui permettent de calculer un taux de réponse défendable.
    """

    COMPLETE = "complete"          # I — entretien complet
    PARTIAL = "partial"            # P — entretien partiel exploitable
    BREAKOFF = "breakoff"          # abandon en cours, non exploitable
    REFUSAL = "refusal"            # R — refus explicite
    NONCONTACT = "noncontact"      # NC — sonne, pas de réponse / boîte vocale
    INELIGIBLE = "ineligible"      # non éligible (hors champ, mineur, numéro pro)
    UNKNOWN_ELIGIBLE = "unknown"   # UH/UO — éligibilité inconnue
    IN_PROGRESS = "in_progress"    # état transitoire


class Consent(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    REFUSED = "refused"
    WITHDRAWN = "withdrawn"


class Channel(str, Enum):
    WEB = "web"          # démonstration navigateur (micro)
    PHONE = "phone"      # appel réel via l'opérateur
    SIMULATION = "simulation"


class AnswerMethod(str, Enum):
    VOICE = "voice"      # réponse vocale transcrite
    DTMF = "dtmf"        # repli clavier (touches)
    TEXT = "text"        # saisie texte (démo / secours)


# Codes réservés : ce que le codeur peut renvoyer en dehors d'une modalité.
CODE_DONTKNOW = "__dk__"
CODE_REFUSED = "__ref__"
CODE_UNCLEAR = "__unclear__"
CODE_SKIPPED = "__skipped__"
RESERVED_CODES = {CODE_DONTKNOW, CODE_REFUSED, CODE_UNCLEAR, CODE_SKIPPED}


@dataclass
class Turn:
    """Un tour de parole : une question posée, une réponse codée."""

    interview_id: str
    step_id: str
    seq: int
    asked_at: str = field(default_factory=utcnow)
    answered_at: str | None = None
    duration_ms: int | None = None
    raw_text: str | None = None          # transcription brute (jamais publiée telle quelle)
    code: str | None = None              # modalité retenue ou code réservé
    value_num: float | None = None       # valeur numérique si question numérique
    confidence: float | None = None      # confiance du codage [0,1]
    asr_confidence: float | None = None  # confiance de la transcription [0,1]
    method: str = AnswerMethod.VOICE.value
    relances: int = 0                    # nombre de relances déclenchées
    audio_path: str | None = None        # chemin local si consentement corpus accordé
    flags: list[str] = field(default_factory=list)


@dataclass
class Interview:
    """Un entretien, du décroché au raccroché."""

    id: str
    questionnaire_id: str
    language: str
    channel: str
    respondent_hash: str
    stratum: str                          # strate d'échantillonnage (opérateur)
    started_at: str = field(default_factory=utcnow)
    ended_at: str | None = None
    disposition: str = Disposition.IN_PROGRESS.value
    consent_survey: str = Consent.PENDING.value
    consent_corpus: str = Consent.PENDING.value
    consent_version: str = "1.0"
    withdrawal_code: str | None = None
    cursor: int = 0                       # index de l'étape courante
    weight: float | None = None           # pondération finale (calculée a posteriori)
    quality_score: float | None = None
    flags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SampleUnit:
    """Une unité tirée dans la base de sondage (un numéro)."""

    id: str
    msisdn_hash: str
    stratum: str
    country: str
    attempts: int = 0
    last_attempt_at: str | None = None
    disposition: str = Disposition.UNKNOWN_ELIGIBLE.value
    interview_id: str | None = None


@dataclass
class CorpusItem:
    """Un segment de parole versé au corpus — uniquement si consentement explicite."""

    id: str
    interview_id: str
    respondent_hash: str
    language: str
    step_id: str
    audio_path: str
    duration_ms: int
    transcript: str
    stratum: str
    weight: float | None = None
    demographics: dict[str, Any] = field(default_factory=dict)
    consent_version: str = "1.0"
    licence: str = "CC-BY-4.0"
    created_at: str = field(default_factory=utcnow)
    redactions: int = 0
