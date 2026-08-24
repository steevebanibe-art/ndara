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


def contains_phrase(haystack: str, needle: str, lang: str) -> bool:
    """Présence d'une expression, sur des frontières de mots.

    La recherche en sous-chaîne nue produit de faux positifs qui ne se voient
    pas : « un » est dans « chacun », « est » est dans « c'est ». Sur une
    enquête, un faux positif est pire qu'une relance, parce qu'il ne laisse
    aucune trace. Le khmer ne sépare pas ses mots par des espaces : la
    recherche y reste en sous-chaîne, faute de mieux.
    """
    if not needle:
        return False
    if lang == "km":
        return needle in haystack
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack) is not None


def _contains_any(haystack: str, needles: list[str], lang: str = "fr") -> bool:
    return any(contains_phrase(haystack, n, lang) for n in needles)


# Marques de négation et d'affirmation, pour les questions fermées où le
# répondant ne dit ni « oui » ni « non » mais répond quand même clairement.
NEGATION = {
    "fr": ["pas", "jamais", "aucun", "aucune", "rien", "nul", "non"],
    "en": ["not", "never", "none", "nothing", "no", "didn t", "don t", "haven t"],
    "km": ["មិន", "អត់", "ទេ"],
}
AFFIRMATION = {
    "fr": ["oui", "ouais", "ouep", "si", "bien sur", "tout a fait", "exact",
           "exactement", "effectivement", "voila", "c est ca", "d accord", "absolument"],
    "en": ["yes", "yeah", "yep", "sure", "of course", "exactly", "absolutely", "right"],
    "km": ["បាទ", "ចាស", "យល់ព្រម"],
}
# « pas mal », « pas cher » ne sont pas des négations de la réponse.
NEGATION_FAUX_AMIS = {"fr": ["pas mal", "pas cher", "pas grand chose"], "en": ["not bad"], "km": []}

# Mots qui annoncent qu'un nombre EST la réponse, et non un nombre de passage.
MARQUEURS_TOUCHE = {
    "fr": ["numero", "touche", "chiffre", "reponse", "choix", "option", "le", "la"],
    "en": ["number", "key", "press", "answer", "choice", "option"],
    "km": ["លេខ"],
}


def parse_numbers(text: str, lang: str) -> list[float]:
    """Tous les nombres d'une réponse orale, dans l'ordre où ils sont dits.

    Une phrase spontanée en contient souvent plusieurs : « on est cinq à la
    maison et j'ai payé mille cinq cents francs ». Rendre la liste permet à
    l'appelant de choisir celui qui a un sens pour SA question, au lieu de
    parier sur le plus long.

    Gère les chiffres (« 1500 », « 1 500 »), les chiffres khmers, et les
    nombres écrits en toutes lettres en français jusqu'à 999 999.
    """
    t = normalize(text, lang)
    if not t:
        return []

    trouves: list[float] = []

    # 1) Chiffres explicites, séparateurs de milliers compris.
    for m in re.findall(r"\d[\d\s.,]*", t):
        digits = re.sub(r"\D", "", m)
        if digits:
            trouves.append(float(digits))

    if lang != "fr":
        return trouves

    # 2) Nombres en toutes lettres. Chaque suite ininterrompue de mots-nombres
    #    donne UN nombre : « cinq ... mille cinq cents » en donne deux, parce
    #    que les deux groupes sont séparés par d'autres mots.
    tokens = [tok for tok in re.split(r"[\s-]+", t) if tok]
    total = current = 0.0
    seen = False

    def cloture() -> None:
        nonlocal total, current, seen
        if seen:
            trouves.append(total + current)
        total = current = 0.0
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
        else:
            cloture()
    cloture()
    return trouves


