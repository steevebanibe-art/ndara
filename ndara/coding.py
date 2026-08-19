"""Codage des réponses : de la parole transcrite vers une modalité.

Deux implémentations derrière la même interface :

* ``RulesCoder``  — déterministe, hors ligne, sans clé d'API. C'est le
  moteur par défaut : il tourne aujourd'hui, il est reproductible, et il
  est auditable ligne à ligne devant un jury.
* ``LLMCoder``    — s'appuie sur un modèle de langage pour les cas que les
  règles n'attrapent pas. Il ne peut renvoyer QUE des codes autorisés :
  la sortie est contrainte et vérifiée. Il ne rédige jamais de question.

Le codeur ne décide jamais de relancer tout seul : il renvoie
``__unclear__`` et c'est le moteur d'entretien qui choisit la relance
dans une liste fermée et pré-synthétisée.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

from .models import CODE_DONTKNOW, CODE_REFUSED, CODE_UNCLEAR
from .questionnaire import Step

# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

KHMER_DIGITS = {"០": "0", "១": "1", "២": "2", "៣": "3", "៤": "4",
                "៥": "5", "៦": "6", "៧": "7", "៨": "8", "៩": "9"}

DONTKNOW = {
    "fr": ["je ne sais pas", "sais pas", "aucune idee", "je sais pas", "je ne sais",
           "peut etre", "je ne me souviens pas", "aucune idée"],
    "en": ["i don't know", "dont know", "no idea", "not sure", "can't remember"],
    "km": ["មិនដឹង", "អត់ដឹង", "មិនច្បាស់"],
}

REFUSAL = {
    "fr": ["je ne veux pas repondre", "je prefere ne pas", "ca ne vous regarde pas",
           "je refuse", "laissez moi", "arretez", "raccrochez"],
    "en": ["i refuse", "prefer not to say", "none of your business", "stop calling"],
    "km": ["មិនចង់ឆ្លើយ", "សុំបដិសេធ"],
}

_FR_UNITS = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12,
    "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "vingt": 20,
    "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60,
    "septante": 70, "octante": 80, "huitante": 80, "nonante": 90,
}


def normalize(text: str, lang: str) -> str:
    """Minuscules, sans accents (fr/en), ponctuation réduite. Le khmer est laissé intact."""
    t = (text or "").strip().lower()
    for k, v in KHMER_DIGITS.items():
        t = t.replace(k, v)
    if lang != "km":
        t = "".join(c for c in unicodedata.normalize("NFD", t)
                    if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^\w\sក-៿-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _contains_any(haystack: str, needles: list[str]) -> bool:
    return any(n and n in haystack for n in needles)


def parse_number(text: str, lang: str) -> float | None:
    """Extrait un nombre d'une réponse orale.

    Gère : les chiffres (« 1500 », « 1 500 », « 1.500 »), les chiffres khmers,
    et les nombres écrits en toutes lettres en français jusqu'à 999 999.
    En cas d'échec on renvoie None → relance → repli clavier. C'est voulu :
    mieux vaut une relance qu'une valeur inventée.
    """
    t = normalize(text, lang)
    if not t:
        return None

    # 1) Chiffres explicites, y compris séparateurs de milliers.
    m = re.findall(r"\d[\d\s.,]*", t)
    if m:
        best = max(m, key=lambda s: len(re.sub(r"\D", "", s)))
        digits = re.sub(r"\D", "", best)
        if digits:
            return float(digits)

    if lang != "fr":
        return None

    # 2) Nombres en toutes lettres (français).
    tokens = [tok for tok in re.split(r"[\s-]+", t) if tok]
    total = 0.0
    current = 0.0
    seen = False
    for tok in tokens:
        if tok in ("et", "s"):
            continue
        if tok in _FR_UNITS:
            current += _FR_UNITS[tok]
            seen = True
        elif tok in ("cent", "cents"):
            current = (current or 1) * 100
            seen = True
        elif tok in ("mille", "milles"):
            total += (current or 1) * 1000
            current = 0.0
            seen = True
        elif tok in ("million", "millions"):
            total += (current or 1) * 1_000_000
            current = 0.0
            seen = True
    return (total + current) if seen else None


# --------------------------------------------------------------------------
# Résultat
# --------------------------------------------------------------------------

@dataclass
class CodingResult:
    code: str
    value_num: float | None = None
    confidence: float = 0.0
    coder: str = "rules"
    flags: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.code not in (CODE_UNCLEAR,)


class Coder(Protocol):
    name: str

    def code_answer(self, step: Step, text: str, lang: str) -> CodingResult: ...


# --------------------------------------------------------------------------
# Codeur déterministe
# --------------------------------------------------------------------------

class RulesCoder:
    name = "rules"

    #: en dessous de ce seuil de similarité, on considère qu'on n'a pas compris
    FUZZY_THRESHOLD = 0.82

    def code_answer(self, step: Step, text: str, lang: str) -> CodingResult:
        t = normalize(text, lang)
        if not t:
            return CodingResult(CODE_UNCLEAR, confidence=0.0, coder=self.name)

        if _contains_any(t, [normalize(x, lang) for x in REFUSAL.get(lang, [])]):
            return CodingResult(CODE_REFUSED, confidence=0.95, coder=self.name)
        if _contains_any(t, [normalize(x, lang) for x in DONTKNOW.get(lang, [])]):
            return CodingResult(CODE_DONTKNOW, confidence=0.9, coder=self.name)

        if step.type == "numeric":
            return self._code_numeric(step, t, lang)
        if step.type in ("single_choice", "yes_no"):
            return self._code_choice(step, t, lang)
        # open_short : on conserve le verbatim, le codage se fait a posteriori
        return CodingResult("__verbatim__", confidence=0.6, coder=self.name)

    # -- numérique --

    def _code_numeric(self, step: Step, t: str, lang: str) -> CodingResult:
        val = parse_number(t, lang)
        if val is None:
            return CodingResult(CODE_UNCLEAR, confidence=0.0, coder=self.name)
        flags: list[str] = []
        if step.min is not None and val < step.min:
            return CodingResult(CODE_UNCLEAR, confidence=0.2, coder=self.name,
                                flags=["hors_bornes_bas"])
        if step.max is not None and val > step.max:
            return CodingResult(CODE_UNCLEAR, confidence=0.2, coder=self.name,
                                flags=["hors_bornes_haut"])
        if step.plausible:
            lo, hi = step.plausible
            if not (lo <= val <= hi):
                flags.append("hors_plage_plausible")
        return CodingResult("__num__", value_num=val, confidence=0.9,
                            coder=self.name, flags=flags)

    # -- modalités --

    def _code_choice(self, step: Step, t: str, lang: str) -> CodingResult:
        # a) correspondance exacte d'un libellé ou synonyme
        for opt in step.options:
            for label in opt.labels.get(lang, []):
                nl = normalize(label, lang)
                if nl and (nl == t or nl in t):
                    return CodingResult(opt.code, confidence=0.95, coder=self.name)
        # b) touche annoncée oralement (« numéro trois »)
        num = parse_number(t, lang)
        if num is not None and num == int(num):
            opt = step.option_by_dtmf(str(int(num)))
            if opt:
                return CodingResult(opt.code, confidence=0.8, coder=self.name,
                                    flags=["modalite_par_numero"])
        # c) similarité approchée — tolère les erreurs de transcription
        best_code, best_score = None, 0.0
        for opt in step.options:
            for label in opt.labels.get(lang, []):
                score = difflib.SequenceMatcher(None, normalize(label, lang), t).ratio()
                if score > best_score:
                    best_code, best_score = opt.code, score
        if best_code and best_score >= self.FUZZY_THRESHOLD:
            return CodingResult(best_code, confidence=round(best_score, 2),
                                coder=self.name, flags=["appariement_approche"])
        return CodingResult(CODE_UNCLEAR, confidence=round(best_score, 2), coder=self.name)


# --------------------------------------------------------------------------
# Codeur assisté par modèle de langage (sortie contrainte)
# --------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Tu es un module de CODAGE de réponses d'enquête. Tu ne poses jamais de question, "
    "tu ne reformules jamais, tu ne t'adresses jamais au répondant. "
    "Tu reçois la transcription d'une réponse orale et tu renvoies UNIQUEMENT un objet "
    "JSON {\"code\": <str>, \"value_num\": <nombre|null>, \"confidence\": <0..1>}. "
    "Le champ code doit appartenir strictement à la liste fournie. "
    "Si la réponse est ambiguë, inaudible ou hors sujet, renvoie \"__unclear__\". "
    "Ne devine jamais une valeur numérique qui n'a pas été prononcée."
)


class LLMCoder:
    """Appelle l'API Anthropic via urllib (aucun SDK requis).

    Se rabat silencieusement sur ``RulesCoder`` si la clé est absente ou si
    l'appel échoue : le moteur ne doit jamais s'arrêter à cause du réseau.
    """

    name = "llm"

    def __init__(self, fallback: Coder | None = None,
                 model: str = "claude-sonnet-5", timeout: float = 8.0) -> None:
        self.fallback = fallback or RulesCoder()
        self.model = os.environ.get("NDARA_LLM_MODEL", model)
        self.timeout = timeout
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def code_answer(self, step: Step, text: str, lang: str) -> CodingResult:
        first = self.fallback.code_answer(step, text, lang)
        if first.is_usable and first.confidence >= 0.8:
            return first          # les règles ont tranché : pas d'appel réseau
        if not self.available:
            return first
        try:
            return self._call(step, text, lang) or first
        except Exception:          # réseau, quota, format : on garde les règles
            first.flags.append("llm_indisponible")
            return first

    # -- interne --

    def _allowed_codes(self, step: Step) -> list[str]:
        if step.type == "numeric":
            base = ["__num__"]
        elif step.type == "open_short":
            base = ["__verbatim__"]
        else:
            base = [o.code for o in step.options]
        return base + [CODE_DONTKNOW, CODE_REFUSED, CODE_UNCLEAR]

    def _call(self, step: Step, text: str, lang: str) -> CodingResult | None:
        import urllib.request

        allowed = self._allowed_codes(step)
        options_desc = "\n".join(
            f"- {o.code} : {', '.join(o.labels.get(lang, [o.code]))}" for o in step.options
        ) or "(question numérique ou ouverte)"
        user = (
            f"Question posée (libellé fixe) : {step.prompt(lang)}\n"
            f"Type : {step.type}\n"
            f"Modalités autorisées :\n{options_desc}\n"
            f"Codes autorisés : {allowed}\n"
            f"Transcription de la réponse : \"{text}\"\n"
            "Renvoie uniquement le JSON."
        )
        payload = json.dumps({
            "model": self.model,
            "max_tokens": 200,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key or "",
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        raw = "".join(b.get("text", "") for b in body.get("content", []))
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        code = str(data.get("code", CODE_UNCLEAR))
        if code not in allowed:            # garde-fou : sortie hors périmètre
            return CodingResult(CODE_UNCLEAR, coder=self.name, flags=["llm_hors_perimetre"])
        val = data.get("value_num")
        return CodingResult(
            code=code,
            value_num=float(val) if isinstance(val, (int, float)) else None,
            confidence=float(data.get("confidence", 0.7)),
            coder=self.name,
        )


def default_coder() -> Coder:
    """Codeur par défaut : règles seules, ou règles + LLM si une clé est présente."""
    rules = RulesCoder()
    llm = LLMCoder(fallback=rules)
    return llm if llm.available else rules
