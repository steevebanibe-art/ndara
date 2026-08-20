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


class ElevenLabsTTS:
    """Voix naturelle, pour les langues où elle est réellement meilleure.

    Choisi pour le français d'Afrique. Le but n'est pas de faire passer la
    machine pour quelqu'un : l'annonce d'ouverture dit qu'elle est une
    intelligence artificielle, et cela ne change pas. Le but est d'être
    compris et supporté pendant deux minutes et demie par une personne qui
    n'a peut-être jamais parlé à une machine, sur une ligne bruitée.

    La voix se choisit par ``ELEVENLABS_VOICE_ID``. Une voix clonée à partir
    d'un locuteur du pays, avec son accord écrit, vaut mieux que n'importe
    quelle voix de catalogue : un accent lointain coûte de la compréhension
    exactement là où l'enquête va chercher les gens les moins joignables.
    """

    name = "elevenlabs"

    def __init__(self, key: str | None = None, voice_id: str | None = None,
                 model: str | None = None, timeout: float = 60.0) -> None:
        self.key = key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "")
        self.model = model or os.environ.get(
            "ELEVENLABS_MODEL", "eleven_multilingual_v2")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.key and self.voice_id)

    def voices(self) -> list[dict]:
        """Les voix du compte, pour choisir un identifiant sans quitter le terminal."""
        import json as _json
        import urllib.request

        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": self.key, "User-Agent": "ndara"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return _json.loads(resp.read()).get("voices", [])

    def synthesize(self, text: str, lang: str) -> bytes:
        import json as _json
        import urllib.request

        body = _json.dumps({
            "text": text,
            "model_id": self.model,
            # Stabilité haute : le stimulus doit être le même d'un libellé à
            # l'autre. Une lecture expressive et variable serait, ici, un défaut.
            "voice_settings": {"stability": 0.75, "similarity_boost": 0.75,
                               "style": 0.0, "use_speaker_boost": True},
        }).encode("utf-8")
        url = (f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
               # 22 kHz mono : au dessus de la bande passante d'un appel, en
               # dessous du poids inutile d'un fichier de studio.
               "?output_format=mp3_22050_32")
        req = urllib.request.Request(
            url, data=body,
            headers={"xi-api-key": self.key, "Content-Type": "application/json",
                     "Accept": "audio/mpeg", "User-Agent": "ndara"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()


def tts_for_language(lang: str) -> TTSProvider:
    """Le bon fournisseur par langue, et la raison est écrite ici.

    ElevenLabs pour le français et l'anglais : c'est là qu'il est nettement
    meilleur. Azure pour le khmer, parce qu'il est le seul grand fournisseur
    à avoir des voix neuronales khmères en production (km-KH-SreymomNeural).
    Aucune clé : la démonstration retombe sur la voix du navigateur, et
    l'interface le dit.
    """
    if lang in ("fr", "en"):
        eleven = ElevenLabsTTS()
        if eleven.available:
            return eleven
    azure = AzureTTS()
    if azure.available:
        return azure
    eleven = ElevenLabsTTS()
    return eleven if eleven.available else NullTTS()


def default_tts() -> TTSProvider:
    azure = AzureTTS()
    return azure if azure.available else NullTTS()


def audio_path_for(root: str | Path, questionnaire_id: str, lang: str, step_id: str) -> Path:
    return Path(root) / questionnaire_id / lang / f"{step_id}.mp3"