def parse_number(text: str, lang: str) -> float | None:
    """Le nombre le plus probable d'une réponse orale, ou None.

    En cas d'échec on renvoie None → relance → repli clavier. C'est voulu :
    mieux vaut une relance qu'une valeur inventée.
    """
    nombres = parse_numbers(text, lang)
    if not nombres:
        return None
    # À défaut de contexte, le nombre le plus long l'emporte : « mille cinq
    # cents » plutôt que le « cinq » qu'il contient.
    return max(nombres)


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

        if _contains_any(t, [normalize(x, lang) for x in REFUSAL.get(lang, [])], lang):
            return CodingResult(CODE_REFUSED, confidence=0.95, coder=self.name)
        if _contains_any(t, [normalize(x, lang) for x in DONTKNOW.get(lang, [])], lang):
            return CodingResult(CODE_DONTKNOW, confidence=0.9, coder=self.name)

        if step.type == "numeric":
            return self._code_numeric(step, t, lang)
        if step.type in ("single_choice", "yes_no"):
            return self._code_choice(step, t, lang)
        # open_short : on conserve le verbatim, le codage se fait a posteriori
        return CodingResult("__verbatim__", confidence=0.6, coder=self.name)

    # -- numérique --

    def _choisir_nombre(self, step: Step, t: str, lang: str) -> tuple[float | None, list[str]]:
        """Parmi les nombres d'une phrase, celui qui répond à CETTE question.

        Une réponse spontanée en contient plusieurs. Les bornes déclarées de
        la question tranchent : « on est cinq et j'ai payé mille cinq cents »
        donne cinq pour la taille du ménage, mille cinq cents pour un prix.
        Quand plusieurs candidats restent plausibles, on ne devine pas.
        """
        nombres = parse_numbers(t, lang)
        if not nombres:
            return None, []
        if len(nombres) == 1:
            return nombres[0], []

        def dans(borne_bas, borne_haut) -> list[float]:
            return [v for v in nombres
                    if (borne_bas is None or v >= borne_bas)
                    and (borne_haut is None or v <= borne_haut)]

        for bornes, marque in ((step.plausible or (None, None), "nombre_choisi_par_plage_plausible"),
                               ((step.min, step.max), "nombre_choisi_par_bornes")):
            retenus = dans(bornes[0], bornes[1])
            if len(retenus) == 1:
                return retenus[0], [marque]
            if len(retenus) > 1:
                # Plusieurs valeurs tiennent debout : on refuse de parier.
                return None, ["plusieurs_nombres_plausibles"]
        return max(nombres), ["plusieurs_nombres_aucun_plausible"]

    def _code_numeric(self, step: Step, t: str, lang: str) -> CodingResult:
        val, flags = self._choisir_nombre(step, t, lang)
        if val is None:
            return CodingResult(CODE_UNCLEAR, confidence=0.0, coder=self.name,
                                flags=flags)
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

    def _touche_dite(self, t: str, lang: str) -> int | None:
        """Le numéro d'une modalité, quand il est vraiment la réponse.

        Le piège que ceci ferme : « dans le Littoral, on est cinq à la
        maison » contient un cinq qui n'a rien à voir avec la question, et
        l'ancien code en faisait la modalité numéro cinq. Un nombre ne vaut
        comme touche que s'il est annoncé (« numéro trois »), ou s'il est à
        peu près tout ce que la personne a dit.
        """
        tokens = [x for x in t.split() if x]
        marqueurs = MARQUEURS_TOUCHE.get(lang, [])
        for i, tok in enumerate(tokens):
            val = None
            if tok.isdigit():
                val = int(tok)
            elif tok in _FR_UNITS and lang == "fr":
                val = _FR_UNITS[tok]
            if val is None or not (0 <= val <= 9):
                continue
            if i > 0 and tokens[i - 1] in marqueurs:
                return val
            if len(tokens) <= 2:          # « trois », « trois s'il vous plait »
                return val
        return None

    @staticmethod
    def _tranche_du_libelle(label: str, lang: str) -> tuple[float, float] | None:
        """Bornes d'une modalité qui décrit un intervalle, sinon None.

        « 18 24 » vaut [18, 24]. « 65 ou plus » vaut [65, +∞[. Un libellé sans
        deux bornes lisibles, comme « Adamaoua », ne décrit pas un intervalle
        et ne participe pas à ce mécanisme.
        """
        nl = normalize(label, lang)
        nombres = [int(x) for x in re.findall(r"\d+", nl)]
        if len(nombres) >= 2:
            return float(min(nombres)), float(max(nombres))
        ouvert = ("plus" in nl or "more" in nl or "over" in nl)
        if len(nombres) == 1 and ouvert:
            return float(nombres[0]), float("inf")
        return None

    def _code_par_tranche(self, step: Step, t: str, lang: str) -> CodingResult | None:
        """Une valeur dite en clair, rangée dans la tranche qui la contient."""
        tranches = []
        for opt in step.options:
            for label in opt.labels.get(lang, []):
                bornes = self._tranche_du_libelle(label, lang)
                if bornes:
                    tranches.append((bornes, opt.code))
                    break
        # Il faut que la question soit VRAIMENT découpée en tranches, sinon un
        # nombre de passage irait se ranger n'importe où.
        if len(tranches) < 2 or len(tranches) < len(step.options) - 1:
            return None
        for val in parse_numbers(t, lang):
            dedans = {code for (lo, hi), code in tranches if lo <= val <= hi}
            if len(dedans) == 1:
                return CodingResult(dedans.pop(), confidence=0.85, coder=self.name,
                                    flags=["valeur_rangee_en_tranche"])
        return None

    def _score_fenetre(self, label: str, t: str) -> float:
        """Meilleure similarité entre un libellé et une fenêtre de la phrase.

        Comparer un libellé de deux mots à une phrase de vingt donne toujours
        un score bas : la phrase noie le libellé. On fait donc glisser une
        fenêtre de la taille du libellé le long de la phrase.
        """
        mots = t.split()
        n = max(1, len(label.split()))
        meilleur = difflib.SequenceMatcher(None, label, t).ratio()
        for taille in (n, n + 1):
            for i in range(0, max(1, len(mots) - taille + 1)):
                fenetre = " ".join(mots[i:i + taille])
                meilleur = max(meilleur,
                               difflib.SequenceMatcher(None, label, fenetre).ratio())
        return meilleur

    def _code_choice(self, step: Step, t: str, lang: str) -> CodingResult:
        # a) un libellé ou un synonyme, sur des frontières de mots.
        #    Le libellé LE PLUS LONG l'emporte, et c'est indispensable : la
        #    région « Est » s'écrit comme le verbe « est », « même » est un
        #    synonyme de « stable ». Dans « j'habite dans le Littoral, on est
        #    cinq », deux libellés répondent présents et seul le plus long dit
        #    quelque chose. Deux libellés de même longueur qui désignent des
        #    modalités différentes : on ne tranche pas, on relance.
        touches: list[tuple[int, str]] = []
        for opt in step.options:
            for label in opt.labels.get(lang, []):
                nl = normalize(label, lang)
                if nl and (nl == t or contains_phrase(t, nl, lang)):
                    touches.append((len(nl), opt.code))
        if touches:
            longueur_max = max(l for l, _ in touches)
            gagnants = {code for l, code in touches if l == longueur_max}
            if len(gagnants) == 1:
                code = gagnants.pop()
                flags = ["libelle_le_plus_long"] if len(touches) > 1 else []
                return CodingResult(code, confidence=0.95, coder=self.name, flags=flags)
            return CodingResult(CODE_UNCLEAR, confidence=0.4, coder=self.name,
                                flags=["modalites_concurrentes"])

        # a bis) une valeur dite en clair qui tombe dans une tranche déclarée.
        #    « j'ai trente-deux ans » doit donner la tranche 25-34, sans que
        #    personne n'ait à connaître le découpage.
        res = self._code_par_tranche(step, t, lang)
        if res is not None:
            return res

        # b) question fermée où personne n'a dit « oui » ni « non » :
        #    « je n'ai pas acheté de riz » est une réponse claire.
        if step.type == "yes_no":
            res = self._code_negation(step, t, lang)
            if res is not None:
                return res

        # c) touche annoncée oralement, sous garde
        num = self._touche_dite(t, lang)
        if num is not None:
            opt = step.option_by_dtmf(str(num))
            if opt:
                return CodingResult(opt.code, confidence=0.8, coder=self.name,
                                    flags=["modalite_par_numero"])

        # d) similarité approchée — tolère les erreurs de transcription
        best_code, best_score = None, 0.0
        for opt in step.options:
            for label in opt.labels.get(lang, []):
                score = self._score_fenetre(normalize(label, lang), t)
                if score > best_score:
                    best_code, best_score = opt.code, score
        if best_code and best_score >= self.FUZZY_THRESHOLD:
            return CodingResult(best_code, confidence=round(best_score, 2),
                                coder=self.name, flags=["appariement_approche"])
        return CodingResult(CODE_UNCLEAR, confidence=round(best_score, 2), coder=self.name)

    def _code_negation(self, step: Step, t: str, lang: str) -> CodingResult | None:
        """Oui ou non déduits de la tournure, jamais devinés.

        L'ordre compte : un « oui » ou un « non » explicite a déjà été cherché
        avant d'arriver ici. Restent les tournures négatives (« je n'ai pas
        acheté ») et affirmatives (« bien sûr », « tout à fait »). Si rien
        n'est net, on renvoie None et l'entretien relance.
        """
        propre = t
        for faux in NEGATION_FAUX_AMIS.get(lang, []):
            propre = propre.replace(faux, " ")

        options = {o.code.lower(): o for o in step.options}
        oui = options.get("yes") or options.get("oui") or step.option_by_dtmf("1")
        non = options.get("no") or options.get("non") or step.option_by_dtmf("2")
        if oui is None or non is None:
            return None

        if _contains_any(propre, NEGATION.get(lang, []), lang):
            return CodingResult(non.code, confidence=0.82, coder=self.name,
                                flags=["deduit_de_la_negation"])
        if _contains_any(propre, [normalize(x, lang) for x in AFFIRMATION.get(lang, [])], lang):
            return CodingResult(oui.code, confidence=0.82, coder=self.name,
                                flags=["deduit_de_l_affirmation"])
        return None


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
