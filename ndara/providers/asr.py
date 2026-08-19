"""Transcription (ASR) — adaptateurs interchangeables.

Aucune dépendance externe : appels HTTP via ``urllib``.

⚠️ Fait mesuré à garder en tête pour le khmer : le meilleur modèle public
annonce un taux d'erreur mot de l'ordre de 20 à 50 %. Le questionnaire est
donc conçu pour que la validité ne dépende PAS d'une transcription parfaite
(réponses courtes, vocabulaire fermé, confirmation, repli clavier).
La transcription est un confort ; le clavier est le filet.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Protocol


class ASRProvider(Protocol):
    name: str

    def transcribe(self, audio: bytes, lang: str, ext: str = "webm") -> tuple[str, float]:
        """Renvoie (texte, confiance ∈ [0,1])."""
        ...


class MockASR:
    """Aucune transcription : le moteur bascule sur la saisie ou le clavier.

    C'est le comportement par défaut tant qu'aucune clé n'est configurée —
    et c'est volontairement transparent : on ne simule jamais une
    transcription, on dit qu'on n'a pas transcrit.
    """

    name = "mock"

    def transcribe(self, audio: bytes, lang: str, ext: str = "webm") -> tuple[str, float]:
        return "", 0.0


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
               ) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
            .encode("utf-8")
        )
    for k, (filename, content, ctype) in files.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode("utf-8")
        )
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class ElevenLabsASR:
    """Scribe. Prend en charge le khmer (précision modérée) et le français."""

    name = "elevenlabs"
    ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"

    def __init__(self, api_key: str | None = None, model: str = "scribe_v1",
                 timeout: float = 30.0) -> None:
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.model = model
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def transcribe(self, audio: bytes, lang: str, ext: str = "webm") -> tuple[str, float]:
        import urllib.request

        body, ctype = _multipart(
            {"model_id": self.model, "language_code": {"km": "khm", "fr": "fra",
                                                       "en": "eng"}.get(lang, lang)},
            {"file": (f"a.{ext}", audio, f"audio/{ext}")},
        )
        req = urllib.request.Request(
            self.ENDPOINT, data=body,
            headers={"xi-api-key": self.api_key, "Content-Type": ctype},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data.get("text", "") or ""
        words = data.get("words") or []
        confs = [w.get("logprob") for w in words if isinstance(w.get("logprob"), (int, float))]
        # Pas de score global publié : on approxime, et on le dit dans le dossier.
        conf = 0.75 if text.strip() else 0.0
        if confs:
            import math
            conf = max(0.0, min(1.0, math.exp(sum(confs) / len(confs))))
        return text.strip(), conf


class AzureASR:
    """Azure Speech — voix et reconnaissance khmères (km-KH) disponibles."""

    name = "azure"

    def __init__(self, key: str | None = None, region: str | None = None,
                 timeout: float = 30.0) -> None:
        self.key = key or os.environ.get("AZURE_SPEECH_KEY", "")
        self.region = region or os.environ.get("AZURE_SPEECH_REGION", "southeastasia")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.key)

    def transcribe(self, audio: bytes, lang: str, ext: str = "wav") -> tuple[str, float]:
        import urllib.parse
        import urllib.request

        locale = {"km": "km-KH", "fr": "fr-FR", "en": "en-US"}.get(lang, lang)
        url = (f"https://{self.region}.stt.speech.microsoft.com/speech/recognition/"
               f"conversation/cognitiveservices/v1?"
               + urllib.parse.urlencode({"language": locale, "format": "detailed"}))
        ctype = ("audio/wav; codecs=audio/pcm; samplerate=16000" if ext == "wav"
                 else f"audio/{ext}")
        req = urllib.request.Request(
            url, data=audio,
            headers={"Ocp-Apim-Subscription-Key": self.key, "Content-Type": ctype},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        best = (data.get("NBest") or [{}])[0]
        return (best.get("Display", "") or "").strip(), float(best.get("Confidence", 0.0))


def default_asr() -> ASRProvider:
    for provider in (ElevenLabsASR(), AzureASR()):
        if getattr(provider, "available", False):
            return provider
    return MockASR()
