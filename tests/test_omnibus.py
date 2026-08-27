"""Tests de la vague omnibus.

    python -m unittest discover -s tests -v

Ce qui est testé ici est ce qui doit tenir devant un client et devant un
jury : qu'un créneau impossible soit refusé avant la vente et non découvert
dans les données, que la rotation soit réellement équilibrée, qu'aucun client
ne puisse voir les questions d'un autre, et qu'un client ne puisse pas
raccourcir l'annonce d'intelligence artificielle ni les consentements.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ndara import omnibus as om  # noqa: E402
from ndara.coding import RulesCoder  # noqa: E402
from ndara.corpus import CorpusWriter  # noqa: E402
from ndara.engine import InterviewEngine  # noqa: E402
from ndara.questionnaire import Option, Questionnaire, Step  # noqa: E402
from ndara.storage import Store  # noqa: E402

QPATH = ROOT / "data" / "questionnaires" / "prix_denrees_cm.json"


def source() -> Questionnaire:
    return Questionnaire.load(QPATH)


def question(sid: str, secondes: float = 8.0, langues=("fr", "en"),
             ask_if=None) -> Step:
    return Step(
        id=sid,
        type="yes_no",
        text={lang: f"Question {sid} en {lang} ?" for lang in langues},
        options=[Option(code="yes", dtmf="1", labels={l: ["oui"] for l in langues}),
                 Option(code="no", dtmf="2", labels={l: ["non"] for l in langues})],
        expected_seconds=secondes,
        ask_if=ask_if,
    )


# --------------------------------------------------------------------------

class TestCompositionDeLaVague(unittest.TestCase):
    def setUp(self):
        self.vague = om.vague_de_demonstration(source())

    def test_le_tronc_passe_en_premier(self):
        q = self.vague.composer(0)
        debut = [s.id for s in q.steps[:4]]
        self.assertEqual(debut, ["region", "sex", "age_group", "hh_size"])

    def test_toutes_les_questions_vendues_sont_posees(self):
        for rang in range(6):
            q = self.vague.composer(rang)
            poses = {s.id for s in q.steps}
            for creneau in self.vague.creneaux:
                self.assertTrue(creneau.step_ids() <= poses,
                                f"créneau {creneau.client} amputé au rang {rang}")

    def test_aucune_question_posee_deux_fois(self):
        for rang in range(6):
            ids = [s.id for s in self.vague.composer(rang).steps]
            self.assertEqual(len(ids), len(set(ids)))

    def test_un_client_ne_peut_pas_toucher_a_l_annonce_ni_aux_consentements(self):
        q = self.vague.composer(0)
        for cle in ("announce", "consent_survey", "consent_corpus"):
            self.assertIn(cle, q.prompts)
        # Et ces messages ne sont pas des étapes : aucun créneau ne peut donc
        # les remplacer en vendant une question du même identifiant.
        ids = {s.id for s in q.steps}
        self.assertFalse(ids & {"announce", "consent_survey", "consent_corpus"})

    def test_la_vague_composee_trouve_son_audio(self):
        """Une vague composée est muette si elle cherche l'audio sous son propre nom."""
        q = self.vague.composer(0)
        self.assertNotEqual(q.id, "prix_denrees_cm")
        self.assertEqual(q.audio_dir_id(), "prix_denrees_cm")
        chemin = ROOT / "data" / "audio" / q.audio_dir_id() / "fr"
        if chemin.exists():
            self.assertTrue((chemin / "region.mp3").exists())


class TestRotationDesCreneaux(unittest.TestCase):
    def setUp(self):
        self.vague = om.vague_de_demonstration(source())

    def test_chaque_creneau_occupe_chaque_position_une_fois(self):
        k = len(self.vague.creneaux)
        vues = {i: set() for i in range(k)}
        for rang in range(k):
            for position, idx in enumerate(self.vague.rotation(rang)):
                vues[idx].add(position)
        for idx, positions in vues.items():
            self.assertEqual(positions, set(range(k)),
                             f"le créneau {idx} n'occupe pas toutes les positions")

    def test_la_rotation_est_une_permutation(self):
        k = len(self.vague.creneaux)
        for rang in range(2 * k + 1):
            self.assertEqual(sorted(self.vague.rotation(rang)), list(range(k)))

    def test_la_position_est_enregistree_avec_l_entretien(self):
        marque = self.vague.marque_entretien(1)
        self.assertEqual(marque["vague"], self.vague.id)
        self.assertEqual(marque["rang"], 1)
        self.assertEqual(sorted(marque["positions"].values()), [0, 1, 2])

    def test_un_filtre_suit_toujours_la_question_qu_il_interroge(self):
        """La rotation déplace des blocs : l'ordre des filtres doit tenir partout."""
        for rang in range(8):
            q = self.vague.composer(rang)   # composer() lève si l'ordre est impossible
            rangs = {s.id: i for i, s in enumerate(q.steps)}
            for s in q.steps:
                if s.ask_if:
                    self.assertLess(rangs[s.ask_if["step"]], rangs[s.id])


