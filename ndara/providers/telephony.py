"""Téléphonie — adaptateur, prêt à brancher.

Rien ici n'est nécessaire pour la démonstration du jury : la demi-finale est
une évaluation en ligne, donc le canal du jury est le navigateur. La
téléphonie sert (a) aux entretiens réels qui produisent le petit chiffre vrai,
(b) à la vidéo de preuve.

⚠️ Tarifs relevés **sur le compte lui-même** le 25 août 2026, et non sur la
page tarifaire publique. Ce ne sont pas des prix uniques mais des fourchettes,
parce que Twilio facture selon l'opérateur qui termine l'appel :

    Cameroun +237 : de 0,410 à 0,787 $ la minute
    Cambodge +855 : de 0,112 à 0,132 $ la minute

Au haut de la fourchette camerounaise, retenu partout par prudence, un
entretien de 2 min 30 revient à 1,97 $ de minutes, et environ 3 $ par entretien
complété une fois répartie la facture des appels qui n'aboutissent pas. Un
répondeur qui décroche est facturé, un refus qui décroche aussi.

Un partenariat opérateur (minutes on-net) diviserait ce poste par cinq, et
c'est ce qui fait passer une vague camerounaise du déficit à la marge. Tant
qu'aucun accord n'est signé, ce chiffre reste une hypothèse et doit être
présenté comme telle.

Le Cambodge, lui, coûte six fois moins cher la minute, et n'a donc pas besoin
du même accord pour tenir debout. Le produit est le même des deux côtés, le
modèle économique ne l'est pas.

Deuxième raison, non financière : un identifiant d'appelant **étranger** fait
chuter le taux de décrochage. Le numéro local n'est pas un détail de confort,
c'est une variable de la qualité statistique.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import Protocol
from xml.sax.saxutils import escape


@dataclass
class CallResult:
    ok: bool
    provider_call_id: str | None = None
    error: str | None = None
    note: str | None = None       # ce qui a été dégradé pour que l'appel parte


def _parametres_refuses(erreur: str) -> bool:
    """L'opérateur refuse-t-il un paramètre, plutôt que l'appel lui-même ?

    Un compte d'essai n'a pas accès à tout. La distinction compte : un numéro
    non vérifié demande une action de l'utilisateur, un paramètre refusé
    demande seulement qu'on s'en passe.
    """
    e = (erreur or "").lower()
    return ("disallowed parameters" in e
            or "limited parameter access" in e
            or "not allowed on trial" in e)


def _detail_http(exc) -> str:
    """Ce que l'opérateur a réellement répondu, et pas seulement son code HTTP.

    Twilio renvoie un JSON du genre ``{"code": 21219, "message": "...", ...}``.
    ``str(HTTPError)`` n'en garde rien : il rend « HTTP Error 400: Bad
    Request », qui est vrai et parfaitement inutile. On lit le corps, on en
    tire le code et le message, et on garde le tout : c'est le code qui permet
    de dire quoi corriger.
    """
    import json as _json

    brut = ""
    try:
        brut = exc.read().decode("utf-8", "replace")
    except Exception:
        pass
    code = message = ""
    try:
        d = _json.loads(brut)
        code = str(d.get("code") or "")
        message = str(d.get("message") or "")
    except Exception:
        message = brut[:300]
    morceaux = [p for p in (f"HTTP {getattr(exc, 'code', '')}", code, message) if p]
    return " · ".join(morceaux) or str(exc)


class TelephonyAdapter(Protocol):
    name: str

    def place_call(self, msisdn: str, questionnaire: str = "", stratum: str = "",
                   lang: str = "fr", essai: bool = False) -> CallResult: ...

    def raccrocher(self, call_sid: str) -> bool: ...

    def verifier(self, pays: list[str] | None = None) -> dict: ...


class NullTelephony:
    """Aucun appel. Utilisé tant que le compte opérateur n'est pas ouvert."""

    name = "null"

    def place_call(self, msisdn: str, questionnaire: str = "", stratum: str = "",
                   lang: str = "fr", essai: bool = False) -> CallResult:
        return CallResult(ok=False, error="aucun fournisseur de téléphonie configuré")

    def raccrocher(self, call_sid: str) -> bool:
        return False

    def verifier(self, pays: list[str] | None = None) -> dict:
        return {"ok": False, "raison": "aucun fournisseur de téléphonie configuré"}


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
        # Ce que le compte accepte réellement, appris au premier appel plutôt
        # que déclaré à l'avance. None tant qu'on n'a pas essayé.
        self.amd_actif: bool | None = None

    @property
    def available(self) -> bool:
        return bool(self.sid and self.token and self.from_number and self.webhook_base)

    def place_call(self, msisdn: str, questionnaire: str = "", stratum: str = "",
                   lang: str = "fr", essai: bool = False) -> CallResult:
        """Compose un numéro. Aucun entretien n'est créé à ce stade.

        L'entretien naît quand quelqu'un décroche, pas quand on compose : créer
        une ligne pour un téléphone qui sonne dans le vide gonflerait le
        dénominateur et fausserait le taux de réponse. La corrélation entre
        l'appel et l'entretien se fait ensuite par l'identifiant d'appel que
        l'opérateur renvoie.
        """
        if not self.available:
            return CallResult(ok=False, error="identifiants Twilio incomplets")
        contexte = urllib.parse.urlencode(
            {"questionnaire": questionnaire, "stratum": stratum, "lang": lang,
             **({"essai": "1"} if essai else {})})

        socle = {
            "To": msisdn,
            "From": self.from_number,
            "Url": f"{self.webhook_base}/twiml/start?{contexte}",
            "StatusCallback": f"{self.webhook_base}/twiml/status",
            "Timeout": "25",
        }
        # Détection de répondeur, sans bloquer l'appel.
        #
        # En mode bloquant, Twilio retient la ligne le temps de décider si
        # c'est une machine qui a décroché, et ce verdict prend plusieurs
        # secondes. Pendant ce temps la personne a dit « allô » deux fois dans
        # le vide : le premier contact d'un dispositif qui marche est un
        # silence. En asynchrone, l'entretien démarre au décrochage et le
        # verdict arrive séparément.
        #
        # Mais tous les comptes n'y ont pas droit : un compte d'essai refuse
        # ces paramètres, et refuse l'appel entier avec eux. On les tente, et
        # on s'en passe s'ils sont refusés. Un confort qui empêche d'appeler
        # n'est plus un confort.
        avances = {
            "StatusCallbackEvent": "initiated ringing answered completed",
            "MachineDetection": "Enable",
            "AsyncAmd": "true",
            "AsyncAmdStatusCallback": f"{self.webhook_base}/twiml/amd",
            "AsyncAmdStatusCallbackMethod": "POST",
        }

        res = self._poster({**socle, **avances})
        if res.ok:
            self.amd_actif = True
            return res
        if _parametres_refuses(res.error or ""):
            repli = self._poster(socle)
            self.amd_actif = False
            if repli.ok:
                repli.note = (
                    "Ce compte n'a pas accès à la détection de répondeur : l'appel "
                    "est passé sans elle. Un répondeur écoutera donc le questionnaire "
                    "au lieu d'être raccroché, et sera compté comme un entretien "
                    "abandonné plutôt que comme un non-contact.")
            return repli
        return res

    def _poster(self, params: dict) -> CallResult:
        """Une tentative, et ce que l'opérateur en a dit."""
        import urllib.request

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Calls.json"
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(params).encode(),
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
            return CallResult(ok=True, provider_call_id=body.get("sid"))
        except urllib.error.HTTPError as exc:
            # Le corps porte le code et le message ; l'exception ne porte que
            # « HTTP Error 400: Bad Request ». Jeter le corps, c'est remplacer
            # « ce numéro n'est pas vérifié » par « mauvaise requête », et
            # transformer une correction de trente secondes en après-midi
            # perdue.
            return CallResult(ok=False, error=_detail_http(exc))
        except Exception as exc:                      # réseau, délai dépassé
            return CallResult(ok=False, error=str(exc))

    def _lire(self, url: str) -> tuple[dict | None, str]:
        """Une lecture chez l'opérateur. Rend le corps, ou la raison du refus.

        Aucune de ces lectures ne coûte quoi que ce soit et aucune ne modifie
        le compte. C'est ce qui permet de tout demander d'un coup plutôt que
        d'apprendre les défauts un appel facturé à la fois.
        """
        import urllib.request

        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode()), ""
        except urllib.error.HTTPError as exc:
            return None, _detail_http(exc)
        except Exception as exc:
            return None, str(exc)[:200]

    def verifier(self, pays: list[str] | None = None) -> dict:
        """Demande à l'opérateur tout ce qu'un appel exige, sans passer d'appel.

        Le code 20003 de Twilio recouvre trois causes très différentes :
        identifiants refusés, permissions insuffisantes, solde épuisé. Le
        message ne dit pas laquelle, et le tableau de bord affichait la
        première comme si c'était la seule. Le résultat était prévisible : on
        refait un jeton qui allait bien, deux fois, avant de soupçonner autre
        chose.

        Cette lecture tranche. Elle demande la fiche du compte, le solde, et ce
        que le numéro appelant est réellement pour ce compte. Quatre requêtes
        gratuites, aucune modification, et rien de secret dans ce qui revient :
        un nom, un état, un montant, un numéro déjà connu.

        Une sous-lecture qui échoue ne fait pas tomber l'audit. Elle laisse son
        champ vide et l'ennui correspondant est signalé : un diagnostic partiel
        vaut mieux qu'une page blanche, tant qu'il dit ce qu'il n'a pas pu voir.
        """
        if not self.available:
            return {"ok": False, "raison": "identifiants incomplets"}

        base = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}"

        compte, refus = self._lire(f"{base}.json")
        if compte is None:
            # La fiche du compte est la seule lecture dont l'échec est
            # concluant : sans elle, les identifiants sont bien en cause.
            return {"ok": False, "raison": refus}

        res: dict = {
            "ok": True,
            "compte": compte.get("friendly_name", ""),
            "etat": compte.get("status", ""),
            "type": compte.get("type", ""),          # Trial ou Full
            "essai": str(compte.get("type", "")).lower() == "trial",
            "ennuis": [],
        }

        # Le solde. Un compte peut être actif, complet, avec des identifiants
        # parfaits, et refuser d'appeler parce qu'il n'a plus un sou. Twilio
        # répond alors 20003, exactement comme pour un mauvais jeton.
        solde, refus_solde = self._lire(f"{base}/Balance.json")
        if solde is not None:
            try:
                montant = float(solde.get("balance") or 0)
            except (TypeError, ValueError):
                montant = 0.0
            devise = solde.get("currency", "")
            res["solde"] = montant
            res["devise"] = devise
            if montant <= 0:
                res["ennuis"].append(
                    "Le solde du compte est épuisé. C'est la deuxième cause du code "
                    "20003, et elle ressemble à s'y méprendre à un jeton refusé : "
                    "les identifiants sont acceptés en lecture, et l'appel est "
                    "refusé. Rechargez le compte dans la console Twilio, Billing.")
            elif montant < 1:
                res["ennuis"].append(
                    f"Solde très bas : {montant:.2f} {devise}. Un appel vers un "
                    "mobile camerounais coûte de 0,410 à 0,787 $ la minute selon "
                    "l'opérateur qui termine, il n'y a plus de quoi mener un "
                    "entretien entier.")
        else:
            res["ennuis"].append("Solde illisible : " + refus_solde)

        # Le numéro appelant. Twilio n'émet que depuis un numéro que le compte
        # possède, ou depuis un numéro vérifié comme identifiant d'appelant. Un
        # numéro qui n'est ni l'un ni l'autre fait échouer tous les appels, et
        # la passation ne disait pas lequel des deux celui-ci était.
        if self.from_number:
            q = urllib.parse.quote(self.from_number)
            achetes, _ = self._lire(f"{base}/IncomingPhoneNumbers.json?PhoneNumber={q}")
            possede = bool((achetes or {}).get("incoming_phone_numbers"))
            verifies, _ = self._lire(f"{base}/OutgoingCallerIds.json?PhoneNumber={q}")
            verifie = bool((verifies or {}).get("outgoing_caller_ids"))

            if possede:
                res["numero_source"] = "acheté sur le compte"
                # L'ENTRANT : est-ce que ce numero sait ou nous joindre ?
                # Un numero achete ne repond a rien tant que sa route vocale
                # ne pointe pas chez nous. La demonstration « composez ce
                # numero devant le jury » depend entierement de ce champ, et
                # rien ne le disait.
                fiche = (achetes or {}).get("incoming_phone_numbers") or [{}]
                voix = str(fiche[0].get("voice_url") or "")
                res["entrant_url"] = voix
                attendu = f"{self.webhook_base}/twiml/start"
                res["entrant"] = voix.startswith(attendu)
                if not res["entrant"]:
                    res["ennuis"].append(
                        "Le numéro " + self.from_number + " n'est pas branché sur NDARA "
                        "pour les appels entrants" + (f" (il pointe vers « {voix} »)" if voix
                        else " (aucune route vocale)") + ". Tant que ce champ n'est pas "
                        "renseigné, composer ce numéro ne déclenche aucun entretien. "
                        "Console Twilio, Phone Numbers, Manage, le numéro, section Voice "
                        "Configuration, « A call comes in » : Webhook, HTTP POST, "
                        + attendu)
            elif verifie:
                res["numero_source"] = "vérifié comme identifiant d'appelant"
            else:
                res["numero_source"] = "inconnu du compte"
                res["ennuis"].append(
                    f"Le numéro appelant {self.from_number} n'est ni un numéro du "
                    "compte, ni un identifiant d'appelant vérifié. Twilio refusera "
                    "tout appel émis depuis lui. Console Twilio, Phone Numbers : "
                    "soit en acheter un, soit vérifier un numéro que vous possédez "
                    "dans Verified Caller IDs, puis corriger TWILIO_FROM_NUMBER.")

        # CE QUE LE COMPTE A LE DROIT DE COMPOSER, PAYS PAR PAYS
        #
        # Twilio bloque par defaut les destinations ou sevit la fraude aux
        # revenus d'interconnexion, et le blocage ne se voit nulle part avant
        # de composer : l'appel part, echoue, et il est facture. Le Cameroun
        # est dans cette liste. Le decouvrir en appelant coute de l'argent et
        # une soiree ; le lire ici ne coute rien.
        #
        # Trois droits distincts par pays, et seul le premier nous concerne :
        # un repondant tire au hasard dans les plages du regulateur est un
        # numero ordinaire. Les deux autres visent les services surtaxes.
        res["pays"] = {}
        for iso in (pays or []):
            fiche, refus_pays = self._lire(
                f"https://voice.twilio.com/v1/DialingPermissions/Countries/{iso}")
            if fiche is None:
                res["pays"][iso] = {"lisible": False, "raison": refus_pays}
                continue
            ouvert = bool(fiche.get("low_risk_numbers_enabled"))
            res["pays"][iso] = {
                "lisible": True, "nom": fiche.get("name", iso), "sortant": ouvert,
                "indicatifs": fiche.get("country_codes", []),
            }
            if not ouvert:
                res["ennuis"].append(
                    f"Appels sortants vers {fiche.get('name', iso)} ({iso}) : refusés par "
                    "l'opérateur. Ce pays est bloqué par défaut contre la fraude aux "
                    "revenus d'interconnexion, et le blocage ne se voit qu'au moment de "
                    "composer, une fois l'appel facturé. Console Twilio, Voice, Settings, "
                    "Geo Permissions : si le pays y est refusable, cochez-le ; s'il est "
                    "verrouillé, il faut le demander au support. L'appel ENTRANT, lui, "
                    "reste ouvert : ce numéro peut recevoir depuis ce pays.")

        return res

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

