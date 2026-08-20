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
from ndara.providers.tts import ElevenLabsTTS, NullTTS, tts_for_language  # noqa: E402
from ndara.questionnaire import Questionnaire  # noqa: E402


def _keys_to_speak(q: Questionnaire, lang: str) -> list[tuple[str, str]]:
    """Tout ce que NDARA dira jamais, sauf ce qui change à chaque entretien.

    Les libellés portant un gabarit (le code de retrait, par exemple) ne sont
    pas pré-synthétisables : ils changent d'un répondant à l'autre, donc ils
    sont rendus au client, qui les lit.
    """
    items: list[tuple[str, str]] = []
    for key in q.prompt_keys():
        text = q.prompt(key, lang)
        if not text or "{" in text:
            continue
        items.append((key, text))
    items += [(s.id, s.prompt(lang)) for s in q.steps]
    return items


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Pré-synthèse des libellés NDARA")
    ap.add_argument("--questionnaire", default="prix_denrees_cm")
    ap.add_argument("--out", default="data/audio")
    ap.add_argument("--force", action="store_true", help="régénérer les fichiers existants")
    ap.add_argument("--voices", action="store_true",
                    help="lister les voix du compte ElevenLabs et s'arrêter")
    args = ap.parse_args()

    if args.voices:
        eleven = ElevenLabsTTS()
        if not eleven.key:
            print("ELEVENLABS_API_KEY absente.")
            return
        for v in eleven.voices():
            labels = v.get("labels") or {}
            desc = " · ".join(f"{k}={val}" for k, val in labels.items() if val)
            print(f"  {v.get('voice_id','')}  {v.get('name',''):<24} {desc}")
        print("\nChoisir un identifiant, puis : set ELEVENLABS_VOICE_ID=<identifiant>")
        return

    q = Questionnaire.load(ROOT / "data" / "questionnaires" / f"{args.questionnaire}.json")

    total = written = skipped = 0
    used: dict[str, str] = {}
    for lang in q.languages:
        tts = tts_for_language(lang)
        live = not isinstance(tts, NullTTS)
        used[lang] = tts.name if live else "aucun (voix du navigateur)"
        outdir = Path(ROOT / args.out) / q.id / lang
        outdir.mkdir(parents=True, exist_ok=True)
        for key, text in _keys_to_speak(q, lang):
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
    for lang, name in used.items():
        print(f"  {lang} : {name}")
    if written:
        print("⚠️  Toute modification d'un libellé impose de changer la version du "
              "questionnaire ET de relancer ce script avec --force.")


if __name__ == "__main__":
    main()
