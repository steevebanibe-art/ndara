"""Production des estimations publiées.

Enchaîne : entretiens exploitables → enregistrements → pondération →
estimateurs → intervalles de confiance par jackknife → tableau de sortie.

Règle de publication : **aucune estimation ne sort sans son intervalle de
confiance, son effectif effectif et le taux de réponse de la collecte.**
Un chiffre sans son incertitude n'est pas un résultat, c'est une opinion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .audit import quality_report
from .models import CODE_DONTKNOW, CODE_REFUSED, CODE_SKIPPED, CODE_UNCLEAR, Disposition
from .questionnaire import Questionnaire
from .sampling import outcomes_from_interviews, outcomes_from_units
from .storage import Store
from .weighting import (
    Record,
    build_weights,
    jackknife_ci,
    weighted_mean,
    weighted_median,
    weighted_proportion,
)

MISSING = {CODE_DONTKNOW, CODE_REFUSED, CODE_UNCLEAR, CODE_SKIPPED, None}


# --------------------------------------------------------------------------
# Construction des enregistrements
# --------------------------------------------------------------------------

def build_records(store: Store, q: Questionnaire,
                  include_partial: bool = True) -> list[Record]:
    keep = {Disposition.COMPLETE.value}
    if include_partial:
        keep.add(Disposition.PARTIAL.value)
    records: list[Record] = []
    for iv in store.interviews():
        if iv.disposition not in keep:
            continue
        rec: Record = {"id": iv.id, "stratum": iv.stratum, "language": iv.language}
        for t in store.turns(iv.id):
            if t.code in MISSING:
                continue
            rec[t.step_id] = t.value_num if t.code == "__num__" else t.code
        records.append(rec)
    return records


def frame_counts(store: Store, records: Sequence[Record]) -> dict[str, int]:
    """Effectifs tirés par strate. À défaut de base de sondage (démo web),
    on retombe sur l'effectif des répondants — les poids valent alors 1 et
    c'est dit explicitement dans le rapport."""
    counts: dict[str, int] = {}
    for u in store.sample_units():
        counts[u.stratum] = counts.get(u.stratum, 0) + 1
    if counts:
        return counts
    for r in records:
        counts[r["stratum"]] = counts.get(r["stratum"], 0) + 1
    return counts


# --------------------------------------------------------------------------
# Indicateurs
# --------------------------------------------------------------------------

@dataclass
class Indicator:
    key: str
    label: str
    kind: str                        # mean | proportion | median
    var: str
    code: str | None = None
    unit: str | None = None
    decimals: int = 1

    def estimator(self) -> Callable[[Sequence[Record], Sequence[float]], float]:
        if self.kind == "mean":
            return lambda recs, w: weighted_mean(recs, w, self.var)
        if self.kind == "median":
            return lambda recs, w: weighted_median(recs, w, self.var) or 0.0
        return lambda recs, w: weighted_proportion(recs, w, self.var, self.code or "")


def default_indicators(q: Questionnaire) -> list[Indicator]:
    cur = q.currency
    return [
        Indicator("rice_price_mean", f"Prix moyen du kilogramme de riz ({cur})",
                  "mean", "rice_price", unit=cur, decimals=0),
        Indicator("rice_price_median", f"Prix médian du kilogramme de riz ({cur})",
                  "median", "rice_price", unit=cur, decimals=0),
        Indicator("oil_price_mean", f"Prix moyen du litre d'huile ({cur})",
                  "mean", "oil_price", unit=cur, decimals=0),
        Indicator("prices_up", "Part des ménages déclarant une hausse des prix",
                  "proportion", "price_direction", code="hausse", unit="%", decimals=1),
        Indicator("reduced_meals", "Part des ménages ayant réduit le nombre de repas (7 jours)",
                  "proportion", "reduced_meals", code="yes", unit="%", decimals=1),
        Indicator("skipped_day", "Part des ménages ayant passé une journée sans manger",
                  "proportion", "skipped_day", code="yes", unit="%", decimals=1),
        Indicator("hh_size_mean", "Taille moyenne du ménage", "mean", "hh_size",
                  unit="personnes", decimals=2),
    ]


# --------------------------------------------------------------------------
# Sortie
# --------------------------------------------------------------------------

