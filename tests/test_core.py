"""Tests du cœur NDARA — stdlib unittest, aucune dépendance.

    python -m unittest discover -s tests -v

Les tests portent en priorité sur ce qui doit être défendable devant un
jury : le double consentement, l'absence d'audio sans consentement, le
codage, la détection d'entretiens dégradés, et le calage sur marges.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ndara.audit import audit_interview  # noqa: E402
from ndara.coding import RulesCoder, parse_number  # noqa: E402
from ndara.corpus import CorpusWriter, redact_text  # noqa: E402
from ndara.engine import InterviewEngine  # noqa: E402
from ndara.models import CODE_UNCLEAR, Consent, Disposition  # noqa: E402
from ndara.questionnaire import Questionnaire  # noqa: E402
from ndara.sampling import OutcomeCounts, draw_frame  # noqa: E402
from ndara.storage import Store  # noqa: E402
from ndara.weighting import build_weights, jackknife_ci, rake, weighted_mean  # noqa: E402

QPATH = ROOT / "data" / "questionnaires" / "prix_denrees_cm.json"


def fresh_engine():
    tmp = tempfile.mkdtemp()
    store = Store(Path(tmp) / "t.db")
    q = Questionnaire.load(QPATH)
    return store, q, InterviewEngine(store, q, RulesCoder(),
                                     CorpusWriter(store, Path(tmp) / "corpus")), tmp


# --------------------------------------------------------------------------

class TestNumberParsing(unittest.TestCase):
    def test_digits(self):
        self.assertEqual(parse_number("environ 1500 francs", "fr"), 1500)
        self.assertEqual(parse_number("1 500", "fr"), 1500)
        self.assertEqual(parse_number("2.500 francs", "fr"), 2500)

    def test_words(self):
        self.assertEqual(parse_number("mille cinq cents", "fr"), 1500)
        self.assertEqual(parse_number("deux mille", "fr"), 2000)
        self.assertEqual(parse_number("sept", "fr"), 7)

    def test_khmer_digits(self):
        self.assertEqual(parse_number("៣០០០", "km"), 3000)

    def test_no_number(self):
        self.assertIsNone(parse_number("je ne sais pas du tout", "fr"))


class TestCoding(unittest.TestCase):
    def setUp(self):
        self.q = Questionnaire.load(QPATH)
        self.c = RulesCoder()

    def test_yes_no(self):
        step = self.q.step("bought_rice")
        self.assertEqual(self.c.code_answer(step, "oui bien sûr", "fr").code, "yes")
        self.assertEqual(self.c.code_answer(step, "non pas du tout", "fr").code, "no")

    def test_dontknow(self):
        step = self.q.step("rice_price")
        self.assertEqual(self.c.code_answer(step, "je ne sais pas", "fr").code, "__dk__")

    def test_choice_by_label(self):
        step = self.q.step("region")
        self.assertEqual(self.c.code_answer(step, "j'habite à Douala", "fr").code, "LITTORAL")

    def test_out_of_bounds_is_unclear(self):
        """Une valeur hors bornes ne doit jamais être acceptée en silence."""
        step = self.q.step("rice_price")
        res = self.c.code_answer(step, "999999", "fr")
        self.assertEqual(res.code, CODE_UNCLEAR)

    def test_implausible_is_flagged_not_rejected(self):
        step = self.q.step("rice_price")
        res = self.c.code_answer(step, "9000", "fr")
        self.assertEqual(res.value_num, 9000)
        self.assertIn("hors_plage_plausible", res.flags)


class TestCodingPhraseSpontanee(unittest.TestCase):
    """La réponse est souvent noyée dans une phrase. Elle doit être trouvée,
    et surtout elle ne doit jamais être inventée."""

    def setUp(self):
        self.q = Questionnaire.load(QPATH)
        self.c = RulesCoder()

    def code(self, step_id, phrase):
        return self.c.code_answer(self.q.step(step_id), phrase, "fr")

    def test_un_nombre_de_passage_n_est_pas_une_touche(self):
        """Le piège qui produisait un faux codage silencieux.

        « on est cinq » contenait un cinq que l'ancien code lisait comme la
        modalité numéro cinq. Un nombre ne vaut comme touche que s'il est
        annoncé, ou s'il est à peu près tout ce que la personne a dit.
        """
        res = self.code("region", "bon moi j'habite dans le Littoral, on est cinq à la maison")
        self.assertEqual(res.code, "LITTORAL")

    def test_le_libelle_le_plus_long_gagne(self):
        """« Est » la région s'écrit comme « est » le verbe, « même » est un
        synonyme de « stable ». Sans cette règle, le hasard de l'ordre tranche."""
        self.assertEqual(self.code("price_direction", "ça a un peu baissé quand même").code,
                         "baisse")
        self.assertEqual(self.code("region", "je suis dans l'Est").code, "EST")

    def test_touche_dite_a_voix_haute(self):
        self.assertEqual(self.code("region", "numéro trois").code, "EST")
        self.assertEqual(self.code("region", "trois").code, "EST")

    def test_negation_sans_le_mot_non(self):
        res = self.code("bought_rice", "je n'ai rien acheté du tout")
        self.assertEqual(res.code, "no")
        self.assertIn("deduit_de_la_negation", res.flags)

    def test_pas_mal_n_est_pas_une_negation(self):
        self.assertEqual(self.code("bought_rice", "pas mal de riz oui").code, "yes")

    def test_le_bon_nombre_parmi_plusieurs(self):
        """Les bornes de la question tranchent entre les nombres de la phrase."""
        res = self.code("hh_size", "nous sommes cinq et j'ai payé mille cinq cents francs")
        self.assertEqual(res.value_num, 5)
        self.assertIn("nombre_choisi_par_plage_plausible", res.flags)

    def test_valeur_rangee_dans_sa_tranche(self):
        res = self.code("age_group", "j'ai trente-deux ans")
        self.assertEqual(res.code, "25_34")
        self.assertIn("valeur_rangee_en_tranche", res.flags)

    def test_tranche_ouverte_vers_le_haut(self):
        self.assertEqual(self.code("age_group", "j'ai soixante-dix ans").code, "65_PLUS")

    def test_ville_pour_region(self):
        self.assertEqual(self.code("region", "je vis à Yaoundé depuis dix ans").code, "CENTRE")

    def test_hors_sujet_reste_incompris(self):
        """Ce qui n'est pas une réponse doit relancer, jamais être rangé quelque part."""
        self.assertEqual(self.code("region", "vous êtes qui exactement").code, CODE_UNCLEAR)
        self.assertEqual(self.code("hh_size", "je suis au marché là").code, CODE_UNCLEAR)


class TestImportQuestionnaire(unittest.TestCase):
    """Un client dépose son tableau. Ce qui est accepté doit être menable,
    et ce qui est ambigu doit être refusé avec la ligne fautive."""

    META = {"titre": "Essai import", "objet": "les prix", "pays": "CM",
            "incitation": "200 francs de crédit"}

    def construire(self, tableau, **meta):
        from ndara.importer import construire
        m = dict(self.META)
        m.update(meta)
        return construire(tableau, m)

    def test_exemple_fourni_est_valide(self):
        from ndara.importer import EXEMPLE_CSV
        res = self.construire(EXEMPLE_CSV)
        self.assertTrue(res.ok, [p.message for p in res.problemes])
        self.assertEqual(res.resume["questions"], 7)

    def test_instrument_produit_est_menable(self):
        """Le seul juge qui compte : le validateur du moteur."""
        from ndara.importer import EXEMPLE_CSV
        res = self.construire(EXEMPLE_CSV)
        q = Questionnaire.from_dict(res.questionnaire)
        self.assertEqual(len(q.steps), 7)
        self.assertEqual(q.step("prix_riz").ask_if, {"step": "riz", "equals": "yes"})

    def test_separateur_et_accents_tolerants(self):
        tableau = ("Libellé,Nature,Réponses\n"
                   "Aimez-vous le riz ?,Oui/Non,\n"
                   "Quelle région ?,choix,Centre|Est\n")
        res = self.construire(tableau)
        self.assertTrue(res.ok, [p.message for p in res.problemes])

    def test_colonne_question_manquante_est_refusee(self):
        res = self.construire("type,modalites\nchoix,Femme|Homme\n")
        self.assertFalse(res.ok)
        self.assertTrue(any("question" in p.message for p in res.problemes))

    def test_choix_a_une_seule_modalite_est_refuse(self):
        res = self.construire("question;type;modalites\nVotre sexe ?;choix;Femme\n")
        self.assertFalse(res.ok)
        self.assertEqual(res.problemes[0].ligne, 2)

    def test_plus_de_dix_modalites_est_refuse(self):
        mods = "|".join(f"M{i}" for i in range(11))
        res = self.construire(f"question;type;modalites\nQuoi ?;choix;{mods}\n")
        self.assertFalse(res.ok)
        self.assertIn("dix", res.problemes[0].message)

    def test_filtre_vers_une_question_inexistante_est_refuse(self):
        res = self.construire("id;question;type;filtre\n"
                              "a;Avez-vous du riz ?;oui_non;\n"
                              "b;Combien ?;nombre;fantome=yes\n")
        self.assertFalse(res.ok)
        self.assertTrue(any("fantome" in p.message for p in res.problemes))

    def test_filtre_vers_une_question_posee_apres_est_refuse(self):
        res = self.construire("id;question;type;filtre\n"
                              "b;Combien ?;nombre;a=yes\n"
                              "a;Avez-vous du riz ?;oui_non;\n")
        self.assertFalse(res.ok)

    def test_les_touches_sont_ajoutees_au_libelle(self):
        """Sans les touches dans le libellé, le repli clavier est impossible
        pour quelqu'un qui ne sait pas lire."""
        res = self.construire("question;type;modalites\nVotre sexe ?;choix;Femme|Homme\n")
        self.assertTrue(res.ok)
        self.assertIn("tapez 1", res.questionnaire["steps"][0]["text"]["fr"])

    def test_question_sensible_exclue_du_corpus(self):
        res = self.construire("question;type;sensible\nAvez-vous faim ?;oui_non;oui\n")
        self.assertTrue(res.ok)
        self.assertIs(res.questionnaire["steps"][0]["corpus_eligible"], False)

    def test_objet_vide_est_refuse(self):
        """L'objet est annoncé au répondant dès la première phrase : il n'a
        pas le droit d'être vague."""
        res = self.construire("question;type\nQuoi ?;libre\n", objet="")
        self.assertFalse(res.ok)

    def test_annonce_et_consentements_ne_sont_pas_negociables(self):
        res = self.construire("question;type\nQuoi ?;libre\n")
        prompts = res.questionnaire["prompts"]
        self.assertIn("intelligence artificielle", prompts["announce"]["fr"])
        self.assertIn("facultatif", prompts["consent_corpus"]["fr"])
        self.assertNotEqual(prompts["consent_survey"]["fr"], prompts["consent_corpus"]["fr"])


