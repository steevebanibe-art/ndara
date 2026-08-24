/* Tableau de bord NDARA.
 * Rien n'est calculé ici : tout vient du serveur, donc de la même chaîne
 * statistique que celle qui produira les chiffres publiés.
 */

const el = (id) => document.getElementById(id);
const pct = (x) => (x == null ? "—" : (x * 100).toFixed(1) + " %");
const num = (x, d = 0) => (x == null ? "—" : Number(x).toLocaleString("fr-FR",
  { minimumFractionDigits: d, maximumFractionDigits: d }));

/* Une langue muette est une panne, pas un détail : elle doit se voir. */
function voixCaps(voix) {
  if (!voix) return [];
  const out = [];
  Object.entries(voix).forEach(([qid, v]) => {
    Object.entries(v.presents || {}).forEach(([lang, n]) => {
      const attendu = (v.attendu_par_langue || {})[lang] ?? v.attendu;
      if (n >= attendu) {
        out.push(`<span class="cap live">voix ${qid}/${lang} : ${n} libellés pré-synthétisés</span>`);
      } else if (n === 0) {
        out.push(`<span class="cap off">voix ${qid}/${lang} : aucun fichier, repli sur la voix du navigateur</span>`);
      } else {
        out.push(`<span class="cap draft">voix ${qid}/${lang} : ${n} sur ${attendu} seulement</span>`);
      }
    });
  });
  return out;
}

/* La téléphonie est soit prête, soit incomplète, et l'écran dit laquelle des
 * quatre pièces manque. Un « non configuré » sans détail fait perdre une
 * heure à celui qui doit le réparer. */
const AIDE_VARIABLES = {
  TWILIO_ACCOUNT_SID: "l'identifiant du compte, visible sur la console Twilio",
  TWILIO_AUTH_TOKEN: "le jeton du compte, à côté de l'identifiant",
  TWILIO_FROM_NUMBER: "le numéro acheté, au format international",
  NDARA_PUBLIC_URL: "l'adresse publique de ce serveur, que Twilio doit joindre",
};

function rendreTelephonie(t) {
  const prete = !!t.prete;
  const c = t.campagne || {};
  let html = `<p class="prov ${prete ? "prov-reel" : "prov-simule"}" style="margin-top:0">
    ${prete
      ? `Téléphonie prête. Numéro appelant <b>${t.numero}</b>, rappels reçus sur <b>${t.adresse_publique}</b>.`
      : `Téléphonie non branchée : NDARA sait mener l'entretien, mais pas décrocher le téléphone.`}
    ${prete ? "" : `<span>Il manque ${(t.manque || []).length} variable${(t.manque || []).length > 1 ? "s" : ""} d'environnement.</span>`}
  </p>`;

  if (!prete && (t.manque || []).length) {
    html += `<table class="data"><thead><tr><th>Variable</th><th>Ce que c'est</th></tr></thead><tbody>`
      + t.manque.map((v) => `<tr><td class="mono">${v}</td><td>${AIDE_VARIABLES[v] || ""}</td></tr>`).join("")
      + `</tbody></table>`;
  }
  el("tel-etat").innerHTML = html;
  el("btn-camp").disabled = !prete || !!c.active;
  el("btn-camp-stop").disabled = !c.active;
  if (c.active) {
    el("camp-state").textContent =
      `Campagne en cours : ${c.compose || 0} numéros composés sur ${c.plafond}, `
      + `${c.places || 0} en ligne, ${c.echecs || 0} échecs.`;
  }
}

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

  // provenance : d'où viennent réellement ces chiffres.
  // Affiché avant tout le reste, parce qu'un chiffre sans sa provenance
  // n'est pas un résultat, c'est une affirmation.
  const p = d.provenance || {};
  const LIBELLE = {
    simulation: "simulés, banc d'essai",
    web: "menés dans un navigateur",
    phone: "appels téléphoniques réels",
  };
  const ordre = ["phone", "web", "simulation"];
  const total = Object.values(p).reduce((a, b) => a + b, 0);
  const parts = ordre
    .filter((k) => p[k])
    .map((k) => `<b>${num(p[k])}</b> ${LIBELLE[k] || k}`);
  const reel = p.phone || 0;
  el("provenance").innerHTML = total
    ? `<p class="prov ${reel ? "prov-reel" : "prov-simule"}">
         ${parts.join(" · ")}
         ${reel ? "" : "<span>Aucun appel téléphonique réel n'a encore été mené. Tout ce qui suit est calculé sur des entretiens fabriqués à taux connu.</span>"}
       </p>`
    : `<p class="prov prov-simule">Aucun entretien en base.</p>`;

  // capacités
  const caps = d.capabilities || {};
  const brouillons = (caps.questionnaires || []).filter((q) => q.draft);
  el("caps").innerHTML = [
    `<span class="cap ${caps.asr_live ? "live" : "off"}">transcription : ${caps.asr}</span>`,
    `<span class="cap ${caps.coder === "llm" ? "live" : "off"}">codage : ${caps.coder}</span>`,
    `<span class="cap ${caps.telephony ? "live" : "off"}">téléphonie : ${caps.telephony ? "branchée" : "non branchée"}</span>`,
    ...brouillons.map(
      (q) => `<span class="cap draft">${q.id} : brouillon ${q.version}, non validé par un locuteur natif</span>`
    ),
    ...voixCaps(caps.voix),
  ].join("");

  rendreTelephonie(caps.telephonie || {});

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

