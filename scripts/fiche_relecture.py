"""Fiche de relecture d'un questionnaire par un locuteur natif.

Un questionnaire traduit et jamais relu par quelqu'un qui parle la langue est
un instrument qui mesure on ne sait quoi. Cette fiche existe pour rendre la
relecture faisable en une heure par une personne occupée, et pour qu'elle ne
puisse pas porter sur autre chose que ce qui sera réellement prononcé.

Elle est **produite depuis le questionnaire**, jamais tenue à la main : une
liste recopiée prend du retard dès la première correction, et on relit alors
une version qui n'existe plus.

    python scripts/fiche_relecture.py --questionnaire prix_denrees_kh \
        --langue km --pivot en > fiche.md

Ce qui est demandé au relecteur tient en trois points, et pas un de plus :
le registre, l'ambiguïté, et la prononçabilité par une voix de synthèse.
Demander davantage, c'est ne rien obtenir.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ndara.console import setup as setup_console  # noqa: E402
from ndara.questionnaire import Questionnaire  # noqa: E402

CONSIGNE = """\
## Ce qui est demandé, et rien d'autre

Chaque phrase ci-dessous sera **dite par une voix de synthèse, au téléphone, à
un ménage inconnu**. Elle ne sera jamais reformulée : la même phrase, mot pour
mot, pour tout l'échantillon. C'est une exigence de méthode, parce qu'un
enquêteur qui reformule introduit un biais.

Merci de regarder trois choses, dans cet ordre.

**1. Le registre.** Le niveau de langue convient-il à un appel non sollicité
vers une personne qu'on ne connaît pas, dans n'importe quelle province ? Trop
familier, trop administratif, ou offensant : dites-le.

**2. L'ambiguïté.** Chaque question doit avoir une seule lecture possible. Une
question d'enquête ambiguë produit un biais qu'aucun traitement statistique ne
rattrape ensuite. Si deux personnes peuvent comprendre deux choses
différentes, c'est un défaut, même si la phrase est correcte.

**3. La prononçabilité.** Certaines tournures passent mal en synthèse vocale :
abréviations, chiffres collés, ponctuation qui coupe mal le souffle. Signalez
ce qui vous paraît risqué, une reformulation est toujours possible.

Ce qui n'est **pas** demandé : améliorer le style, raccourcir, ou proposer
d'autres questions. Le contenu de l'enquête est fixé.

## Comment répondre

Écrivez sous chaque phrase, à la ligne « correction ». Si la phrase convient,
écrivez « ok » et passez à la suivante. Une fiche où la moitié des lignes
disent « ok » est un excellent résultat, pas un travail bâclé.

