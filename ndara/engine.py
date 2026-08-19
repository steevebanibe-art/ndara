"""Moteur d'entretien : la machine à états d'un appel NDARA.

Séquence imposée, dans cet ordre, sans exception :

    1. ANNONCE      — « je suis une intelligence artificielle »  (jamais facultatif)
    2. CONSENTEMENT 1 — participer à l'enquête
    3. CONSENTEMENT 2 — verser l'enregistrement au corpus public
                        → REFUSABLE SANS CONSÉQUENCE : la personne participe
                          quand même et reçoit la même incitation.
    4. QUESTIONS
    5. REMERCIEMENT + code de retrait

Le refus du consentement 1 met fin à l'appel immédiatement.
Le refus du consentement 2 n'a qu'un seul effet technique : aucun fichier
audio n'est écrit sur le disque. Rien d'autre ne change.
"""
from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from typing import Any

from .coding import Coder, CodingResult, RulesCoder, normalize
from .corpus import CorpusWriter, redact_text
from .models import (
    CODE_REFUSED,
    CODE_SKIPPED,
    CODE_UNCLEAR,
    AnswerMethod,
    Channel,
    Consent,
    Disposition,
    Interview,
    Turn,
    hash_msisdn,
    new_id,
    utcnow,
)
from .questionnaire import Questionnaire, Step
from .storage import Store

CONSENT_STEPS = ("__announce__", "__consent_survey__", "__consent_corpus__")


@dataclass
class Prompt:
    """Ce que le canal (web ou téléphone) doit restituer au répondant."""

    kind: str                     # announce | consent | question | end
    step_id: str
    text: str                     # libellé FIXE, issu du questionnaire
    audio_url: str | None = None  # audio pré-synthétisé si disponible
    options: list[dict[str, Any]] = field(default_factory=list)
    allow_voice: bool = True
    allow_dtmf: bool = False
    input_type: str = "choice"    # choice | number | open | consent | none
    unit: str | None = None
    progress: float = 0.0
    done: bool = False
    interview_id: str | None = None
    note: str | None = None       # message d'accompagnement (relance, accusé)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "step_id": self.step_id, "text": self.text,
            "audio_url": self.audio_url, "options": self.options,
            "allow_voice": self.allow_voice, "allow_dtmf": self.allow_dtmf,
            "input_type": self.input_type, "unit": self.unit,
            "progress": round(self.progress, 3), "done": self.done,
            "interview_id": self.interview_id, "note": self.note,
        }