class TestRefusDeVendre(unittest.TestCase):
    def setUp(self):
        self.vague = om.vague_de_demonstration(source())

    def test_un_creneau_trop_long_est_refuse_avant_la_vente(self):
        restant = self.vague.duree_restante_s()
        trop = om.Creneau(client="Client tardif", intitule="Trop long",
                          steps=[question("q_longue", secondes=restant + 5)])
        with self.assertRaises(om.CreneauRefuse) as ctx:
            self.vague.ajouter(trop)
        self.assertTrue(any("secondes" in r for r in ctx.exception.raisons))
        self.assertEqual(len(self.vague.creneaux), 3)

    def test_un_creneau_qui_tient_juste_est_accepte(self):
        restant = self.vague.duree_restante_s()
        pile = om.Creneau(client="Client pile", intitule="Tient juste",
                          steps=[question("q_courte", secondes=restant)])
        self.vague.ajouter(pile)
        self.assertEqual(len(self.vague.creneaux), 4)
        self.assertAlmostEqual(self.vague.duree_restante_s(), 0.0, places=6)

    def test_plus_de_trois_questions_est_refuse(self):
        gros = om.Creneau(client="Client gourmand", intitule="Quatre questions",
                          steps=[question(f"q{i}", secondes=1.0) for i in range(4)])
        with self.assertRaises(om.CreneauRefuse) as ctx:
            self.vague.ajouter(gros)
        self.assertTrue(any("maximum" in r for r in ctx.exception.raisons))

    def test_un_identifiant_deja_pris_est_refuse(self):
        doublon = om.Creneau(client="Client distrait", intitule="Doublon",
                             steps=[question("region", secondes=1.0)])
        with self.assertRaises(om.CreneauRefuse) as ctx:
            self.vague.ajouter(doublon)
        self.assertTrue(any("déjà pris" in r for r in ctx.exception.raisons))

    def test_un_filtre_qui_traverse_deux_creneaux_est_refuse(self):
        """Les blocs tournent : un filtre vers un autre créneau casserait un appel sur deux."""
        traversant = om.Creneau(
            client="Client curieux", intitule="Filtre vers un autre client",
            steps=[question("q_filtree", secondes=1.0,
                            ask_if={"step": "skipped_day", "equals": "yes"})])
        with self.assertRaises(om.CreneauRefuse) as ctx:
            self.vague.ajouter(traversant)
        self.assertTrue(any("créneau" in r for r in ctx.exception.raisons))

    def test_un_filtre_vers_le_tronc_est_accepte(self):
        vers_tronc = om.Creneau(
            client="Client correct", intitule="Filtre vers le tronc",
            steps=[question("q_ok", secondes=1.0,
                            ask_if={"step": "sex", "equals": "F"})])
        self.assertEqual(self.vague.verifier(vers_tronc), [])

    def test_une_question_non_traduite_est_refusee(self):
        muette = om.Creneau(client="Client pressé", intitule="Pas traduit",
                            steps=[question("q_fr", secondes=1.0, langues=("fr",))])
        raisons = self.vague.verifier(muette)
        self.assertTrue(any("traduite" in r for r in raisons))


