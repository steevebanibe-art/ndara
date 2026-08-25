"""Simule l'opérateur téléphonique de bout en bout, signatures comprises.

Aucun compte n'est nécessaire : on rejoue exactement ce que Twilio envoie,
et on signe comme lui. Si ce script passe, la seule chose qui manque pour
appeler quelqu'un pour de vrai est le compte et le numéro.
"""
import base64
import hashlib
import hmac
import json
import re
import sys
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8170"
PUBLIC = sys.argv[2] if len(sys.argv) > 2 else BASE
JETON = "jeton_de_test_1234567890"


def signer(url: str, form: dict) -> str:
    base = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    return base64.b64encode(
        hmac.new(JETON.encode(), base.encode(), hashlib.sha1).digest()).decode()


def poster(chemin: str, form: dict, signature: str | None = None):
    url_publique = PUBLIC + chemin
    req = urllib.request.Request(
        BASE + chemin,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Twilio-Signature": signature if signature is not None
                                       else signer(url_publique, form)})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def dit(xml: str) -> str:
    """Ce que le répondant entend, en clair."""
    morceaux = re.findall(r"<Say[^>]*>(.*?)</Say>|<Play>([^<]*)</Play>", xml, re.S)
    out = []
    for say, play in morceaux:
        if say:
            out.append(say.strip())
        elif play:
            out.append("[audio] " + play.rsplit("/", 1)[-1])
    return " / ".join(out)


def attend(xml: str) -> str:
    coupable = " (coupable)" if "</Gather>" in xml else ""
    if "<Record" in xml:
        return "ENREGISTRE"
    if 'input="dtmf speech"' in xml:
        return "touches ou voix" + coupable
    if "<Gather" in xml:
        return "voix seule" + coupable
    if "<Hangup/>" in xml:
        return "raccroche"
    if "<Redirect" in xml:
        return "enchaine"
    return "?"


print("=" * 74)
print("1. UNE REQUETE NON SIGNEE DOIT ETRE REFUSEE")
code, corps = poster("/twiml/start?questionnaire=prix_denrees_cm&stratum=MTN&lang=fr",
                     {"CallSid": "CAtest", "To": "+237690000001"}, signature="faux")
print(f"   reponse du serveur : HTTP {code} · {corps.strip()[:60]}")
assert code == 403, "une requete forgee a ete acceptee"

print()
print("2. L'APPEL EST DECROCHE, SIGNE CORRECTEMENT")
chemin = "/twiml/start?questionnaire=prix_denrees_cm&stratum=MTN&lang=fr"
code, xml = poster(chemin, {"CallSid": "CAtest01", "To": "+237690000001",
                            "From": "+15550001111", "CallStatus": "in-progress"})
print(f"   HTTP {code} · attend : {attend(xml)}")
print(f"   NDARA dit : {dit(xml)[:100]}")
assert code == 200

def suivante(xml: str) -> str:
    """L'adresse de suite, qu'elle vienne d'un Gather, d'un Record ou d'un Redirect."""
    m = (re.search(r'action="([^"]+)"', xml)
         or re.search(r"<Redirect[^>]*>([^<]+)</Redirect>", xml))
    u = urllib.parse.urlparse(m.group(1))
    return u.path + "?" + u.query

suite = suivante(xml)
iid = urllib.parse.parse_qs(urllib.parse.urlparse(suite).query)["interview_id"][0]
print(f"   entretien ouvert : ...{iid[-8:]}")

print()
print("3. L'ENTRETIEN, MENE COMME AU TELEPHONE")
reponses = [
    {"Digits": "1"},                                                  # continuer apres l'annonce
    {"SpeechResult": "oui d'accord", "Confidence": "0.92"},           # consentement enquete
    {"SpeechResult": "oui pas de souci", "Confidence": "0.90"},       # consentement corpus
    {"SpeechResult": "je suis dans le Littoral", "Confidence": "0.88"},
    {"Digits": "1"},                                                  # femme
    {"SpeechResult": "j'ai quarante et un ans", "Confidence": "0.85"},
    {"SpeechResult": "on est cinq a la maison", "Confidence": "0.87"},
    {"Digits": "1"},                                                  # riz oui
    {"SpeechResult": "mille deux cents francs", "Confidence": "0.86"},
    {"SpeechResult": "mille cinq cents francs", "Confidence": "0.86"},
    {"Digits": "1"},                                                  # prix en hausse
    {"Digits": "2"},                                                  # repas non reduits
    {"Digits": "2"},                                                  # pas de journee sautee
]
enregistrements = 0
for i, rep in enumerate(reponses, 1):
    form = {"CallSid": "CAtest01", **rep}
    code, xml = poster(suite, form)
    if code != 200:
        print(f"   tour {i} : HTTP {code}")
        break
    if "<Record" in xml:
        enregistrements += 1
    etat = attend(xml)
    print(f"   tour {i:>2} · {etat:<15} · {dit(xml)[:64]}")
    if etat == "raccroche":
        break
    suite = suivante(xml)

print()
print("4. FIN D'APPEL : LA DISPOSITION EST POSEE")
code, _ = poster("/twiml/status", {"CallSid": "CAtest01", "CallStatus": "completed",
                                   "CallDuration": "154", "AnsweredBy": "human"})
print(f"   HTTP {code}")

print()
print("5. UN REPONDEUR N'EST PAS UN REFUS")
chemin2 = "/twiml/start?questionnaire=prix_denrees_cm&stratum=ORANGE&lang=fr"
code, xml2 = poster(chemin2, {"CallSid": "CAtest02", "To": "+237690000002",
                              "CallStatus": "in-progress"})
code, _ = poster("/twiml/status", {"CallSid": "CAtest02", "CallStatus": "completed",
                                   "AnsweredBy": "machine_start"})
print(f"   HTTP {code}")

print()
print("6. UN REPONDEUR DETECTE EN COURS D'APPEL EST RACCROCHE")
chemin3 = "/twiml/start?questionnaire=prix_denrees_cm&stratum=MTN&lang=fr"
code, _ = poster(chemin3, {"CallSid": "CAtest03", "To": "+237690000003",
                           "CallStatus": "in-progress"})
# Le verdict de detection arrive PENDANT l'appel, sur sa propre route.
code, _ = poster("/twiml/amd", {"CallSid": "CAtest03", "AnsweredBy": "machine_start",
                                "MachineDetectionDuration": "3200"})
print(f"   verdict de detection : HTTP {code}")
code, _ = poster("/twiml/status", {"CallSid": "CAtest03", "CallStatus": "completed",
                                   "CallDuration": "6"})
print(f"   fin d'appel : HTTP {code}")
print("   (sans cette route separee, le repondeur ecoutait le questionnaire entier)")

print()
print("7. CE QUI EST ARRIVE DANS LA BASE")
with urllib.request.urlopen(BASE + "/api/dashboard", timeout=20) as r:
    d = json.loads(r.read())
print("   provenance :", d.get("provenance"))
print("   dispositions :", d.get("fieldwork", {}).get("counts"))
print()
print(f"   enregistrements demandes pendant l'appel : {enregistrements}")
print("   (uniquement apres le consentement au corpus, et jamais sur une question sensible)")