Vous serez **crédité nommément** dans le questionnaire, qui est un fichier
public, et dans toute publication qui en découle. Dites-nous sous quel nom.
"""


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Fiche de relecture NDARA")
    ap.add_argument("--questionnaire", default="prix_denrees_kh")
    ap.add_argument("--langue", default="km", help="la langue à faire relire")
    ap.add_argument("--pivot", default="en",
                    help="langue de référence affichée à côté, pour comprendre l'intention")
    args = ap.parse_args()

    q = Questionnaire.load(ROOT / "data" / "questionnaires" / f"{args.questionnaire}.json")
    lang, pivot = args.langue, args.pivot
    if lang not in q.languages:
        raise SystemExit(f"« {lang} » n'est pas une langue de {q.id} : {q.languages}")

    lignes: list[str] = []
    out = lignes.append

    out(f"# Relecture du questionnaire « {q.id} », langue {lang}")
    out("")
    out(f"Version relue : **{q.version}**. Pays : {q.country}. Monnaie : {q.currency}.")
    out(f"Langue de référence affichée en regard : {pivot}.")
    out("")
    out("Ce questionnaire est bloqué en brouillon dans le système, et l'interface "
        "l'affiche comme tel à tout visiteur, précisément parce qu'il n'a pas été "
        "relu. Il le restera tant qu'un locuteur natif ne l'aura pas validé.")
    out("")
    out(CONSIGNE)
    out("")
    out("---")
    out("")

    n = 0

    def bloc(titre: str, texte: str, ref: str, note: str = "") -> None:
        nonlocal n
        n += 1
        out(f"### {n}. {titre}")
        if note:
            out(f"*{note}*")
            out("")
        out(f"**{lang}** : {texte}")
        out("")
        out(f"*{pivot}* : {ref}")
        out("")
        out("correction :")
        out("")

    out("## Les messages qui encadrent l'appel")
    out("")
    out("Ces phrases sont dites à chaque appel, avant et après les questions. "
        "L'annonce d'intelligence artificielle et les deux consentements ne sont "
        "pas négociables sur le fond : ils peuvent seulement être mieux dits.")
    out("")

    NOTES = {
        "announce": "Première phrase entendue. Elle doit dire sans détour que "
                    "l'appelant est une machine.",
        "consent_survey": "Consentement à participer. Un refus met fin à l'appel.",
        "consent_corpus": "Consentement séparé, sur l'enregistrement de la voix. "
                          "Il doit être clair qu'un refus ne coûte rien.",
        "thanks": "Dernière phrase, avec la compensation annoncée.",
        "refusal_ack": "Ce qui est dit à quelqu'un qui refuse. Le ton compte "
                       "plus ici que partout ailleurs.",
        "withdrawal": "Contient un code qui change à chaque appel : il est lu "
                      "par le système, pas pré-enregistré.",
    }
    for key in q.prompt_keys():
        texte = q.prompt(key, lang)
        if not texte:
            continue
        bloc(f"Message « {key} »", texte, q.prompt(key, pivot), NOTES.get(key, ""))

    out("---")
    out("")
    out("## Les questions")
    out("")
    out("Chaque question porte ses touches dans le libellé : sans elles, une "
        "personne qui ne sait pas lire ne peut pas répondre au clavier quand la "
        "reconnaissance vocale échoue. Vérifiez que le rappel des touches est "
        "naturel à l'oreille.")
    out("")

    for s in q.steps:
        note = f"type : {s.type}"
        if s.unit:
            note += f" · unité : {s.unit}"
        if s.ask_if:
            note += f" · posée seulement si « {s.ask_if['step']} » vaut " \
                    f"« {s.ask_if.get('equals', s.ask_if.get('in'))} »"
        if not s.corpus_eligible:
            note += " · question sensible, jamais versée au corpus vocal"
        bloc(f"Question « {s.id} »", s.prompt(lang), s.prompt(pivot), note)

        if s.options:
            out("Modalités reconnues à l'oral, en plus des touches. Ajoutez les "
                "façons courantes de le dire que nous aurions manquées.")
            out("")
            out(f"| touche | code | ce que le système reconnaît en {lang} | à ajouter |")
            out("|---|---|---|---|")
            for o in s.options:
                mots = ", ".join(o.labels.get(lang, [])) or "aucune"
                out(f"| {o.dtmf or 'aucune'} | {o.code} | {mots} |  |")
            out("")

    out("---")
    out("")
    out("## Trois questions de fond, pour finir")
    out("")
    out("1. Y a-t-il une question à laquelle vous ne répondriez pas au téléphone "
        "à un inconnu, même à une machine qui annonce ce qu'elle est ?")
    out("")
    out("2. La liste des provinces couvre-t-elle bien là où vivent les gens "
        "qu'il faut atteindre ? Elle est limitée à neuf entrées parce qu'un "
        "clavier de téléphone a dix touches, et la dernière sert à « autre ».")
    out("")
    out("3. La compensation annoncée est-elle d'un montant qui se comprend, "
        "sans paraître ni dérisoire ni suspect ?")
    out("")
    out("---")
    out("")
    out(f"Fiche produite depuis `data/questionnaires/{q.id}.json`, version "
        f"{q.version}. Toute correction apportée à un libellé impose de relancer "
        f"la pré-synthèse des voix avec `--force`.")

    sys.stdout.write("\n".join(lignes) + "\n")


if __name__ == "__main__":
    main()