class TestFacturation(unittest.TestCase):
    def setUp(self):
        self.vague = om.vague_de_demonstration(source())

    def test_les_couts_imputes_somment_au_cout_total(self):
        f = self.vague.facture(3000, om.TARIF_OPERATEUR)
        somme = sum(l["cout_impute_usd"] for l in f["lignes"])
        self.assertAlmostEqual(somme, f["cout_total_usd"], places=1)

    def test_la_recette_est_le_prix_par_question(self):
        f = self.vague.facture(1000)
        for ligne, creneau in zip(f["lignes"], self.vague.creneaux):
            self.assertAlmostEqual(
                ligne["recette_usd"], creneau.prix_question_usd * len(creneau.steps))

    def test_un_creneau_plus_long_paie_plus(self):
        f = self.vague.facture(1000)
        par_duree = sorted(f["lignes"], key=lambda l: l["duree_s"])
        self.assertLessEqual(par_duree[0]["cout_impute_usd"],
                             par_duree[-1]["cout_impute_usd"])

    def test_la_vague_ne_tient_pas_sans_accord_operateur(self):
        """Le fait central du modèle économique, vérifié par le code et pas seulement écrit."""
        twilio = self.vague.facture(3000, om.TARIF_TWILIO_CM)
        operateur = self.vague.facture(3000, om.TARIF_OPERATEUR)
        self.assertLess(twilio["marge_totale_usd"], 0)
        self.assertGreater(operateur["marge_totale_usd"], 0)

    def test_la_these_resiste_au_bas_de_la_fourchette_camerounaise(self):
        """Le tarif camerounais est une fourchette, pas un prix. La conclusion
        ne doit pas dépendre de l'endroit où l'on se place dedans.

        Retenir le haut de la fourchette serait suspect si la thèse ne tenait
        qu'à ce choix. On refait donc le calcul au tarif le plus favorable qui
        se soit présenté sur le compte : la vague perd encore de l'argent.
        """
        bas, haut = om.FOURCHETTE_TWILIO_CM
        self.assertEqual(haut, om.TARIF_TWILIO_CM.minute_usd)
        au_plus_bas = om.Tarif("Twilio, Cameroun au tarif bas",
                               minute_usd=bas,
                               fixe_usd=om.TARIF_TWILIO_CM.fixe_usd)
        self.assertLess(self.vague.facture(3000, au_plus_bas)["marge_totale_usd"], 0)

    def test_le_cambodge_coute_bien_moins_cher_que_le_cameroun(self):
        """Six fois moins cher la minute, et cela change le modèle, pas la marge.

        C'est l'argument à porter devant le ministère cambodgien : le même
        produit exige un accord opérateur au Cameroun et s'en passe presque
        au Cambodge.
        """
        cm = self.vague.facture(3000, om.TARIF_TWILIO_CM)
        kh = self.vague.facture(3000, om.TARIF_TWILIO_KH)
        self.assertLess(kh["cout_total_usd"], cm["cout_total_usd"] / 2)
        # Presque à l'équilibre, sans aucun accord : la perte cambodgienne est
        # au moins cinq fois plus petite que la perte camerounaise.
        self.assertLess(abs(kh["marge_totale_usd"]), abs(cm["marge_totale_usd"]) / 5)

    def test_la_question_de_plus_est_moins_chere_au_cambodge_qu_en_gros_au_cameroun(self):
        """Le fait le plus fort du dossier, et le moins intuitif.

        Une question de plus n'achète que des secondes de voix, donc son coût
        est le tarif à la minute et rien d'autre. Or la minute cambodgienne au
        détail (0,132 $) est moins chère que la minute camerounaise au tarif de
        gros qu'on espère négocier (0,152 $). Autrement dit : au Cambodge,
        l'économie marginale de l'omnibus est **déjà** meilleure aujourd'hui,
        sans accord, qu'elle ne le serait au Cameroun après une négociation
        réussie. C'est écrit dans le dossier, donc c'est tenu par un test.
        """
        self.assertLess(om.TARIF_TWILIO_KH.minute_usd, om.TARIF_OPERATEUR.minute_usd)
        kh = self.vague.cout_question_supplementaire(10.0, 3000, om.TARIF_TWILIO_KH)
        op = self.vague.cout_question_supplementaire(10.0, 3000, om.TARIF_OPERATEUR)
        self.assertLess(kh["cout_total_usd"], op["cout_total_usd"])

    def test_au_cambodge_la_vague_devient_rentable_a_800_dollars(self):
        """Le levier du prix, vérifié plutôt qu'annoncé.

        À 500 $ la question, la vague cambodgienne perd encore un peu. À 800 $,
        elle passe au vert sans accord opérateur, ce que le Cameroun ne fait
        dans aucun des deux cas.
        """
        for creneau in self.vague.creneaux:
            creneau.prix_question_usd = 800.0
        kh = self.vague.facture(3000, om.TARIF_TWILIO_KH)
        cm = self.vague.facture(3000, om.TARIF_TWILIO_CM)
        self.assertGreater(kh["marge_totale_usd"], 0)
        self.assertLess(cm["marge_totale_usd"], 0)

    def test_la_question_supplementaire_ne_paie_pas_la_part_fixe(self):
        """C'est toute la logique de l'omnibus : le fixe est déjà payé par la vague."""
        sup = self.vague.cout_question_supplementaire(10.0, 1000, om.TARIF_TWILIO_CM)
        appel = om.TARIF_TWILIO_CM.cout_appel(10.0)
        self.assertLess(sup["cout_par_entretien_usd"], appel)
        self.assertAlmostEqual(sup["cout_par_entretien_usd"],
                               10.0 / 60.0 * om.TARIF_TWILIO_CM.minute_usd, places=4)

    def test_la_question_supplementaire_est_refusee_si_l_appel_est_plein(self):
        sup = self.vague.cout_question_supplementaire(
            self.vague.duree_restante_s() + 1, 1000)
        self.assertFalse(sup["tient_dans_l_appel"])


