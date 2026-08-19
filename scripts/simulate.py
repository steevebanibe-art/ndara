"""Simulation de terrain — et surtout : banc de validation de l'auto-audit.

Deux usages :

1. **Remplir le tableau de bord** pour que la démonstration ne soit pas vide.
2. **Mesurer ce que l'auto-audit détecte.** On injecte une proportion connue
   d'entretiens dégradés (réponses en ligne droite, durées impossibles,
   incohérences internes) et on publie la sensibilité et le taux de fausses
   alertes. C'est la table qu'un jury institutionnel lit comme une
   publication, pas comme un argumentaire.

Les entretiens simulés sont marqués ``channel="simulation"`` : ils ne doivent
jamais être confondus avec de la collecte réelle.

    python scripts/simulate.py --n 200 --fraud 0.1 --seed 42
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ndara.console import setup as setup_console  # noqa: E402
from ndara.audit import audit_interview  # noqa: E402
from ndara.coding import RulesCoder  # noqa: E402
from ndara.corpus import CorpusWriter  # noqa: E402
from ndara.engine import InterviewEngine  # noqa: E402
from ndara.models import Channel, Disposition  # noqa: E402
from ndara.questionnaire import Questionnaire  # noqa: E402
from ndara.sampling import draw_frame, to_sample_units  # noqa: E402
from ndara.storage import Store  # noqa: E402

# Issues de terrain d'une composition aléatoire à froid — ordres de grandeur
# réalistes, à remplacer par les vôtres dès la première vague réelle.
OUTCOME_MIX = [
    (Disposition.NONCONTACT.value, 0.42),
    (Disposition.REFUSAL.value, 0.24),
    (Disposition.INELIGIBLE.value, 0.05),
    (Disposition.UNKNOWN_ELIGIBLE.value, 0.06),
    (Disposition.COMPLETE.value, 0.23),
]

# Profils dégradés injectés. Les deux derniers sont volontairement DIFFICILES :
# un détecteur qu'on ne teste que contre des fraudes caricaturales affiche 100 %
# de détection et ne prouve rien. On construit donc des adversaires qui
# ressemblent à des répondants pressés plutôt qu'à des robots.
FRAUD_PROFILES = [
    ("straightliner", 0.22),        # même touche partout, très rapide
    ("speeder", 0.22),              # durées impossibles
    ("incoherent", 0.20),           # viole l'ordre de sévérité de l'échelle
    ("subtle_speeder", 0.18),       # ~55 % du temps attendu : sous le radar grossier
    ("partial_straightliner", 0.18),  # même touche sur 3 items sur 5 seulement
]

REGION_KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]
REGION_W = [0.05, 0.19, 0.04, 0.18, 0.15, 0.12, 0.07, 0.10, 0.03, 0.07]


def pick(rng: random.Random, options, weights):
    return rng.choices(options, weights=weights, k=1)[0]


def answer_for(step_id: str, rng: random.Random, profile: str, ctx: dict) -> dict:
    """Réponse plausible à une étape, selon le profil du répondant.

    ``ctx`` porte l'état intra-entretien. Il sert notamment à respecter
    l'**ordre de sévérité** de l'échelle alimentaire : on ne passe pas une
    journée entière sans manger sans avoir d'abord réduit ses repas. Simuler
    ces deux réponses indépendamment produirait des incohérences chez des
    répondants sains et fausserait la mesure de l'auto-audit.
    """
    if profile == "straightliner":
        return {"dtmf": "1"}
    if profile == "partial_straightliner" and rng.random() < 0.6:
        # Se rabat sur la première modalité la plupart du temps, mais pas toujours :
        # la variance résiduelle suffit à passer sous un test de « ligne droite ».
        return {"dtmf": "1"}

    if step_id == "region":
        return {"dtmf": pick(rng, REGION_KEYS, REGION_W)}
    if step_id == "sex":
        return {"dtmf": pick(rng, ["1", "2"], [0.5, 0.5])}
    if step_id == "age_group":
        return {"dtmf": pick(rng, ["1", "2", "3", "4", "5"], [.26, .27, .26, .14, .07])}
    if step_id == "hh_size":
        return {"text": str(max(1, int(rng.gauss(5.4, 2.3))))}
    if step_id == "bought_rice":
        return {"dtmf": pick(rng, ["1", "2"], [0.55, 0.45])}
    if step_id == "rice_price":
        return {"text": str(int(rng.gauss(850, 160)))}
    if step_id == "oil_price":
        return {"text": str(int(rng.gauss(1600, 320)))}
    if step_id == "price_direction":
        return {"dtmf": pick(rng, ["1", "2", "3"], [0.62, 0.29, 0.09])}
    if step_id == "reduced_meals":
        reduced = rng.random() < 0.31
        ctx["reduced_meals"] = reduced
        return {"dtmf": "1" if reduced else "2"}
    if step_id == "skipped_day":
        # Conditionnel : la privation sévère n'existe pas sans la privation légère.
        skipped = ctx.get("reduced_meals", False) and rng.random() < 0.35
        return {"dtmf": "1" if skipped else "2"}
    return {"dtmf": "1"}


def duration_for(expected_s: float, rng: random.Random, profile: str) -> int:
    if profile in ("straightliner", "speeder"):
        return int(max(200, rng.gauss(expected_s * 1000 * 0.14, 120)))
    if profile in ("subtle_speeder", "partial_straightliner"):
        # 55 % du temps attendu : plus rapide qu'un répondant normal, mais
        # au-dessus des seuils grossiers. C'est le cas difficile.
        return int(max(400, rng.gauss(expected_s * 1000 * 0.55, expected_s * 120)))
    return int(max(600, rng.gauss(expected_s * 1000 * 0.85, expected_s * 210)))


def run_one(engine: InterviewEngine, q: Questionnaire, rng: random.Random,
            lang: str, stratum: str, profile: str) -> str:
    p = engine.start(language=lang, stratum=stratum, channel=Channel.SIMULATION.value)
    iid = p.interview_id
    p = engine.submit(iid)                       # annonce → consentement enquête
    p = engine.submit(iid, dtmf="1")             # enquête : oui
    corpus_yes = rng.random() < 0.72             # ~7 sur 10 acceptent le corpus
    p = engine.submit(iid, dtmf="1" if corpus_yes else "2")

    ctx: dict = {}
    guard = 0
    while not p.done and guard < 60:
        guard += 1
        step = q.step(p.step_id)
        if step is None:
            break
        ans = answer_for(step.id, rng, profile, ctx)
        # Incohérence interne injectée : « journée sans manger » alors que les repas
        # n'ont pas été réduits — violation de l'ordre de sévérité de l'échelle.
        if profile == "incoherent" and step.id == "reduced_meals":
            ans = {"dtmf": "2"}
            ctx["reduced_meals"] = False
        if profile == "incoherent" and step.id == "skipped_day":
            ans = {"dtmf": "1"}
        ans["duration_ms"] = duration_for(step.expected_seconds, rng, profile)
        p = engine.submit(iid, **ans)
    return iid


def run_wave(db_path, q, n: int, fraud: float, seed: int, lang: str,
             corpus_root) -> dict:
    """Mène une vague complète et renvoie les métriques de détection."""
    rng = random.Random(seed)
    store = Store(db_path)
    engine = InterviewEngine(store, q, RulesCoder(), CorpusWriter(store, corpus_root))

    units = to_sample_units(draw_frame(q.country, n, seed=seed))
    store.add_sample_units(units)

    outcomes = [o for o, _ in OUTCOME_MIX]
    weights = [w for _, w in OUTCOME_MIX]

    injected: set[str] = set()
    by_profile: dict[str, str] = {}
    completed = 0
    for u in units:
        outcome = pick(rng, outcomes, weights)   # un seul tirage : deux tirages
        if outcome != Disposition.COMPLETE.value:  # marqueraient « complet » une
            store.set_unit_disposition(u.msisdn_hash, outcome)  # unité jamais interrogée
            continue
        profile = "clean"
        if rng.random() < fraud:
            profile = pick(rng, [p for p, _ in FRAUD_PROFILES],
                           [w for _, w in FRAUD_PROFILES])
        iid = run_one(engine, q, rng, lang, u.stratum, profile)
        store.set_unit_disposition(u.msisdn_hash, Disposition.COMPLETE.value, iid)
        by_profile[iid] = profile
        if profile != "clean":
            injected.add(iid)
        completed += 1

    tp = fp = fn = tn = 0
    detected: dict[str, list[int]] = {}
    for iv in store.interviews():
        if iv.channel != Channel.SIMULATION.value:
            continue
        flagged = audit_interview(q, iv, store.turns(iv.id)).needs_review
        bad = iv.id in injected
        tp += flagged and bad
        fp += flagged and not bad
        fn += (not flagged) and bad
        tn += (not flagged) and (not bad)
        prof = by_profile.get(iv.id, "clean")
        d = detected.setdefault(prof, [0, 0])
        d[0] += 1
        d[1] += 1 if flagged else 0

    return {
        "n_drawn": n, "completed": completed, "injected": len(injected),
        "fraud_target": fraud,
        "sensitivity": tp / (tp + fn) if (tp + fn) else 0.0,
        "fpr": fp / (fp + tn) if (fp + tn) else 0.0,
        "precision": tp / (tp + fp) if (tp + fp) else 0.0,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "by_profile": {k: {"n": v[0], "flagged": v[1],
                           "rate": (v[1] / v[0]) if v[0] else 0.0}
                       for k, v in sorted(detected.items())},
    }


def print_wave(m: dict) -> None:
    print(f"  numéros tirés             {m['n_drawn']}")
    print(f"  entretiens menés          {m['completed']}")
    print(f"  dégradés injectés         {m['injected']} ({m['fraud_target']:.0%} visés)")
    print()
    print("  Détection par l'auto-audit")
    print(f"    sensibilité             {m['sensitivity']:.1%}   (dégradés correctement signalés)")
    print(f"    taux de fausses alertes {m['fpr']:.1%}   (entretiens sains signalés à tort)")
    print(f"    précision               {m['precision']:.1%}")
    print(f"    matrice                 VP={m['tp']}  FP={m['fp']}  FN={m['fn']}  VN={m['tn']}")


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Simulation de vague NDARA")
    ap.add_argument("--n", type=int, default=200, help="numéros tirés")
    ap.add_argument("--fraud", type=float, default=0.10,
                    help="proportion d'entretiens dégradés injectés")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--lang", default="fr")
    ap.add_argument("--questionnaire", default="prix_denrees_cm")
    ap.add_argument("--db", default="data/ndara.db")
    ap.add_argument("--reset", action="store_true", help="repartir d'une base vide")
    ap.add_argument("--sweep", action="store_true",
                    help="table de validation à plusieurs taux de dégradation")
    ap.add_argument("--md", help="écrire la table de validation en Markdown")
    args = ap.parse_args()

    q = Questionnaire.load(ROOT / "data" / "questionnaires" / f"{args.questionnaire}.json")

    # ---------------- balayage : la table de validation du dossier ----------------
    if args.sweep:
        import tempfile
        rows = []
        print(f"Table de validation de l'auto-audit — {q.id} v{q.version}")
        print()
        print(f"{'taux injecté':>13} {'entretiens':>11} {'sensibilité':>12} "
              f"{'fausses alertes':>16} {'précision':>10}")
        print("  " + "-" * 62)
        for rate in (0.02, 0.05, 0.10, 0.20):
            tmp = Path(tempfile.mkdtemp())
            m = run_wave(tmp / "s.db", q, args.n, rate, args.seed, args.lang, tmp / "corpus")
            rows.append(m)
            print(f"{rate:>12.0%} {m['completed']:>11} {m['sensitivity']:>11.1%} "
                  f"{m['fpr']:>15.1%} {m['precision']:>10.1%}")
        print()
        print("  Détection par type de dégradation (taux injecté = 20 %)")
        for prof, d in rows[-1]["by_profile"].items():
            if prof == "clean":
                print(f"    {'sains (fausses alertes)':<26} {d['flagged']:>3} / {d['n']:<4} "
                      f"{d['rate']:>7.1%}")
            else:
                print(f"    {prof:<26} {d['flagged']:>3} / {d['n']:<4} {d['rate']:>7.1%}")
        print()
        print("  Lecture : la sensibilité est la part des entretiens dégradés que le")
        print("  système signale ; le taux de fausses alertes est la part d'entretiens")
        print("  sains signalés à tort. C'est ce second chiffre qui décide de l'adoption :")
        print("  un contrôleur ne rouvre pas un dossier sur une accusation infondée.")
        if args.md:
            lines = [f"# Validation de l'auto-audit — {q.id} v{q.version}", "",
                     f"Vague simulée de {args.n} numéros tirés, graine {args.seed}. "
                     "Des entretiens dégradés (réponses en ligne droite, durées "
                     "impossibles, violation de l'ordre de sévérité de l'échelle "
                     "alimentaire) sont injectés à taux connu.", "",
                     "| Taux injecté | Entretiens | Sensibilité | Fausses alertes | Précision |",
                     "|---:|---:|---:|---:|---:|"]
            for m in rows:
                lines.append(f"| {m['fraud_target']:.0%} | {m['completed']} | "
                             f"{m['sensitivity']:.1%} | {m['fpr']:.1%} | {m['precision']:.1%} |")
            lines += ["", "## Détection par type de dégradation (taux injecté 20 %)", "",
                      "| Profil injecté | n | Signalés | Taux |", "|---|---:|---:|---:|"]
            for prof, d in rows[-1]["by_profile"].items():
                label = "sains (fausses alertes)" if prof == "clean" else prof
                lines.append(f"| {label} | {d['n']} | {d['flagged']} | {d['rate']:.1%} |")
            lines += ["", "Le taux de fausses alertes est le chiffre qui décide de "
                      "l'adoption : signaler à tort un entretien sain coûte une "
                      "revérification inutile et détruit la confiance dans l'outil."]
            Path(args.md).parent.mkdir(parents=True, exist_ok=True)
            Path(args.md).write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
            print()
            print(f"  Table écrite : {args.md}")
        return

    # ---------------- vague unique, persistée ----------------
    db_path = ROOT / args.db
    if args.reset and db_path.exists():
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            extra = Path(str(db_path) + suffix)
            if extra.exists():
                extra.unlink()

    m = run_wave(db_path, q, args.n, args.fraud, args.seed, args.lang,
                 ROOT / "data" / "corpus")
    print(f"Vague simulée — questionnaire {q.id} v{q.version}, graine {args.seed}")
    print_wave(m)
    print()
    print("  Note : la simulation ne fabrique aucun audio. Le corpus vocal reste")
    print("  vide par construction — il ne se remplit qu'avec de la parole réelle")
    print("  consentie.")
    print()
    print("  → python scripts/report.py   pour les estimations pondérées")
    print("  → python web/server.py       pour le tableau de bord")


if __name__ == "__main__":
    main()
