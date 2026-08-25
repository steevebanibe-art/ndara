"""Pondération et estimation — la couche que personne d'autre ne construit.

Chaîne complète, en Python pur :

    poids de sondage  →  correction de non-réponse  →  calage sur marges
    (raking / IPF)    →  écrêtement  →  estimation  →  intervalle de confiance
                                                       par jackknife par groupes

Le calage est ce qui rattrape la couverture géographique, impossible à
stratifier sur une base RDD mobile (voir ``sampling.py``). Le jackknife par
groupes est utilisé plutôt qu'une formule fermée parce que le calage rend la
variance analytique fausse : on re-cale à chaque réplique.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

Record = dict[str, Any]


# --------------------------------------------------------------------------
# 1. Poids de sondage et non-réponse
# --------------------------------------------------------------------------

def design_weights(records: Sequence[Record], frame_counts: dict[str, int]) -> list[float]:
    """Poids de base par classe de pondération (ici : la strate opérateur).

    ``d_i = N_h / r_h`` où ``N_h`` est le nombre d'unités tirées dans la
    strate et ``r_h`` le nombre de répondants de cette strate. Cette forme
    combine la probabilité d'inclusion et l'ajustement de non-réponse dans
    une seule classe — pratique standard quand la non-réponse est traitée
    par classes homogènes.
    """
    resp: dict[str, int] = {}
    for r in records:
        resp[r["stratum"]] = resp.get(r["stratum"], 0) + 1
    out = []
    for r in records:
        h = r["stratum"]
        n_h = max(1, resp.get(h, 1))
        out.append(frame_counts.get(h, n_h) / n_h)
    return out


# --------------------------------------------------------------------------
# 2. Calage sur marges (raking / IPF)
# --------------------------------------------------------------------------

@dataclass
class RakeReport:
    converged: bool
    iterations: int
    max_gap: float
    warnings: list[str] = field(default_factory=list)
    # Les variables sur lesquelles on a réellement calé. Vide veut dire qu'il
    # n'y a eu aucun calage, et cela doit se lire : sans marges, les poids
    # restent des poids de sondage, et le biais de couverture n'est pas
    # corrigé. Un calage inexistant qui « converge » est le pire des deux
    # mondes, parce qu'il ressemble à un calage réussi.
    variables: list[str] = field(default_factory=list)


def rake(records: Sequence[Record], weights: Sequence[float],
         margins: dict[str, dict[str, float]], *, max_iter: int = 60,
         tol: float = 1e-4) -> tuple[list[float], RakeReport]:
    """Ajustement proportionnel itératif sur des marges en proportions.

    ``margins`` : {"region": {"CENTRE": 0.19, ...}, "sex": {...}, ...}
    Les modalités absentes de l'échantillon sont signalées, pas ignorées en
    silence : une case vide est une information, pas un détail.
    """
    w = list(weights)
    warnings: list[str] = []
    variables = list(margins)
    for var, targets in margins.items():
        present = {r.get(var) for r in records}
        for cat in targets:
            if cat not in present:
                warnings.append(f"marge '{var}={cat}' absente de l'échantillon")

    gap = float("inf")
    it = 0
    for it in range(1, max_iter + 1):
        gap = 0.0
        for var, targets in margins.items():
            total = sum(w)
            if total <= 0:
                break
            for cat, target_share in targets.items():
                idx = [i for i, r in enumerate(records) if r.get(var) == cat]
                if not idx:
                    continue
                current = sum(w[i] for i in idx) / total
                if current <= 0:
                    continue
                factor = target_share / current
                gap = max(gap, abs(current - target_share))
                for i in idx:
                    w[i] *= factor
        if gap < tol:
            break
    return w, RakeReport(converged=gap < tol, iterations=it, max_gap=gap,
                         warnings=warnings, variables=variables)


def trim(weights: Sequence[float], factor: float = 4.0) -> tuple[list[float], int]:
    """Écrête les poids extrêmes à ``factor`` × la moyenne, puis renormalise.

    Un poids de 50 sur un répondant unique fait basculer une estimation :
    on préfère un peu de biais à beaucoup de variance, et on dit combien de
    poids ont été écrêtés.
    """
    if not weights:
        return [], 0
    mean = sum(weights) / len(weights)
    cap = factor * mean
    trimmed = [min(w, cap) for w in weights]
    n_trimmed = sum(1 for a, b in zip(weights, trimmed) if a != b)
    total_before, total_after = sum(weights), sum(trimmed)
    if total_after > 0:
        scale = total_before / total_after
        trimmed = [w * scale for w in trimmed]
    return trimmed, n_trimmed


def design_effect(weights: Sequence[float]) -> float:
    """Effet de plan dû à la variabilité des poids (Kish)."""
    n = len(weights)
    if n == 0:
        return 1.0
    s = sum(weights)
    ss = sum(w * w for w in weights)
    return (n * ss / (s * s)) if s else 1.0


def effective_n(weights: Sequence[float]) -> float:
    deff = design_effect(weights)
    return len(weights) / deff if deff else 0.0


# --------------------------------------------------------------------------
# 3. Estimateurs
# --------------------------------------------------------------------------

def weighted_proportion(records: Sequence[Record], weights: Sequence[float],
                        var: str, code: str) -> float:
    num = den = 0.0
    for r, w in zip(records, weights):
        val = r.get(var)
        if val is None:
            continue
        den += w
        if val == code:
            num += w
    return (num / den) if den else 0.0


def weighted_mean(records: Sequence[Record], weights: Sequence[float], var: str) -> float:
    num = den = 0.0
    for r, w in zip(records, weights):
        val = r.get(var)
        if val is None or not isinstance(val, (int, float)):
            continue
        num += w * float(val)
        den += w
    return (num / den) if den else 0.0


def weighted_median(records: Sequence[Record], weights: Sequence[float], var: str) -> float | None:
    pairs = sorted(((float(r[var]), w) for r, w in zip(records, weights)
                    if isinstance(r.get(var), (int, float))), key=lambda p: p[0])
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


# --------------------------------------------------------------------------
# 4. Variance : jackknife par groupes (delete-a-group)
# --------------------------------------------------------------------------

def jackknife_ci(records: Sequence[Record], frame_counts: dict[str, int],
                 margins: dict[str, dict[str, float]],
                 estimator: Callable[[Sequence[Record], Sequence[float]], float],
                 *, groups: int = 10, trim_factor: float = 4.0,
                 z: float = 1.96) -> dict[str, float]:
    """Estimation ponctuelle + intervalle de confiance.

    Chaque réplique retire un groupe aléatoire systématique et **re-cale**
    les poids : c'est ce qui rend l'intervalle honnête quand on a rakké.
    """
    n = len(records)
    if n < groups * 2:
        groups = max(2, n // 2) if n >= 4 else 2

    w0 = design_weights(records, frame_counts)
    w0, _ = rake(records, w0, margins)
    w0, _ = trim(w0, trim_factor)
    theta = estimator(records, w0)

    reps: list[float] = []
    for g in range(groups):
        keep = [i for i in range(n) if i % groups != g]
        if len(keep) < 2:
            continue
        sub = [records[i] for i in keep]
        wg = design_weights(sub, frame_counts)
        wg, _ = rake(sub, wg, margins)
        wg, _ = trim(wg, trim_factor)
        reps.append(estimator(sub, wg))

    if len(reps) < 2:
        return {"estimate": theta, "se": 0.0, "ci_low": theta, "ci_high": theta,
                "groups": len(reps)}

    g_eff = len(reps)
    var = (g_eff - 1) / g_eff * sum((t - theta) ** 2 for t in reps)
    se = math.sqrt(max(0.0, var))
    return {
        "estimate": theta,
        "se": se,
        "ci_low": theta - z * se,
        "ci_high": theta + z * se,
        "groups": g_eff,
    }


# --------------------------------------------------------------------------
# 5. Chaîne complète
# --------------------------------------------------------------------------

@dataclass
class WeightingResult:
    weights: list[float]
    rake_report: RakeReport
    n_trimmed: int
    design_effect: float
    effective_n: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": len(self.weights),
            "design_effect": round(self.design_effect, 3),
            "effective_n": round(self.effective_n, 1),
            "trimmed_weights": self.n_trimmed,
            "raking": {
                "converged": self.rake_report.converged,
                "iterations": self.rake_report.iterations,
                "max_gap": round(self.rake_report.max_gap, 5),
                "warnings": self.rake_report.warnings,
                "variables": self.rake_report.variables,
            },
        }


def build_weights(records: Sequence[Record], frame_counts: dict[str, int],
                  margins: dict[str, dict[str, float]],
                  trim_factor: float = 4.0) -> WeightingResult:
    w = design_weights(records, frame_counts)
    w, report = rake(records, w, margins)
    w, n_trimmed = trim(w, trim_factor)
    return WeightingResult(weights=w, rake_report=report, n_trimmed=n_trimmed,
                           design_effect=design_effect(w), effective_n=effective_n(w))
