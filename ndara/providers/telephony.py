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

    def raccrocher(self, call_sid: str) -> bool: ...


class NullTelephony:
    """Aucun appel. Utilisé tant que le compte opérateur n'est pas ouvert."""

    name = "null"

    def place_call(self, msisdn: str, questionnaire: str = "",
                   stratum: str = "", lang: str = "fr") -> CallResult:
        return CallResult(ok=False, error="aucun fournisseur de téléphonie configuré")

    def raccrocher(self, call_sid: str) -> bool:
        return False


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
            # Détection de répondeur, mais SANS bloquer l'appel.
            #
            # En mode bloquant, Twilio retient la ligne le temps de décider si
            # c'est une machine qui a décroché, et ce verdict peut prendre
            # plusieurs secondes. Pendant ce temps la personne a dit « allô »
            # deux fois dans le vide. C'est le défaut qui fait dire « ça ne
            # marche pas » d'un dispositif qui marche : le premier contact est
            # un silence.
            #
            # En mode asynchrone, l'entretien démarre à la seconde où l'on
            # décroche, et le verdict arrive par un rappel séparé. On garde
            # donc la disposition « non-contact » sur un répondeur, sans la
            # payer en silence sur les vrais décrochages.
            "MachineDetection": "Enable",
            "AsyncAmd": "true",
            "AsyncAmdStatusCallback": f"{self.webhook_base}/twiml/amd",
            "AsyncAmdStatusCallbackMethod": "POST",
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

    def raccrocher(self, call_sid: str) -> bool:
        """Met fin à un appel en cours.

        Sert quand la détection asynchrone finit par dire qu'un répondeur a
        décroché. Sans ce raccrochage, le répondeur écoute le questionnaire
        jusqu'au bout et la minute se facture comme si quelqu'un répondait.
        Sur une vague de plusieurs milliers d'appels, c'est le poste de coût le
        plus bête qui soit : on paie pour parler à une machine.
        """
        import urllib.request

        if not (self.available and call_sid):
            return False
        url = (f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}"
               f"/Calls/{urllib.parse.quote(call_sid)}.json")
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode({"Status": "completed"}).encode(),
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                return True
        except Exception:
            return False


# --------------------------------------------------------------------------
# TwiML : traduction d'une invite NDARA en instructions téléphoniques
# --------------------------------------------------------------------------

# Modèle de reconnaissance taillé pour des réponses brèves. Le modèle par
# défaut est réglé pour de la dictée : sur « oui », « le Littoral » ou « mille
# cinq cents », il attend une suite qui ne vient pas, et ce délai s'entend.
_MODELE_COURT = "googlev2_short"


def _indices(prompt: dict) -> str:
    """Ce que la reconnaissance doit s'attendre à entendre.

    Le moteur connaît déjà les seules réponses recevables : ce sont les
    modalités de la question. Les passer en indices ne coûte rien et redresse
    la reconnaissance là où elle se trompe le plus, sur les noms de lieux et
    les mots régionaux. Les touches y figurent aussi, parce qu'un « deux »
    prononcé vaut la touche 2.

    Twilio accepte 500 entrées de 100 caractères. On reste loin en dessous :
    une question de sondage a dix modalités au plus.
    """
    vus: list[str] = []
    for o in prompt.get("options") or []:
        for valeur in (o.get("dtmf"), o.get("label")):
            v = (valeur or "").strip()
            if v and v not in vus and len(v) <= 100:
                vus.append(v)
    return ",".join(vus[:500])


