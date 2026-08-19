"""Produit le rapport publiable : estimations, incertitude, qualité, corpus.

    python scripts/report.py                 # affichage console
    python scripts/report.py --md rapport.md # note méthodologique en Markdown
    python scripts/report.py --json out.json # sortie machine

Règle : aucune estimation n'est imprimée sans son intervalle de confiance,
et la section « limites » n'est jamais optionnelle.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ndara.console import setup as setup_console  # noqa: E402
from ndara.analysis import estimate_all  # noqa: E402
from ndara.audit import quality_report  # noqa: E402
from ndara.corpus import CorpusWriter  # noqa: E402
from ndara.questionnaire import Questionnaire  # noqa: E402
from ndara.sampling import load_margins  # noqa: E402
from ndara.storage import Store  # noqa: E402


def fmt(x, d=1):
    return "—" if x is None else f"{x:,.{d}f}".replace(",", " ").replace(".", ",")


def to_markdown(data: dict, quality: dict, corpus: dict) -> str:
    q = data.get("questionnaire", {})
    fw = data.get("fieldwork", {})
    w = data.get("weighting", {})
    lines = [
        f"# NDARA — note de résultats · {q.get('id', '')} v{q.get('version', '')}",
        "",
        "## 1. Collecte",
        "",
        f"- Entretiens exploitables : **{data.get('n', 0)}**",
        f"- Taux de réponse RR3 (AAPOR) : **{fw.get('response_rate_rr3', 0):.1%}** "
        f"(RR2 : {fw.get('response_rate_rr2', 0):.1%})",
        f"- Taux de coopération : **{fw.get('cooperation_rate', 0):.1%}**",
        f"- Effectif effectif après pondération : **{w.get('effective_n', 0):.0f}** "
        f"(effet de plan {w.get('design_effect', 1):.2f})",
        "",
        "## 2. Estimations pondérées",
        "",
        "| Indicateur | n | Estimation | IC 95 % | Erreur-type |",
        "|---|---:|---:|:--|---:|",
    ]
    for r in data.get("estimates", []):
        if r.get("estimate") is None:
            lines.append(f"| {r['label']} | {r.get('n', 0)} | — | — | — |")
            continue
        unit = " %" if r.get("unit") == "%" else (f" {r['unit']}" if r.get("unit") else "")
        d = 1 if r.get("unit") == "%" else 0
        lines.append(
            f"| {r['label']} | {r['n']} | **{fmt(r['estimate'], d)}{unit}** | "
            f"[{fmt(r['ci_low'], d)} ; {fmt(r['ci_high'], d)}] | {fmt(r['se'], 2)} |"
        )

    lines += [
        "",
        "## 3. Auto-audit",
        "",
        f"- Score de qualité moyen : **{quality['quality_score_mean']:.1f} / 100**",
        f"- Entretiens signalés pour revérification : "
        f"**{quality['flagged_share']:.1%}** ({quality['flagged_for_review']} sur "
        f"{quality['interviews_audited']})",
    ]
    ca = quality.get("coding_agreement", {})
    if ca.get("agreement") is not None:
        lines.append(f"- Accord de codage machine/humain : **{ca['agreement']:.1%}** "
                     f"(kappa {ca['kappa']:.2f}, n={ca['n']})")
    else:
        lines.append("- Accord de codage : **non publiable** — aucun sous-échantillon "
                     "recodé à la main.")
    if quality.get("flag_counts"):
        lines += ["", "| Signalement | Entretiens |", "|---|---:|"]
        for k, v in quality["flag_counts"].items():
            lines.append(f"| {k} | {v} |")

    lines += [
        "",
        "## 4. Corpus vocal consenti",
        "",
        f"- Parole consentie : **{corpus.get('minutes', 0)} minutes** "
        f"({corpus.get('segments', 0)} segments, {corpus.get('speakers', 0)} locuteurs)",
        f"- Expurgations appliquées : {corpus.get('redactions', 0)}",
        "- Le corpus n'est pas vendu : publication prévue sous licence ouverte, "
        "copubliée avec l'institution partenaire du pays.",
        "",
        "## 5. Ce que ces chiffres ne disent pas",
        "",
    ]
    lines += [f"- {d}" for d in data.get("disclosure", [])]
    return "\n".join(lines) + "\n"


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Rapport NDARA")
    ap.add_argument("--db", default="data/ndara.db")
    ap.add_argument("--questionnaire", default="prix_denrees_cm")
    ap.add_argument("--margins", default="data/margins/cm_margins.json")
    ap.add_argument("--md", help="écrire une note Markdown")
    ap.add_argument("--json", help="écrire la sortie JSON")
    args = ap.parse_args()

    store = Store(ROOT / args.db)
    q = Questionnaire.load(ROOT / "data" / "questionnaires" / f"{args.questionnaire}.json")
    margins = load_margins(ROOT / args.margins)

    data = estimate_all(store, q, margins)
    ivs = store.interviews()
    quality = quality_report(q, ivs, {iv.id: store.turns(iv.id) for iv in ivs})
    corpus = CorpusWriter(store, ROOT / "data" / "corpus").stats()

    if args.json:
        Path(args.json).write_text(
            json.dumps({"estimates": data, "quality": quality, "corpus": corpus},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON écrit : {args.json}")
    if args.md:
        Path(args.md).write_text(to_markdown(data, quality, corpus), encoding="utf-8")
        print(f"Note écrite : {args.md}")
    if args.json or args.md:
        return

    print(to_markdown(data, quality, corpus))


if __name__ == "__main__":
    main()