class TestTelephonie(unittest.TestCase):
    """La surface téléphonique est publique : elle se défend ou elle est une
    porte ouverte sur l'instrument."""

    JETON = "jeton_de_test_1234567890"
    BASE = "https://exemple.test"

    def signer(self, url, form):
        import base64 as b64
        import hashlib
        import hmac
        base = url + "".join(f"{k}{form[k]}" for k in sorted(form))
        return b64.b64encode(
            hmac.new(self.JETON.encode(), base.encode(), hashlib.sha1).digest()).decode()

    def test_signature_valide_est_acceptee(self):
        from ndara.providers.telephony import signature_valide
        url = f"{self.BASE}/twiml/step?interview_id=iv_1"
        form = {"Digits": "1", "CallSid": "CA123"}
        self.assertTrue(signature_valide(self.JETON, url, form, self.signer(url, form)))

    def test_signature_forgee_est_refusee(self):
        from ndara.providers.telephony import signature_valide
        url = f"{self.BASE}/twiml/step?interview_id=iv_1"
        form = {"Digits": "1", "CallSid": "CA123"}
        self.assertFalse(signature_valide(self.JETON, url, form, "AAAAbbbbCCCCddddEEEE="))

    def test_parametre_modifie_invalide_la_signature(self):
        """Le cœur de la garde : on ne doit pas pouvoir changer une réponse
        en cours de route sans casser la signature."""
        from ndara.providers.telephony import signature_valide
        url = f"{self.BASE}/twiml/step?interview_id=iv_1"
        form = {"Digits": "1", "CallSid": "CA123"}
        sig = self.signer(url, form)
        self.assertFalse(signature_valide(self.JETON, url, {**form, "Digits": "2"}, sig))

    def test_sans_jeton_rien_ne_passe(self):
        from ndara.providers.telephony import signature_valide
        self.assertFalse(signature_valide("", self.BASE, {}, "peu importe"))

    def test_pas_d_enregistrement_sans_consentement_au_corpus(self):
        """La règle la plus importante de tout le module : sans accord
        explicite, aucune voix n'est conservée nulle part."""
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"text": "Combien de personnes ?", "input_type": "number",
                  "allow_voice": True, "allow_dtmf": False, "options": []}
        xml = prompt_to_twiml(prompt, action_url="https://x/step", corpus_consenti=False)
        self.assertNotIn("<Record", xml)
        self.assertIn("<Gather", xml)

    def test_enregistrement_seulement_apres_consentement(self):
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"text": "Combien de personnes ?", "input_type": "number",
                  "allow_voice": True, "allow_dtmf": False, "options": []}
        xml = prompt_to_twiml(prompt, action_url="https://x/step",
                              corpus_consenti=True, transcription=True)
        self.assertIn("<Record", xml)

    def test_pas_d_enregistrement_sans_moteur_de_transcription(self):
        """Record ne rend qu'un fichier. Sans moteur, la réponse arriverait
        vide, le moteur relancerait deux fois, et le corpus recevrait du son
        sans texte. On collecte quand on sait transcrire."""
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"kind": "question", "text": "Combien de personnes ?",
                  "input_type": "number", "allow_voice": True,
                  "allow_dtmf": False, "options": []}
        xml = prompt_to_twiml(prompt, action_url="https://x/step",
                              corpus_consenti=True, transcription=False)
        self.assertNotIn("<Record", xml)
        # Et la question reste posée : le repli est Gather, qui transcrit au
        # vol par le canal, pas un tour perdu.
        self.assertIn("<Gather", xml)

    def test_question_sensible_jamais_enregistree_meme_consentie(self):
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"text": "Avez-vous sauté un repas ?", "allow_voice": True,
                  "allow_dtmf": False, "options": [], "corpus_eligible": False}
        xml = prompt_to_twiml(prompt, action_url="https://x/step", corpus_consenti=True)
        self.assertNotIn("<Record", xml)

    def test_le_clavier_reste_offert_sur_les_modalites(self):
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"text": "Femme ou homme ?", "allow_dtmf": True,
                  "options": [{"code": "F", "dtmf": "1"}, {"code": "M", "dtmf": "2"}]}
        xml = prompt_to_twiml(prompt, action_url="https://x/step")
        self.assertIn('input="dtmf speech"', xml)
        # Séparées par des virgules : « 12 » collé demandait à la
        # reconnaissance d'attendre le mot « douze ».
        self.assertIn('hints="1,2"', xml)

    def test_les_modalites_sont_donnees_en_indices_de_reconnaissance(self):
        """Le moteur connaît les seules réponses recevables. Les taire serait
        laisser la reconnaissance deviner des noms de lieux toute seule."""
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"kind": "question", "text": "Quelle région ?", "allow_dtmf": True,
                  "options": [{"code": "LT", "dtmf": "1", "label": "Littoral"},
                              {"code": "EN", "dtmf": "2", "label": "Extrême-Nord"}]}
        xml = prompt_to_twiml(prompt, action_url="https://x/step")
        self.assertIn("Littoral", xml)
        self.assertIn("Nord", xml)

    def test_une_question_peut_etre_coupee_par_la_reponse(self):
        """Sans cela, l'écoute ne commence qu'à la fin de la phrase : la
        première syllabe se perd et chaque tour porte un blanc."""
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"kind": "question", "text": "Combien de personnes ?",
                  "allow_voice": True, "allow_dtmf": True,
                  "options": [{"code": "A", "dtmf": "1", "label": "un"}]}
        xml = prompt_to_twiml(prompt, action_url="https://x/step")
        self.assertIn("</Gather>", xml)
        # La lecture est DANS l'écoute.
        self.assertLess(xml.index("<Gather"), xml.index("Combien de personnes"))

    def test_un_consentement_ne_peut_pas_etre_coupe(self):
        """Un « oui » lâché à la moitié de la phrase n'est pas un consentement."""
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"kind": "consent", "text": "Acceptez-vous de répondre ?",
                  "allow_voice": True, "allow_dtmf": True,
                  "options": [{"code": "yes", "dtmf": "1", "label": "Oui"},
                              {"code": "no", "dtmf": "2", "label": "Non"}]}
        xml = prompt_to_twiml(prompt, action_url="https://x/step")
        self.assertNotIn("</Gather>", xml)
        # La phrase est dite en entier AVANT que l'écoute s'ouvre.
        self.assertLess(xml.index("Acceptez-vous"), xml.index("<Gather"))

    def test_la_relance_precede_la_question_et_garde_la_voix_de_studio(self):
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"kind": "question", "text": "Combien de personnes ?",
                  "audio_url": "/audio/q/fr/hh_size.mp3",
                  "note": "Je n'ai pas bien compris.",
                  "note_audio_url": "/audio/q/fr/relance_unclear.mp3",
                  "allow_voice": True, "allow_dtmf": False, "options": []}
        xml = prompt_to_twiml(prompt, action_url="https://x/step",
                              audio_base="https://x")
        self.assertLess(xml.index("relance_unclear.mp3"), xml.index("hh_size.mp3"))
        # Et pas un mot en voix de secours : la relance est pré-synthétisée.
        self.assertNotIn("<Say", xml)

    def test_la_reconnaissance_emploie_un_modele_telephonique(self):
        """Le support commande le modèle, pas seulement la durée des réponses.

        Réglé sur `googlev2_short` jusqu'au 27 août 2026 : un modèle taillé
        pour des réponses brèves, ce qui était le bon raisonnement, mais
        entraîné sur de l'audio pleine bande, ce qui était le mauvais support.
        Une ligne téléphonique ne transporte que 8 kHz. Mesuré sur le premier
        vrai appel : un « oui » en français camerounais transcrit « puis-je ».

        La liste ci-dessous est celle des modèles téléphoniques de Twilio. On
        peut passer de l'un à l'autre, jamais revenir à un modèle pleine bande.
        """
        from ndara.providers.telephony import prompt_to_twiml
        xml = prompt_to_twiml({"kind": "question", "text": "Le prix ?",
                               "allow_voice": True, "options": []},
                              action_url="https://x/step")
        telephoniques = ("phone_call", "googlev2_telephony",
                         "googlev2_telephony_short")
        self.assertTrue(any(f'speechModel="{m}"' in xml for m in telephoniques),
                        f"modèle non téléphonique dans : {xml}")
        # Une réponse d'enquête ne se fait pas censurer en « f*** » : ce qui
        # est dit est la donnée.
        self.assertIn('profanityFilter="false"', xml)
        # Un silence remonte au moteur, qui relance, au lieu de tomber
        # silencieusement dans le filet de sécurité.
        self.assertIn('actionOnEmptyResult="true"', xml)

    def test_le_repondeur_ne_se_paie_pas_en_silence_au_decrochage(self):
        """La détection de répondeur ne doit pas retenir la ligne pendant que
        la personne dit « allô » dans le vide."""
        from ndara.providers.telephony import TwilioTelephony
        tel = TwilioTelephony(sid="AC1", token="t", from_number="+237600000000",
                              webhook_base="https://x")
        vus = {}

        class FauxReponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"sid": "CA1"}'

        import urllib.request
        vrai = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda req, timeout=None: (
                vus.update(urllib.parse.parse_qsl(req.data.decode())), FauxReponse())[1]
            res = tel.place_call("+237600000001")
        finally:
            urllib.request.urlopen = vrai
        self.assertTrue(res.ok)
        self.assertEqual(vus.get("AsyncAmd"), "true")
        self.assertEqual(vus.get("MachineDetection"), "Enable")
        # Le verdict de détection a sa propre route : confondu avec la fin
        # d'appel, il libérerait un créneau de campagne encore occupé.
        self.assertTrue(vus.get("AsyncAmdStatusCallback", "").endswith("/twiml/amd"))
        self.assertNotEqual(vus.get("AsyncAmdStatusCallback"),
                            vus.get("StatusCallback"))

    def test_la_boucle_se_referme_toujours(self):
        """Un silence total ne doit pas laisser l'appel ouvert : il se
        facture à la minute."""
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"text": "Une question", "allow_dtmf": True,
                  "options": [{"code": "F", "dtmf": "1"}]}
        self.assertIn("<Redirect", prompt_to_twiml(prompt, action_url="https://x/step"))

    def test_annonce_n_attend_pas_de_reponse(self):
        """Chaque seconde d'attente inutile est facturée sur chaque appel."""
        from ndara.providers.telephony import prompt_to_twiml
        prompt = {"text": "Bonjour, je suis une intelligence artificielle.",
                  "allow_voice": False, "allow_dtmf": False, "options": []}
        xml = prompt_to_twiml(prompt, action_url="https://x/step")
        self.assertNotIn("<Gather", xml)
        self.assertNotIn("<Record", xml)
        self.assertIn("<Redirect", xml)

    def test_fin_d_entretien_raccroche(self):
        from ndara.providers.telephony import prompt_to_twiml
        xml = prompt_to_twiml({"text": "Merci.", "done": True}, action_url="https://x/step")
        self.assertIn("<Hangup/>", xml)
        self.assertNotIn("<Gather", xml)


