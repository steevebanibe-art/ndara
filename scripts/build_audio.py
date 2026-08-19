"""Pré-synthèse des libellés — à lancer UNE FOIS par version de questionnaire.

Pourquoi pré-synthétiser plutôt que générer à la volée :

* **Méthodologie** : chaque répondant doit entendre exactement le même
  stimulus. Une synthèse à la volée introduit des variations de débit et de
  prosodie, donc un biais d'enquêteur.
* **Coût** : une question = un fichier, réutilisé par tous les appels. Le
  poste « synthèse vocale » tombe à zéro en production.
* **Latence** : un fichier servi en statique arrive plus vite qu'une API.

    set AZURE_SPEECH_KEY=...     (ou export sous bash)
    python scripts/build_audio.py --questionnaire prix_denrees_cm

Sans clé, le script liste ce qui serait produit et s'arrête proprement :
la démonstration navigateur retombe alors sur la voix du navigateur.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ndara.console import setup as setup_console  # noqa: E402
from ndara.providers.tts import AzureTTS, NullTTS  # noqa: E402
from ndara.questionnaire import Questionnaire  # noqa: E402

SYSTEM_KEYS = [
    "announce", "consent_survey", "consent_corpus", "consent_corpus_ack",
    "relance_unclear", "relance_dtmf", "thanks", "refusal_ack",
]


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Pré-synthèse des libellés NDARA")
    ap.add_argument("--questionnaire", default="prix_denrees_cm")
    ap.add_argument("--out", default="data/audio")
    ap.add_argument("--force", action="store_true", help="régénérer les fichiers existants")
    args = ap.parse_args()

    q = Questionnaire.load(ROOT / "data" / "questionnaires" / f"{args.questionnaire}.json")
    tts = AzureTTS()
    live = getattr(tts, "available", False)
    if not live:
        tts = NullTTS()
        print("AZURE_SPEECH_KEY absente — mode inventaire (aucun fichier écrit).\n")

    total = written = skipped = 0
    for lang in q.languages:
        outdir = Path(ROOT / args.out) / q.id / lang
        outdir.mkdir(parents=True, exist_ok=True)
        items: list[tuple[str, str]] = [(k, q.prompt(k, lang)) for k in SYSTEM_KEYS]
        items += [(s.id, s.prompt(lang)) for s in q.steps]
        for key, text in items:
            total += 1
            path = outdir / f"{key}.mp3"
            if path.exists() and not args.force:
                skipped += 1
                continue
            if not live:
                print(f"  [{lang}] {key:<22} {text[:64]}…")
                continue
            data = tts.synthesize(text, lang)
            if data:
                path.write_bytes(data)
                written += 1
                print(f"  [{lang}] {key:<22} {len(data) / 1024:.0f} Ko")

    print(f"\n{total} libellés · {written} écrits · {skipped} déjà présents")
    if live:
        print(f"Voix utilisées : {', '.join(sorted(set(q.languages)))} "
              f"(khmer : km-KH-SreymomNeural)")
        print("⚠️  Toute modification d'un libellé impose de changer la version du "
              "questionnaire ET de relancer ce script avec --force.")


if __name__ == "__main__":
    main()
