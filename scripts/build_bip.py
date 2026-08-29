"""Le signal de tour de parole.

    python scripts/build_bip.py

Produit `data/audio/_commun/bip.wav`, la tonalité qui dit « c'est à vous » et
rien d'autre. Le fichier est versionné : ce script sert à le régénérer si le
cahier des charges change, pas à le fabriquer à chaque déploiement. Un fichier
régénéré différemment serait un stimulus différent, donc une rupture de série.

POURQUOI CE FICHIER EXISTE
--------------------------
Premier appel réel, le 29 août : « il faut absolument attendre jusqu'à la fin
pour qu'il écoute ». C'est vrai, et ce n'était écrit nulle part dans l'oreille
du répondant. Twilio documente qu'avant d'ouvrir le décompte d'écoute, il
attend la fin de tous les verbes imbriqués. Entre la dernière syllabe et
l'ouverture de l'écoute, il n'y avait qu'un blanc, et sur une ligne 2G un
blanc nu ne se distingue pas d'une coupure.

Le fondateur savait quoi faire parce qu'il connaît le produit. Une femme de
cinquante ans à Garoua ne le saura pas. Elle parlera pendant la phrase, ne
sera pas entendue, réentendra tout depuis le début, et raccrochera. C'est le
taux de réponse qui se joue là, c'est-à-dire le chiffre sur lequel repose
toute la validité du produit.

CHAQUE CONSTANTE VIENT DU TERRAIN, PAS DU GOÛT
----------------------------------------------
**800 Hz** parce que la bande téléphonique transporte 300 à 3400 Hz et rien
d'autre : au-delà, un codec 2G jette purement et simplement le son.

**Une seule tonalité, jamais trois montantes.** La triple tonalité montante
est le signal d'échec réseau des opérateurs d'Afrique centrale et de l'Ouest.
Elle dirait au répondant exactement l'inverse de ce qu'on veut lui dire.

**350 ms de silence avant.** Sans ce détachement, le bip s'entend comme la
dernière syllabe de « non » et non comme un événement séparé.

**Un fondu de 15 ms aux deux bouts.** Une sinusoïde qui démarre net produit un
claquement large bande que le GSM transforme en grésillement.

**Du WAV, pas du MP3.** Twilio l'écrit noir sur blanc à propos des `<Play>`
imbriqués : « Use a .wav file instead, as transcoding .mp3 files can add
delay. » Ce délai tomberait exactement là où l'appel s'entend comme une
machine.
"""
from __future__ import annotations

import argparse
import math
import wave
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "data" / "audio" / "_commun" / "bip.wav"

ECHANTILLONNAGE = 8000     # Hz, la bande téléphonique, ni plus ni moins
FREQUENCE = 800.0          # Hz, au centre de ce que la ligne transporte
SILENCE_AVANT_MS = 350     # ce qui détache le bip de la phrase précédente
DUREE_MS = 250             # la tonalité elle-même
SILENCE_APRES_MS = 30      # de quoi ne pas ouvrir l'écoute sur une coupure net
FONDU_MS = 15              # contre le claquement de démarrage
AMPLITUDE = 0.35           # ni timide ni agressif à côté d'une voix de studio


def echantillons() -> list[int]:
    """La forme d'onde, en entiers signés sur seize bits."""
    n_avant = int(ECHANTILLONNAGE * SILENCE_AVANT_MS / 1000)
    n_ton = int(ECHANTILLONNAGE * DUREE_MS / 1000)
    n_apres = int(ECHANTILLONNAGE * SILENCE_APRES_MS / 1000)
    n_fondu = max(1, int(ECHANTILLONNAGE * FONDU_MS / 1000))

    sortie = [0] * n_avant
    for i in range(n_ton):
        # Fondu en cosinus surélevé : pente nulle aux deux extrémités, donc
        # aucun front raide à transmettre.
        if i < n_fondu:
            gain = 0.5 - 0.5 * math.cos(math.pi * i / n_fondu)
        elif i > n_ton - n_fondu:
            gain = 0.5 - 0.5 * math.cos(math.pi * (n_ton - i) / n_fondu)
        else:
            gain = 1.0
        v = AMPLITUDE * gain * math.sin(2 * math.pi * FREQUENCE * i / ECHANTILLONNAGE)
        sortie.append(int(max(-1.0, min(1.0, v)) * 32767))
    return sortie + [0] * n_apres


def ecrire(chemin: Path) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    data = echantillons()
    with wave.open(str(chemin), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(ECHANTILLONNAGE)
        f.writeframes(b"".join(
            int(v).to_bytes(2, "little", signed=True) for v in data))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(SORTIE))
    args = ap.parse_args()

    chemin = Path(args.out)
    ecrire(chemin)

    import hashlib
    octets = chemin.read_bytes()
    duree = (SILENCE_AVANT_MS + DUREE_MS + SILENCE_APRES_MS) / 1000
    print(f"{chemin}")
    print(f"  {len(octets)} octets · {duree:.2f} s · "
          f"{ECHANTILLONNAGE} Hz mono 16 bits")
    print(f"  sha256 {hashlib.sha256(octets).hexdigest()[:16]}…")
    print("  Le fichier est versionné : ne le régénère que si le cahier des")
    print("  charges change, un stimulus différent rompt la série.")


if __name__ == "__main__":
    main()
