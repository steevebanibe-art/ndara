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