def estimate_all(store: Store, q: Questionnaire,
                 margins: dict[str, dict[str, float]],
                 indicators: Sequence[Indicator] | None = None,
                 groups: int = 10) -> dict[str, Any]:
    records = build_records(store, q)
    counts = frame_counts(store, records)
    indicators = list(indicators or default_indicators(q))

    if not records:
        return {"n": 0, "note": "aucun entretien exploitable", "estimates": []}

    wr = build_weights(records, counts, margins)
    rows: list[dict[str, Any]] = []
    for ind in indicators:
        sub = [r for r in records if r.get(ind.var) is not None]
        if len(sub) < 3:
            rows.append({"key": ind.key, "label": ind.label, "n": len(sub),
                         "estimate": None, "note": "effectif insuffisant"})
            continue
        res = jackknife_ci(sub, counts, margins, ind.estimator(), groups=groups)
        scale = 100.0 if ind.unit == "%" else 1.0
        rows.append({
            "key": ind.key,
            "label": ind.label,
            "unit": ind.unit,
            "n": len(sub),
            "estimate": round(res["estimate"] * scale, ind.decimals),
            "ci_low": round(res["ci_low"] * scale, ind.decimals),
            "ci_high": round(res["ci_high"] * scale, ind.decimals),
            "se": round(res["se"] * scale, ind.decimals + 1),
            "replicates": res["groups"],
        })

    # Le taux de réponse se calcule sur la BASE DE SONDAGE, pas sur les
    # entretiens : un refus et un non-décroché ne produisent aucun entretien.
    # Compter les issues à partir des seuls entretiens donnerait mécaniquement
    # 100 % de réponse — l'erreur qui détruit un dossier en question-réponse.
    units = store.sample_units()
    outcomes = (outcomes_from_units(units) if units
                else outcomes_from_interviews(store.interviews()))
    turns_by = {iv.id: store.turns(iv.id) for iv in store.interviews()}
    quality = quality_report(q, store.interviews(), turns_by)

    return {
        "questionnaire": {"id": q.id, "version": q.version, "country": q.country,
                          "currency": q.currency},
        "n": len(records),
        "weighting": wr.as_dict(),
        "fieldwork": outcomes.as_dict(),
        "estimates": rows,
        "quality": {
            "quality_score_mean": quality["quality_score_mean"],
            "flagged_share": quality["flagged_share"],
            "flag_counts": quality["flag_counts"],
            "coding_agreement": quality["coding_agreement"],
        },
        "disclosure": _disclosure(wr, outcomes, quality),
    }


def _fr(x: float, decimales: int = 1) -> str:
    """Un nombre écrit en français : virgule décimale, espace avant le pour cent.

    Ces lignes sont lues par un jury francophone et finissent recopiées dans un
    rapport. Un « 21.7% » au milieu d'une page française se voit, et il donne
    l'impression d'un chiffre importé d'ailleurs.
    """
    return f"{x:.{decimales}f}".replace(".", ",")


def _disclosure(wr, outcomes, quality) -> list[str]:
    """Les limites qu'on publie avec les chiffres. Toujours, pas seulement quand ça arrange."""
    lines = [
        f"Taux de réponse (RR3, méthode AAPOR) : {_fr(outcomes.rr3() * 100)} %. "
        f"Taux de coopération : {_fr(outcomes.cooperation() * 100)} %.",
        f"Effectif effectif après pondération : {wr.effective_n:.0f} "
        f"(effet de plan {_fr(wr.design_effect, 2)}).",
        f"Part des entretiens signalés pour revérification par l'auto-audit : "
        f"{_fr(quality['flagged_share'] * 100)} %.",
        "Intervalles de confiance à 95 % par jackknife par groupes, avec recalage "
        "des poids à chaque réplique.",
        "Biais de couverture non corrigé : les ménages sans accès à un téléphone "
        "restent hors du champ de l'enquête.",
    ]
    if not wr.rake_report.variables:
        lines.append(
            "⚠ Aucun calage sur marges : ce questionnaire n'a pas de population "
            "de référence renseignée. Les poids restent des poids de sondage, le "
            "biais de couverture géographique n'est pas corrigé, et ces "
            "estimations ne sont pas publiables comme représentatives.")
    if not wr.rake_report.converged:
        lines.append("⚠ Le calage sur marges n'a pas convergé : estimations à interpréter "
                     "avec prudence.")
    for w in wr.rake_report.warnings:
        lines.append(f"⚠ {w}")
    if quality["coding_agreement"].get("agreement") is None:
        lines.append("⚠ Aucun sous-échantillon recodé à la main : le taux d'erreur de "
                     "codage n'est pas publiable en l'état.")
    return lines