# Le modèle de reconnaissance, et ce choix a été payé une fois.
#
# `googlev2_short` était employé jusqu'au 27 août 2026 parce qu'il est taillé
# pour des réponses brèves : le modèle de dictée, sur « oui » ou « le
# Littoral », attend une suite qui ne vient pas, et ce délai s'entend. Le
# raisonnement était juste sur la durée et faux sur le support. `googlev2_short`
# est un modèle courte-durée mais **non téléphonique**, entraîné sur de l'audio
# pleine bande. Une ligne téléphonique transporte 8 kHz, et tout ce qui
# distingue un « oui » d'un « puis-je » vit précisément dans les fréquences que
# le codec a jetées.
#
# Mesuré sur le premier vrai appel entrant, le 26 août 2026 : un « oui » en
# français camerounais est revenu transcrit « puis-je », confiance 0,0.
#
# La documentation de Twilio recommande explicitement `phone_call` avec
# `language="fr-FR"` pour du français accentué sur une ligne téléphonique.
# C'est un modèle entraîné sur de l'audio téléphonique, donc sur la bande qui
# nous reste réellement.
#
# Deux voisins existent si celui-ci déçoit à son tour, et ils se changent ici,
# à un seul endroit : `googlev2_telephony_short`, l'équivalent téléphonique
# direct de l'ancien réglage, et `googlev2_telephony` pour des réponses plus
# longues. Ne pas revenir à un modèle non téléphonique sans avoir mesuré.
_MODELE_VOIX = "phone_call"


