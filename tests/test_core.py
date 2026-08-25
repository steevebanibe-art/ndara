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
        xml = prompt_to_twiml(prompt, action_url="https://x/step", corpus_consenti=True)
        self.assertIn("<Record", xml)

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

    def test_la_reconnaissance_est_reglee_pour_des_reponses_breves(self):
        from ndara.providers.telephony import prompt_to_twiml
        xml = prompt_to_twiml({"kind": "question", "text": "Le prix ?",
                               "allow_voice": True, "options": []},
                              action_url="https://x/step")
        self.assertIn('speechModel="googlev2_short"', xml)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
