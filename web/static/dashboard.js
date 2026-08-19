/* Tableau de bord NDARA.
 * Rien n'est calculé ici : tout vient du serveur, donc de la même chaîne
 * statistique que celle qui produira les chiffres publiés.
 */

const el = (id) => document.getElementById(id);
const pct = (x) => (x == null ? "—" : (x * 100).toFixed(1) + " %");
const num = (x, d = 0) => (x == null ? "—" : Number(x).toLocaleString("fr-FR",
  { minimumFractionDigits: d, maximumFractionDigits: d }));

function tile(label, value, sub) {
  return `<div class="stat">
    <div class="label">${label}</div>
    <div class="value">${value}</div>
    ${sub ? `<div class="sub">${sub}</div>` : ""}
  </div>`;
}

const FIELD_LABELS = {
  complete: "Entretiens complets",
  partial: "Entretiens partiels",
  refusal: "Refus",
  noncontact: "Non-contacts",
  other: "Abandons en cours",
  ineligible: "Non éligibles",
  unknown: "Éligibilité inconnue",
};

const FLAG_LABELS = {
  duree_totale_trop_courte: "Durée totale anormalement courte",
  reponses_trop_rapides: "Réponses individuelles trop rapides",
  ligne_droite: "Réponses en ligne droite",
  taux_nsp_eleve: "Taux de « ne sait pas » élevé",
  relances_excessives: "Relances excessives",
  transcription_faible: "Transcription de faible qualité",
  valeurs_implausibles: "Valeurs implausibles",
  incoherence_interne: "Incohérence interne",
  repli_clavier_systematique: "Repli clavier systématique",
};

async function load() {
  const d = await (await fetch("/api/dashboard")).json();
  const q = await (await fetch("/api/quality")).json();

  // capacités
  const caps = d.capabilities || {};
  el("caps").innerHTML = [
    `<span class="cap ${caps.asr_live ? "live" : "off"}">transcription : ${caps.asr}</span>`,
    `<span class="cap ${caps.coder === "llm" ? "live" : "off"}">codage : ${caps.coder}</span>`,
    `<span class="cap ${caps.telephony ? "live" : "off"}">téléphonie : ${caps.telephony ? "branchée" : "non branchée"}</span>`,
  ].join("");

  const fw = d.fieldwork || { counts: {} };
  const w = d.weighting || {};

  el("tiles").innerHTML = [
    tile("Entretiens exploitables", num(d.n),
         `${num(fw.counts.complete)} complets · ${num(fw.counts.partial)} partiels`),
    tile("Taux de réponse (RR3)", pct(fw.response_rate_rr3),
         `RR2 : ${pct(fw.response_rate_rr2)} — méthode AAPOR`),
    tile("Taux de coopération", pct(fw.cooperation_rate),
         "parmi les personnes effectivement jointes"),
    tile("Effectif effectif", num(w.effective_n, 0),
         `effet de plan ${num(w.design_effect, 2)}`),
  ].join("");

  // estimations
  const tb = el("est").querySelector("tbody");
  const rows = (d.estimates || []).filter((r) => r.estimate != null);
  tb.innerHTML = rows.map((r) => `<tr>
      <td>${r.label}</td>
      <td class="num">${num(r.n)}</td>
      <td class="num"><b>${num(r.estimate, r.unit === "%" ? 1 : 0)}</b> ${r.unit === "%" ? "%" : (r.unit || "")}</td>
      <td class="ci">[${num(r.ci_low, r.unit === "%" ? 1 : 0)} ; ${num(r.ci_high, r.unit === "%" ? 1 : 0)}]</td>
      <td class="num">${num(r.se, 2)}</td>
    </tr>`).join("");
  el("est-empty").style.display = rows.length ? "none" : "";

  el("disclosure").innerHTML = (d.disclosure || [])
    .map((s) => `<li>${s}</li>`).join("");

  el("field").querySelector("tbody").innerHTML = Object.entries(fw.counts || {})
    .map(([k, v]) => `<tr><td>${FIELD_LABELS[k] || k}</td><td class="num">${num(v)}</td></tr>`)
    .join("");

  // audit
  const flagged = q.flagged_share ?? 0;
  el("quality-tiles").innerHTML = [
    tile("Score de qualité moyen", num(q.quality_score_mean, 1) + " / 100",
         `médiane ${num(q.quality_score_median, 1)}`),
    tile("Signalés pour revérification",
         `<span class="${flagged > 0.15 ? "pill bad" : "pill ok"}">${pct(flagged)}</span>`,
         `${num(q.flagged_for_review)} entretien(s) sur ${num(q.interviews_audited)}`),
    tile("Accord de codage",
         q.coding_agreement && q.coding_agreement.agreement != null
           ? pct(q.coding_agreement.agreement) : "non publiable",
         q.coding_agreement && q.coding_agreement.agreement != null
           ? `kappa ${num(q.coding_agreement.kappa, 2)} sur ${num(q.coding_agreement.n)} items recodés`
           : "aucun sous-échantillon recodé à la main"),
  ].join("");

  el("flags").querySelector("tbody").innerHTML =
    Object.entries(q.flag_counts || {})
      .map(([k, v]) => `<tr><td>${FLAG_LABELS[k] || k}</td><td class="num">${num(v)}</td></tr>`)
      .join("") || `<tr><td colspan="2" class="hint">Aucun signalement.</td></tr>`;

  el("items").querySelector("tbody").innerHTML = (q.items || []).map((i) => `<tr>
      <td>${i.step_id}</td>
      <td class="num">${num(i.n)}</td>
      <td class="num">${pct(i.item_nonresponse_rate)}</td>
      <td class="num">${num(i.mean_relances, 2)}</td>
      <td class="num">${pct(i.dtmf_fallback_rate)}</td>
      <td class="num">${i.mean_duration_s == null ? "—" : num(i.mean_duration_s, 1) + " s"}</td>
    </tr>`).join("");

  // corpus
  const c = d.corpus || {};
  el("corpus-tiles").innerHTML = [
    tile("Parole consentie", num(c.minutes, 2) + " min", `${num(c.segments)} segments`),
    tile("Locuteurs distincts", num(c.speakers), "tirés au sort, non volontaires"),
    tile("Expurgations appliquées", num(c.redactions), "identifiants retirés avant stockage"),
  ].join("");

  el("foot").textContent =
    `Questionnaire ${d.questionnaire ? d.questionnaire.id + " v" + d.questionnaire.version : "—"} · `
    + `calage sur marges ${w.raking && w.raking.converged ? "convergent" : "NON convergent"} `
    + `(${w.raking ? w.raking.iterations : "—"} itérations) · `
    + `${w.trimmed_weights ?? 0} poids écrêtés.`;
}

load();
setInterval(load, 15000);
