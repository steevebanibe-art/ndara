"""Auto-audit : le système contrôle ses propres entretiens.

C'est la couche qui distingue « un modèle de langage au téléphone » d'un
instrument statistique. Elle produit, avec chaque jeu de données, un
**rapport de qualité affichant le taux d'erreur** — y compris quand ce taux
est mauvais.

Deux niveaux :

* par entretien  — indices de satisficing, de fabrication ou de mauvaise
  captation (durées anormales, réponses en ligne droite, taux de « ne sait
  pas », relances, repli clavier, valeurs implausibles, incohérences) ;
* agrégé         — non-réponse par item, accord de codage mesuré sur un
  sous-échantillon recodé à la main (kappa de Cohen).

Principe de conception : l'audit ne produit jamais une accusation, mais une
**priorité de revérification**.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .models import CODE_DONTKNOW, CODE_REFUSED, CODE_SKIPPED, CODE_UNCLEAR, Interview, Turn
from .questionnaire import Questionnaire

# Pénalités appliquées au score de qualité (100 = aucun signalement).
#
# ⚠️ Ces poids sont des valeurs de départ, fixées par jugement méthodologique
# — pas ajustées sur les données simulées. Les ajuster sur la simulation
# reviendrait à mesurer sa propre invention. Ils DOIVENT être recalibrés sur
# la première vague réelle, contre un sous-échantillon réécouté à la main,
# puis publiés avec la sensibilité et le taux de fausses alertes obtenus.
#
# `incoherence_interne` pèse lourd parce qu'un entretien administré par
# machine ne peut pas se tromper de filtre : le questionnaire les applique
# lui-même. Une incohérence logique qui survit vient donc du répondant ou de
# la captation — dans les deux cas elle justifie une réécoute.
FLAG_WEIGHTS = {
    "duree_totale_trop_courte": 25,
    "reponses_trop_rapides": 20,
    "ligne_droite": 20,
    "taux_nsp_eleve": 15,
    "relances_excessives": 10,
    "transcription_faible": 10,
    "valeurs_implausibles": 15,
    "incoherence_interne": 32,
    "repli_clavier_systematique": 5,
}


@dataclass
class InterviewAudit:
    interview_id: str
    quality_score: float
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_review(self) -> bool:
        return self.quality_score < 70

    def as_dict(self) -> dict[str, Any]:
        return {
            "interview_id": self.interview_id,
            "quality_score": round(self.quality_score, 1),
            "needs_review": self.needs_review,
            "flags": self.flags,
            "details": self.details,
        }


def audit_interview(q: Questionnaire, iv: Interview, turns: Sequence[Turn]) -> InterviewAudit:
    flags: list[str] = []
    details: dict[str, Any] = {}
    answered = [t for t in turns if t.code not in (None, CODE_SKIPPED)]

    # -- durée totale --
    durations = [t.duration_ms for t in answered if t.duration_ms]
    total_s = sum(durations) / 1000 if durations else 0.0
    expected_s = sum(q.step(t.step_id).expected_seconds for t in answered if q.step(t.step_id))
    details["duration_s"] = round(total_s, 1)
    details["expected_s"] = round(expected_s, 1)
    if expected_s and total_s and total_s < 0.45 * expected_s:
        flags.append("duree_totale_trop_courte")

    # -- réponses individuelles trop rapides --
    too_fast = 0
    for t in answered:
        step = q.step(t.step_id)
        if step and t.duration_ms and t.duration_ms < step.min_seconds * 1000:
            too_fast += 1
    details["too_fast_items"] = too_fast
    if answered and too_fast / len(answered) > 0.3:
        flags.append("reponses_trop_rapides")

    # -- ligne droite sur les questions à modalités --
    choice_codes = [t.code for t in answered
                    if (q.step(t.step_id) and q.step(t.step_id).type in ("single_choice", "yes_no")
                        and t.code and not t.code.startswith("__"))]
    details["choice_items"] = len(choice_codes)
    if len(choice_codes) >= 4 and len(set(choice_codes)) == 1:
        flags.append("ligne_droite")

    # -- non-réponse partielle --
    dk = sum(1 for t in answered if t.code in (CODE_DONTKNOW, CODE_REFUSED, CODE_UNCLEAR))
    details["dk_refusal_unclear"] = dk
    if answered and dk / len(answered) > 0.34:
        flags.append("taux_nsp_eleve")

    # -- relances --
    relances = sum(t.relances for t in turns)
    details["relances"] = relances
    if answered and relances / len(answered) > 0.8:
        flags.append("relances_excessives")

    # -- qualité de transcription --
    confs = [t.asr_confidence for t in answered if t.asr_confidence is not None]
    # Un tour sans confiance n'est pas un tour mal transcrit. Les modèles
    # téléphoniques de Twilio n'en fournissent pas, et le taire donnerait un
    # rapport de qualité qui parle d'une mesure qu'il n'a pas faite.
    sans = [t for t in answered if t.asr_confidence is None and t.method != "dtmf"]
    if sans:
        details["asr_confiance_non_fournie"] = len(sans)
    if confs:
        details["asr_confidence_mean"] = round(statistics.fmean(confs), 3)
        if statistics.fmean(confs) < 0.55:
            flags.append("transcription_faible")

    # -- repli clavier --
    dtmf = sum(1 for t in answered if t.method == "dtmf")
    details["dtmf_items"] = dtmf
    if answered and dtmf / len(answered) > 0.8:
        flags.append("repli_clavier_systematique")

    # -- valeurs implausibles --
    implausible = sum(1 for t in answered if "hors_plage_plausible" in (t.flags or []))
    details["implausible_values"] = implausible
    if implausible >= 2:
        flags.append("valeurs_implausibles")

    # -- cohérence interne, définie dans le questionnaire --
    codes = {t.step_id: (t.value_num if t.code == "__num__" else t.code) for t in turns}
    broken = [c["id"] for c in q.checks if not _check_holds(c, codes)]
    details["failed_checks"] = broken
    if broken:
        flags.append("incoherence_interne")

    penalty = sum(FLAG_WEIGHTS.get(f, 5) for f in flags)
    return InterviewAudit(interview_id=iv.id, quality_score=max(0.0, 100.0 - penalty),
                          flags=flags, details=details)


def _check_holds(check: dict[str, Any], codes: dict[str, Any]) -> bool:
    """Évalue une règle de cohérence. Une variable absente ne fait pas échouer la règle."""
    kind = check.get("type")
    if kind == "implies":
        left, right = check["if"], check["then"]
        lv = codes.get(left["step"])
        if lv is None or lv != left["equals"]:
            return True
        rv = codes.get(right["step"])
        if rv is None:
            return True
        if "equals" in right:
            return rv == right["equals"]
        if "not_equals" in right:
            return rv != right["not_equals"]
        if "max" in right:
            return isinstance(rv, (int, float)) and rv <= right["max"]
        if "min" in right:
            return isinstance(rv, (int, float)) and rv >= right["min"]
    if kind == "ratio_max":
        a, b = codes.get(check["numerator"]), codes.get(check["denominator"])
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or b == 0:
            return True
        return (a / b) <= check["max"]
    return True


# --------------------------------------------------------------------------
# Rapport agrégé
# --------------------------------------------------------------------------

def item_report(q: Questionnaire, turns: Iterable[Turn]) -> list[dict[str, Any]]:
    """Qualité par question, à raison d'UNE observation par entretien.

    Une relance crée un tour supplémentaire pour la même question. Compter les
    tours gonflerait l'effectif et diluerait le taux de non-réponse : on ne
    retient donc que le dernier tour de chaque couple (entretien, question),
    et on comptabilise les relances à part.
    """
    last: dict[tuple[str, str], Turn] = {}
    relances: dict[tuple[str, str], int] = {}
    for t in turns:
        key = (t.interview_id, t.step_id)
        relances[key] = max(relances.get(key, 0), t.relances)
        if key not in last or t.seq >= last[key].seq:
            last[key] = t

    by_step: dict[str, list[Turn]] = {}
    for (iid, sid), t in last.items():
        t.relances = relances[(iid, sid)]
        by_step.setdefault(sid, []).append(t)

    out = []
    for step in q.steps:
        ts = [t for t in by_step.get(step.id, []) if t.code != CODE_SKIPPED]
        if not ts:
            continue
        n = len(ts)
        miss = sum(1 for t in ts if t.code in (CODE_DONTKNOW, CODE_REFUSED, CODE_UNCLEAR))
        confs = [t.asr_confidence for t in ts if t.asr_confidence is not None]
        out.append({
            "step_id": step.id,
            "type": step.type,
            "n": n,
            "item_nonresponse_rate": round(miss / n, 4),
            "mean_relances": round(sum(t.relances for t in ts) / n, 2),
            "dtmf_fallback_rate": round(sum(1 for t in ts if t.method == "dtmf") / n, 4),
            "mean_asr_confidence": round(statistics.fmean(confs), 3) if confs else None,
            "mean_duration_s": round(
                statistics.fmean([t.duration_ms / 1000 for t in ts if t.duration_ms]), 1)
            if any(t.duration_ms for t in ts) else None,
        })
    return out


def coding_agreement(machine: dict[str, str], gold: dict[str, str]) -> dict[str, Any]:
    """Accord entre le codage automatique et un recodage humain.

    C'est **le** taux d'erreur qu'on publie. Il exige un sous-échantillon
    recodé à la main : sans lui, on ne publie rien et on le dit.
    """
    keys = [k for k in gold if k in machine]
    n = len(keys)
    if n == 0:
        return {"n": 0, "agreement": None, "kappa": None,
                "note": "aucun sous-échantillon recodé — taux d'erreur non publiable"}
    agree = sum(1 for k in keys if machine[k] == gold[k])
    po = agree / n
    cats = set(machine[k] for k in keys) | set(gold[k] for k in keys)
    pe = sum(
        (sum(1 for k in keys if machine[k] == c) / n) * (sum(1 for k in keys if gold[k] == c) / n)
        for c in cats
    )
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    return {"n": n, "agreement": round(po, 4), "kappa": round(kappa, 4),
            "disagreements": n - agree}


def quality_report(q: Questionnaire, interviews: Sequence[Interview],
                   turns_by_interview: dict[str, list[Turn]],
                   gold: dict[str, str] | None = None) -> dict[str, Any]:
    audits = [audit_interview(q, iv, turns_by_interview.get(iv.id, [])) for iv in interviews]
    scores = [a.quality_score for a in audits] or [0.0]
    flagged = [a for a in audits if a.needs_review]
    flag_counts: dict[str, int] = {}
    for a in audits:
        for f in a.flags:
            flag_counts[f] = flag_counts.get(f, 0) + 1

    machine = {}
    for iid, ts in turns_by_interview.items():
        for t in ts:
            if t.code:
                machine[f"{iid}:{t.step_id}"] = t.code

    return {
        "interviews_audited": len(audits),
        "quality_score_mean": round(statistics.fmean(scores), 1),
        "quality_score_median": round(statistics.median(scores), 1),
        "flagged_for_review": len(flagged),
        "flagged_share": round(len(flagged) / len(audits), 4) if audits else 0.0,
        "flag_counts": dict(sorted(flag_counts.items(), key=lambda kv: -kv[1])),
        "items": item_report(q, [t for ts in turns_by_interview.values() for t in ts]),
        "coding_agreement": coding_agreement(machine, gold or {}),
        "review_queue": [a.as_dict() for a in sorted(audits, key=lambda a: a.quality_score)[:20]],
    }
