"""Serveur NDARA — bibliothèque standard uniquement.

Pourquoi pas de framework : le dossier doit tourner sur n'importe quelle
machine, sans installation, sans réseau, y compris pour un évaluateur qui
clone le dépôt et lance une commande. Zéro dépendance = zéro excuse.

Deux surfaces :
  * ``/``           l'entretien, dans le navigateur — c'est ce que le jury utilise
  * ``/dashboard``  le tableau de bord : terrain, estimations, qualité, corpus

Et une surface dormante, prête pour l'opérateur :
  * ``/twiml/*``    traduction des invites en instructions téléphoniques
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ndara.console import setup as setup_console  # noqa: E402
from ndara.analysis import estimate_all  # noqa: E402
from ndara.audit import quality_report  # noqa: E402
from ndara.coding import default_coder  # noqa: E402
from ndara.corpus import CorpusWriter  # noqa: E402
from ndara.engine import InterviewEngine  # noqa: E402
from ndara.importer import EXEMPLE_CSV, construire  # noqa: E402
from ndara.providers.asr import MockASR, default_asr  # noqa: E402
from ndara.providers.telephony import (  # noqa: E402
    default_telephony, prompt_to_twiml, signature_valide,
)
from ndara import omnibus  # noqa: E402
from ndara.questionnaire import Questionnaire  # noqa: E402
from ndara.sampling import load_margins  # noqa: E402
from ndara.storage import Store  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
AUDIO_ROOT = ROOT / "data" / "audio"


class Bus:
    """Diffusion des événements de terrain vers les tableaux de bord ouverts.

    Un entretien qui commence, une réponse qui tombe, un entretien qui se
    termine : chaque fait est poussé aux écrans connectés au moment où il se
    produit. C'est la différence entre un tableau de bord qu'on rafraîchit et
    une salle de contrôle.

    Un abonné lent ne bloque jamais le terrain : sa file déborde, on jette
    l'événement pour lui seul, et la collecte continue.
    """

    MAX_ABONNES = 32

    def __init__(self) -> None:
        self._abonnes: set[queue.Queue] = set()
        self._verrou = threading.Lock()

    def subscribe(self) -> queue.Queue | None:
        with self._verrou:
            if len(self._abonnes) >= self.MAX_ABONNES:
                return None
            f: queue.Queue = queue.Queue(maxsize=256)
            self._abonnes.add(f)
            return f

    def unsubscribe(self, f: queue.Queue) -> None:
        with self._verrou:
            self._abonnes.discard(f)

    def publish(self, evt: dict) -> None:
        with self._verrou:
            abonnes = list(self._abonnes)
        for f in abonnes:
            try:
                f.put_nowait(evt)
            except queue.Full:
                pass

    @property
    def count(self) -> int:
        with self._verrou:
            return len(self._abonnes)


# Les erreurs de l'opérateur arrivent en anglais, avec un code et une URL.
# Traduites, elles disent quoi faire ; brutes, elles font perdre une heure.
_ERREURS_TWILIO = {
    "21215": ("Ce pays n'est pas autorisé sur le compte. Console Twilio, "
              "Voice, Settings, Geo permissions : cochez le pays et enregistrez."),
    "21219": ("Ce numéro n'est pas vérifié. Un compte d'essai n'appelle que des "
              "numéros vérifiés : Console Twilio, Phone Numbers, Verified Caller IDs."),
    "21210": ("Le numéro appelant n'est pas vérifié ou n'appartient pas au compte. "
              "Vérifiez TWILIO_FROM_NUMBER."),
    "21211": "Numéro appelé invalide. Format international attendu, avec le « + ».",
    "21606": ("Le numéro appelant n'est pas utilisable pour émettre. Prenez un numéro "
              "Twilio du compte, ou vérifiez ce numéro comme identifiant d'appelant."),
    # Twilio range trois causes sous ce seul code, et elles ne se corrigent pas
    # du tout au même endroit. Annoncer la première comme si c'était la seule
    # envoie refaire un jeton qui allait bien : c'est arrivé deux fois.
    "20003": ("Opérateur : accès refusé. Trois causes possibles, dans cet ordre de "
              "fréquence : le solde du compte est épuisé, le numéro appelant n'est "
              "pas utilisable par ce compte, ou les identifiants sont faux. "
              "Cliquez « Vérifier les identifiants » : le diagnostic dit laquelle."),
    "21608": ("Compte d'essai : ce numéro n'est pas vérifié. Ajoutez-le dans "
              "Verified Caller IDs."),
}


def _forme_identifiants() -> list[str]:
    """Ce qui cloche dans la FORME des identifiants, sans jamais lire leur contenu.

    Un identifiant de compte Twilio fait 34 caractères et commence par AC. Un
    jeton en fait 32. Trois erreurs de collage sur quatre se voient là : une
    valeur tronquée, un espace ramassé au passage, un champ mis pour l'autre.
    Aucune ne demande d'afficher le secret, et aucune n'est visible dans
    « identifiants refusés », qui ne dit ni lequel des deux ni pourquoi.
    """
    ennuis: list[str] = []
    sid_brut = os.environ.get("TWILIO_ACCOUNT_SID", "")
    jeton_brut = os.environ.get("TWILIO_AUTH_TOKEN", "")

    for nom, brut, longueur, prefixe in (
            ("TWILIO_ACCOUNT_SID", sid_brut, 34, "AC"),
            ("TWILIO_AUTH_TOKEN", jeton_brut, 32, ""),
    ):
        if not brut:
            continue
        propre = brut.strip()
        if propre != brut:
            ennuis.append(f"{nom} porte un espace ou un retour à la ligne autour de "
                          f"sa valeur : recollez-le sans rien avant ni après.")
        if len(propre) != longueur:
            ennuis.append(f"{nom} fait {len(propre)} caractères au lieu de {longueur} : "
                          f"la valeur a été tronquée au collage, ou ce n'est pas la bonne.")
        if prefixe and not propre.startswith(prefixe):
            ennuis.append(f"{nom} ne commence pas par « {prefixe} » : c'est peut-être "
                          f"une clé d'API, ou les deux champs ont été intervertis.")

    if (sid_brut.strip().startswith("SK")
            or (jeton_brut.strip().startswith("AC") and len(jeton_brut.strip()) == 34)):
        ennuis.append("Une clé d'API ne remplace pas le jeton du compte : Twilio signe "
                      "ses appels entrants avec le Primary Auth Token, onglet "
                      "AUTH TOKENS et non API KEYS.")
    return ennuis


def _confiance_twilio(brut: str | None) -> float | None:
    """La confiance de transcription, ou None quand l'opérateur n'en donne pas.

    Les modèles téléphoniques de Twilio, `phone_call` et la famille googlev2,
    renvoient presque toujours `Confidence = 0.0`, y compris sur une
    transcription parfaite. Ce zéro n'est pas une mesure basse, c'est une
    absence de mesure, et les confondre coûte deux fois.

    En aval, `audit.py` déclare « transcription faible » sous 0,55 : lu
    littéralement, tout entretien téléphonique réel serait donc marqué comme
    dégradé, et le rapport de qualité publierait une moyenne de confiance de
    zéro. Sur un instrument dont toute la thèse est de publier l'erreur avec le
    chiffre, un défaut de qualité inventé est aussi grave qu'un défaut caché.

    On ne remplace pas le zéro par une valeur plausible, ce serait fabriquer.
    On dit qu'il n'y a pas de mesure, et l'audit compte les tours concernés.
    """
    if brut is None or brut == "":
        return None
    try:
        valeur = float(brut)
    except (TypeError, ValueError):
        return None
    return None if valeur == 0.0 else valeur


def _code_twilio(erreur: str) -> str:
    """Le code d'erreur de l'opérateur, et lui seul.

    ``_detail_http`` compose « HTTP 400 · 21215 · message ». Chercher le code
    par simple sous-chaîne dans le tout marcherait presque toujours, et se
    tromperait le jour où un numéro appelé contient les mêmes cinq chiffres.
    On lit donc le champ, pas le texte.
    """
    morceaux = [m.strip() for m in erreur.split("·")]
    for m in morceaux[1:]:
        if m.isdigit():
            return m
    return ""


def _twilio_lisible(erreur: str) -> str:
    """Rend une erreur d'opérateur actionnable, sans masquer l'originale.

    Sans masquer, vraiment : la version précédente promettait ceci dans sa
    première ligne et rendait uniquement sa propre traduction. Or c'est la
    phrase de Twilio qui nomme la cause, et notre traduction qui la devine.
    Quand les deux se contredisent, il faut pouvoir le voir.
    """
    # La conformité passe avant le code. Twilio répond 20003, « accès refusé »,
    # qui envoie chercher du côté des identifiants et du solde alors que les
    # deux sont bons : c'est la vérification d'identité qui manque. Une heure
    # perdue la première fois, sur une piste qui n'en était pas une.
    bas = (erreur or "").lower()
    if "compliance profile" in bas or "trust hub" in bas or "kyc" in bas:
        return (
            "Vérification d'identité non terminée chez l'opérateur. Le compte est "
            "actif et les identifiants sont bons : Twilio bloque les appels tant que "
            "le profil de conformité n'est pas approuvé. Console Twilio, Products & "
            "Services, Trust Hub, Profiles, Primary profile : renseignez identité et "
            "adresse exactement comme sur la pièce d'identité. La revue prend jusqu'à "
            "48 heures. Twilio dit : " + erreur[:200])

    code = _code_twilio(erreur)
    explication = _ERREURS_TWILIO.get(code)
    if explication is None:
        for c, e in _ERREURS_TWILIO.items():
            if c in erreur:
                code, explication = c, e
                break
    if explication is None and "HTTP Error 401" in erreur:
        code, explication = "20003", _ERREURS_TWILIO["20003"]
    if explication is None:
        return erreur[:300]
    return f"{explication} (code {code}) Twilio dit : {erreur[:200]}"


class App:
    """État partagé du serveur."""

    def __init__(self, db: str = "data/ndara.db") -> None:
        self.store = Store(ROOT / db)
        self.coder = default_coder()
        self.asr = default_asr()
        self.corpus = CorpusWriter(self.store, ROOT / "data" / "corpus")
        qdir = ROOT / "data" / "questionnaires"
        self.questionnaires = {
            p.stem: Questionnaire.load(p) for p in sorted(qdir.glob("*.json"))
        }
        self.engines = {
            qid: InterviewEngine(self.store, q, self.coder, self.corpus)
            for qid, q in self.questionnaires.items()
        }
        self.margins = {
            "prix_denrees_cm": load_margins(ROOT / "data" / "margins" / "cm_margins.json"),
        }
        # La vague omnibus de démonstration. Elle ne crée aucune question :
        # elle redécoupe un questionnaire déjà synthétisé en un tronc commun
        # et trois créneaux, pour que le modèle économique se voie au lieu de
        # se lire. Si le questionnaire de référence manque, la section
        # disparaît de l'écran plutôt que de faire tomber le serveur.
        try:
            self.vague = omnibus.vague_de_demonstration(
                self.questionnaires[self.default_qid])
        except Exception:
            self.vague = None

        self.tel = default_telephony()
        self.par_call_sid: dict[str, str] = {}
        self.campagne: dict = {"active": False, "places": 0, "plafond": 0,
                               "aboutis": 0, "echecs": 0, "arret": False}
        self.bus = Bus()
        # Les entretiens en cours, vus du poste de contrôle. Un entretien qui
        # se termine ou qui reste muet trop longtemps en sort.
        self.live: dict[str, dict] = {}
        self._live_lock = threading.Lock()
        self._wave_running = False
        self.db_path = ROOT / db

    # ------------------------------------------------------------------
    # Le terrain, vu en direct
    # ------------------------------------------------------------------

    def touch(self, iid: str, **champs) -> dict:
        """Met à jour la ligne d'un entretien en cours et la renvoie."""
        with self._live_lock:
            ligne = self.live.setdefault(iid, {"id": iid[-6:], "debut": time.time()})
            ligne.update(champs)
            ligne["vu"] = time.time()
            return dict(ligne)

    def drop(self, iid: str) -> None:
        with self._live_lock:
            self.live.pop(iid, None)

    def live_rows(self) -> list[dict]:
        """Les entretiens en cours, les plus récents d'abord.

        Un entretien sans nouvelle depuis trois minutes est considéré comme
        abandonné : il quitte l'écran, il ne pollue pas le compteur.
        """
        limite = time.time() - 180
        with self._live_lock:
            morts = [k for k, v in self.live.items() if v.get("vu", 0) < limite]
            for k in morts:
                self.live.pop(k, None)
            lignes = list(self.live.values())
        for l in lignes:
            l["age"] = round(time.time() - l.get("debut", time.time()))
        return sorted(lignes, key=lambda l: -l.get("vu", 0))[:12]

    def pulse(self) -> dict:
        """L'état du terrain à cet instant, tel qu'il part vers les écrans."""
        prov = self.store.provenance()
        rows = self.live_rows()
        return {
            "type": "pulse",
            "provenance": prov,
            "total": sum(prov.values()),
            "en_cours": len(rows),
            "lignes": rows,
            "ecrans": self.bus.count,
            "vague": self._wave_running,
        }

    # ------------------------------------------------------------------
    # Une vague simulée, menée sous les yeux du visiteur
    # ------------------------------------------------------------------

    def start_campagne(self, n: int, qid: str, simultanes: int = 3) -> dict:
        """Compose réellement des numéros. De l'argent part à chaque appel.

        Trois garde-fous, et aucun n'est décoratif. Un plafond dur de numéros,
        parce qu'une boucle qui s'emballe se facture à la minute. Un nombre
        d'appels simultanés borné, parce qu'un opérateur coupe une ligne qui
        se comporte comme un automate d'abus. Et un bouton d'arrêt qui vide la
        file immédiatement.
        """
        from ndara.sampling import draw_frame, to_sample_units

        if self.campagne["active"]:
            return {"lance": False, "raison": "une campagne est déjà en cours"}
        etat = self.telephony_state()
        if not etat["prete"]:
            return {"lance": False, "raison": "téléphonie non configurée",
                    "manque": etat["manque"]}
        if qid not in self.questionnaires:
            return {"lance": False, "raison": f"questionnaire « {qid} » inconnu"}
        n = max(1, min(200, int(n)))

        self.campagne.update({"active": True, "places": 0, "plafond": n,
                              "aboutis": 0, "echecs": 0, "arret": False,
                              "questionnaire": qid, "compose": 0})
        threading.Thread(target=self._run_campagne,
                         args=(n, qid, max(1, min(10, simultanes))),
                         daemon=True).start()
        return {"lance": True, "plafond": n, "questionnaire": qid}

    def _run_campagne(self, n: int, qid: str, simultanes: int) -> None:
        from ndara.sampling import draw_frame, to_sample_units

        try:
            q = self.questionnaires[qid]
            graine = random.randrange(1, 10_000_000)
            units = to_sample_units(draw_frame(q.country, n, seed=graine))
            self.store.add_sample_units(units)
            self.bus.publish({"type": "campagne", "etat": "debut", "n": n,
                              "questionnaire": qid})
            for u in units:
                if self.campagne["arret"]:
                    break
                # On attend qu'une ligne se libère plutôt que d'inonder.
                attente = 0
                while self.campagne["places"] >= simultanes and not self.campagne["arret"]:
                    time.sleep(1.0)
                    attente += 1
                    if attente > 180:      # une ligne bloquée ne bloque pas la vague
                        self.campagne["places"] = max(0, self.campagne["places"] - 1)
                        break
                if self.campagne["arret"]:
                    break
                res = self.tel.place_call(u.msisdn, questionnaire=qid,
                                          stratum=u.stratum, lang=q.languages[0])
                self.campagne["compose"] += 1
                if res.ok:
                    self.campagne["places"] += 1
                    self.store.log("telephony_appel_place", None,
                                   call_sid=res.provider_call_id, strate=u.stratum)
                else:
                    self.campagne["echecs"] += 1
                    self.store.log("telephony_appel_echoue", None, erreur=(res.error or "")[:200])
                self.bus.publish({"type": "appel", "etat": "place" if res.ok else "echec",
                                  "compose": self.campagne["compose"],
                                  "plafond": n, "strate": u.stratum,
                                  "erreur": None if res.ok else (res.error or "")[:120]})
                time.sleep(1.5)      # cadence : un opérateur coupe une rafale
            self.bus.publish({"type": "campagne",
                              "etat": "arret" if self.campagne["arret"] else "fin",
                              "compose": self.campagne["compose"],
                              "aboutis": self.campagne["aboutis"],
                              "echecs": self.campagne["echecs"]})
        except Exception as exc:
            self.bus.publish({"type": "campagne", "etat": "erreur", "message": str(exc)[:200]})
        finally:
            self.campagne["active"] = False

    def stop_campagne(self) -> dict:
        self.campagne["arret"] = True
        return {"arret": True, "compose": self.campagne.get("compose", 0)}

    def start_wave(self, n: int, cadence: float = 0.05) -> bool:
        """Lance une vague simulée en tâche de fond, entretien par entretien.

        C'est la démonstration de l'élasticité : une vague nationale prend
        trois jours au lieu de six mois parce que la capacité se loue à la
        minute. Ici on la regarde se remplir en une minute, et chaque
        entretien produit porte le canal « simulation », affiché comme tel.
        """
        if self._wave_running:
            return False
        self._wave_running = True
        threading.Thread(target=self._run_wave, args=(n, cadence), daemon=True).start()
        return True

    def _run_wave(self, n: int, cadence: float) -> None:
        # Import tardif : le script de simulation n'est pas nécessaire au
        # fonctionnement du serveur, il ne doit pas peser sur son démarrage.
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from simulate import OUTCOME_MIX, run_one  # noqa: E402
            from ndara.coding import RulesCoder  # noqa: E402
            from ndara.models import Channel, Disposition  # noqa: E402
            from ndara.sampling import draw_frame, to_sample_units  # noqa: E402

            qid = self.default_qid
            q = self.questionnaires[qid]
            # Connexion propre à ce fil : deux fils ne partagent jamais un
            # curseur SQLite.
            store = Store(self.db_path)
            engine = InterviewEngine(store, q, RulesCoder(),
                                     CorpusWriter(store, ROOT / "data" / "corpus"))
            graine = random.randrange(1, 10_000_000)
            rng = random.Random(graine)
            units = to_sample_units(draw_frame(q.country, n, seed=graine))
            store.add_sample_units(units)
            issues = [o for o, _ in OUTCOME_MIX]
            poids = [w for _, w in OUTCOME_MIX]

            aboutis = tires = 0
            self.bus.publish({"type": "vague", "etat": "debut", "n": n})
            for u in units:
                tires += 1
                issue = rng.choices(issues, weights=poids, k=1)[0]
                if issue != Disposition.COMPLETE.value:
                    store.set_unit_disposition(u.msisdn_hash, issue)
                    self.bus.publish({"type": "tirage", "issue": issue,
                                      "tires": tires, "aboutis": aboutis})
                else:
                    iid = run_one(engine, q, rng, "fr", u.stratum, "clean")
                    store.set_unit_disposition(u.msisdn_hash,
                                               Disposition.COMPLETE.value, iid)
                    aboutis += 1
                    self.bus.publish({
                        "type": "abouti", "id": iid[-6:], "strate": u.stratum,
                        "canal": Channel.SIMULATION.value,
                        "tires": tires, "aboutis": aboutis,
                    })
                time.sleep(cadence)
            self.bus.publish({"type": "vague", "etat": "fin",
                              "tires": tires, "aboutis": aboutis})
        except Exception as exc:  # la vague échoue seule, le serveur continue
            self.bus.publish({"type": "vague", "etat": "erreur", "message": str(exc)[:200]})
        finally:
            self._wave_running = False

    @property
    def default_qid(self) -> str:
        return "prix_denrees_cm" if "prix_denrees_cm" in self.questionnaires \
            else next(iter(self.questionnaires))

    def capabilities(self) -> dict:
        """Ce qui est réellement branché. Affiché tel quel dans l'interface :
        un évaluateur doit voir sans ambiguïté ce qui tourne et ce qui est simulé."""
        return {
            "asr": self.asr.name,
            "asr_live": not isinstance(self.asr, MockASR),
            "coder": self.coder.name,
            "telephony": os.environ.get("TWILIO_ACCOUNT_SID") is not None,
            "telephonie": self.telephony_state(),
            "voix": self.voice_inventory(),
            "questionnaires": [
                {"id": qid, "languages": q.languages, "country": q.country,
                 "version": q.version, "steps": len(q.steps),
                 "draft": q.version.endswith("draft")}
                for qid, q in self.questionnaires.items()
            ],
        }

    # ------------------------------------------------------------------
    # La vague omnibus
    # ------------------------------------------------------------------

    def omnibus_view(self, n_aboutis: int = 3000) -> dict:
        """La vague du mois : ce qui est vendu, ce qu'il reste, ce que ça rapporte.

        Le même calcul est fait sous les deux tarifs, parce que le fait le
        plus important du modèle économique est qu'il change de signe entre
        les deux. Le dire soi-même vaut mieux que se le faire dire.
        """
        if self.vague is None:
            return {"disponible": False,
                    "raison": "aucun questionnaire de référence pour composer une vague"}

        v = self.vague
        twilio = v.facture(n_aboutis, omnibus.TARIF_TWILIO_CM)
        operateur = v.facture(n_aboutis, omnibus.TARIF_OPERATEUR)
        # Le même calcul au tarif cambodgien, où la minute coûte six fois
        # moins cher. Ce n'est pas une curiosité : c'est le seul des trois
        # scénarios qui passe au vert sans qu'aucun accord ne soit signé, dès
        # que la question se vend 800 $. Le modèle n'est donc pas le même des
        # deux côtés, et le dossier doit le dire au lieu de présenter une
        # économie camerounaise comme si elle valait partout.
        cambodge = v.facture(n_aboutis, omnibus.TARIF_TWILIO_KH)

        # Ce que coûterait une question de plus, si elle tenait dans l'appel.
        # Dix secondes est la durée d'une question fermée de ce questionnaire.
        marginal = {
            "twilio": v.cout_question_supplementaire(10.0, n_aboutis,
                                                     omnibus.TARIF_TWILIO_CM),
            "operateur": v.cout_question_supplementaire(10.0, n_aboutis,
                                                        omnibus.TARIF_OPERATEUR),
            "cambodge": v.cout_question_supplementaire(10.0, n_aboutis,
                                                       omnibus.TARIF_TWILIO_KH),
        }

        # L'ordre des créneaux pour les premiers appels : la rotation se
        # constate, elle ne se prend pas sur parole.
        rotations = [
            {"rang": r,
             "ordre": [v.creneaux[i].client for i in v.rotation(r)]}
            for r in range(len(v.creneaux))
        ]

        return {
            "disponible": True,
            "vague": v.as_dict(),
            "n_aboutis": n_aboutis,
            "facture": {"twilio": twilio, "operateur": operateur,
                        "cambodge": cambodge},
            # Les tarifs sont des fourchettes relevées sur le compte, pas des
            # prix uniques. Publier la fourchette empêche de faire passer le
            # haut de la plage pour une mesure.
            "fourchettes_usd_minute": {
                "cameroun": list(omnibus.FOURCHETTE_TWILIO_CM),
                "cambodge": list(omnibus.FOURCHETTE_TWILIO_KH),
                "releve": "compte Twilio, 25 août 2026",
            },
            "question_supplementaire": marginal,
            "rotations": rotations,
            "note": (
                "Vague de démonstration : elle redécoupe un questionnaire déjà "
                "synthétisé en un tronc commun et trois créneaux. Les clients "
                "nommés sont des exemples, aucun contrat n'existe."),
        }

    # ------------------------------------------------------------------
    # Téléphonie
    # ------------------------------------------------------------------

    def bind_call(self, call_sid: str | None, interview_id: str) -> None:
        """Relie l'appel de l'opérateur à l'entretien né au décrochage.

        C'est cette table qui permet, à la fin de l'appel, de savoir de quel
        entretien on parle : l'opérateur ne connaît que son propre identifiant.
        """
        if not call_sid:
            return
        with self._live_lock:
            self.par_call_sid[call_sid] = interview_id
            if len(self.par_call_sid) > 2000:      # rien ne doit croître sans fin
                for vieux in list(self.par_call_sid)[:500]:
                    self.par_call_sid.pop(vieux, None)

    def fetch_recording(self, url: str) -> bytes | None:
        """Récupère un enregistrement chez l'opérateur, uniquement s'il existe.

        Appelée seulement quand le consentement au corpus a été donné : c'est
        la route téléphonique qui vérifie avant d'appeler ici, et rien dans
        cette fonction ne doit lui permettre de l'oublier.
        """
        import urllib.request

        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not (sid and token and url):
            return None
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        req = urllib.request.Request(url + ".wav",
                                     headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except Exception as exc:
            self.store.log("telephony_enregistrement_indisponible", None, erreur=str(exc)[:160])
            return None

    def appel_unique(self, numero: str, qid: str, langue: str = "") -> dict:
        """Appelle UN numéro que l'on possède, pour éprouver la chaîne.

        Ce n'est pas une campagne, et la différence n'est pas cosmétique. Une
        campagne tire des numéros au hasard dans les plages du régulateur : on
        ne peut ni choisir qui décroche, ni s'appeler soi-même. C'est
        exactement ce qu'il faut pour collecter, et exactement ce qu'il ne faut
        pas pour éprouver.

        L'entretien produit est marqué comme essai, et il est ensuite exclu des
        estimations. Un appel qu'on se passe à soi-même pour vérifier que la
        ligne marche n'est pas une observation : le compter reviendrait à
        s'interroger soi-même et à publier la réponse.

        Aucune unité de sondage n'est créée non plus, donc le taux de réponse
        n'en sait rien. C'est déjà le cas de tout appel hors campagne, et c'est
        volontaire.
        """
        # On interroge l'adaptateur, pas l'environnement : c'est lui qui
        # composera, et c'est donc lui qui sait s'il en est capable.
        if self.tel.name == "null":
            etat = self.telephony_state()
            return {"lance": False, "raison": "téléphonie non configurée",
                    "manque": etat["manque"]}
        if qid not in self.questionnaires:
            return {"lance": False, "raison": f"questionnaire « {qid} » inconnu"}

        numero = re.sub(r"[^0-9+]", "", numero or "")
        if not re.fullmatch(r"\+[1-9]\d{7,14}", numero):
            return {"lance": False, "raison":
                    "Numéro au format international attendu, indicatif compris : "
                    "+237690000000. Sans le « + » et l'indicatif, l'opérateur ne "
                    "sait pas quel pays composer."}

        q = self.questionnaires[qid]
        res = self.tel.place_call(numero, questionnaire=qid, stratum="essai",
                                  lang=langue or q.languages[0], essai=True)
        self.store.log("telephony_appel_essai", None, ok=res.ok,
                       call_sid=res.provider_call_id, erreur=res.error,
                       note=res.note)
        if not res.ok:
            return {"lance": False, "raison": _twilio_lisible(res.error or "")}
        self.bus.publish({"type": "campagne", "etat": "essai", "n": 1})
        return {"lance": True, "call_sid": res.provider_call_id,
                "questionnaire": qid, "note": res.note}

    def machine_a_decroche(self, interview_id: str | None, call_sid: str,
                           repondu_par: str) -> None:
        """Un répondeur a décroché : on note le non-contact et on raccroche.

        Deux raisons, et les deux comptent. La comptable : sans raccrochage, le
        répondeur écoute deux minutes trente de questionnaire, facturées comme
        un entretien. La statistique : un répondeur n'est ni un refus ni une
        réponse, c'est un non-contact, et confondre les trois fausse le taux de
        réponse, qui est le premier chiffre qu'un lecteur sérieux regarde.

        Le compteur d'appels simultanés n'est pas touché ici : c'est la fin
        d'appel qui le libérera, et elle arrivera juste après.
        """
        from ndara.models import Disposition, utcnow

        if interview_id:
            iv = self.store.get_interview(interview_id)
            if iv and iv.disposition == Disposition.IN_PROGRESS.value:
                iv.disposition = Disposition.NONCONTACT.value
                iv.ended_at = utcnow()
                self.store.save_interview(iv)
                self.drop(interview_id)
                self.bus.publish({"type": "appel", "etat": "repondeur",
                                  "id": interview_id[-6:]})
        raccroche = self.tel.raccrocher(call_sid)
        self.store.log("telephony_repondeur_raccroche", interview_id,
                       repondu_par=repondu_par, raccroche=raccroche)

    def close_call(self, interview_id: str | None, form: dict) -> None:
        """Traduit la fin d'un appel en disposition AAPOR.

        Un appel qui sonne dans le vide, un répondeur, une ligne occupée et un
        raccrochage en plein entretien ne sont pas la même chose. Les confondre
        fausserait le taux de réponse, qui est le premier chiffre qu'un
        statisticien regarde.
        """
        from ndara.models import Disposition, utcnow

        statut = (form.get("CallStatus") or "").lower()
        repondu_par = (form.get("AnsweredBy") or "").lower()
        if statut in ("initiated", "ringing", "queued", "in-progress"):
            return

        self.campagne["places"] = max(0, self.campagne["places"] - 1)
        if not interview_id:
            if statut in ("busy", "no-answer", "canceled", "failed"):
                self.campagne["echecs"] += 1
            return

        iv = self.store.get_interview(interview_id)
        if iv is None:
            return
        nouvelle = None
        if repondu_par.startswith("machine"):
            nouvelle = Disposition.NONCONTACT.value
        elif statut in ("busy", "no-answer", "canceled", "failed"):
            nouvelle = Disposition.NONCONTACT.value
        elif statut == "completed" and iv.disposition == Disposition.IN_PROGRESS.value:
            # Décroché puis raccroché avant la fin : ce n'est ni un refus ni un
            # non-contact, c'est un abandon, et il a son propre code.
            nouvelle = Disposition.BREAKOFF.value

        if nouvelle:
            iv.disposition = nouvelle
            iv.ended_at = utcnow()
            self.store.save_interview(iv)
            self.store.log("telephony_disposition", interview_id, disposition=nouvelle,
                           statut=statut, repondu_par=repondu_par or "—")
            self.drop(interview_id)
            self.bus.publish({"type": "appel", "etat": nouvelle, "id": interview_id[-6:]})
        elif iv.disposition == Disposition.COMPLETE.value:
            self.campagne["aboutis"] += 1

    def telephony_state(self) -> dict:
        """Ce qui manque encore pour pouvoir appeler, nommé un par un.

        La forme des identifiants est vérifiée, jamais leur contenu. Un
        identifiant de compte fait 34 caractères et commence par AC, un jeton
        en fait 32 : trois erreurs de collage sur quatre se voient à ce
        contrôle, et aucune ne demande de lire le secret. « Identifiants
        refusés » ne dit pas lequel des deux, ni pourquoi ; « votre jeton fait
        18 caractères au lieu de 32 » le dit.
        """
        manque = []
        if not os.environ.get("TWILIO_ACCOUNT_SID"):
            manque.append("TWILIO_ACCOUNT_SID")
        if not os.environ.get("TWILIO_AUTH_TOKEN"):
            manque.append("TWILIO_AUTH_TOKEN")
        if not os.environ.get("TWILIO_FROM_NUMBER"):
            manque.append("TWILIO_FROM_NUMBER")
        if not os.environ.get("NDARA_PUBLIC_URL"):
            manque.append("NDARA_PUBLIC_URL")
        return {
            "fournisseur": self.tel.name,
            "prete": not manque,
            "manque": manque,
            "numero": os.environ.get("TWILIO_FROM_NUMBER", ""),
            "adresse_publique": os.environ.get("NDARA_PUBLIC_URL", ""),
            "forme": _forme_identifiants(),
            "campagne": dict(self.campagne),
        }

    def register_questionnaire(self, qid: str) -> None:
        """Rend un questionnaire déposé immédiatement menable.

        Aucun redémarrage : le client dépose son tableau et peut passer son
        premier entretien dans la foulée. C'est là que se joue la promesse
        « on charge le questionnaire, la machine fait le reste ».
        """
        q = Questionnaire.load(ROOT / "data" / "questionnaires" / f"{qid}.json")
        self.questionnaires[qid] = q
        self.engines[qid] = InterviewEngine(self.store, q, self.coder, self.corpus)

    def voice_inventory(self) -> dict:
        """Combien de libellés parlent, par questionnaire et par langue.

        Une image livrée sans ses fichiers audio retombe sur la synthèse du
        navigateur, en silence, et la démonstration perd sa voix sans que
        personne le voie. Ce compte rend la panne visible à l'écran, ce qui
        est la seule façon de ne pas la découvrir devant un jury.
        """
        inv: dict[str, dict] = {}
        for qid, q in self.questionnaires.items():
            par_langue, attendus = {}, {}
            for lang in q.languages:
                # Les libellés à gabarit, comme le code de retrait, changent à
                # chaque entretien : ils ne sont jamais pré-synthétisés et ne
                # doivent donc pas manquer au compte.
                attendus[lang] = sum(
                    1 for k in q.prompt_keys()
                    if q.prompt(k, lang) and "{" not in q.prompt(k, lang)
                ) + len(q.steps)
                d = AUDIO_ROOT / qid / lang
                par_langue[lang] = len(list(d.glob("*.mp3"))) if d.is_dir() else 0
            inv[qid] = {"attendu": max(attendus.values()) if attendus else 0,
                        "attendu_par_langue": attendus, "presents": par_langue}
        return inv


APP: App | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "NDARA/0.1"

    # ---------------- utilitaires ----------------

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return dict(urllib.parse.parse_qsl(raw.decode("utf-8")))

    def _read_form(self) -> dict:
        """Les paramètres du corps, **y compris ceux dont la valeur est vide**.

        `keep_blank_values` n'est pas un détail de confort ici, c'est la
        condition pour que la signature de l'opérateur soit vérifiable. Twilio
        calcule la sienne sur tous les paramètres qu'il envoie, vides compris,
        et un appel entrant en porte une dizaine de vides : la ville, la région
        et le code postal de l'appelant sont inconnus dès que l'appel vient de
        l'étranger. Sans cette option, Python les jette en silence, la
        concaténation n'est plus la même, et **toute requête entrante est
        refusée en 403** alors que le jeton est bon. Le symptôme est trompeur
        au possible : la console dit « identifiants acceptés », les appels
        sortants passent, et seuls les entrants échouent.
        """
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype)

    def log_message(self, fmt: str, *args) -> None:  # silence
        pass

    # ---------------- flux d'événements ----------------

    def _sse(self, evt: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(evt, ensure_ascii=False).encode("utf-8")
                         + b"\n\n")
        self.wfile.flush()

    def _stream(self) -> None:
        """Flux d'événements tenu ouvert : le terrain pousse, l'écran suit.

        Un battement toutes les deux secondes sert à la fois de rafraîchissement
        des compteurs et de signe de vie pour les intermédiaires réseau, qui
        referment volontiers une connexion silencieuse.
        """
        assert APP is not None
        f = APP.bus.subscribe()
        if f is None:
            return self._json({"error": "trop d'écrans connectés"}, 503)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")   # pas de tampon chez l'intermédiaire
        self.end_headers()
        try:
            self._sse(APP.pulse())
            dernier = time.time()
            while True:
                try:
                    self._sse(f.get(timeout=1.0))
                except queue.Empty:
                    pass
                if time.time() - dernier >= 2.0:
                    dernier = time.time()
                    self._sse(APP.pulse())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            APP.bus.unsubscribe(f)

    # ---------------- GET ----------------

    def do_GET(self) -> None:
        assert APP is not None
        parsed = urllib.parse.urlparse(self.path)
        route, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            return self._serve_file(STATIC / "index.html")
        if route == "/dashboard":
            return self._serve_file(STATIC / "dashboard.html")
        if route in ("/nouvelle", "/nouvelle-enquete"):
            return self._serve_file(STATIC / "nouvelle.html")
        if route == "/exemple.csv":
            return self._send(200, EXEMPLE_CSV.encode("utf-8"),
                              "text/csv; charset=utf-8")
        if route.startswith("/static/"):
            return self._serve_file(STATIC / route[len("/static/"):])
        if route.startswith("/docs/"):
            # La table de validation de l'auto-audit est citée depuis le
            # tableau de bord : elle doit être lisible sans cloner le dépôt.
            name = route[len("/docs/"):]
            if "/" in name or ".." in name:
                return self._send(404, b"not found", "text/plain")
            path = ROOT / "docs" / name
            if not path.is_file():
                return self._send(404, b"not found", "text/plain")
            return self._send(200, path.read_bytes(), "text/plain; charset=utf-8")
        if route.startswith("/audio/"):
            return self._serve_file(AUDIO_ROOT / route[len("/audio/"):])

        if route in ("/health", "/healthz"):
            # Les hébergeurs sondent cette route. Elle sert aussi de diagnostic
            # à distance : elle dit ce qui est branché sans ouvrir l'interface.
            caps = APP.capabilities()
            return self._json({
                "ok": True,
                "asr": caps["asr"],
                "asr_live": caps["asr_live"],
                "coder": caps["coder"],
                "telephony": caps["telephony"],
                "questionnaires": [q["id"] for q in caps["questionnaires"]],
            })

        if route == "/api/pulse":
            return self._json(APP.pulse())

        if route == "/api/stream":
            return self._stream()

        if route == "/api/capabilities":
            return self._json(APP.capabilities())

        if route == "/api/dashboard":
            qid = (qs.get("questionnaire") or [APP.default_qid])[0]
            q = APP.questionnaires[qid]
            margins = APP.margins.get(qid, {})
            data = estimate_all(APP.store, q, margins)
            data["corpus"] = APP.corpus.stats()
            data["capabilities"] = APP.capabilities()
            data["provenance"] = APP.store.provenance()
            return self._json(data)

        if route == "/api/quality":
            qid = (qs.get("questionnaire") or [APP.default_qid])[0]
            q = APP.questionnaires[qid]
            ivs = APP.store.interviews()
            turns = {iv.id: APP.store.turns(iv.id) for iv in ivs}
            return self._json(quality_report(q, ivs, turns))

        if route == "/api/corpus":
            return self._json(APP.corpus.stats())

        if route == "/api/omnibus":
            # Combien d'entretiens aboutis on suppose pour chiffrer la vague.
            # Par défaut la taille d'une vague nationale mensuelle du dossier.
            try:
                n_aboutis = max(1, int((qs.get("n") or ["3000"])[0]))
            except ValueError:
                n_aboutis = 3000
            return self._json(APP.omnibus_view(n_aboutis))

        return self._send(404, b"not found", "text/plain")

    # ---------------- POST ----------------

    def do_POST(self) -> None:
        assert APP is not None
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route == "/api/start":
            body = self._read_json()
            qid = body.get("questionnaire") or APP.default_qid
            engine = APP.engines[qid]
            prompt = engine.start(
                language=body.get("language") or engine.q.languages[0],
                stratum=body.get("stratum") or "WEB",
                channel=body.get("channel") or "web",
            )
            out = prompt.to_dict()
            out["questionnaire"] = qid
            ligne = APP.touch(prompt.interview_id, langue=body.get("language") or "fr",
                              canal=body.get("channel") or "web", etape=prompt.step_id,
                              progression=0.0, methode="—", questionnaire=qid)
            APP.bus.publish({"type": "entretien", "etat": "debut", "ligne": ligne})
            return self._json(out)

        if route in ("/api/questionnaire/verifier", "/api/questionnaire"):
            body = self._read_json()
            res = construire(body.get("tableau") or "", body.get("meta") or {})
            sortie = res.to_dict()
            if route == "/api/questionnaire/verifier" or not res.ok:
                # La vérification ne dépose rien : on peut la relancer autant
                # de fois qu'il faut avant d'engager quoi que ce soit.
                sortie.pop("questionnaire", None)
                return self._json(sortie, 200 if res.ok else 422)

            doc = res.questionnaire
            chemin = ROOT / "data" / "questionnaires" / f"{doc['id']}.json"
            if chemin.exists():
                return self._json({"ok": False, "problemes": [{
                    "ligne": None, "colonne": "titre",
                    "message": f"Une enquête « {doc['id']} » existe déjà.",
                    "correction": "Changez le titre : deux instruments de même nom "
                                  "rendraient les données impossibles à démêler."}]}, 409)
            chemin.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            APP.register_questionnaire(doc["id"])
            sortie["deposee"] = True
            sortie["chemin"] = f"data/questionnaires/{doc['id']}.json"
            sortie["voix"] = APP.voice_inventory().get(doc["id"], {})
            APP.bus.publish({"type": "questionnaire", "id": doc["id"],
                             "questions": res.resume.get("questions")})
            return self._json(sortie)

        if route == "/api/campagne":
            body = self._read_json()
            res = APP.start_campagne(int(body.get("n") or 10),
                                     body.get("questionnaire") or APP.default_qid,
                                     int(body.get("simultanes") or 3))
            return self._json(res, 200 if res.get("lance") else 409)

        if route == "/api/campagne/arret":
            return self._json(APP.stop_campagne())

        if route == "/api/telephonie/verifier":
            # Une lecture chez l'opérateur, gratuite, qui tranche entre « le
            # jeton est faux » et « le jeton est bon mais autre chose bloque ».
            # Les pays a interroger sont ceux des questionnaires charges :
            # inutile de demander a Twilio des droits sur un pays ou l'on
            # n'enquete pas, et il ne faut en oublier aucun de ceux ou l'on
            # enquete.
            pays = sorted({q.country for q in APP.questionnaires.values() if q.country})
            res = APP.tel.verifier(pays)
            APP.store.log("telephony_verification", None, ok=res.get("ok"),
                          etat=res.get("etat"), type=res.get("type"))
            if not res.get("ok"):
                res["raison"] = _twilio_lisible(res.get("raison") or "")
            return self._json(res, 200 if res.get("ok") else 400)

        if route == "/api/appel":
            # Un appel, vers un numéro qu'on possède, pour éprouver la chaîne.
            # Distinct de la campagne, qui tire au hasard et ne permet donc ni
            # de choisir qui décroche, ni de s'appeler soi-même.
            body = self._read_json()
            res = APP.appel_unique(str(body.get("numero") or ""),
                                   body.get("questionnaire") or APP.default_qid,
                                   body.get("langue") or "")
            return self._json(res, 200 if res.get("lance") else 400)

        if route == "/api/wave":
            body = self._read_json()
            n = max(10, min(400, int(body.get("n") or 120)))
            cadence = max(0.01, min(0.5, float(body.get("cadence") or 0.05)))
            lance = APP.start_wave(n, cadence)
            return self._json({"lance": lance, "n": n,
                               "note": "vague simulée, canal « simulation »"},
                              200 if lance else 409)

        if route == "/api/answer":
            body = self._read_json()
            qid = body.get("questionnaire") or APP.default_qid
            engine = APP.engines[qid]
            audio_bytes = None
            transcript = body.get("text")
            asr_conf = None
            if body.get("audio_b64"):
                audio_bytes = base64.b64decode(body["audio_b64"])
                transcript, asr_conf = APP.asr.transcribe(
                    audio_bytes, body.get("language") or "fr",
                    body.get("audio_ext") or "webm")
            elif body.get("asr"):
                # Reconnaissance faite dans le navigateur du répondant. C'est
                # une vraie transcription par un vrai moteur, pas une
                # simulation : le nom du moteur est renvoyé et affiché.
                asr_conf = body.get("asr_confidence")
                if asr_conf is None:
                    asr_conf = 0.0
                asr_conf = max(0.0, min(1.0, float(asr_conf)))
            try:
                prompt = engine.submit(
                    body["interview_id"],
                    text=transcript,
                    dtmf=body.get("dtmf"),
                    audio_bytes=audio_bytes,
                    audio_ext=body.get("audio_ext") or "webm",
                    asr_confidence=asr_conf,
                    duration_ms=body.get("duration_ms"),
                )
            except KeyError as exc:
                return self._json({"error": str(exc)}, 404)
            out = prompt.to_dict()
            out["questionnaire"] = qid
            out["transcript"] = transcript
            out["asr_confidence"] = asr_conf
            methode = "clavier" if body.get("dtmf") else (
                "voix" if (body.get("audio_b64") or body.get("asr")) else "saisie")
            iid = body["interview_id"]
            if prompt.done:
                ligne = APP.touch(iid, etape="terminé", progression=1.0, methode=methode)
                APP.drop(iid)
                APP.bus.publish({"type": "entretien", "etat": "fin", "ligne": ligne})
            else:
                ligne = APP.touch(iid, etape=prompt.step_id, methode=methode,
                                  progression=round(prompt.progress or 0.0, 3))
                APP.bus.publish({"type": "entretien", "etat": "tour", "ligne": ligne})
            return self._json(out)

        if route == "/api/withdraw":
            body = self._read_json()
            engine = APP.engines[APP.default_qid]
            return self._json(engine.withdraw((body.get("code") or "").strip().upper()))

        # ---- surface téléphonique (dormante tant qu'aucun opérateur n'est branché) ----

        if route.startswith("/twiml/"):
            return self._handle_twiml(route, parsed)

        return self._send(404, b"not found", "text/plain")

    def _handle_twiml(self, route: str, parsed) -> None:
        assert APP is not None
        qs = urllib.parse.parse_qs(parsed.query)
        form = self._read_form()
        base = os.environ.get("NDARA_PUBLIC_URL", "").rstrip("/")

        # ---- garde d'entrée : personne d'autre que l'opérateur téléphonique --
        #
        # Ces routes ouvrent des entretiens et y répondent. Sur une adresse
        # publique, les laisser sans signature reviendrait à laisser un inconnu
        # fabriquer des données qui entreraient ensuite dans les estimations.
        # Aucun contournement n'est prévu, pas même pour les essais : un test
        # signe ses requêtes comme le ferait l'opérateur.
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not token:
            APP.store.log("telephony_rejet", None, raison="aucun jeton configuré")
            return self._send(503, b"telephonie non configuree", "text/plain")
        url = base + self.path if base else self.path
        if not signature_valide(token, url, form,
                                self.headers.get("X-Twilio-Signature", "")):
            APP.store.log("telephony_rejet", (qs.get("interview_id") or [None])[0],
                          raison="signature invalide", route=route)
            return self._send(403, b"signature invalide", "text/plain")

        qid = (qs.get("questionnaire") or [APP.default_qid])[0]
        if qid not in APP.engines:
            return self._send(404, b"questionnaire inconnu", "text/plain")
        engine = APP.engines[qid]

        def repondre(prompt, interview_id: str, langue: str) -> None:
            iv = APP.store.get_interview(interview_id)
            consenti = bool(iv and iv.consent_corpus == "granted")
            # Sans moteur de transcription, un enregistrement ne rend qu'un
            # fichier : la réponse arriverait vide, le moteur relancerait deux
            # fois, et le corpus recevrait du son sans texte. On collecte
            # quand on sait transcrire, et on dit quand on ne collecte pas.
            transcrit = not isinstance(APP.asr, MockASR)
            if consenti and not transcrit and prompt.kind == "question":
                APP.store.log("corpus_segment_non_collecte", interview_id,
                              etape=prompt.step_id,
                              raison="aucun moteur de transcription configuré")
            action = f"{base}/twiml/step?interview_id={interview_id}&questionnaire={qid}"
            xml = prompt_to_twiml(prompt.to_dict(), action_url=action, audio_base=base,
                                  corpus_consenti=consenti, transcription=transcrit,
                                  langue=langue)
            ligne = APP.touch(interview_id, canal="phone", langue=langue,
                              etape=prompt.step_id, questionnaire=qid,
                              progression=round(prompt.progress or 0.0, 3),
                              methode="téléphone")
            if prompt.done:
                APP.drop(interview_id)
            APP.bus.publish({"type": "entretien",
                             "etat": "fin" if prompt.done else "tour", "ligne": ligne})
            self._send(200, xml.encode("utf-8"), "text/xml")

        if route == "/twiml/start":
            langue = (qs.get("lang") or ["fr"])[0]
            # Le numéro du répondant n'est pas au même endroit selon le sens de
            # l'appel. En sortant, c'est nous qui composons, donc le répondant
            # est « To ». En entrant, c'est lui qui compose : « To » est notre
            # propre numéro, et le prendre reviendrait à enregistrer tous les
            # appels entrants sous un seul et même numéro, le nôtre.
            entrant = (form.get("Direction") or "").startswith("inbound")
            prompt = engine.start(language=langue,
                                  stratum=(qs.get("stratum") or ["MTN"])[0],
                                  channel="phone",
                                  msisdn=form.get("From") if entrant
                                         else form.get("To"))
            if (qs.get("essai") or [""])[0]:
                # Un appel qu'on se passe à soi-même pour vérifier la ligne
                # n'est pas une observation. On le marque ici, et l'analyse
                # l'écarte : le compter reviendrait à s'interroger soi-même
                # puis à publier la réponse.
                iv = APP.store.get_interview(prompt.interview_id)
                if iv is not None:
                    iv.meta["essai"] = True
                    iv.flags = list(iv.flags) + ["essai"]
                    APP.store.save_interview(iv)
            APP.store.log("telephony_appel_decroche", prompt.interview_id,
                          call_sid=form.get("CallSid"), essai=bool((qs.get("essai") or [""])[0]))
            APP.bind_call(form.get("CallSid"), prompt.interview_id)
            return repondre(prompt, prompt.interview_id, langue)

        if route == "/twiml/step":
            interview_id = (qs.get("interview_id") or [""])[0]
            iv = APP.store.get_interview(interview_id)
            if iv is None:
                return self._send(404, b"entretien inconnu", "text/plain")
            duree = int(float(form.get("RecordingDuration") or 0) * 1000) or None
            audio = None
            if form.get("RecordingUrl") and iv.consent_corpus == "granted":
                audio = APP.fetch_recording(form["RecordingUrl"])
            try:
                prompt = engine.submit(
                    interview_id,
                    text=form.get("SpeechResult"),
                    dtmf=form.get("Digits"),
                    audio_bytes=audio,
                    audio_ext="wav",
                    asr_confidence=_confiance_twilio(form.get("Confidence")),
                    duration_ms=duree,
                )
            except KeyError:
                return self._send(404, b"entretien inconnu", "text/plain")
            return repondre(prompt, interview_id, iv.language)

        if route == "/twiml/amd":
            # Verdict de la détection de répondeur, rendu PENDANT l'appel.
            #
            # Il a sa propre route, et ce n'est pas un détail d'organisation :
            # confondu avec la fin d'appel, il décrémenterait le compteur
            # d'appels simultanés une fois de trop, et la campagne composerait
            # plus de numéros que son plafond ne l'autorise.
            call_sid = form.get("CallSid") or ""
            iid = APP.par_call_sid.get(call_sid) or (qs.get("interview_id") or [None])[0]
            repondu_par = (form.get("AnsweredBy") or "").lower()
            APP.store.log("telephony_amd", iid, repondu_par=repondu_par or "—",
                          duree_ms=form.get("MachineDetectionDuration"))
            if repondu_par.startswith("machine") or repondu_par == "fax":
                APP.machine_a_decroche(iid, call_sid, repondu_par)
            return self._send(200, b"", "text/plain")

        if route == "/twiml/status":
            iid = (APP.par_call_sid.get(form.get("CallSid") or "")
                   or (qs.get("interview_id") or [None])[0])
            APP.store.log("telephony_status", iid, **form)
            APP.close_call(iid, form)
            return self._send(200, b"", "text/plain")

        return self._send(404, b"not found", "text/plain")


def main() -> None:
    setup_console()
    global APP
    ap = argparse.ArgumentParser(description="Serveur NDARA")
    # Un hébergeur impose son port et écoute sur toutes les interfaces. Sans
    # cette lecture d'environnement, le service démarre et reste injoignable.
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--db", default="data/ndara.db")
    args = ap.parse_args()

    APP = App(args.db)
    caps = APP.capabilities()
    print("NDARA — serveur d'entretien")
    print(f"  entretien       http://{args.host}:{args.port}/")
    print(f"  tableau de bord http://{args.host}:{args.port}/dashboard")
    print(f"  transcription   {caps['asr']} ({'branchée' if caps['asr_live'] else 'non branchée → saisie/clavier'})")
    print(f"  codage          {caps['coder']}")
    print(f"  questionnaires  {', '.join(q['id'] for q in caps['questionnaires'])}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
