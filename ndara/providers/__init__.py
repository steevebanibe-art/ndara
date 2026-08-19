"""Adaptateurs vers les services externes.

Tout est optionnel : sans clé configurée, le moteur fonctionne en mode
saisie/clavier et l'annonce en est faite explicitement dans l'interface.
On ne simule jamais une transcription.
"""

from .asr import ASRProvider, MockASR, default_asr
from .telephony import TelephonyAdapter, NullTelephony, default_telephony, prompt_to_twiml
from .tts import TTSProvider, NullTTS, default_tts

__all__ = [
    "ASRProvider", "MockASR", "default_asr",
    "TTSProvider", "NullTTS", "default_tts",
    "TelephonyAdapter", "NullTelephony", "default_telephony", "prompt_to_twiml",
]