class InterviewEngine:
    def __init__(self, store: Store, questionnaire: Questionnaire,
                 coder: Coder | None = None, corpus: CorpusWriter | None = None) -> None:
        self.store = store
        self.q = questionnaire
        self.coder = coder or RulesCoder()
        self.corpus = corpus or CorpusWriter(store)

    # ------------------------------------------------------------------
    # Démarrage
    # ------------------------------------------------------------------

    def start(self, *, language: str, stratum: str, channel: str = Channel.WEB.value,
              msisdn: str | None = None, respondent_hash: str | None = None) -> Prompt:
        if language not in self.q.languages:
            language = self.q.languages[0]
        rhash = respondent_hash or (hash_msisdn(msisdn) if msisdn else new_id("anon"))
        iv = Interview(
            id=new_id("iv"),
            questionnaire_id=self.q.id,
            language=language,
            channel=channel,
            respondent_hash=rhash,
            stratum=stratum,
            consent_version=self.q.consent_version,
        )
        self.store.save_interview(iv)
        self.store.log("interview_started", iv.id, stratum=stratum, language=language,
                       channel=channel)
        return self._announce_prompt(iv)

    # ------------------------------------------------------------------
    # Étapes de consentement
    # ------------------------------------------------------------------

    def _announce_prompt(self, iv: Interview) -> Prompt:
        return Prompt(
            kind="announce",
            step_id="__announce__",
            text=self.q.prompt("announce", iv.language),
            input_type="none",
            allow_voice=False,
            interview_id=iv.id,
            progress=0.0,
        )

    def _consent_prompt(self, iv: Interview, which: str) -> Prompt:
        key = "consent_survey" if which == "survey" else "consent_corpus"
        return Prompt(
            kind="consent",
            step_id=f"__consent_{which}__",
            text=self.q.prompt(key, iv.language),
            input_type="consent",
            allow_voice=True,
            allow_dtmf=True,
            options=[
                {"code": "yes", "dtmf": "1",
                 "label": self.q.prompt("yes_label", iv.language)},
                {"code": "no", "dtmf": "2",
                 "label": self.q.prompt("no_label", iv.language)},
            ],
            interview_id=iv.id,
            progress=0.05 if which == "survey" else 0.08,
        )

    # ------------------------------------------------------------------
    # Soumission d'une réponse
    # ------------------------------------------------------------------

    def submit(self, interview_id: str, *, text: str | None = None,
               dtmf: str | None = None, audio_bytes: bytes | None = None,
               audio_ext: str = "webm", asr_confidence: float | None = None,
               duration_ms: int | None = None) -> Prompt:
        iv = self.store.get_interview(interview_id)
        if iv is None:
            raise KeyError(f"Entretien inconnu : {interview_id}")
        if iv.disposition not in (Disposition.IN_PROGRESS.value,):
            return self._end_prompt(iv)

        # -- 1. Annonce : simple accusé, aucune donnée collectée --
        if iv.consent_survey == Consent.PENDING.value and iv.cursor == 0 and not self._announced(iv):
            iv.meta["announced_at"] = utcnow()
            self.store.save_interview(iv)
            return self._consent_prompt(iv, "survey")

        # -- 2. Consentement à l'enquête --
        if iv.consent_survey == Consent.PENDING.value:
            granted = self._read_consent(text, dtmf, iv.language)
            if granted is None:
                p = self._consent_prompt(iv, "survey")
                p.note = self.q.prompt("relance_dtmf", iv.language)
                p.allow_dtmf = True
                return p
            if not granted:
                iv.consent_survey = Consent.REFUSED.value
                iv.disposition = Disposition.REFUSAL.value
                iv.ended_at = utcnow()
                self.store.save_interview(iv)
                self.store.log("consent_survey_refused", iv.id)
                return Prompt(kind="end", step_id="__end__",
                              text=self.q.prompt("refusal_ack", iv.language),
                              input_type="none", allow_voice=False, done=True,
                              interview_id=iv.id, progress=1.0)
            iv.consent_survey = Consent.GRANTED.value
            self.store.save_interview(iv)
            self.store.log("consent_survey_granted", iv.id)
            return self._consent_prompt(iv, "corpus")

        # -- 3. Consentement au corpus (refusable sans conséquence) --
        if iv.consent_corpus == Consent.PENDING.value:
            granted = self._read_consent(text, dtmf, iv.language)
            if granted is None:
                p = self._consent_prompt(iv, "corpus")
                p.note = self.q.prompt("relance_dtmf", iv.language)
                return p
            iv.consent_corpus = (Consent.GRANTED.value if granted else Consent.REFUSED.value)
            self.store.save_interview(iv)
            self.store.log("consent_corpus", iv.id, granted=bool(granted))
            nxt = self._next_prompt(iv)
            nxt.note = self.q.prompt("consent_corpus_ack", iv.language)
            return nxt

        # -- 4. Questions --
        return self._handle_answer(iv, text=text, dtmf=dtmf, audio_bytes=audio_bytes,
                                   audio_ext=audio_ext, asr_confidence=asr_confidence,
                                   duration_ms=duration_ms)

    def _announced(self, iv: Interview) -> bool:
        return bool(iv.meta.get("announced_at"))

    def _read_consent(self, text: str | None, dtmf: str | None, lang: str) -> bool | None:
        if dtmf in ("1", "2"):
            return dtmf == "1"
        if not text:
            return None
        res = RulesCoder().code_answer(
            Step(id="c", type="yes_no", text={lang: ""},
                 options=_yes_no_options()), text, lang)
        if res.code == "yes":
            return True
        if res.code == "no" or res.code == CODE_REFUSED:
            return False
        return None

    # ------------------------------------------------------------------
    # Traitement d'une réponse à une question
    # ------------------------------------------------------------------

    def _handle_answer(self, iv: Interview, *, text, dtmf, audio_bytes,
                       audio_ext, asr_confidence, duration_ms) -> Prompt:
        step = self._current_step(iv)
        if step is None:
            return self._finish(iv)

        seq = len(self.store.turns(iv.id))
        prior = [t for t in self.store.turns(iv.id) if t.step_id == step.id]
        relances = prior[-1].relances if prior else 0

        method = AnswerMethod.DTMF.value if dtmf else (
            AnswerMethod.VOICE.value if audio_bytes else AnswerMethod.TEXT.value)

        # Repli clavier : la modalité est certaine, on ne passe pas par le codeur.
        if dtmf:
            opt = step.option_by_dtmf(dtmf)
            if opt:
                res = CodingResult(opt.code, confidence=1.0, coder="dtmf")
            elif step.type == "numeric":
                res = CodingResult("__num__", value_num=float(dtmf), confidence=1.0, coder="dtmf")
            else:
                res = CodingResult(CODE_UNCLEAR, coder="dtmf")
        else:
            res = self.coder.code_answer(step, text or "", iv.language)

        # Relance : libellé fixe, jamais généré. Dernière relance = passage au clavier.
        if res.code == CODE_UNCLEAR and relances < step.max_relances:
            turn = Turn(interview_id=iv.id, step_id=step.id, seq=seq,
                        answered_at=utcnow(), duration_ms=duration_ms,
                        raw_text=redact_text(text or ""), code=CODE_UNCLEAR,
                        confidence=res.confidence, asr_confidence=asr_confidence,
                        method=method, relances=relances + 1,
                        flags=res.flags + ["relance"])
            self.store.save_turn(turn)
            last = (relances + 1) >= step.max_relances
            p = self._prompt_for_step(iv, step)
            p.note = self.q.prompt("relance_dtmf" if last else "relance_unclear", iv.language)
            p.allow_dtmf = last or p.allow_dtmf
            return p

        audio_path = None
        if audio_bytes and iv.consent_corpus == Consent.GRANTED.value and step.corpus_eligible:
            audio_path = self.corpus.store_audio(iv, step, audio_bytes, audio_ext)

        turn = Turn(
            interview_id=iv.id, step_id=step.id, seq=seq, answered_at=utcnow(),
            duration_ms=duration_ms, raw_text=redact_text(text or ""),
            code=res.code, value_num=res.value_num, confidence=res.confidence,
            asr_confidence=asr_confidence, method=method, relances=relances,
            audio_path=audio_path, flags=res.flags,
        )
        self.store.save_turn(turn)

        if audio_path:
            self.corpus.register(iv, step, turn)

        if res.code == CODE_REFUSED and step.id in ("region", "sex", "age_group"):
            self.store.log("refusal_on_key_var", iv.id, step=step.id)

        iv.cursor = self._index_of(step) + 1
        self.store.save_interview(iv)
        return self._next_prompt(iv)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _index_of(self, step: Step) -> int:
        return next(i for i, s in enumerate(self.q.steps) if s.id == step.id)

    def _current_step(self, iv: Interview) -> Step | None:
        idx = iv.cursor
        codes = self._codes(iv)
        while idx < len(self.q.steps):
            step = self.q.steps[idx]
            if self._should_ask(step, codes):
                return step
            self._record_skip(iv, step, idx)
            idx += 1
            iv.cursor = idx
        return None

    def _codes(self, iv: Interview) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for t in self.store.turns(iv.id):
            if t.code and t.code != CODE_UNCLEAR:
                out[t.step_id] = t.value_num if t.code == "__num__" else t.code
        return out

    def _should_ask(self, step: Step, codes: dict[str, Any]) -> bool:
        if not step.ask_if:
            return True
        cond = step.ask_if
        actual = codes.get(cond["step"])
        if "equals" in cond:
            return actual == cond["equals"]
        if "in" in cond:
            return actual in cond["in"]
        if "not_equals" in cond:
            return actual != cond["not_equals"]
        return True

    def _record_skip(self, iv: Interview, step: Step, idx: int) -> None:
        seq = len(self.store.turns(iv.id))
        if any(t.step_id == step.id for t in self.store.turns(iv.id)):
            return
        self.store.save_turn(Turn(interview_id=iv.id, step_id=step.id, seq=seq,
                                  answered_at=utcnow(), code=CODE_SKIPPED,
                                  method="filter", confidence=1.0))

    def _next_prompt(self, iv: Interview) -> Prompt:
        step = self._current_step(iv)
        self.store.save_interview(iv)
        if step is None:
            return self._finish(iv)
        return self._prompt_for_step(iv, step)

    def _prompt_for_step(self, iv: Interview, step: Step) -> Prompt:
        total = max(1, len(self.q.steps))
        return Prompt(
            kind="question",
            step_id=step.id,
            text=step.prompt(iv.language),
            audio_url=f"/audio/{self.q.id}/{iv.language}/{step.id}.mp3",
            options=[{"code": o.code, "dtmf": o.dtmf, "label": o.label_for(iv.language)}
                     for o in step.options],
            allow_voice=True,
            allow_dtmf=bool(step.options),
            input_type={"numeric": "number", "open_short": "open"}.get(step.type, "choice"),
            unit=step.unit,
            progress=0.1 + 0.9 * (self._index_of(step) / total),
            interview_id=iv.id,
        )

    # ------------------------------------------------------------------
    # Clôture
    # ------------------------------------------------------------------

    def _finish(self, iv: Interview) -> Prompt:
        answered = [t for t in self.store.turns(iv.id)
                    if t.code not in (None, CODE_UNCLEAR, CODE_SKIPPED)]
        substantive = [s for s in self.q.steps if s.id not in CONSENT_STEPS]
        ratio = len(answered) / max(1, len(substantive))
        iv.disposition = (Disposition.COMPLETE.value if ratio >= 0.8
                          else Disposition.PARTIAL.value if ratio >= 0.5
                          else Disposition.BREAKOFF.value)
        iv.withdrawal_code = iv.withdrawal_code or _withdrawal_code()
        iv.ended_at = utcnow()
        self.store.save_interview(iv)
        self.store.log("interview_finished", iv.id, disposition=iv.disposition,
                       answered=len(answered))
        return self._end_prompt(iv)

    def _end_prompt(self, iv: Interview) -> Prompt:
        thanks = self.q.prompt("thanks", iv.language)
        if iv.consent_corpus == Consent.GRANTED.value and iv.withdrawal_code:
            thanks += " " + self.q.prompt("withdrawal", iv.language).replace(
                "{code}", iv.withdrawal_code)
        return Prompt(kind="end", step_id="__end__", text=thanks, input_type="none",
                      allow_voice=False, done=True, interview_id=iv.id, progress=1.0)

    # ------------------------------------------------------------------
    # Droit de retrait
    # ------------------------------------------------------------------

    def withdraw(self, code: str) -> dict[str, Any]:
        """Efface les enregistrements d'un répondant à partir de son code de retrait."""
        row = self.store.conn.execute(
            "SELECT id, respondent_hash FROM interviews WHERE withdrawal_code=?", (code,)
        ).fetchone()
        if not row:
            return {"found": False, "deleted": 0}
        deleted = self.corpus.withdraw(row["respondent_hash"])
        self.store.conn.execute(
            "UPDATE interviews SET consent_corpus=? WHERE respondent_hash=?",
            (Consent.WITHDRAWN.value, row["respondent_hash"]),
        )
        self.store.conn.commit()
        self.store.log("corpus_withdrawn", row["id"], deleted=deleted)
        return {"found": True, "deleted": deleted}


def _withdrawal_code() -> str:
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    return "".join(random.choice(alphabet) for _ in range(6))


def _yes_no_options():
    from .questionnaire import YES_NO_OPTIONS, Option
    return [Option(code=o["code"], dtmf=o["dtmf"], labels=o["labels"]) for o in YES_NO_OPTIONS]
