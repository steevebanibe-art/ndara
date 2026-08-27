/* Tableau de bord NDARA.
 * Rien n'est calculé ici : tout vient du serveur, donc de la même chaîne
 * statistique que celle qui produira les chiffres publiés.
 */

const el = (id) => document.getElementById(id);
const pct = (x) => (x == null ? "-" : num(x * 100, 1) + " %");
const num = (x, d = 0) => (x == null ? "-" : Number(x).toLocaleString("fr-FR",
  { minimumFractionDigits: d, maximumFractionDigits: d }));

/* Une langue muette est une panne, pas un détail : elle doit se voir. */
function voixCaps(voix) {
  if (!voix) return [];
  // Une étiquette par langue faisait six lignes dont trois disaient la même
  // chose, avant tout le contenu utile de la page. On compte, on résume, et
  // le détail des langues muettes tient dans l'infobulle.
  const pretes = [], muettes = [], partielles = [];
  Object.entries(voix).forEach(([qid, v]) => {
    Object.entries(v.presents || {}).forEach(([lang, n]) => {
      const attendu = (v.attendu_par_langue || {})[lang] ?? v.attendu;
      const nom = `${qid}/${lang}`;
      if (n >= attendu) pretes.push(`${nom} (${n})`);
      else if (n === 0) muettes.push(nom);
      else partielles.push(`${nom} : ${n} sur ${attendu}`);
    });
  });
  const out = [];
  if (pretes.length) {
    out.push(`<span class="cap live" title="${pretes.join(", ")}">voix de studio : `
      + `${pretes.length} langue${pretes.length > 1 ? "s" : ""} pré-synthétisée`
      + `${pretes.length > 1 ? "s" : ""}</span>`);
  }
  if (partielles.length) {
    out.push(`<span class="cap draft" title="${partielles.join(", ")}">voix incomplète : `
      + `${partielles.length} langue${partielles.length > 1 ? "s" : ""}</span>`);
  }
  if (muettes.length) {
    out.push(`<span class="cap off" title="${muettes.join(", ")}">`
      + `${muettes.length} langue${muettes.length > 1 ? "s" : ""} sans voix de studio, `
      + `repli sur celle du navigateur</span>`);
  }
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
  // Les identifiants peuvent être présents et faux. « Identifiants refusés »
  // ne dit ni lequel des deux, ni pourquoi ; leur forme, elle, se vérifie
  // sans jamais les lire.
  const forme = t.forme || [];
  if (forme.length) {
    html += `<p class="prov prov-simule" style="margin-top:12px">`
      + `Les identifiants sont posés, mais leur forme est douteuse :</p>`
      + `<ul class="disclosure">${forme.map((f) => `<li>${f}</li>`).join("")}</ul>`;
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

/* Une borne d'échelle qui se lit : 1, 2, 5 fois une puissance de dix. */
function arrondiHaut(v) {
  if (!isFinite(v) || v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const r = v / p;
  return (r <= 1 ? 1 : r <= 2 ? 2 : r <= 5 ? 5 : 10) * p;
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

/* La vague omnibus : le modèle économique, calculé et non promis.
 *
 * Trois choses doivent se voir ici, et les deux premières sont désagréables.
 * Un appel a une durée maximale, donc les créneaux sont un stock épuisable.
 * La même vague est bénéficiaire ou déficitaire selon qu'un accord opérateur
 * existe ou non : cacher ce fait reviendrait à se le faire dire en finale.
 * Et elle change aussi de signe selon le pays appelé, la minute cambodgienne
 * coûtant six fois moins cher que la camerounaise. Le produit est le même,
 * le modèle économique ne l'est pas, et c'est le tarif qui en décide.
 */
function usd(x, d = 2) {
  if (x == null) return "-";
  const signe = x < 0 ? "moins " : "";
  return signe + num(Math.abs(x), d) + " $";
}

async function chargerOmnibus() {
  let o;
  try {
    o = await (await fetch("/api/omnibus")).json();
  } catch (e) {
    o = { disponible: false };
  }
  const bloc = ["omnibus-titre", "omni-tiles", "omni-panel", "omni-tarifs", "omni-note"];
  if (!o || !o.disponible) {
    bloc.forEach((id) => { const n = el(id); if (n) n.style.display = "none"; });
    // Le chapeau vit entre le titre et les cartes : il part avec eux.
    const chapo = el("omnibus-titre") && el("omnibus-titre").nextElementSibling;
    if (chapo) chapo.style.display = "none";
    return;
  }

  const v = o.vague;
  const ref = o.facture.operateur;          // la vague telle qu'elle est vendable
  const part = v.duree_engagee_s / v.duree_max_s;

  el("omni-tiles").innerHTML = [
    ["Durée de l'appel", `${num(v.duree_engagee_s, 0)}<i>s</i>`,
     `sur ${num(v.duree_max_s, 0)} s au maximum, ${pct(part)} occupés`],
    ["Créneaux vendus", num(v.questions_vendues),
     `${v.creneaux.length} commanditaires, ${num(v.tronc.questions)} questions de tronc commun`],
    ["Reste à vendre", `${num(v.duree_restante_s, 0)}<i>s</i>`,
     v.duree_restante_s > 0
       ? "une question fermée en occupe une dizaine"
       : "l'appel est plein, un créneau de plus serait refusé"],
    ["Recette de la vague", usd(ref.recette_totale_usd, 0),
     `pour ${num(o.n_aboutis)} entretiens aboutis`],
  ].map(([k, val, s]) => `<div class="ch">
      <span class="ch-k">${k}</span>
      <span class="ch-v">${val}</span>
      <span class="ch-s">${s}</span>
    </div>`).join("");

  const parClient = {};
  ref.lignes.forEach((l) => { parClient[l.client] = l; });
  el("omni-creneaux").querySelector("tbody").innerHTML = v.creneaux.map((c) => {
    const l = parClient[c.client] || {};
    const marge = l.marge_usd;
    return `<tr>
      <td>${c.client}</td>
      <td>${c.intitule}</td>
      <td class="num">${num(c.questions)}</td>
      <td class="num">${num(c.duree_s, 0)} s</td>
      <td class="num">${usd(c.prix_usd, 0)}</td>
      <td class="num">${usd(l.cout_impute_usd, 0)}</td>
      <td class="num">${usd(marge, 0)}</td>
    </tr>`;
  }).join("");

  const ordres = (o.rotations || [])
    .map((r) => `appel ${r.rang + 1} : ${r.ordre.join(", puis ")}`)
    .join(" · ");
  el("omni-repartition").innerHTML =
    "Le coût d'un appel est réparti au prorata des secondes que chaque créneau "
    + "occupe : le tronc commun sert tout le monde, donc il se partage de la même "
    + `façon. Rotation des créneaux : ${ordres}.`;

  // Le fait central, dit avant qu'on nous le dise.
  const tw = o.facture.twilio, op = o.facture.operateur;
  const kh = o.facture.cambodge;
  const sup = o.question_supplementaire;
  const fo = o.fourchettes_usd_minute || {};
  const plage = (f) => (f ? `de ${num(f[0], 3)} à ${num(f[1], 3)} $` : "");
  el("omni-tarifs").innerHTML = `
    <h3 class="sub" style="margin-top:0">La même vague, sous trois tarifs</h3>
    <div class="tablewrap">
    <table class="data">
      <thead><tr>
        <th>Tarif de la minute</th><th class="num">Coût par entretien</th>
        <th class="num">Coût de la vague</th><th class="num">Recette</th>
        <th class="num">Marge</th><th class="num">Une question de plus</th>
      </tr></thead>
      <tbody>
        <tr>
          <td>${tw.tarif}</td>
          <td class="num">${usd(tw.cout_par_entretien_usd)}</td>
          <td class="num">${usd(tw.cout_total_usd, 0)}</td>
          <td class="num">${usd(tw.recette_totale_usd, 0)}</td>
          <td class="num">${usd(tw.marge_totale_usd, 0)}</td>
          <td class="num">${usd(sup.twilio.cout_total_usd, 0)}</td>
        </tr>
        <tr>
          <td>${kh.tarif}</td>
          <td class="num">${usd(kh.cout_par_entretien_usd)}</td>
          <td class="num">${usd(kh.cout_total_usd, 0)}</td>
          <td class="num">${usd(kh.recette_totale_usd, 0)}</td>
          <td class="num">${usd(kh.marge_totale_usd, 0)}</td>
          <td class="num">${usd(sup.cambodge.cout_total_usd, 0)}</td>
        </tr>
        <tr>
          <td>${op.tarif}</td>
          <td class="num">${usd(op.cout_par_entretien_usd)}</td>
          <td class="num">${usd(op.cout_total_usd, 0)}</td>
          <td class="num">${usd(op.recette_totale_usd, 0)}</td>
          <td class="num">${usd(op.marge_totale_usd, 0)}</td>
          <td class="num">${usd(sup.operateur.cout_total_usd, 0)}</td>
        </tr>
      </tbody>
    </table>
    </div>
    <p class="hint" style="margin-top:14px">
      La dernière colonne est ce qui décide du modèle : une question de plus dans
      une vague déjà lancée n'ajoute ni incitation, ni quote-part d'appels échoués,
      ni recrutement. Elle n'ajoute que des secondes de voix. À tarif de gros
      public ces secondes coûtent presque autant que la question se vend ; sous
      accord opérateur elles ne coûtent presque rien. C'est là, et nulle part
      ailleurs, que l'omnibus devient un modèle économique.
    </p>
    <p class="hint" style="margin-top:10px">
      Les deux premières lignes sont le même opérateur et le même code, appelant
      deux pays. Au Cameroun la vague exige un accord de gros pour exister ; au
      Cambodge elle en est à portée sans aucun accord, et elle passe au vert dès
      que la question se vend 800 $ au lieu de 500. Relevé sur le compte le
      ${fo.releve ? fo.releve.replace("compte Twilio, ", "") : "25 août 2026"} :
      Cameroun ${plage(fo.cameroun)} la minute, Cambodge ${plage(fo.cambodge)}.
      Ce sont des fourchettes et non des prix, parce que la facturation dépend de
      l'opérateur qui termine l'appel. Le calcul ci-dessus retient le haut de
      chaque plage, du côté qui coûte.
    </p>`;

  el("omni-note").textContent = o.note;
}

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

  // Quatre chiffres de terrain, posés à plat. Les enfermer dans quatre cartes
  // identiques ne les rendait pas plus lisibles, ça les rendait interchangeables.
  el("tiles").innerHTML = [
    ["Entretiens exploitables", num(d.n), "",
     `${num(fw.counts.complete)} complets, ${num(fw.counts.partial)} partiels`],
    ["Taux de réponse", pct(fw.response_rate_rr3), "RR3",
     `RR2 : ${pct(fw.response_rate_rr2)}, méthode AAPOR`],
    ["Taux de coopération", pct(fw.cooperation_rate), "",
     "parmi les personnes effectivement jointes"],
    ["Effectif effectif", num(w.effective_n, 0), "",
     `effet de plan ${num(w.design_effect, 2)}`],
  ].map(([k, v, u, s]) => `<div class="ch">
      <span class="ch-k">${k}</span>
      <span class="ch-v">${v}${u ? `<i>${u}</i>` : ""}</span>
      <span class="ch-s">${s}</span>
    </div>`).join("");

  // Estimations. Le tableau devient un dessin : l'intervalle de confiance est
  // tracé à l'échelle, l'estimation posée dessus. C'est toute la thèse du
  // projet rendue visible, au lieu d'être écrite en petit dans une colonne.
  const rows = (d.estimates || []).filter((r) => r.estimate != null);
  el("est").innerHTML = rows.map((r) => {
    const pct100 = r.unit === "%";
    const dec = pct100 ? 1 : 0;
    // Deux natures de grandeur, deux échelles, et ce n'est pas un détail.
    // Une proportion se lit sur 0 à 100 : la position dans cet intervalle a un
    // sens. Un prix, non. Le faire partir de zéro écrase l'intervalle contre le
    // bord droit et le rend invisible, ce qui laisse croire à une précision
    // qu'on n'a pas. L'axe d'un prix est donc local, et ses bornes sont
    // écrites en dessous pour que personne ne le confonde avec un axe zéro.
    const marge = Math.max(1e-9, r.ci_high - r.ci_low);
    const bas = pct100 ? 0 : r.ci_low - marge;
    const haut = pct100 ? 100 : r.ci_high + marge;
    const x = (v) => Math.max(0, Math.min(100, ((v - bas) / (haut - bas)) * 100));
    const a = x(r.ci_low), b = x(r.ci_high), m = x(r.estimate);
    const large = (r.ci_high - r.ci_low) / (r.estimate || 1) > 0.35;
    return `<article class="estim">
      <div class="e-tete">
        <h3>${r.label}</h3>
        <span class="e-n">${num(r.n)} réponses</span>
      </div>
      <div class="e-val">
        <b>${num(r.estimate, dec)}</b><span class="u">${pct100 ? "%" : (r.unit || "")}</span>
      </div>
      <div class="e-axe">
        <span class="e-piste"></span>
        <span class="e-ic" style="left:${a}%;right:${100 - b}%"></span>
        <span class="e-pt" style="left:${m}%"></span>
      </div>
      <div class="e-bornes">
        <span>${num(r.ci_low, dec)}</span>
        <span class="e-mid">intervalle à 95 %${large ? ", encore large" : ""}${
          pct100 ? "" : ", axe local"}</span>
        <span>${num(r.ci_high, dec)}</span>
      </div>
    </article>`;
  }).join("");
  el("est-empty").style.display = rows.length ? "none" : "";

  el("disclosure").innerHTML = (d.disclosure || [])
    .map((s) => `<li>${s}</li>`).join("");

  // Le terrain était un tableau de sept lignes. C'est un partage, donc ça se
  // dessine : une barre à l'échelle dit d'un coup d'œil que les non-contacts
  // dominent, ce que sept nombres alignés laissaient deviner.
  const ORDRE_ISSUES = ["complete", "partial", "refusal", "noncontact",
                        "ineligible", "unknown", "other"];
  const issues = ORDRE_ISSUES
    .filter((k) => (fw.counts || {})[k])
    .map((k) => [k, fw.counts[k]]);
  const totIssues = issues.reduce((a, [, v]) => a + v, 0) || 1;
  el("field").innerHTML = `
    <div class="partage">
      ${issues.map(([k, v]) => `<span class="p-${k}" style="flex:${v}"
        title="${FIELD_LABELS[k] || k} : ${num(v)}"></span>`).join("")}
    </div>
    <div class="partage-l">
      ${issues.map(([k, v]) => `<div class="pl">
          <span class="pl-p p-${k}"></span>
          <span class="pl-k">${FIELD_LABELS[k] || k}</span>
          <span class="pl-v">${num(v)}<i>${(100 * v / totIssues).toFixed(1).replace(".", ",")} %</i></span>
        </div>`).join("")}
    </div>`;

  // L'auto-audit sortait en trois cartes identiques, comme tout le reste de
  // la page. Il passe en registre plat : la séparation se fait par l'espace,
  // pas par un cadre. C'est le registre du site, et c'est le ton juste pour
  // des chiffres dont deux sont mauvais.
  const flagged = q.flagged_share ?? 0;
  const accord = q.coding_agreement && q.coding_agreement.agreement != null;
  const lignes = [
    ["Score de qualité moyen", num(q.quality_score_mean, 1),
     " / 100", `médiane ${num(q.quality_score_median, 1)}`, false],
    ["Signalés pour revérification", pct(flagged), "",
     `${num(q.flagged_for_review)} entretien${q.flagged_for_review > 1 ? "s" : ""} sur ${num(q.interviews_audited)} audités`,
     flagged > 0.15],
    ["Accord de codage", accord ? pct(q.coding_agreement.agreement) : "non publiable", "",
     accord ? `kappa ${num(q.coding_agreement.kappa, 2)} sur ${num(q.coding_agreement.n)} items recodés`
            : "aucun sous-échantillon n'a encore été recodé à la main", !accord],
  ];
  const qt = el("quality-tiles");
  qt.className = "registre";
  qt.innerHTML = lignes.map(([lab, val, unite, sous, alerte]) => `<div class="reg-l">
      <span class="reg-k">${lab}</span>
      <span class="reg-v ${alerte ? "reg-alerte" : ""}">${val}<i>${unite}</i></span>
      <span class="reg-s">${sous}</span>
    </div>`).join("");

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
      <td class="num">${i.mean_duration_s == null ? "-" : num(i.mean_duration_s, 1) + " s"}</td>
    </tr>`).join("");

  // Le corpus est vide, et il le restera tant qu'aucune parole réelle n'aura
  // été consentie. Trois grandes cartes affichant zéro donnaient l'impression
  // d'un tableau de bord en panne. Une phrase dit mieux la même chose, et un
  // corpus qui se remplit reprendra ses chiffres.
  const c = d.corpus || {};
  el("corpus-tiles").innerHTML = c.segments
    ? `<div class="chiffres">
         <div class="ch"><span class="ch-k">Parole consentie</span>
           <span class="ch-v">${num(c.minutes, 1)}<i>min</i></span>
           <span class="ch-s">${num(c.segments)} segments</span></div>
         <div class="ch"><span class="ch-k">Locuteurs distincts</span>
           <span class="ch-v">${num(c.speakers)}</span>
           <span class="ch-s">tirés au sort, jamais volontaires</span></div>
         <div class="ch"><span class="ch-k">Expurgations</span>
           <span class="ch-v">${num(c.redactions)}</span>
           <span class="ch-s">identifiants retirés avant stockage</span></div>
       </div>`
    : `<p class="vide">Le corpus est vide, et c'est voulu. Il ne se remplit
         qu'avec de la parole réelle dont la personne a accepté la conservation,
         par un consentement distinct de celui de l'enquête. La simulation ne
         fabrique aucun audio, donc rien de synthétique ne peut le contaminer.</p>`;

  el("foot").textContent =
    `Questionnaire ${d.questionnaire ? d.questionnaire.id + " v" + d.questionnaire.version : "-"} · `
    + `calage sur marges ${w.raking && w.raking.converged ? "convergent" : "NON convergent"} `
    + `(${w.raking ? w.raking.iterations : "-"} itérations) · `
    + `${w.trimmed_weights ?? 0} poids écrêtés.`;
}

load();
setInterval(load, 15000);

// La composition de la vague et sa facture ne bougent pas pendant qu'on
// regarde : elles se chargent une fois, pas toutes les quinze secondes.
chargerOmnibus();

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
  ].join("");

  const corps = el("live-rows").querySelector("tbody");
  const lignes = p.lignes || [];
  el("live-empty").style.display = lignes.length ? "none" : "";
  el("live-rows").style.display = lignes.length ? "" : "none";
  corps.innerHTML = lignes.map((l) => {
    const av = Math.round((l.progression || 0) * 100);
    return `<tr>
      <td class="mono">…${l.id}</td>
      <td>${ETIQ_CANAL[l.canal] || l.canal || "-"}</td>
      <td class="mono">${l.strate || "-"}</td>
      <td>${ETAPES_SYS[l.etape] || l.etape || "-"}</td>
      <td><div class="mini"><i style="width:${av}%"></i></div><span class="num">${av} %</span></td>
      <td>${l.methode || "-"}</td>
      <td class="num">${l.age == null ? "-" : l.age + " s"}</td>
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
  pouls("wait", "connexion…");
  flux = new EventSource("/api/stream");
  flux.onopen = () => { fluxDelai = 1000; pouls("on", "flux ouvert"); };
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
      + `${(n * 2.36).toFixed(0)} dollars si tous répondent et vont au bout, `
      + `et le double si beaucoup tombent sur un répondeur.`)) return;
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

/* Demander à l'opérateur tout ce qu'un appel exige, sans passer d'appel.
 *
 * Quatre lectures gratuites. Elles existent parce que le code 20003 de Twilio
 * recouvre trois causes qui ne se corrigent pas au même endroit : jeton faux,
 * solde épuisé, numéro appelant inutilisable par le compte. Le tableau de bord
 * n'en annonçait qu'une, et a envoyé refaire deux fois un jeton qui allait
 * bien. Un diagnostic qui ne cite qu'une cause sur trois est pire que pas de
 * diagnostic : il est convaincant.
 */
const echapper = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

el("btn-verif").onclick = async () => {
  el("btn-verif").disabled = true;
  el("verif-etat").textContent = "Interrogation de l'opérateur…";
  const r = await fetch("/api/telephonie/verifier", { method: "POST" })
    .then((x) => x.json()).catch(() => ({ ok: false, raison: "réseau" }));
  el("btn-verif").disabled = false;

  if (!r.ok) {
    el("verif-etat").innerHTML =
      "<strong>Identifiants refusés par l'opérateur.</strong> "
      + echapper(r.raison || "")
      + "<br>Cette lecture-ci est concluante : c'est bien la valeur du jeton ou "
      + "du SID qui est en cause, et non le solde ni le numéro appelant.";
    return;
  }

  const lignes = [];
  lignes.push("<strong>Identifiants acceptés.</strong> Compte « "
    + echapper(r.compte) + " », état " + echapper(r.etat)
    + ", type " + echapper(r.type) + ".");
  lignes.push(r.essai
    ? "⚠ Compte en essai : le TwiML personnalisé reste bloqué, donc NDARA ne peut "
      + "pas mener l'entretien tant que la mise à niveau n'est pas effective."
    : "Compte complet : le TwiML personnalisé est autorisé.");
  if (r.solde !== undefined) {
    lignes.push("Solde : " + Number(r.solde).toFixed(2) + " " + echapper(r.devise || "")
      + ". Un entretien de deux minutes trente vers un mobile camerounais coûte "
      + "environ deux dollars.");
  }
  if (r.numero_source) {
    lignes.push("Numéro appelant : " + echapper(r.numero_source) + "."
      + (r.entrant === true
          ? " Appels entrants branchés sur NDARA : composer ce numéro ouvre un entretien."
          : r.entrant === false
            ? " Appels entrants non branchés."
            : ""));
  }
  /* Ce que le compte a le droit de composer, pays par pays. Twilio bloque
   * certaines destinations contre la fraude, et le blocage ne se voit qu'au
   * moment de composer, une fois l'appel facturé. */
  const pays = r.pays || {};
  const isos = Object.keys(pays);
  if (isos.length) {
    lignes.push("<strong>Appels sortants, pays par pays</strong><ul>" + isos.map((i) => {
      const p = pays[i];
      if (!p.lisible) return "<li>" + echapper(i) + " : illisible</li>";
      const etat = !p.ordinaires ? "pays fermé"
        : p.plages_signalees ? "ouvert, plages signalées comprises"
        : "ouvert aux numéros ordinaires, PAS aux plages signalées pour fraude";
      return "<li>" + echapper(p.nom || i) + " : " + etat + "</li>";
    }).join("") + "</ul>");
  }

  const ennuis = r.ennuis || [];
  if (ennuis.length) {
    lignes.push("<strong>Ce qui empêchera l'appel :</strong><ul>"
      + ennuis.map((e) => "<li>" + echapper(e) + "</li>").join("") + "</ul>");
  } else {
    lignes.push("<strong>Rien ne s'oppose à un appel.</strong>");
  }
  el("verif-etat").innerHTML = lignes.join("<br>");
};

/* L'appel d'essai vers un numéro qu'on possède.
 *
 * Pas de demande de confirmation ici, à la différence de la campagne : on
 * compose un seul numéro, choisi et tapé à l'instant, et c'est le sien. Une
 * fenêtre de confirmation sur un geste déjà explicite n'apprend rien et se
 * clique sans lire, ce qui abîme celles qui comptent.
 */
el("btn-essai").onclick = async () => {
  const numero = (el("essai-num").value || "").trim();
  if (!numero) {
    el("essai-etat").textContent =
      "Entrez un numéro au format international, indicatif compris : +237690000000.";
    return;
  }
  el("btn-essai").disabled = true;
  el("essai-etat").textContent = "Composition…";
  const r = await fetch("/api/appel", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ numero }),
  }).then((x) => x.json()).catch(() => ({ lance: false, raison: "réseau" }));
  el("btn-essai").disabled = false;
  if (r.lance) {
    el("essai-etat").textContent =
      "Ça sonne. Décrochez : l'entretien démarre tout de suite, et cet écran le "
      + "suit tour par tour dans la bande du haut."
      // Ce qui a dû être dégradé pour que l'appel parte se dit, au lieu de
      // laisser croire que tout tourne à pleine capacité.
      + (r.note ? " " + r.note : "");
    ligneFeed("Appel d'essai composé vers " + numero, "ok");
  } else {
    el("essai-etat").textContent = "Appel refusé : " + (r.raison || "");
    ligneFeed("Appel d'essai refusé : " + (r.raison || ""), "bad");
  }
};

brancher();
