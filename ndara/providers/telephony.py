"""Téléphonie — adaptateur, prêt à brancher.

Rien ici n'est nécessaire pour la démonstration du jury : la demi-finale est
une évaluation en ligne, donc le canal du jury est le navigateur. La
téléphonie sert (a) aux entretiens réels qui produisent le petit chiffre vrai,
(b) à la vidéo de preuve.

⚠️ Ordre de grandeur vérifié : un appel sortant vers un mobile camerounais
coûte environ 0,55 $/minute chez Twilio. Un entretien de 2 min 30 revient donc
à ~1,38 $ de minutes. Un partenariat opérateur (minutes on-net) divise ce
poste par près de dix — c'est l'argument économique du partenariat, pas un
logo sur une diapositive.

Deuxième raison, non financière : un identifiant d'appelant **étranger** fait
chuter le taux de décrochage. Le numéro local n'est pas un détail de confort,
c'est une variable de la qualité statistique.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Protocol
from xml.sax.saxutils import escape


@dataclass
class CallResult:
    ok: bool
    provider_call_id: str | None = None
    error: str | None = None


class TelephonyAdapter(Protocol):
    name: str

    def place_call(self, msisdn: str, interview_id: str) -> CallResult: ...


class NullTelephony:
    """Aucun appel. Utilisé tant que le compte opérateur n'est pas ouvert."""

    name = "null"

    def place_call(self, msisdn: str, interview_id: str) -> CallResult:
        return CallResult(ok=False, error="aucun fournisseur de téléphonie configuré")


class TwilioTelephony:
    """Appels sortants via l'API REST Twilio (urllib, sans SDK).

    Non testé sans identifiants — à valider dès l'ouverture du compte.
    """

    name = "twilio"

    def __init__(self, sid: str | None = None, token: str | None = None,
                 from_number: str | None = None, webhook_base: str | None = None,
                 timeout: float = 20.0) -> None:
        self.sid = sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.token = token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.from_number = from_number or os.environ.get("TWILIO_FROM_NUMBER", "")
        self.webhook_base = (webhook_base or os.environ.get("NDARA_PUBLIC_URL", "")).rstrip("/")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.sid and self.token and self.from_number and self.webhook_base)

    def place_call(self, msisdn: str, interview_id: str) -> CallResult:
        import urllib.request

        if not self.available:
            return CallResult(ok=False, error="identifiants Twilio incomplets")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Calls.json"
        data = urllib.parse.urlencode({
            "To": msisdn,
            "From": self.from_number,
            "Url": f"{self.webhook_base}/twiml/start?interview_id={interview_id}",
            "StatusCallback": f"{self.webhook_base}/twiml/status?interview_id={interview_id}",
            "StatusCallbackEvent": "initiated ringing answered completed",
            "MachineDetection": "Enable",     # répondeur → disposition « non-contact »
            "Timeout": "25",
        }).encode()
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
            return CallResult(ok=True, provider_call_id=body.get("sid"))
        except Exception as exc:                      # réseau, quota, numéro invalide
            return CallResult(ok=False, error=str(exc))


# --------------------------------------------------------------------------
# TwiML : traduction d'une invite NDARA en instructions téléphoniques
# --------------------------------------------------------------------------

def prompt_to_twiml(prompt: dict, *, action_url: str, audio_base: str | None = None,
                    record_seconds: int = 12) -> str:
    """Convertit une invite du moteur en TwiML.

    Deux modes de saisie sont proposés simultanément : la parole (enregistrée
    puis transcrite) et le clavier. Le clavier est toujours disponible sur les
    questions à modalités — c'est le filet quand la transcription échoue.
    """
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]

    audio_url = prompt.get("audio_url")
    if audio_base and audio_url:
        lines.append(f"<Play>{escape(audio_base.rstrip('/') + audio_url)}</Play>")
    else:
        lines.append(f"<Say>{escape(prompt.get('text', ''))}</Say>")

    if prompt.get("note"):
        lines.append(f"<Say>{escape(prompt['note'])}</Say>")

    if prompt.get("done"):
        lines.append("<Hangup/>")
        lines.append("</Response>")
        return "\n".join(lines)

    if prompt.get("allow_dtmf") and prompt.get("options"):
        digits = "".join(o["dtmf"] for o in prompt["options"] if o.get("dtmf"))
        lines.append(
            f'<Gather input="dtmf speech" numDigits="1" timeout="6" '
            f'action="{escape(action_url)}" method="POST" '
            f'speechTimeout="auto" language="fr-FR" hints="{escape(digits)}"/>'
        )
    else:
        lines.append(
            f'<Record action="{escape(action_url)}" method="POST" '
            f'maxLength="{record_seconds}" timeout="3" playBeep="true" '
            f'trim="trim-silence"/>'
        )
    lines.append("</Response>")
    return "\n".join(lines)


def default_telephony() -> TelephonyAdapter:
    twilio = TwilioTelephony()
    return twilio if twilio.available else NullTelephony()