def _indices(prompt: dict, langue: str = "fr") -> str:
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

    def ajouter(valeur: str) -> None:
        v = (valeur or "").strip()
        if v and v not in vus and len(v) <= 100:
            vus.append(v)

    for o in prompt.get("options") or []:
        ajouter(o.get("dtmf"))
        ajouter(o.get("label"))

    # Ce que le codeur sait déjà accepter, la reconnaissance doit s'y attendre.
    #
    # C'est le trou trouvé le 27 août 2026, et il était invisible parce que les
    # deux moitiés du problème vivaient dans deux fichiers. `coding.py` connaît
    # depuis toujours les façons naturelles de dire oui et non : « ouais »,
    # « bien sûr », « d'accord », « tout à fait », « voilà ». Le moteur les code
    # correctement quand elles lui arrivent. Mais elles n'étaient jamais dites à
    # la reconnaissance, qui n'avait en indices que « 1, Oui, 2, Non ».
    #
    # Or personne ne répond « Oui » tout court à une question de consentement.
    # On demandait donc à la reconnaissance de retrouver un mot isolé, sur une
    # ligne à 8 kHz, dans une phrase entière, sans lui dire ce qu'elle cherchait.
    from ..coding import AFFIRMATION, MARQUEURS_TOUCHE, NEGATION, _FR_UNITS

    codes = {(o.get("code") or "").lower() for o in prompt.get("options") or []}
    if {"yes", "no"} & codes or {"oui", "non"} & codes:
        for mot in AFFIRMATION.get(langue, []) + NEGATION.get(langue, []):
            ajouter(mot)

    # Une question numérique n'a pas de modalités, donc pas un seul indice
    # jusqu'ici. C'est pourtant là que la reconnaissance travaille le plus : un
    # montant dit à voix haute, en chiffres ou en toutes lettres.
    if prompt.get("input_type") == "number":
        if langue == "fr":
            for mot in _FR_UNITS:
                ajouter(mot)
            for mot in ("cent", "cents", "mille", "million"):
                ajouter(mot)
        for mot in MARQUEURS_TOUCHE.get(langue, []):
            if len(mot) > 2:          # « le » et « la » ne sont pas des indices
                ajouter(mot)
        ajouter(prompt.get("unit"))

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

    # Les indices valent pour toute écoute, pas seulement pour celles à
    # modalités. Ils étaient posés sur la seule branche « dtmf speech », donc
    # une question ouverte et une question numérique n'en recevaient aucun.
    indices = _indices(prompt, langue)
    commun = (f'action="{escape(action_url)}" method="POST" language="{locale}" '
              f'speechTimeout="auto" speechModel="{_MODELE_VOIX}" '
              f'profanityFilter="false" actionOnEmptyResult="true"'
              + (f' hints="{escape(indices)}"' if indices else ""))

    if prompt.get("allow_dtmf") and prompt.get("options"):
        poser(f'<Gather input="dtmf speech" numDigits="1" timeout="7" {commun}')
    elif corpus_consenti and transcription and prompt.get("corpus_eligible", True):
        lines.extend(enonce)
        lines.append(
            f'<Record action="{escape(action_url)}" method="POST" '
            f'maxLength="{record_seconds}" timeout="3" playBeep="true" '
            f'trim="trim-silence" transcribe="false"/>'
        )
    elif prompt.get("input_type") == "number":
        # Une question numérique n'a pas de modalités, donc pas de touche
        # attribuée d'avance. Elle n'écoutait pour cette raison que la parole.
        # Or la relance de dernier recours dit, en toutes lettres et dans la
        # voix de studio : « utilisez les touches de votre téléphone ». On
        # promettait un filet qui n'existait pas, exactement sur les questions
        # où la reconnaissance échoue le plus, celles où il faut dire un
        # montant. Le clavier est ici aussi le seul recours de quelqu'un que la
        # transcription ne comprend pas.
        #
        # Pas de numDigits : un nombre n'a pas de longueur connue, et en
        # imposer une couperait « 1500 » à « 1 ». On ferme sur la touche dièse,
        # ou sur le silence.
        poser(f'<Gather input="dtmf speech" finishOnKey="#" timeout="7" {commun}')

    else:
        # Réponse libre sans accord au corpus : on transcrit au vol, on ne
        # garde rien.
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