class TestAppelDEssai(unittest.TestCase):
    """Appeler un numéro qu'on possède, pour éprouver la chaîne.

    Une campagne tire au hasard : elle ne permet ni de choisir qui décroche,
    ni de s'appeler soi-même. C'est juste pour collecter et inutilisable pour
    vérifier que la ligne marche.
    """

    def test_un_numero_mal_forme_est_refuse_avec_la_correction(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("srv_t", ROOT / "web" / "server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        tmp = tempfile.mkdtemp()
        app = srv.App(str(Path(tmp) / "t.db"))
        app.tel = _TelephonieFactice()
        for mauvais in ("", "690000000", "0690000000", "abc", "+1"):
            res = app.appel_unique(mauvais, app.default_qid)
            self.assertFalse(res["lance"], mauvais)
            self.assertIn("international", res["raison"])

    def test_un_numero_correct_est_compose_et_marque_essai(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("srv_t2", ROOT / "web" / "server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        tmp = tempfile.mkdtemp()
        app = srv.App(str(Path(tmp) / "t.db"))
        faux = _TelephonieFactice()
        app.tel = faux
        # Les espaces d'un numéro recopié à la main ne doivent pas le faire échouer.
        res = app.appel_unique("+237 690 00 00 00", app.default_qid)
        self.assertTrue(res["lance"], res.get("raison"))
        self.assertEqual(faux.appels[0]["msisdn"], "+237690000000")
        self.assertTrue(faux.appels[0]["essai"],
                        "un appel d'essai doit se déclarer comme tel jusqu'au webhook")

    def test_la_forme_des_identifiants_se_verifie_sans_les_lire(self):
        """« Identifiants refusés » ne dit ni lequel des deux, ni pourquoi.

        Un SID fait 34 caractères et commence par AC, un jeton en fait 32.
        Trois erreurs de collage sur quatre se voient là, et aucune ne demande
        d'afficher le secret.
        """
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location("srv_t4", ROOT / "web" / "server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)

        avant = (os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
        try:
            def forme(sid, jeton):
                os.environ["TWILIO_ACCOUNT_SID"] = sid
                os.environ["TWILIO_AUTH_TOKEN"] = jeton
                return " ".join(srv._forme_identifiants())

            self.assertEqual(forme("AC" + "0" * 32, "f" * 32), "",
                             "des identifiants bien formés ne doivent rien signaler")
            self.assertIn("18 caractères", forme("AC" + "0" * 32, "f" * 18))
            self.assertIn("espace", forme("AC" + "0" * 32, " " + "f" * 32))
            self.assertIn("ne commence pas", forme("SK" + "0" * 32, "f" * 32))
            self.assertIn("AUTH TOKENS", forme("SK" + "0" * 32, "f" * 32))
            # Et surtout : le secret lui-même ne sort jamais du diagnostic.
            self.assertNotIn("f" * 10, forme("AC" + "0" * 32, "f" * 18))
        finally:
            for cle, val in zip(("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"), avant):
                if val is None:
                    os.environ.pop(cle, None)
                else:
                    os.environ[cle] = val

    def test_un_compte_qui_refuse_la_detection_de_repondeur_appelle_quand_meme(self):
        """Un confort qui empêche d'appeler n'est plus un confort.

        Un compte d'essai refuse les paramètres de détection de répondeur, et
        refuse l'appel entier avec eux. On les tente, on s'en passe si besoin,
        et on dit ce qu'on a perdu.
        """
        from ndara.providers.telephony import TwilioTelephony

        tel = TwilioTelephony(sid="AC1", token="t", from_number="+15550001111",
                              webhook_base="https://x")
        tentatives = []

        def faux_poster(params):
            from ndara.providers.telephony import CallResult
            tentatives.append(dict(params))
            if "MachineDetection" in params:
                return CallResult(ok=False, error=(
                    "HTTP 400 · Invalid or disallowed parameters provided - trial "
                    "accounts have limited parameter access, upgrade your account"))
            return CallResult(ok=True, provider_call_id="CA1")

        tel._poster = faux_poster
        res = tel.place_call("+237690000000")

        self.assertTrue(res.ok, "l'appel aurait dû partir sans la détection")
        self.assertEqual(len(tentatives), 2, "il faut exactement une reprise")
        self.assertIn("MachineDetection", tentatives[0])
        self.assertNotIn("MachineDetection", tentatives[1])
        self.assertNotIn("AsyncAmd", tentatives[1])
        self.assertEqual(tentatives[1]["To"], "+237690000000")
        self.assertFalse(tel.amd_actif)
        self.assertIn("répondeur", res.note or "")

    def test_un_vrai_refus_n_est_pas_retente(self):
        """Un numéro non vérifié ne devient pas valable en retirant un paramètre."""
        from ndara.providers.telephony import CallResult, TwilioTelephony

        tel = TwilioTelephony(sid="AC1", token="t", from_number="+15550001111",
                              webhook_base="https://x")
        tentatives = []

        def faux_poster(params):
            tentatives.append(dict(params))
            return CallResult(ok=False, error="HTTP 400 · 21219 · unverified")

        tel._poster = faux_poster
        res = tel.place_call("+237690000000")
        self.assertFalse(res.ok)
        self.assertEqual(len(tentatives), 1, "on ne rappelle pas pour rien")

    def test_le_corps_de_la_reponse_n_est_pas_jete(self):
        """« HTTP Error 400: Bad Request » est vrai et parfaitement inutile.

        Le code et le message sont dans le corps. Les perdre transforme une
        correction de trente secondes en après-midi perdue.
        """
        import io
        import urllib.error
        from ndara.providers.telephony import _detail_http

        class FausseReponse(urllib.error.HTTPError):
            def __init__(self, corps):
                super().__init__("https://api.twilio.test", 400, "Bad Request",
                                 {}, io.BytesIO(corps.encode()))

        dit = _detail_http(FausseReponse(
            '{"code": 21219, "message": "The number is unverified.", "status": 400}'))
        self.assertIn("21219", dit)
        self.assertIn("unverified", dit)
        # Un corps qui n'est pas du JSON ne doit pas faire disparaître l'info.
        self.assertIn("passerelle en panne",
                      _detail_http(FausseReponse("passerelle en panne")))

    def test_une_erreur_de_l_operateur_devient_actionnable(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("srv_t3", ROOT / "web" / "server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        dit = srv._twilio_lisible("Unable to create record: ... 21219 ...")
        self.assertIn("Verified Caller IDs", dit)
        self.assertIn("21219", dit)
        # Une erreur inconnue passe telle quelle plutôt que d'être avalée.
        self.assertIn("panne inconnue", srv._twilio_lisible("panne inconnue"))

    def test_un_entretien_d_essai_n_entre_pas_dans_les_estimations(self):
        """S'interroger soi-même puis publier la réponse ne serait pas une mesure."""
        from ndara.analysis import build_records
        store, q, moteur, _tmp = fresh_engine()
        for essai in (False, True):
            p = moteur.start(language="fr", stratum="MTN", channel="phone")
            iid = p.interview_id
            if essai:
                iv = store.get_interview(iid)
                iv.meta["essai"] = True
                store.save_interview(iv)
            moteur.submit(iid)
            moteur.submit(iid, dtmf="1")
            moteur.submit(iid, dtmf="2")
            garde = 0
            while garde < 40:
                garde += 1
                p = moteur.submit(iid, dtmf="1")
                if p.done:
                    break
        recs = build_records(store, q)
        self.assertEqual(len(recs), 1, "l'entretien d'essai a été compté")


class _TelephonieFactice:
    """Un opérateur qui note ce qu'on lui demande, sans composer."""

    name = "factice"

    def __init__(self):
        self.appels = []

    def place_call(self, msisdn, questionnaire="", stratum="", lang="fr", essai=False):
        from ndara.providers.telephony import CallResult
        self.appels.append({"msisdn": msisdn, "questionnaire": questionnaire,
                            "stratum": stratum, "lang": lang, "essai": essai})
        return CallResult(ok=True, provider_call_id="CAfactice")

    def raccrocher(self, call_sid):
        return True


class TestDoubleConsent(unittest.TestCase):
    """Le point le plus sensible du dossier : il doit être vérifié par un test."""

    def test_announce_comes_first(self):
        _, _, eng, _ = fresh_engine()
        p = eng.start(language="fr", stratum="MTN")
        self.assertEqual(p.kind, "announce")
        self.assertIn("intelligence artificielle", p.text.lower())

    def test_survey_refusal_ends_interview(self):
        store, _, eng, _ = fresh_engine()
        p = eng.start(language="fr", stratum="MTN")
        eng.submit(p.interview_id)
        p2 = eng.submit(p.interview_id, dtmf="2")
        self.assertTrue(p2.done)
        iv = store.get_interview(p.interview_id)
        self.assertEqual(iv.disposition, Disposition.REFUSAL.value)
        self.assertEqual(iv.consent_survey, Consent.REFUSED.value)

    def test_consents_are_separate_and_ordered(self):
        store, _, eng, _ = fresh_engine()
        p = eng.start(language="fr", stratum="MTN")
        p1 = eng.submit(p.interview_id)
        self.assertEqual(p1.step_id, "__consent_survey__")
        p2 = eng.submit(p.interview_id, dtmf="1")
        self.assertEqual(p2.step_id, "__consent_corpus__")
        self.assertIn("facultatif", p2.text.lower())

    def test_corpus_refusal_does_not_stop_interview(self):
        store, _, eng, _ = fresh_engine()
        p = eng.start(language="fr", stratum="MTN")
        eng.submit(p.interview_id)
        eng.submit(p.interview_id, dtmf="1")
        p3 = eng.submit(p.interview_id, dtmf="2")
        self.assertEqual(p3.kind, "question")
        iv = store.get_interview(p.interview_id)
        self.assertEqual(iv.consent_corpus, Consent.REFUSED.value)
        self.assertEqual(iv.consent_survey, Consent.GRANTED.value)

    def test_no_audio_written_without_corpus_consent(self):
        store, _, eng, tmp = fresh_engine()
        p = eng.start(language="fr", stratum="MTN")
        eng.submit(p.interview_id)
        eng.submit(p.interview_id, dtmf="1")
        eng.submit(p.interview_id, dtmf="2")          # corpus refusé
        eng.submit(p.interview_id, dtmf="2", audio_bytes=b"FAKEAUDIO", duration_ms=3000)
        turns = store.turns(p.interview_id)
        self.assertTrue(all(t.audio_path is None for t in turns))
        self.assertEqual(store.corpus_items(), [])

    def test_audio_written_with_corpus_consent(self):
        store, _, eng, _ = fresh_engine()
        p = eng.start(language="fr", stratum="MTN")
        eng.submit(p.interview_id)
        eng.submit(p.interview_id, dtmf="1")
        eng.submit(p.interview_id, dtmf="1")          # corpus accordé
        eng.submit(p.interview_id, dtmf="2", audio_bytes=b"FAKEAUDIO", duration_ms=3000)
        self.assertEqual(len(store.corpus_items()), 1)

    def test_sensitive_step_excluded_from_corpus(self):
        """`reduced_meals` est marquée non éligible : jamais versée au corpus."""
        q = Questionnaire.load(QPATH)
        self.assertFalse(q.step("reduced_meals").corpus_eligible)
        self.assertFalse(q.step("skipped_day").corpus_eligible)


class TestFlowAndRelance(unittest.TestCase):
    def _consented(self, eng):
        p = eng.start(language="fr", stratum="MTN")
        eng.submit(p.interview_id)
        eng.submit(p.interview_id, dtmf="1")
        return eng.submit(p.interview_id, dtmf="1"), p.interview_id

    def test_dontknow_is_a_valid_answer_and_advances(self):
        """« Je ne sais pas » est une réponse codable, pas une incompréhension :
        l'entretien avance et la non-réponse partielle est enregistrée."""
        store, _, eng, _ = fresh_engine()
        p, iid = self._consented(eng)
        first = p.step_id
        p2 = eng.submit(iid, text="je ne sais pas")
        self.assertNotEqual(p2.step_id, first)
        codes = {t.step_id: t.code for t in store.turns(iid)}
        self.assertEqual(codes[first], "__dk__")

    def test_unintelligible_triggers_relance_then_keypad(self):
        store, _, eng, _ = fresh_engine()
        p, iid = self._consented(eng)
        first = p.step_id
        p2 = eng.submit(iid, text="zzk wbbr ttq")
        self.assertEqual(p2.step_id, first)          # on reste sur la même question
        self.assertIsNotNone(p2.note)
        p3 = eng.submit(iid, text="zzk wbbr ttq")
        self.assertEqual(p3.step_id, first)
        self.assertTrue(p3.allow_dtmf)               # dernière relance : repli clavier
        self.assertIn("touches", p3.note.lower())

    def test_filter_skips_rice_price(self):
        store, q, eng, _ = fresh_engine()
        p, iid = self._consented(eng)
        for step_id, ans in [("region", "2"), ("sex", "1"), ("age_group", "3"),
                             ("hh_size", None), ("bought_rice", "2")]:
            if step_id == "hh_size":
                p = eng.submit(iid, text="5")
            else:
                p = eng.submit(iid, dtmf=ans)
        self.assertNotEqual(p.step_id, "rice_price")
        codes = {t.step_id: t.code for t in store.turns(iid)}
        self.assertEqual(codes.get("rice_price"), "__skipped__")

    def test_withdrawal_deletes_corpus(self):
        store, q, eng, _ = fresh_engine()
        p, iid = self._consented(eng)
        eng.submit(iid, dtmf="2", audio_bytes=b"A", duration_ms=2000)
        self.assertEqual(len(store.corpus_items()), 1)
        iv = store.get_interview(iid)
        iv.withdrawal_code = "TESTCD"
        store.save_interview(iv)
        res = eng.withdraw("TESTCD")
        self.assertTrue(res["found"])
        self.assertEqual(store.corpus_items(), [])


class TestAudit(unittest.TestCase):
    def test_straightliner_is_flagged(self):
        store, q, eng, _ = fresh_engine()
        p = eng.start(language="fr", stratum="MTN")
        iid = p.interview_id
        eng.submit(iid)
        eng.submit(iid, dtmf="1")
        p = eng.submit(iid, dtmf="1")
        guard = 0
        while not p.done and guard < 30:
            guard += 1
            step = q.step(p.step_id)
            if step and step.type == "numeric":
                p = eng.submit(iid, text="1", duration_ms=200)
            else:
                p = eng.submit(iid, dtmf="1", duration_ms=200)
        iv = store.get_interview(iid)
        a = audit_interview(q, iv, store.turns(iid))
        self.assertTrue(a.needs_review)
        self.assertIn("reponses_trop_rapides", a.flags)

    def test_clean_interview_not_flagged(self):
        store, q, eng, _ = fresh_engine()
        p = eng.start(language="fr", stratum="MTN")
        iid = p.interview_id
        eng.submit(iid)
        eng.submit(iid, dtmf="1")
        p = eng.submit(iid, dtmf="1")
        answers = {"region": "2", "sex": "1", "age_group": "3", "bought_rice": "1",
                   "price_direction": "1", "reduced_meals": "2", "skipped_day": "2"}
        numeric = {"hh_size": "6", "rice_price": "800", "oil_price": "1600"}
        guard = 0
        while not p.done and guard < 30:
            guard += 1
            step = q.step(p.step_id)
            if step is None:
                break
            ms = int(step.expected_seconds * 900)
            if step.id in numeric:
                p = eng.submit(iid, text=numeric[step.id], duration_ms=ms)
            else:
                p = eng.submit(iid, dtmf=answers.get(step.id, "1"), duration_ms=ms)
        iv = store.get_interview(iid)
        a = audit_interview(q, iv, store.turns(iid))
        self.assertFalse(a.needs_review, f"flags inattendus : {a.flags}")
        self.assertEqual(iv.disposition, Disposition.COMPLETE.value)


class TestWeighting(unittest.TestCase):
    def _records(self):
        recs = []
        for i in range(60):
            recs.append({
                "id": f"i{i}", "stratum": "MTN" if i % 2 else "ORANGE",
                "region": "CENTRE" if i < 45 else "EST",      # échantillon déséquilibré
                "sex": "F" if i % 3 else "M",
                "age_group": "25_34",
                "rice_price": 800 + (i % 7) * 25,
            })
        return recs

    def test_raking_hits_margins(self):
        recs = self._records()
        margins = {"region": {"CENTRE": 0.5, "EST": 0.5}}
        w, rep = rake(recs, [1.0] * len(recs), margins)
        total = sum(w)
        share_centre = sum(x for r, x in zip(recs, w) if r["region"] == "CENTRE") / total
        self.assertAlmostEqual(share_centre, 0.5, places=3)
        self.assertTrue(rep.converged)

    def test_raking_reports_missing_category(self):
        recs = self._records()
        margins = {"region": {"CENTRE": 0.4, "EST": 0.4, "SUD": 0.2}}
        _, rep = rake(recs, [1.0] * len(recs), margins)
        self.assertTrue(any("SUD" in w for w in rep.warnings))

    def test_design_effect_and_trimming(self):
        recs = self._records()
        margins = {"region": {"CENTRE": 0.3, "EST": 0.7}}
        res = build_weights(recs, {"MTN": 30, "ORANGE": 30}, margins)
        self.assertGreaterEqual(res.design_effect, 1.0)
        self.assertLessEqual(res.effective_n, len(recs))

    def test_absence_de_calage_est_signalee(self):
        """Sans marges, le calage « converge » sans rien caler. Ça doit se lire.

        Le questionnaire khmer n'a pas de population de référence : sans cette
        ligne, ses estimations sortiraient avec l'apparence d'un calage réussi.
        """
        recs = self._records()
        sans = build_weights(recs, {"MTN": 30, "ORANGE": 30}, {})
        avec = build_weights(recs, {"MTN": 30, "ORANGE": 30},
                             {"region": {"CENTRE": 0.5, "EST": 0.5}})
        self.assertEqual(sans.rake_report.variables, [])
        self.assertEqual(avec.rake_report.variables, ["region"])
        self.assertTrue(sans.rake_report.converged,
                        "un calage vide converge : c'est bien pourquoi il faut le dire")

        from ndara.analysis import _disclosure
        from ndara.sampling import OutcomeCounts
        outcomes = OutcomeCounts(complete=40, partial=5, refusal=10, noncontact=20)
        qualite = {"flagged_share": 0.0, "coding_agreement": {"agreement": None}}
        dit_sans = " ".join(_disclosure(sans, outcomes, qualite))
        dit_avec = " ".join(_disclosure(avec, outcomes, qualite))
        self.assertIn("Aucun calage sur marges", dit_sans)
        self.assertNotIn("Aucun calage sur marges", dit_avec)

    def test_jackknife_gives_interval(self):
        recs = self._records()
        margins = {"region": {"CENTRE": 0.5, "EST": 0.5}}
        out = jackknife_ci(recs, {"MTN": 30, "ORANGE": 30}, margins,
                           lambda r, w: weighted_mean(r, w, "rice_price"), groups=8)
        self.assertGreater(out["ci_high"], out["ci_low"])
        self.assertGreater(out["estimate"], 700)


class TestSamplingAndPrivacy(unittest.TestCase):
    def test_frame_respects_prefixes_and_allocation(self):
        drawn = draw_frame("CM", 100, seed=1)
        self.assertEqual(len(drawn), 100)
        self.assertTrue(all(len(d.msisdn) == 9 for d in drawn))
        self.assertGreater(sum(1 for d in drawn if d.stratum == "MTN"), 30)

    def test_msisdn_is_hashed(self):
        drawn = draw_frame("CM", 5, seed=2)
        for d in drawn:
            self.assertTrue(d.msisdn_hash.startswith("r_"))
            self.assertNotIn(d.msisdn, d.msisdn_hash)

    def test_response_rates(self):
        c = OutcomeCounts(complete=25, partial=5, refusal=24, noncontact=42,
                          ineligible=5, unknown=6)
        self.assertGreater(c.rr3(), c.rr2())
        self.assertGreater(c.cooperation(), c.rr3())

    def test_redaction(self):
        self.assertIn("[TEL]", redact_text("appelle moi au 699 12 34 56"))
        self.assertNotIn("699", redact_text("appelle moi au 699123456"))
        self.assertIn("1500", redact_text("le riz coûte 1500 francs"))
        self.assertIn("[NOM]", redact_text("je m'appelle Amadou Bello"))


class TestAppelEntrant(unittest.TestCase):
    """NDARA décroche quand c'est l'autre qui appelle.

    Le sens de l'appel change deux choses et une seule est visible. Twilio
    envoie « Direction: inbound », et surtout il inverse les rôles : « To »
    devient notre propre numéro, « From » celui du répondant. Prendre « To »
    sans regarder le sens enregistrerait tous les entretiens entrants sous un
    seul numéro, le nôtre, ce qui saborde silencieusement la déduplication et
    le respect des retraits.

    Ce chemin compte parce qu'il ne dépend d'aucune autorisation d'appel
    sortant : la liste noire anti-fraude de l'opérateur ne s'applique qu'à ce
    qu'on compose, jamais à ce qu'on reçoit.
    """

    JETON = "jeton_de_test_1234567890"

    def _serveur(self):
        import importlib.util, os, threading
        from http.server import ThreadingHTTPServer
        spec = importlib.util.spec_from_file_location("srv_in", ROOT / "web" / "server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        tmp = tempfile.mkdtemp()
        srv.APP = srv.App(str(Path(tmp) / "t.db"))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        port = httpd.server_address[1]
        os.environ["TWILIO_AUTH_TOKEN"] = self.JETON
        os.environ["NDARA_PUBLIC_URL"] = f"http://127.0.0.1:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return srv, port

    def _poster(self, port, chemin, form):
        import base64, hashlib, hmac, urllib.parse, urllib.request
        url = f"http://127.0.0.1:{port}{chemin}"
        brut = url + "".join(f"{k}{form[k]}" for k in sorted(form))
        sig = base64.b64encode(
            hmac.new(self.JETON.encode(), brut.encode(), hashlib.sha1).digest()).decode()
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Twilio-Signature": sig})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8")

    def test_un_appel_entrant_ouvre_un_entretien_et_annonce_l_ia(self):
        srv, port = self._serveur()
        code, xml = self._poster(port, "/twiml/start?essai=1&lang=fr", {
            "CallSid": "CAentrant0001", "From": "+237690000001",
            "To": "+16193041285", "Direction": "inbound", "CallStatus": "ringing"})
        self.assertEqual(code, 200)
        self.assertIn("<Response>", xml)
        # La toute première chose entendue est l'annonce, jamais une question.
        self.assertIn("announce", xml)

    def test_en_entrant_le_repondant_est_l_appelant_pas_notre_numero(self):
        srv, port = self._serveur()
        self._poster(port, "/twiml/start?essai=1&lang=fr", {
            "CallSid": "CAentrant0002", "From": "+237690000002",
            "To": "+16193041285", "Direction": "inbound", "CallStatus": "ringing"})
        ivs = srv.APP.store.list_interviews()
        self.assertEqual(len(ivs), 1)
        iv = srv.APP.store.get_interview(ivs[0].id if hasattr(ivs[0], "id") else ivs[0])
        self.assertNotIn("6193041285", str(iv.msisdn or ""))

    def test_en_sortant_le_repondant_reste_le_numero_compose(self):
        """La correction ne doit pas casser le sens qui marchait déjà."""
        srv, port = self._serveur()
        self._poster(port, "/twiml/start?essai=1&lang=fr", {
            "CallSid": "CAsortant0003", "From": "+16193041285",
            "To": "+237690000003", "Direction": "outbound-api",
            "CallStatus": "in-progress"})
        ivs = srv.APP.store.list_interviews()
        iv = srv.APP.store.get_interview(ivs[0].id if hasattr(ivs[0], "id") else ivs[0])
        self.assertNotIn("6193041285", str(iv.msisdn or ""))


class TestAppelEntrant(unittest.TestCase):
    """NDARA décroche quand c'est l'autre qui appelle, et vérifie la signature.

    Ce chemin compte doublement. Il ne dépend d'aucune autorisation d'appel
    sortant, donc il fonctionne même quand la destination est sur la liste
    noire anti-fraude de l'opérateur. Et c'est celui que le jury empruntera
    s'il compose le numéro lui-même.

    Le piège qu'il porte a coûté une soirée le 26 août 2026 : un appel entrant
    venu de l'étranger arrive avec une dizaine de paramètres vides, la ville et
    la région de l'appelant étant inconnues. Twilio les compte dans sa
    signature. Le serveur les jetait. Résultat : jeton valide, sortants qui
    passent, entrants tous refusés en 403.
    """

    JETON = "jeton_de_test_1234567890"

    # Ce qu'un vrai appel entrant transporte, vides compris. Les champs
    # géographiques de l'appelant sont vides parce que l'appel vient du
    # Cameroun vers un numéro américain.
    ENTRANT = {
        "AccountSid": "AC00000000000000000000000000000000",
        "CallSid": "CA11111111111111111111111111111111",
        "CallStatus": "ringing", "Direction": "inbound", "ApiVersion": "2010-04-01",
        "From": "+237658841523", "To": "+16193041285",
        "Caller": "+237658841523", "Called": "+16193041285",
        "FromCity": "", "FromState": "", "FromZip": "", "FromCountry": "CM",
        "CallerCity": "", "CallerState": "", "CallerZip": "", "CallerCountry": "CM",
        "ToCity": "SAN DIEGO", "ToState": "CA", "ToZip": "", "ToCountry": "US",
        "CalledCity": "SAN DIEGO", "CalledState": "CA", "CalledZip": "",
        "CalledCountry": "US", "StirVerstat": "",
    }

    def _serveur(self):
        import importlib.util, os, threading
        from http.server import ThreadingHTTPServer
        spec = importlib.util.spec_from_file_location("srv_in", ROOT / "web" / "server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        srv.APP = srv.App(str(Path(tempfile.mkdtemp()) / "t.db"))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        port = httpd.server_address[1]
        os.environ["TWILIO_AUTH_TOKEN"] = self.JETON
        os.environ["NDARA_PUBLIC_URL"] = f"http://127.0.0.1:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return srv, port

    def _poster(self, port, chemin, form):
        import base64, hashlib, hmac, urllib.parse, urllib.request, urllib.error
        url = f"http://127.0.0.1:{port}{chemin}"
        # La signature de Twilio, à la lettre : l'URL complète puis chaque
        # paramètre par ordre alphabétique, vides compris.
        brut = url + "".join(f"{k}{form[k]}" for k in sorted(form))
        sig = base64.b64encode(
            hmac.new(self.JETON.encode(), brut.encode(), hashlib.sha1).digest()).decode()
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Twilio-Signature": sig})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_un_entrant_avec_des_parametres_vides_est_accepte(self):
        """Le test qui aurait épargné la soirée du 26 août."""
        srv, port = self._serveur()
        code, xml = self._poster(port, "/twiml/start?essai=1&lang=fr", self.ENTRANT)
        self.assertEqual(code, 200, f"refusé : {xml[:120]}")
        self.assertIn("<Response>", xml)
        self.assertIn("announce", xml)      # l'annonce d'IA, avant toute question

    def test_une_signature_forgee_reste_refusee(self):
        """La tolérance aux vides ne doit pas ouvrir la porte."""
        srv, port = self._serveur()
        import urllib.parse, urllib.request, urllib.error
        url = f"http://127.0.0.1:{port}/twiml/start"
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(self.ENTRANT).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Twilio-Signature": "AAAAbbbbCCCCddddEEEEffffGGGG="})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 403)

    def test_en_entrant_le_repondant_est_l_appelant_pas_notre_numero(self):
        """« To » est notre numéro quand c'est l'autre qui compose.

        Le prendre pour le répondant rangerait tous les entretiens entrants
        sous un seul et même numéro, le nôtre.
        """
        srv, port = self._serveur()
        code, _ = self._poster(port, "/twiml/start?essai=1&lang=fr", self.ENTRANT)
        self.assertEqual(code, 200)
        from ndara.models import hash_msisdn
        ivs = srv.APP.store.interviews()
        self.assertEqual(len(ivs), 1)
        # Le numéro est haché, donc y chercher des chiffres ne prouverait rien.
        # On compare au haché du bon numéro, et à celui du mauvais.
        self.assertEqual(ivs[0].respondent_hash, hash_msisdn(self.ENTRANT["From"]))
        self.assertNotEqual(ivs[0].respondent_hash, hash_msisdn(self.ENTRANT["To"]))


class TestClavierSurQuestionNumerique(unittest.TestCase):
    """La relance promet les touches : elles doivent exister.

    Trouvé pendant le premier vrai appel entrant, le 26 août 2026. Une question
    numérique n'a pas de modalités, donc aucune touche ne lui est attribuée
    d'avance, et l'écoute n'acceptait que la parole. Mais la relance de dernier
    recours dit « utilisez les touches de votre téléphone », dans la voix de
    studio. On promettait un filet qui n'existait pas, et précisément sur les
    questions où la transcription échoue le plus : celles où il faut dire un
    montant.
    """

    def _twiml(self, input_type, options):
        from ndara.providers.telephony import prompt_to_twiml
        return prompt_to_twiml(
            {"kind": "question", "step_id": "s", "text": "Combien ?",
             "options": options, "allow_voice": True,
             "allow_dtmf": bool(options), "input_type": input_type, "done": False},
            action_url="https://exemple/step")

    def test_une_question_numerique_accepte_les_touches(self):
        x = self._twiml("number", [])
        self.assertIn('input="dtmf speech"', x)
        # Pas de longueur imposée : « 1500 » ne doit pas être coupé à « 1 ».
        self.assertNotIn("numDigits", x)
        self.assertIn('finishOnKey="#"', x)

    def test_une_question_a_modalites_garde_une_seule_touche(self):
        x = self._twiml("choice", [{"code": "a", "dtmf": "1", "label": "A"}])
        self.assertIn('input="dtmf speech"', x)
        self.assertIn('numDigits="1"', x)

    def test_une_question_libre_n_ecoute_que_la_parole(self):
        """Une réponse ouverte ne se tape pas : lui offrir le clavier n'a pas de sens."""
        x = self._twiml("open", [])
        self.assertIn('input="speech"', x)
        self.assertNotIn('input="dtmf speech"', x)


class TestComprehensionAuTelephone(unittest.TestCase):
    """Ce que la reconnaissance reçoit, et ce qu'on fait de ce qu'elle rend.

    Écrit le 27 août 2026, après le premier vrai appel entrant. Un « oui » en
    français camerounais était revenu transcrit « puis-je », confiance 0,0, et
    l'entretien s'était arrêté là.
    """

    def _twiml(self, prompt):
        from ndara.providers.telephony import prompt_to_twiml
        base = {"kind": "question", "text": "T", "allow_voice": True,
                "options": [], "done": False}
        base.update(prompt)
        base.setdefault("allow_dtmf", bool(base.get("options")))
        return prompt_to_twiml(base, action_url="https://x/step")

    def _indices(self, xml):
        import re
        m = re.search(r'hints="([^"]*)"', xml)
        return m.group(1).split(",") if m else []

    def test_le_consentement_annonce_les_facons_naturelles_de_dire_oui(self):
        """Personne ne répond « Oui » tout court à une question de consentement.

        Le codeur sait depuis toujours lire « d'accord » ou « bien sûr ». La
        reconnaissance, elle, n'en savait rien : elle ne recevait que
        « 1, Oui, 2, Non ». Les deux moitiés du problème vivaient dans deux
        fichiers, et c'est pour ça que le trou ne se voyait pas.
        """
        xml = self._twiml({"kind": "consent", "input_type": "choice", "options": [
            {"code": "yes", "dtmf": "1", "label": "Oui"},
            {"code": "no", "dtmf": "2", "label": "Non"}]})
        indices = self._indices(xml)
        for attendu in ("d accord", "bien sur", "ouais", "tout a fait"):
            self.assertIn(attendu, indices)
        self.assertIn("Oui", indices)      # le libellé reste, il ne disparaît pas
        self.assertIn("1", indices)        # la touche aussi

    def test_une_question_numerique_annonce_les_nombres_et_son_unite(self):
        """C'est là que la reconnaissance travaille le plus, et elle n'avait rien."""
        xml = self._twiml({"input_type": "number", "unit": "FCFA"})
        indices = self._indices(xml)
        for attendu in ("cinq", "mille", "cent", "FCFA"):
            self.assertIn(attendu, indices)

    def test_les_indices_ne_depassent_pas_la_limite_de_twilio(self):
        """500 entrées de 100 caractères, et pas une de plus."""
        xml = self._twiml({"input_type": "number", "unit": "FCFA"})
        indices = self._indices(xml)
        self.assertLessEqual(len(indices), 500)
        for i in indices:
            self.assertLessEqual(len(i), 100)

    def test_une_confiance_de_zero_est_une_absence_pas_une_mesure(self):
        """Sinon tout entretien téléphonique réel serait déclaré dégradé.

        Les modèles téléphoniques de Twilio renvoient 0.0 même sur une
        transcription parfaite. Pris pour une mesure, ce zéro tombe sous le
        seuil de 0,55 de l'audit et invente un défaut de qualité.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("srv_conf", ROOT / "web" / "server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        self.assertIsNone(srv._confiance_twilio("0.0"))
        self.assertIsNone(srv._confiance_twilio(""))
        self.assertIsNone(srv._confiance_twilio(None))
        self.assertIsNone(srv._confiance_twilio("bavardage"))
        # Une vraie mesure, elle, doit passer intacte.
        self.assertAlmostEqual(srv._confiance_twilio("0.87"), 0.87)

    def test_l_audit_compte_les_tours_sans_confiance_au_lieu_de_les_taire(self):
        """Une mesure manquante se publie, elle ne se remplace pas."""
        from ndara.audit import audit_interview
        from ndara.models import Interview, Turn
        q = Questionnaire.load(QPATH)
        etapes = [st.id for st in q.steps][:3]
        iv = Interview(id="iv1", questionnaire_id=q.id, language="fr", channel="phone",
                       respondent_hash="h", stratum="MTN")
        tours = [Turn(interview_id="iv1", seq=i, step_id=sid, code="a",
                      method="voice", asr_confidence=None, relances=0,
                      duration_ms=4000)
                 for i, sid in enumerate(etapes)]
        a = audit_interview(q, iv, tours)
        self.assertEqual(a.details.get("asr_confiance_non_fournie"), len(etapes))
        # Aucune confiance mesurée ne doit produire un défaut de transcription.
        self.assertNotIn("transcription_faible", a.flags)
        self.assertNotIn("asr_confidence_mean", a.details)


# --------------------------------------------------------------------------
# Le tour de parole
#
# Écrit le 29 août 2026, après le deuxième appel réel. L'agent avait compris et
# codé correctement, et l'appel était pourtant invivable : « il faut absolument
# attendre jusqu'à la fin pour qu'il écoute », « il y a des choses où je dois
# insister pour qu'il écoute ».
#
# La cause n'était pas dans le code mais dans ce que le code ne disait pas.
# Twilio n'ouvre le décompte qu'après exécution des verbes imbriqués : le
# répondant DOIT attendre la fin de la phrase, sur toutes les invites. Rien à
# l'écran, rien dans l'oreille ne le lui disait. Un répondant réel ne le
# devinera pas : il parlera pendant le texte, ne sera pas entendu, subira une
# relance qui reprend tout, et raccrochera. C'est le taux de réponse, donc
# toute la crédibilité statistique du produit, qui se joue sur ce signal.
# --------------------------------------------------------------------------

class TestTourDeParole(unittest.TestCase):

    BASE = "https://ndara.test"

    def _twiml(self, prompt, **kw):
        from ndara.providers.telephony import prompt_to_twiml
        base = {"kind": "question", "step_id": "s", "text": "T",
                "audio_url": "/audio/q/fr/s.mp3", "allow_voice": True,
                "options": [], "done": False}
        base.update(prompt)
        base.setdefault("allow_dtmf", bool(base.get("options")))
        kw.setdefault("audio_base", self.BASE)
        return prompt_to_twiml(base, action_url="https://x/step", **kw)

    OUI_NON = [{"code": "yes", "dtmf": "1", "label": "Oui"},
               {"code": "no", "dtmf": "2", "label": "Non"}]

    def test_toute_invite_qui_attend_une_reponse_porte_le_signal(self):
        """Un signal posé à un seul endroit n'enseigne aucune règle.

        Le bip existait déjà dans le code, mais sur la seule branche `Record`,
        que la quasi-totalité des appels ne traverse jamais. Le répondant
        l'entendait une fois et ne pouvait rien en déduire.
        """
        invites = [
            {"kind": "consent", "input_type": "consent", "options": self.OUI_NON},
            {"kind": "question", "input_type": "choice", "options": self.OUI_NON},
            {"kind": "question", "input_type": "number", "unit": "FCFA"},
            {"kind": "question", "input_type": "open"},
        ]
        for invite in invites:
            with self.subTest(invite=invite.get("input_type")):
                self.assertIn("bip.wav", self._twiml(invite))

    def test_sur_une_question_le_signal_est_le_dernier_verbe_imbrique(self):
        """C'est ce qui le fait tomber pile à l'ouverture de l'écoute.

        Twilio : « Before Twilio begins the timeout period, it waits until all
        nested verbs have executed. » Le bip imbriqué en dernier retarde donc
        le décompte jusqu'à sa propre fin. Placé ailleurs, il annoncerait un
        tour de parole qui n'est pas encore ouvert.
        """
        xml = self._twiml({"kind": "question", "input_type": "choice",
                           "options": self.OUI_NON})
        self.assertIn("</Gather>", xml)
        self.assertLess(xml.index("<Gather"), xml.index("/s.mp3"))
        self.assertLess(xml.index("/s.mp3"), xml.index("bip.wav"))
        self.assertLess(xml.index("bip.wav"), xml.index("</Gather>"))

    def test_sur_un_consentement_le_signal_reste_hors_de_l_ecoute(self):
        """On ne consent pas à ce qu'on n'a pas fini d'entendre.

        Le bip ne doit pas devenir la porte dérobée par laquelle un « oui »
        arraché à la moitié d'une phrase entrerait quand même : il est joué
        après l'énoncé et avant que le `Gather` s'ouvre, comme l'énoncé
        lui-même. Le consentement reste non interruptible, y compris au
        clavier.
        """
        xml = self._twiml({"kind": "consent", "input_type": "consent",
                           "options": self.OUI_NON})
        self.assertNotIn("</Gather>", xml)
        self.assertLess(xml.index("/s.mp3"), xml.index("bip.wav"))
        self.assertLess(xml.index("bip.wav"), xml.index("<Gather"))

    def test_une_annonce_qui_n_attend_rien_ne_porte_aucun_signal(self):
        """Annoncer un tour de parole qui n'existe pas ferait parler dans le
        vide, puis attendre une réponse que personne n'écoute."""
        xml = self._twiml({"kind": "announce", "input_type": "none",
                           "allow_voice": False, "allow_dtmf": False})
        self.assertNotIn("bip.wav", xml)
        self.assertNotIn("<Gather", xml)

    def test_le_signal_est_un_son_et_jamais_une_voix(self):
        """Une invite parlée par la synthèse du canal, au milieu d'une voix de
        studio, s'entend : la machine se dénonce au moment précis où le
        répondant hésite. Et un son n'a pas de langue à traduire."""
        for langue in ("fr", "en", "km"):
            with self.subTest(langue=langue):
                xml = self._twiml({"kind": "question", "input_type": "choice",
                                   "options": self.OUI_NON}, langue=langue)
                self.assertNotIn("<Say", xml)
                # Le même fichier partout : c'est ce qui en fait une convention.
                self.assertIn(f"{self.BASE}/audio/_commun/bip.wav", xml)

    def test_le_meme_signal_avant_un_enregistrement_et_pas_celui_de_twilio(self):
        """Deux sons pour un même sens dans un même appel n'enseignent rien."""
        xml = self._twiml({"kind": "question", "input_type": "open"},
                          corpus_consenti=True, transcription=True)
        self.assertIn("<Record", xml)
        self.assertIn('playBeep="false"', xml)
        self.assertLess(xml.index("bip.wav"), xml.index("<Record"))

    def test_sans_adresse_publique_le_bip_de_twilio_reste_en_secours(self):
        """Faute de pouvoir servir le nôtre, mieux vaut le sien que rien :
        ouvrir un enregistrement sans prévenir personne est pire."""
        xml = self._twiml({"kind": "question", "input_type": "open"},
                          audio_base=None, corpus_consenti=True, transcription=True)
        self.assertIn('playBeep="true"', xml)

    def test_le_silence_de_fin_de_parole_n_est_jamais_auto(self):
        """La référence de Twilio l'interdit avec un `speechModel`, mot pour
        mot : « This attribute requires you to set speechTimeout to a positive
        integer value. Don't use auto. » Nous envoyions les deux ensemble.

        Un réglage refusé par la plateforme ne se plaint pas, il dégrade — et
        c'est un candidat sérieux à « je dois insister pour qu'il écoute ».
        """
        import re
        for invite in ({"input_type": "choice", "options": self.OUI_NON},
                       {"input_type": "number", "unit": "FCFA"},
                       {"input_type": "open"}):
            with self.subTest(invite=invite.get("input_type")):
                xml = self._twiml(invite)
                if "speechModel" not in xml:
                    continue
                valeurs = re.findall(r'speechTimeout="([^"]*)"', xml)
                self.assertTrue(valeurs)
                for v in valeurs:
                    self.assertTrue(v.isdigit() and int(v) > 0,
                                    f"speechTimeout={v!r} avec un speechModel")

    def test_un_montant_dit_a_voix_haute_a_droit_a_une_pause_interne(self):
        """« mille… cinq cents » ne doit pas être clos après « mille »."""
        import re
        court = re.search(r'speechTimeout="(\d+)"',
                          self._twiml({"input_type": "choice",
                                       "options": self.OUI_NON})).group(1)
        long = re.search(r'speechTimeout="(\d+)"',
                         self._twiml({"input_type": "number",
                                      "unit": "FCFA"})).group(1)
        self.assertGreater(int(long), int(court))


class TestFichierDuSignal(unittest.TestCase):
    """Le son lui-même. Régénérable par `python scripts/build_bip.py`."""

    CHEMIN = ROOT / "data" / "audio" / "_commun" / "bip.wav"

    def _lire(self):
        import struct
        import wave
        with wave.open(str(self.CHEMIN), "rb") as f:
            params = f.getparams()
            brut = f.readframes(f.getnframes())
        return params, list(struct.unpack(f"<{len(brut) // 2}h", brut))

    def test_le_fichier_existe(self):
        self.assertTrue(self.CHEMIN.is_file(),
                        "sans ce fichier, chaque invite ouvre l'écoute en silence")

    def test_il_est_au_format_de_la_ligne_et_non_en_mp3(self):
        """Twilio, sur un `Play` imbriqué : « Use a .wav file instead, as
        transcoding .mp3 files can add delay. » Ce délai tomberait exactement
        là où l'appel s'entend comme une machine."""
        params, _ = self._lire()
        self.assertEqual(self.CHEMIN.suffix, ".wav")
        self.assertEqual(params.nchannels, 1)
        self.assertEqual(params.sampwidth, 2)
        self.assertEqual(params.framerate, 8000)

    def test_il_commence_par_un_silence_qui_le_detache_de_la_phrase(self):
        """Sans ce blanc, le bip s'entend comme la dernière syllabe de la
        question au lieu d'un événement séparé. Il est gravé dans le fichier :
        `Pause` ne compte qu'en secondes entières, et une seconde par question
        se facture."""
        params, ech = self._lire()
        silence = next((i for i, v in enumerate(ech) if abs(v) > 100), len(ech))
        self.assertGreaterEqual(silence / params.framerate, 0.25)

    def test_il_est_court_car_il_se_paie_a_chaque_question(self):
        params, ech = self._lire()
        self.assertLess(len(ech) / params.framerate, 1.0)

    def test_une_seule_tonalite_jamais_trois(self):
        """En Afrique centrale et de l'Ouest, la triple tonalité montante est
        le signal d'échec réseau des opérateurs : elle serait comprise comme
        « l'appel a coupé », exactement l'inverse de « c'est à vous »."""
        params, ech = self._lire()
        seuil = 100
        creux = int(0.05 * params.framerate)   # 50 ms de blanc = deux sons
        blocs, dans, vide = 0, False, 0
        for v in ech:
            if abs(v) > seuil:
                if not dans:
                    blocs += 1
                dans, vide = True, 0
            elif dans:
                vide += 1
                if vide >= creux:
                    dans = False
        self.assertEqual(blocs, 1, "le signal doit être une tonalité unique")

    def test_il_tient_dans_la_bande_que_la_ligne_transporte(self):
        """Hors de 300–3400 Hz, un codec 2G le jette purement et simplement."""
        from scripts.build_bip import FREQUENCE
        self.assertGreater(FREQUENCE, 300)
        self.assertLess(FREQUENCE, 3400)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# --------------------------------------------------------------------------
# Le diagnostic de téléphonie
#
# Le code 20003 de Twilio recouvre trois causes qui ne se corrigent pas au même
# endroit : jeton faux, solde épuisé, numéro appelant inutilisable. Le tableau
# de bord n'en annonçait qu'une, et a envoyé refaire deux fois un jeton qui
# allait bien. Ces tests verrouillent le fait qu'il les distingue désormais.
# --------------------------------------------------------------------------
BRANCHE_CLASSE = {"incoming_phone_numbers":
                  [{"voice_url": "https://exemple.test/twiml/start"}]}


class TestDiagnosticTelephonie(unittest.TestCase):

    def _srv(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("srv_diag", ROOT / "web" / "server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        return srv

    def _adaptateur(self, reponses: dict):
        """Un Twilio de papier : chaque URL rend ce qu'on lui a dit de rendre."""
        from ndara.providers.telephony import TwilioTelephony
        tel = TwilioTelephony(sid="AC" + "0" * 32, token="t" * 32,
                              from_number="+17372508034",
                              webhook_base="https://exemple.test")

        def lire(url: str):
            for motif, valeur in reponses.items():
                if motif in url:
                    return valeur if isinstance(valeur, tuple) else (valeur, "")
            return None, "non prévu par le test"

        tel._lire = lire                                   # type: ignore[method-assign]
        return tel

    COMPTE = {"friendly_name": "Compte", "status": "active", "type": "Full"}

    def test_la_phrase_de_twilio_n_est_pas_jetee(self):
        """Notre traduction devine la cause, la phrase de Twilio la nomme.

        La version précédente promettait « sans masquer l'originale » dans sa
        docstring et ne rendait que sa propre traduction. Quand les deux se
        contredisent, il faut pouvoir le voir.
        """
        srv = self._srv()
        dit = srv._twilio_lisible("HTTP 400 · 20003 · Authentication Error")
        self.assertIn("Authentication Error", dit)
        self.assertIn("20003", dit)

    def test_le_code_est_lu_dans_son_champ_et_pas_dans_le_texte(self):
        """Un numéro appelé peut contenir les cinq chiffres d'un code d'erreur."""
        srv = self._srv()
        self.assertEqual(srv._code_twilio("HTTP 400 · 21215 · calling +23721219000"),
                         "21215")

    def test_20003_ne_designe_plus_le_jeton_comme_seule_cause(self):
        srv = self._srv()
        dit = srv._twilio_lisible("HTTP 401 · 20003 · Authenticate")
        self.assertIn("solde", dit.lower())

    def test_un_solde_epuise_est_nomme(self):
        """Identifiants acceptés en lecture, appel refusé : c'est le solde."""
        tel = self._adaptateur({".json?": ({}, ""),
                                "/Balance.json": {"balance": "0", "currency": "USD"},
                                "IncomingPhoneNumbers": BRANCHE_CLASSE,
                                ".json": self.COMPTE})
        res = tel.verifier()
        self.assertTrue(res["ok"])
        self.assertTrue(any("solde" in e.lower() for e in res["ennuis"]))

    def test_un_numero_appelant_inconnu_du_compte_est_nomme(self):
        tel = self._adaptateur({"/Balance.json": {"balance": "20.00", "currency": "USD"},
                                "IncomingPhoneNumbers": {"incoming_phone_numbers": []},
                                "OutgoingCallerIds": {"outgoing_caller_ids": []},
                                ".json": self.COMPTE})
        res = tel.verifier()
        self.assertEqual(res["numero_source"], "inconnu du compte")
        self.assertTrue(any("+17372508034" in e for e in res["ennuis"]))

    BRANCHE = {"incoming_phone_numbers":
               [{"voice_url": "https://exemple.test/twiml/start"}]}

    def test_un_compte_sain_ne_signale_rien(self):
        tel = self._adaptateur({"/Balance.json": {"balance": "20.00", "currency": "USD"},
                                "IncomingPhoneNumbers": self.BRANCHE,
                                ".json": self.COMPTE})
        res = tel.verifier()
        self.assertEqual(res["ennuis"], [])
        self.assertTrue(res["entrant"])
        self.assertEqual(res["numero_source"], "acheté sur le compte")
        self.assertFalse(res["essai"])

    def test_une_sous_lecture_qui_echoue_ne_fait_pas_tomber_l_audit(self):
        """Un diagnostic partiel vaut mieux qu'une page blanche."""
        tel = self._adaptateur({"/Balance.json": (None, "passerelle en panne"),
                                "IncomingPhoneNumbers": BRANCHE_CLASSE,
                                ".json": self.COMPTE})
        res = tel.verifier()
        self.assertTrue(res["ok"])
        self.assertTrue(any("panne" in e for e in res["ennuis"]))

    def test_un_numero_achete_mais_non_branche_est_nomme(self):
        """Un numero achete ne repond a rien tant que sa route vocale est vide.

        C'est la demonstration « composez ce numero devant le jury » qui en
        depend, et rien ne le disait : le numero apparaissait comme acquis,
        donc comme pret.
        """
        tel = self._adaptateur({"/Balance.json": {"balance": "20.00", "currency": "USD"},
                                "IncomingPhoneNumbers": {"incoming_phone_numbers": [{}]},
                                ".json": self.COMPTE})
        res = tel.verifier()
        self.assertFalse(res["entrant"])
        self.assertTrue(any("entrants" in e for e in res["ennuis"]))

    def test_un_pays_ferme_au_sortant_est_nomme_avant_de_composer(self):
        """Twilio bloque certaines destinations, et le blocage ne se voit qu'en payant.

        L'appel part, echoue, et il est facture. Le lire dans le diagnostic ne
        coute rien.
        """
        tel = self._adaptateur({
            "/Balance.json": {"balance": "20.00", "currency": "USD"},
            "IncomingPhoneNumbers": BRANCHE_CLASSE,
            "DialingPermissions/Countries/CM": {
                "name": "Cameroon", "country_codes": ["237"],
                "low_risk_numbers_enabled": False},
            "DialingPermissions/Countries/KH": {
                "name": "Cambodia", "country_codes": ["855"],
                "low_risk_numbers_enabled": True},
            ".json": self.COMPTE})
        res = tel.verifier(["CM", "KH"])
        self.assertFalse(res["pays"]["CM"]["sortant"])
        self.assertTrue(res["pays"]["KH"]["sortant"])
        self.assertTrue(any("Cameroon" in e and "entrant" in e.lower()
                            for e in res["ennuis"]),
                        "le refus doit nommer le pays ET rappeler que l'entrant reste ouvert")

    def test_un_pays_ouvert_mais_ferme_aux_plages_signalees_est_nomme(self):
        """Le cas reel du Cameroun, et celui que le diagnostic ratait.

        Twilio classe des PLAGES, pas des pays. Ne lire que « low risk »
        repondait « oui » sur un pays ou l'appel echoue, parce que les mobiles
        camerounais tombent dans les plages signalees pour fraude aux revenus
        d'interconnexion, et que ce sont exactement les numeros qu'une enquete
        compose. Un instrument qui rassure a tort est pire qu'un instrument
        muet.
        """
        tel = self._adaptateur({
            "/Balance.json": {"balance": "20.00", "currency": "USD"},
            "IncomingPhoneNumbers": BRANCHE_CLASSE,
            "DialingPermissions/Countries/CM": {
                "name": "Cameroon", "country_codes": ["237"],
                "low_risk_numbers_enabled": True,
                "high_risk_tollfraud_numbers_enabled": False,
                "high_risk_special_numbers_enabled": False},
            ".json": self.COMPTE})
        res = tel.verifier(["CM"])
        self.assertTrue(res["pays"]["CM"]["ordinaires"])
        self.assertFalse(res["pays"]["CM"]["plages_signalees"])
        ennui = " ".join(res["ennuis"])
        self.assertIn("plages signalées", ennui)
        self.assertIn("ENTRANT", ennui,
                      "il faut dire ce qui reste possible, pas seulement ce qui est ferme")

    def test_un_numero_dans_un_prefixe_a_haut_risque_est_nomme(self):
        """Le cas reel : un pays ouvert, et pourtant l'appel refuse en 21216.

        Twilio ne bloque pas seulement par pays et par categorie, il publie
        aussi une liste de PREFIXES a haut risque. Le diagnostic annoncait
        « Cameroun : ouvert, plages signalees comprises » et l'appel echouait
        sur « Account not allowed to call ». Un instrument qui rassure a tort
        fait chercher ailleurs, ce qui coute plus cher que de ne rien dire.
        """
        tel = self._adaptateur({
            "/Balance.json": {"balance": "20.00", "currency": "USD"},
            "IncomingPhoneNumbers": BRANCHE_CLASSE,
            "HighRiskSpecialPrefixes": {"content": [{"prefix": "+23765"},
                                                    {"prefix": "+23767"}]},
            "DialingPermissions/Countries/CM": {
                "name": "Cameroon", "country_codes": ["237"],
                "low_risk_numbers_enabled": True,
                "high_risk_tollfraud_numbers_enabled": True,
                "high_risk_special_numbers_enabled": False},
            ".json": self.COMPTE})
        res = tel.verifier(["CM"], "+237658841523")
        cm = res["pays"]["CM"]
        self.assertTrue(cm["ordinaires"])
        self.assertTrue(cm["plages_signalees"])
        self.assertTrue(cm["numero_dans_prefixe_special"],
                        "+23765... doit tomber dans le prefixe +23765")
        ennui = " ".join(res["ennuis"])
        self.assertIn("+23765", ennui)
        self.assertIn("special", ennui.lower())

    def test_un_numero_hors_prefixe_ne_declenche_aucun_ennui(self):
        """On ne signale pas un blocage qui n'existe pas."""
        tel = self._adaptateur({
            "/Balance.json": {"balance": "20.00", "currency": "USD"},
            "IncomingPhoneNumbers": BRANCHE_CLASSE,
            "HighRiskSpecialPrefixes": {"content": [{"prefix": "+23767"}]},
            "DialingPermissions/Countries/CM": {
                "name": "Cameroon", "country_codes": ["237"],
                "low_risk_numbers_enabled": True,
                "high_risk_tollfraud_numbers_enabled": True,
                "high_risk_special_numbers_enabled": False},
            ".json": self.COMPTE})
        res = tel.verifier(["CM"], "+237658841523")
        self.assertFalse(res["pays"]["CM"]["numero_dans_prefixe_special"])
        self.assertEqual(res["ennuis"], [])

    def test_le_journal_retient_le_dernier_appel_refuse(self):
        """Le diagnostic ne doit pas contredire ce qu'il a lui-meme enregistre.

        Il a annonce trois fois « rien ne s'oppose a un appel » alors qu'un
        appel venait d'etre refuse. Tous les droits peuvent etre ouverts et
        l'operateur refuser quand meme : restriction de compte, revue
        anti-fraude, ou permission qui n'a pas encore atteint le service
        vocal. Aucune ne se lit dans les permissions, toutes se lisent dans
        le journal.
        """
        store, _q, _m, _tmp = fresh_engine()
        self.assertIsNone(store.dernier_refus_appel())
        store.log("telephony_appel_essai", None, ok=True, call_sid="CA1")
        self.assertIsNone(store.dernier_refus_appel(),
                          "un appel accepte n'est pas un refus")
        store.log("telephony_appel_essai", None, ok=False,
                  erreur="HTTP 400 . 21216 . Account not allowed to call +237658841523")
        refus = store.dernier_refus_appel()
        self.assertIsNotNone(refus)
        self.assertIn("21216", refus["erreur"])
        self.assertTrue(refus["quand"])

    def test_des_identifiants_refuses_restent_concluants(self):
        """La fiche du compte est la seule lecture dont l'échec tranche."""
        tel = self._adaptateur({".json": (None, "HTTP 401 · 20003 · Authenticate")})
        res = tel.verifier()
        self.assertFalse(res["ok"])
        self.assertIn("20003", res["raison"])


# --------------------------------------------------------------------------
# Plusieurs entretiens en meme temps
#
# Le serveur est un ThreadingHTTPServer : un fil par requete. La base etait
# ouverte une seule fois et partagee par tous ces fils, avec
# check_same_thread=False, qui desactive le garde-fou sans rendre la connexion
# utilisable a plusieurs pour autant.
#
# Ce n'etait pas un melange de donnees, c'etait un plantage. Mesure sur douze
# entretiens simultanes menes par HTTP : deux aboutissaient, dix mouraient sur
# « sqlite3.InterfaceError: bad parameter or other API misuse » ou sur
# « SystemError: error return without exception set » leve par conn.commit().
# La campagne d'appels reels accepte jusqu'a dix appels simultanes : elle
# serait tombee des le deuxieme decrochage, en production, en depensant de
# l'argent.
# --------------------------------------------------------------------------
class TestEntretiensSimultanes(unittest.TestCase):

    def test_douze_entretiens_menes_en_meme_temps(self):
        """Aucun ne tombe, et aucun ne recupere la reponse d'un autre."""
        import threading

        store, q, _, tmp = fresh_engine()
        # Chaque fil a SON moteur, comme le serveur qui partage les siens, et
        # tous ecrivent dans la meme base : c'est la configuration reelle.
        moteurs = [InterviewEngine(store, q, RulesCoder(),
                                   CorpusWriter(store, Path(tmp) / "corpus"))
                   for _ in range(12)]
        ennuis: list[str] = []
        produits: dict[int, str] = {}
        verrou = threading.Lock()

        def mene(n: int) -> None:
            try:
                moteur = moteurs[n]
                p = moteur.start(language="fr", stratum="MTN", channel="phone")
                iid = p.interview_id
                for _ in range(30):
                    if p.done:
                        break
                    if p.kind == "consent":
                        p = moteur.submit(iid, dtmf="1")
                    elif p.options:
                        touches = [o for o in p.options if o.get("dtmf")]
                        p = (moteur.submit(iid, dtmf=touches[n % len(touches)]["dtmf"])
                             if touches else moteur.submit(iid, text="oui"))
                    elif p.kind == "announce":
                        p = moteur.submit(iid)
                    else:
                        # Le montant propre a ce fil. Tout melange le deplacerait.
                        p = moteur.submit(iid, text=str(1000 + n))
                with verrou:
                    produits[n] = iid
            except Exception as exc:                  # noqa: BLE001
                with verrou:
                    ennuis.append(f"fil {n} : {type(exc).__name__} {exc}")

        fils = [threading.Thread(target=mene, args=(i,)) for i in range(12)]
        for f in fils:
            f.start()
        for f in fils:
            f.join(timeout=60)

        self.assertEqual(ennuis, [], "des entretiens simultanes ont echoue")
        self.assertEqual(len(produits), 12)
        self.assertEqual(len(set(produits.values())), 12,
                         "deux fils ont partage le meme entretien")

        # Aucun entretien ne doit porter le montant d'un autre fil.
        famille = {float(1000 + k) for k in range(12)}
        for n, iid in produits.items():
            marques = {t.value_num for t in store.turns(iid)
                       if t.value_num is not None} & famille
            self.assertLessEqual(
                len(marques), 1,
                f"l'entretien du fil {n} porte plusieurs montants : {sorted(marques)}")
            if marques:
                self.assertEqual(marques, {float(1000 + n)},
                                 f"l'entretien du fil {n} porte le montant d'un autre")