/* ------------------------------------------------------------------ direct
 *
 * Le terrain pousse ses événements, cet écran les suit. Aucune interrogation
 * répétée du serveur : une connexion tenue ouverte, un battement toutes les
 * deux secondes, et une reconnexion automatique si la ligne tombe.
 */

const ETIQ_CANAL = { simulation: "simulation", web: "navigateur", phone: "téléphone" };
const ETAPES_SYS = {
  __announce__: "annonce", __consent_survey__: "consentement enquête",
  __consent_corpus__: "consentement corpus", __end__: "fin", terminé: "terminé",
};

let flux = null;
let fluxDelai = 1000;
let vagueN = 0;
let vagueTires = 0;

function pouls(etat, texte) {
  const p = el("pouls");
  p.className = "pouls " + etat;
  p.textContent = texte;
}

function ligneFeed(texte, classe) {
  const ul = el("feed");
  const li = document.createElement("li");
  li.className = classe || "";
  const h = new Date().toLocaleTimeString("fr-FR", { hour12: false });
  li.innerHTML = `<time>${h}</time><span></span>`;
  li.querySelector("span").textContent = texte;
  ul.insertBefore(li, ul.firstChild);
  while (ul.children.length > 40) ul.removeChild(ul.lastChild);
}

function rendreDirect(p) {
  const prov = p.provenance || {};
  el("live-tiles").innerHTML = [
    tile("Entretiens en cours", num(p.en_cours), "à cette seconde"),
    tile("Appels téléphoniques", num(prov.phone || 0), "collecte réelle"),
    tile("Dans un navigateur", num(prov.web || 0), "démonstration"),
    tile("Simulés", num(prov.simulation || 0), "banc d'essai, canal séparé"),
    tile("Écrans connectés", num(p.ecrans), "ce tableau de bord"),
  ].join("");

  const corps = el("live-rows").querySelector("tbody");
  const lignes = p.lignes || [];
  el("live-empty").style.display = lignes.length ? "none" : "";
  el("live-rows").style.display = lignes.length ? "" : "none";
  corps.innerHTML = lignes.map((l) => {
    const av = Math.round((l.progression || 0) * 100);
    return `<tr>
      <td class="mono">…${l.id}</td>
      <td>${ETIQ_CANAL[l.canal] || l.canal || "—"}</td>
      <td class="mono">${l.strate || "—"}</td>
      <td>${ETAPES_SYS[l.etape] || l.etape || "—"}</td>
      <td><div class="mini"><i style="width:${av}%"></i></div><span class="num">${av} %</span></td>
      <td>${l.methode || "—"}</td>
      <td class="num">${l.age == null ? "—" : l.age + " s"}</td>
    </tr>`;
  }).join("");
}

