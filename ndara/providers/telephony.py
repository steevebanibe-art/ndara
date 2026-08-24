"""Téléphonie — adaptateur, prêt à brancher.

Rien ici n'est nécessaire pour la démonstration du jury : la demi-finale est
une évaluation en ligne, donc le canal du jury est le navigateur. La
téléphonie sert (a) aux entretiens réels qui produisent le petit chiffre vrai,
(b) à la vidéo de preuve.

⚠️ Tarif relevé sur la page tarifaire publique de Twilio le 24 août 2026 :
**0,7873 $ la minute** vers un mobile camerounais. Un entretien de 2 min 30
revient donc à 1,97 $ de minutes, et environ 3 $ par entretien complété une
fois répartie la facture des appels qui n'aboutissent pas. Un répondeur qui
décroche est facturé, un refus qui décroche aussi.

Un partenariat opérateur (minutes on-net) diviserait ce poste par cinq, et
c'est ce qui fait passer une vague mensuelle du déficit à la marge. Tant
qu'aucun accord n'est signé, ce chiffre reste une hypothèse et doit être
présenté comme telle.

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

    def place_call(self, msisdn: str, questionnaire: str = "",
                   stratum: str = "", lang: str = "fr") -> CallResult: ...


class NullTelephony:
    """Aucun appel. Utilisé tant que le compte opérateur n'est pas ouvert."""

    name = "null"

    def place_call(self, msisdn: str, questionnaire: str = "",
                   stratum: str = "", lang: str = "fr") -> CallResult:
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

    def place_call(self, msisdn: str, questionnaire: str = "",
                   stratum: str = "", lang: str = "fr") -> CallResult:
        """Compose un numéro. Aucun entretien n'est créé à ce stade.

        L'entretien naît quand quelqu'un décroche, pas quand on compose : créer
        une ligne pour un téléphone qui sonne dans le vide gonflerait le
        dénominateur et fausserait le taux de réponse. La corrélation entre
        l'appel et l'entretien se fait ensuite par l'identifiant d'appel que
        l'opérateur renvoie.
        """
        import urllib.request

        if not self.available:
            return CallResult(ok=False, error="identifiants Twilio incomplets")
        contexte = urllib.parse.urlencode(
            {"questionnaire": questionnaire, "stratum": stratum, "lang": lang})
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Calls.json"
        data = urllib.parse.urlencode({
            "To": msisdn,
            "From": self.from_number,
            "Url": f"{self.webhook_base}/twiml/start?{contexte}",
            "StatusCallback": f"{self.webhook_base}/twiml/status",
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
                    record_seconds: int = 12, corpus_consenti: bool = False,
                    langue: str = "fr") -> str:
    """Convertit une invite du moteur en instructions téléphoniques.

    Deux modes de saisie sont toujours offerts ensemble : la parole et le
    clavier. Le clavier est le filet, et il ne disparaît jamais des questions
    à modalités : c'est lui qui rattrape une transcription ratée, et c'est le
    seul recours de quelqu'un qui ne sait pas lire.

    L'ENREGISTREMENT SUIT LE CONSENTEMENT, ET LUI SEUL
    --------------------------------------------------
    Par défaut on ne conserve aucune voix : ``Gather`` fait transcrire la
    réponse au passage, sans qu'un fichier audio soit stocké nulle part.
    Ce n'est qu'après un accord explicite au corpus que ``Record`` est employé,
    et seulement là où la parole a une valeur pour le corpus. Enregistrer
    d'abord et trier ensuite serait une collecte non consentie, quelle que
    soit la bonne foi du tri.
    """
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    locale = {"fr": "fr-FR", "en": "en-US", "km": "km-KH"}.get(langue, "fr-FR")

    def dire(texte: str) -> str:
        return f'<Say language="{locale}">{escape(texte)}</Say>'

    audio_url = prompt.get("audio_url")
    if audio_base and audio_url:
        lines.append(f"<Play>{escape(audio_base.rstrip('/') + audio_url)}</Play>")
    else:
        lines.append(dire(prompt.get("text", "")))

    if prompt.get("note"):
        lines.append(dire(prompt["note"]))

    if prompt.get("done"):
        lines.append("<Hangup/>")
        lines.append("</Response>")
        return "\n".join(lines)

    if not prompt.get("allow_voice") and not prompt.get("allow_dtmf"):
        # L'annonce n'attend aucune réponse. Lui coller une écoute ferait
        # patienter sept secondes chaque appel, facturées à la minute, pour
        # un silence que personne n'a demandé. On enchaîne.
        lines.append('<Pause length="1"/>')
        lines.append(f'<Redirect method="POST">{escape(action_url)}</Redirect>')
        lines.append("</Response>")
        return "\n".join(lines)

    if prompt.get("allow_dtmf") and prompt.get("options"):
        digits = "".join(o["dtmf"] or "" for o in prompt["options"])
        lines.append(
            f'<Gather input="dtmf speech" numDigits="1" timeout="7" '
            f'action="{escape(action_url)}" method="POST" '
            f'speechTimeout="auto" language="{locale}" hints="{escape(digits)}"/>'
        )
    elif corpus_consenti and prompt.get("corpus_eligible", True):
        lines.append(
            f'<Record action="{escape(action_url)}" method="POST" '
            f'maxLength="{record_seconds}" timeout="3" playBeep="true" '
            f'trim="trim-silence" transcribe="false"/>'
        )
    else:
        # Réponse libre ou numérique sans accord au corpus : on transcrit au
        # vol, on ne garde rien.
        lines.append(
            f'<Gather input="speech" timeout="7" speechTimeout="auto" '
            f'action="{escape(action_url)}" method="POST" language="{locale}"/>'
        )

    # Silence complet : la boucle doit se refermer, sinon l'appel reste ouvert
    # et se facture pour rien.
    lines.append(f'<Redirect method="POST">{escape(action_url)}</Redirect>')
    lines.append("</Response>")
    return "\n".join(lines)


def signature_valide(token: str, url: str, form: dict[str, str], signature: str) -> bool:
    """Vérifie qu'une requête vient bien de Twilio et pas d'un inconnu.

    Sans cette vérification, les routes téléphoniques d'un serveur public sont
    une porte ouverte : n'importe qui peut ouvrir des entretiens, y répondre à
    la place des gens et fabriquer des données qui entreront dans les
    estimations sans laisser de trace. Sur un instrument statistique, c'est la
    faille la plus grave possible.

    Le calcul est celui de Twilio : l'URL complète, puis chaque paramètre du
    formulaire par ordre alphabétique, nom collé à sa valeur, le tout signé en
    HMAC-SHA1 avec le jeton du compte.

    La comparaison est à temps constant : comparer deux signatures avec ``==``
    laisse fuir, par la durée, combien de caractères sont justes.
    """
    import hashlib
    import hmac

    if not token or not signature:
        return False
    base = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    attendu = base64.b64encode(
        hmac.new(token.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(attendu, signature)


def default_telephony() -> TelephonyAdapter:
    twilio = TwilioTelephony()
    return twilio if twilio.available else NullTelephony()