def prompt_to_twiml(prompt: dict, *, action_url: str, audio_base: str | None = None,
                    record_seconds: int = 12, corpus_consenti: bool = False,
                    transcription: bool = False, langue: str = "fr") -> str:
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

    ET ON N'ENREGISTRE PAS UNE VOIX QU'ON NE SAIT PAS TRANSCRIRE
    ------------------------------------------------------------
    ``Record`` rend un fichier audio et rien d'autre : aucune transcription
    n'accompagne le tour. Sans moteur de transcription configuré, la réponse
    arrive donc vide, le moteur ne comprend pas, relance, relance encore, puis
    renvoie au clavier. Le répondant a parlé deux fois pour rien, et l'appel
    s'allonge de vingt secondes par question.

    Le corpus y perdrait aussi : un segment sonore sans transcription n'est pas
    un corpus annoté, c'est un fichier. Tant qu'aucun moteur n'est branché, on
    passe donc par ``Gather``, qui fait transcrire au vol par le canal, et on
    inscrit au journal que le segment consenti n'a pas été collecté. Un
    consentement non utilisé se dit ; il ne se convertit pas en silence.

    ON PEUT COUPER UNE QUESTION, JAMAIS UN CONSENTEMENT
    ---------------------------------------------------
    Un enquêteur humain n'oblige personne à écouter la fin d'une question déjà
    comprise : on répond, et l'entretien avance. C'est ce que permet
    l'imbrication de la lecture *dans* l'écoute. Sans elle, l'écoute ne
    commence qu'une fois la phrase finie, la première syllabe du répondant se
    perd, et chaque tour porte un blanc qui s'entend comme une machine.

    Sur l'annonce d'intelligence artificielle et sur les deux consentements,
    c'est l'inverse, et ce n'est pas négociable : la phrase doit avoir été
    entendue en entier avant qu'un « oui » puisse compter. Un consentement
    arraché à la moitié d'une phrase n'est pas un consentement. La lecture
    reste donc hors de l'écoute, et personne ne peut la couper.

    CE QUI EST DIT AVANT LA QUESTION
    --------------------------------
    Une relance précède la question qu'elle relance au lieu de la suivre :
    « je n'ai pas bien compris », puis la question à nouveau. Et elle est dite
    dans la même voix que le reste, parce qu'une relance en voix de secours au
    milieu d'un entretien s'entend, précisément à l'instant où le répondant
    hésite déjà.
    """
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    locale = {"fr": "fr-FR", "en": "en-US", "km": "km-KH"}.get(langue, "fr-FR")

    def dire(texte: str) -> str:
        return f'<Say language="{locale}">{escape(texte)}</Say>'

    def lire(texte, audio_url) -> list[str]:
        """La voix de studio si le libellé est pré-synthétisé, sinon celle du canal."""
        if audio_base and audio_url:
            return [f"<Play>{escape(audio_base.rstrip('/') + audio_url)}</Play>"]
        return [dire(texte)] if texte else []

    # La relance d'abord, la question ensuite.
    enonce: list[str] = []
    if prompt.get("note"):
        enonce += lire(prompt["note"], prompt.get("note_audio_url"))
    enonce += lire(prompt.get("text", ""), prompt.get("audio_url"))

    if prompt.get("done"):
        lines += enonce
        lines.append("<Hangup/>")
        lines.append("</Response>")
        return "\n".join(lines)

    if not prompt.get("allow_voice") and not prompt.get("allow_dtmf"):
        # L'annonce n'attend aucune réponse. Lui coller une écoute ferait
        # patienter sept secondes chaque appel, facturées à la minute, pour
        # un silence que personne n'a demandé. On enchaîne.
        lines += enonce
        lines.append('<Pause length="1"/>')
        lines.append(f'<Redirect method="POST">{escape(action_url)}</Redirect>')
        lines.append("</Response>")
        return "\n".join(lines)

    coupable = prompt.get("kind") == "question"

    def poser(gather_ouvrant: str) -> None:
        """Écoute avec ou sans interruption possible, selon la nature de l'invite."""
        if coupable:
            lines.append(gather_ouvrant + ">")
            lines.extend("  " + l for l in enonce)
            lines.append("</Gather>")
        else:
            lines.extend(enonce)
            lines.append(gather_ouvrant + "/>")

    commun = (f'action="{escape(action_url)}" method="POST" language="{locale}" '
              f'speechTimeout="auto" speechModel="{_MODELE_COURT}" '
              f'profanityFilter="false" actionOnEmptyResult="true"')

    if prompt.get("allow_dtmf") and prompt.get("options"):
        poser(f'<Gather input="dtmf speech" numDigits="1" timeout="7" '
              f'{commun} hints="{escape(_indices(prompt))}"')
    elif corpus_consenti and transcription and prompt.get("corpus_eligible", True):
        lines.extend(enonce)
        lines.append(
            f'<Record action="{escape(action_url)}" method="POST" '
            f'maxLength="{record_seconds}" timeout="3" playBeep="true" '
            f'trim="trim-silence" transcribe="false"/>'
        )
    else:
        # Réponse libre ou numérique sans accord au corpus : on transcrit au
        # vol, on ne garde rien.
        poser(f'<Gather input="speech" timeout="7" {commun}')

    # Silence complet : la boucle doit se refermer, sinon l'appel reste ouvert
    # et se facture pour rien. Avec actionOnEmptyResult ce filet ne devrait
    # jamais servir, et c'est bien ainsi qu'on veut un filet.
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
