"""Synthèse vocale (TTS) — utilisée UNE SEULE FOIS, hors ligne.

Les libellés de questions sont pré-synthétisés par ``scripts/build_audio.py``
puis servis comme fichiers statiques. Deux conséquences :

* **coût de synthèse nul en production** (une question = un fichier, réutilisé
  par tous les répondants) ;
* **stimulus rigoureusement identique** pour tout l'échantillon — une exigence
  méthodologique, pas une optimisation.

Le navigateur peut lire les libellés avec sa propre synthèse quand aucun
fichier n'est disponible : c'est un secours pour la démo, jamais pour la
collecte réelle.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

# Voix neuronales khmères disponibles sur Azure (vérifié) :
AZURE_VOICES = {
    "km": "km-KH-SreymomNeural",     # alternative masculine : km-KH-PisethNeural
    "fr": "fr-FR-DeniseNeural",
    "en": "en-US-AriaNeural",
}


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, lang: str) -> bytes: ...


class NullTTS:
    """Pas de synthèse : la démo web utilise la voix du navigateur."""

    name = "null"

    def synthesize(self, text: str, lang: str) -> bytes:
        return b""


class AzureTTS:
    name = "azure"

    def __init__(self, key: str | None = None, region: str | None = None,
                 timeout: float = 30.0) -> None:
        self.key = key or os.environ.get("AZURE_SPEECH_KEY", "")
        self.region = region or os.environ.get("AZURE_SPEECH_REGION", "southeastasia")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.key)

    def synthesize(self, text: str, lang: str) -> bytes:
        import urllib.request
        from xml.sax.saxutils import escape

        voice = AZURE_VOICES.get(lang, AZURE_VOICES["en"])
        locale = {"km": "km-KH", "fr": "fr-FR", "en": "en-US"}.get(lang, "en-US")
        ssml = (
            f"<speak version='1.0' xml:lang='{locale}'>"
            f"<voice name='{voice}'>"
            # Débit légèrement ralenti : ligne téléphonique bruitée, public non lettré.
            f"<prosody rate='-8%'>{escape(text)}</prosody>"
            f"</voice></speak>"
        )
        url = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        req = urllib.request.Request(
            url, data=ssml.encode("utf-8"),
            headers={
                "Ocp-Apim-Subscription-Key": self.key,
                "Content-Type": "application/ssml+xml",
                # 8 kHz mono : c'est la bande passante réelle d'un appel.
                "X-Microsoft-OutputFormat": "audio-16khz-64kbitrate-mono-mp3",
                "User-Agent": "ndara",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()


def default_tts() -> TTSProvider:
    azure = AzureTTS()
    return azure if azure.available else NullTTS()


def audio_path_for(root: str | Path, questionnaire_id: str, lang: str, step_id: str) -> Path:
    return Path(root) / questionnaire_id / lang / f"{step_id}.mp3"