function evenement(e) {
  if (e.type === "pulse") { rendreDirect(e); return; }

  if (e.type === "entretien") {
    const l = e.ligne || {};
    if (e.etat === "debut") ligneFeed(`Entretien ouvert …${l.id} (${ETIQ_CANAL[l.canal] || l.canal})`, "ok");
    else if (e.etat === "fin") ligneFeed(`Entretien terminé …${l.id}`, "ok");
    else ligneFeed(`…${l.id} répond à « ${ETAPES_SYS[l.etape] || l.etape} » par ${l.methode}`);
    return;
  }

  if (e.type === "vague") {
    if (e.etat === "debut") {
      vagueN = e.n; vagueTires = 0;
      el("btn-wave").disabled = true;
      ligneFeed(`Vague simulée lancée : ${e.n} numéros tirés`, "ok");
    } else if (e.etat === "fin") {
      el("btn-wave").disabled = false;
      el("wave-fill").style.width = "100%";
      el("wave-state").textContent =
        `Vague terminée : ${e.aboutis} entretiens aboutis sur ${e.tires} numéros tirés, `
        + `soit un taux d'aboutissement de ${(100 * e.aboutis / e.tires).toFixed(1)} %.`;
      ligneFeed(`Vague terminée : ${e.aboutis} aboutis sur ${e.tires} tirés`, "ok");
      load();
    } else {
      el("btn-wave").disabled = false;
      el("wave-state").textContent = "La vague s'est arrêtée : " + (e.message || "erreur");
      ligneFeed("Vague interrompue", "bad");
    }
    return;
  }

  if (e.type === "campagne") {
    if (e.etat === "debut") {
      ligneFeed(`Campagne d'appels lancée : ${e.n} numéros`, "ok");
      el("btn-camp").disabled = true;
      el("btn-camp-stop").disabled = false;
    } else {
      el("btn-camp").disabled = false;
      el("btn-camp-stop").disabled = true;
      el("camp-state").textContent = e.etat === "erreur"
        ? "La campagne s'est arrêtée : " + (e.message || "")
        : `Campagne ${e.etat === "arret" ? "interrompue" : "terminée"} : `
          + `${e.compose} numéros composés, ${e.aboutis} entretiens aboutis, ${e.echecs} échecs.`;
      ligneFeed(el("camp-state").textContent, e.etat === "erreur" ? "bad" : "ok");
      load();
    }
    return;
  }

  if (e.type === "appel") {
    if (e.etat === "place" || e.etat === "echec") {
      if (e.plafond) el("camp-fill").style.width = Math.round(100 * e.compose / e.plafond) + "%";
      el("camp-state").textContent = `${e.compose} numéros composés sur ${e.plafond}`;
      ligneFeed(e.etat === "place"
        ? `Appel composé · strate ${e.strate}`
        : `Appel échoué · ${e.erreur || "raison inconnue"}`, e.etat === "place" ? "" : "bad");
    } else {
      ligneFeed(`Appel terminé …${e.id} · ${e.etat}`);
    }
    return;
  }

  if (e.type === "abouti" || e.type === "tirage") {
    vagueTires = e.tires;
    if (vagueN) {
      el("wave-fill").style.width = Math.round(100 * vagueTires / vagueN) + "%";
      el("wave-state").textContent =
        `${e.tires} numéros tirés sur ${vagueN} · ${e.aboutis} entretiens aboutis`;
    }
    if (e.type === "abouti") ligneFeed(`Entretien abouti …${e.id} · strate ${e.strate}`, "ok");
    return;
  }
}

function brancher() {
  if (flux) flux.close();
  pouls("wait", "connexion");
  flux = new EventSource("/api/stream");
  flux.onopen = () => { fluxDelai = 1000; pouls("on", "en direct"); };
  flux.onmessage = (m) => { try { evenement(JSON.parse(m.data)); } catch (_) {} };
  flux.onerror = () => {
    pouls("off", "reconnexion");
    flux.close();
    // Attente qui double à chaque échec, jusqu'à trente secondes : une panne
    // réseau ne doit pas transformer l'écran en marteau-pilon.
    setTimeout(brancher, fluxDelai);
    fluxDelai = Math.min(30000, fluxDelai * 2);
  };
}

el("btn-wave").onclick = async () => {
  const n = Number(el("wave-n").value);
  el("btn-wave").disabled = true;
  el("wave-fill").style.width = "0%";
  el("wave-state").textContent = "Tirage de la base de sondage…";
  const r = await fetch("/api/wave", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ n, cadence: 0.05 }),
  }).then((x) => x.json()).catch(() => ({ lance: false }));
  if (!r.lance) {
    el("btn-wave").disabled = false;
    el("wave-state").textContent = "Une vague est déjà en cours.";
  }
};

el("btn-camp").onclick = async () => {
  const n = Number(el("camp-n").value);
  if (!confirm(
      `Composer ${n} numéro${n > 1 ? "s" : ""} pour de vrai ?\n\n`
      + `Ce sont de vraies personnes et de l'argent réel : environ `
      + `${(n * 1.4).toFixed(2)} dollars si tous répondent et vont au bout.`)) return;
  el("btn-camp").disabled = true;
  el("camp-fill").style.width = "0%";
  el("camp-state").textContent = "Tirage de la base de sondage…";
  const r = await fetch("/api/campagne", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ n, simultanes: 3 }),
  }).then((x) => x.json()).catch(() => ({ lance: false, raison: "réseau" }));
  if (!r.lance) {
    el("btn-camp").disabled = false;
    el("camp-state").textContent = "Campagne refusée : " + (r.raison || "");
  }
};

el("btn-camp-stop").onclick = async () => {
  await fetch("/api/campagne/arret", { method: "POST" });
  el("camp-state").textContent = "Arrêt demandé. Les appels déjà en ligne vont au bout.";
};

brancher();