class TestRestitutionCloisonnee(unittest.TestCase):
    """Un client ne voit jamais les questions d'un autre. C'est vendable ou ça ne l'est pas."""

    def setUp(self):
        self.vague = om.vague_de_demonstration(source())

    def test_les_indicateurs_d_un_client_ne_portent_que_sur_ses_questions(self):
        for creneau in self.vague.creneaux:
            siens = creneau.step_ids()
            autres = set()
            for c in self.vague.creneaux:
                if c.client != creneau.client:
                    autres |= c.step_ids()
            for ind in self.vague.indicateurs(creneau.client):
                self.assertIn(ind.var, siens)
                self.assertNotIn(ind.var, autres)

    def test_un_client_inconnu_n_obtient_rien(self):
        self.assertEqual(self.vague.indicateurs("Client qui n'a rien acheté"), [])

    def test_chaque_question_vendue_produit_au_moins_un_indicateur(self):
        for creneau in self.vague.creneaux:
            couverts = {i.var for i in self.vague.indicateurs(creneau.client)}
            self.assertEqual(couverts, creneau.step_ids())

    def test_la_propriete_de_chaque_question_est_connue(self):
        self.assertEqual(self.vague.proprietaire("region"), "__tronc__")
        self.assertEqual(self.vague.proprietaire("skipped_day"),
                         "Programme alimentaire mondial")
        self.assertIsNone(self.vague.proprietaire("question_inexistante"))


class TestVagueMeneeParLeMoteur(unittest.TestCase):
    """Une vague composée doit se mener comme n'importe quel questionnaire."""

    def test_un_entretien_complet_sur_une_vague(self):
        tmp = tempfile.mkdtemp()
        store = Store(Path(tmp) / "t.db")
        vague = om.vague_de_demonstration(source())
        q = vague.composer(1)
        moteur = InterviewEngine(store, q, RulesCoder(),
                                 CorpusWriter(store, Path(tmp) / "corpus"))

        p = moteur.start(language="fr", stratum="MTN")
        self.assertEqual(p.kind, "announce")
        iid = p.interview_id
        p = moteur.submit(iid)                      # accusé d'annonce
        self.assertEqual(p.step_id, "__consent_survey__")
        p = moteur.submit(iid, dtmf="1")            # consentement enquête
        self.assertEqual(p.step_id, "__consent_corpus__")
        p = moteur.submit(iid, dtmf="2")            # refus du corpus, sans conséquence

        vus = []
        garde = 0
        while not p.done and garde < 40:
            garde += 1
            vus.append(p.step_id)
            p = moteur.submit(iid, dtmf="1")

        self.assertTrue(p.done)
        iv = store.get_interview(iid)
        self.assertEqual(iv.disposition, "complete")
        # Le tronc a bien été posé en premier, et chaque client a eu ses questions.
        self.assertEqual(vus[0], "region")
        for creneau in vague.creneaux:
            self.assertTrue(creneau.step_ids() & set(vus),
                            f"aucune question posée pour {creneau.client}")

    def test_la_marque_de_vague_se_range_dans_l_entretien(self):
        tmp = tempfile.mkdtemp()
        store = Store(Path(tmp) / "t.db")
        vague = om.vague_de_demonstration(source())
        moteur = InterviewEngine(store, vague.composer(2), RulesCoder(),
                                 CorpusWriter(store, Path(tmp) / "corpus"))
        p = moteur.start(language="fr", stratum="MTN")
        iv = store.get_interview(p.interview_id)
        iv.meta.update(vague.marque_entretien(2))
        store.save_interview(iv)

        relu = store.get_interview(p.interview_id)
        self.assertEqual(relu.meta["vague"], vague.id)
        self.assertEqual(sorted(relu.meta["positions"].values()), [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
