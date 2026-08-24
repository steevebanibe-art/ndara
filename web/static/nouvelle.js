/* Dépôt d'une enquête cliente.
 *
 * Ce fichier n'interprète rien : la lecture du tableau, la validation et la
 * construction de l'instrument se font entièrement sur le serveur, dans le
 * même code que celui qui mènera les appels. Un tableau accepté ici est un
 * tableau que le moteur sait mener, sans deuxième avis.
 */

const $ = (id) => document.getElementById(id);

function meta() {
  return {
    titre: $("titre").value,
    pays: $("pays").value,
    objet: $("objet").value,
    incitation: $("incitation").value,
  };
}

function rapport(res, deposee) {
  const box = $("rapport");
  const pbs = res.problemes || [];
  const avs = res.avertissements || [];
  const r = res.resume || {};

  let html = "";

  if (pbs.length) {
    html += `<h2 class="section">À corriger</h2><div class="panel">
      <p class="prov" style="margin-top:0">
        ${pbs.length} point${pbs.length > 1 ? "s" : ""} bloque${pbs.length > 1 ? "nt" : ""} le dépôt.
        Rien n'a été enregistré : corrigez le tableau et revérifiez autant de fois qu'il faut.
      </p>
      <table class="data">
        <thead><tr><th>Ligne</th><th>Colonne</th><th>Ce qui ne va pas</th><th>Ce qu'il faut faire</th></tr></thead>
        <tbody>${pbs.map((p) => `<tr>
          <td class="num">${p.ligne == null ? "—" : p.ligne}</td>
          <td class="mono">${p.colonne || "—"}</td>
          <td>${p.message}</td>
          <td class="hint" style="margin:0">${p.correction}</td>
        </tr>`).join("")}</tbody>
      </table></div>`;
  }

  if (r.questions) {
    const types = Object.entries(r.types || {})
      .map(([t, n]) => `${n} ${{ single_choice: "à choix", yes_no: "oui/non", numeric: "numérique", open_short: "libre" }[t] || t}`)
      .join(" · ");
    html += `<h2 class="section">${deposee ? "Enquête déposée" : "Ce que NDARA a compris"}</h2>
      <div class="panel">
        <div class="grid">
          <div class="stat"><div class="label">Questions</div><div class="value">${r.questions}</div><div class="sub">${types}</div></div>
          <div class="stat"><div class="label">Durée estimée</div><div class="value">${r.duree_estimee_min} min</div><div class="sub">annoncée au répondant</div></div>
          <div class="stat"><div class="label">Filtres</div><div class="value">${r.filtres}</div><div class="sub">questions posées sous condition</div></div>
          <div class="stat"><div class="label">Exclues du corpus</div><div class="value">${r.sensibles}</div><div class="sub">même en cas d'accord</div></div>
        </div>`;

    if (avs.length) {
      html += `<h3 class="sub" style="margin-top:18px">Ce que NDARA a corrigé ou signalé</h3>
        <ul class="disclosure">${avs.map((a) => `<li>${a}</li>`).join("")}</ul>`;
    }

    if (deposee) {
      const v = res.voix || {};
      const n = (v.presents || {}).fr || 0;
      html += `<p class="prov ${n ? "prov-reel" : "prov-simule"}" style="margin-top:18px">
        ${n ? `${n} libellés déjà enregistrés en voix de studio.`
            : "Aucun libellé n'est encore enregistré en voix de studio : l'entretien "
              + "utilisera la voix du navigateur, qui sonne comme une machine. La "
              + "pré-synthèse se lance à part, une seule fois, et coûte quelques centimes."}
      </p>
      <div class="row-actions">
        <a class="act" href="/?questionnaire=${encodeURIComponent(r.id)}">Mener un entretien maintenant</a>
        <a class="act ghost" href="/dashboard">Voir le tableau de bord</a>
      </div>`;
    }
    html += `</div>`;
  }

  box.innerHTML = html;
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function envoyer(route) {
  const etat = $("etat");
  etat.textContent = "Vérification…";
  $("rapport").innerHTML = "";
  try {
    const r = await fetch(route, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tableau: $("tableau").value, meta: meta() }),
    });
    const res = await r.json();
    etat.textContent = res.ok
      ? (res.deposee ? "Enquête déposée. Elle est menable immédiatement."
                     : "Le tableau est valide. Vous pouvez déposer.")
      : "Le tableau n'est pas encore valide.";
    rapport(res, !!res.deposee);
  } catch (e) {
    etat.textContent = "La vérification n'a pas abouti : " + e.message;
  }
}

$("btn-verifier").onclick = () => envoyer("/api/questionnaire/verifier");
$("btn-deposer").onclick = () => envoyer("/api/questionnaire");
$("btn-exemple").onclick = async () => {
  $("tableau").value = await (await fetch("/exemple.csv")).text();
  $("titre").value = $("titre").value || "Prix des denrées, août";
  $("objet").value = $("objet").value || "les prix des denrées de base";
  $("incitation").value = $("incitation").value
    || "200 francs de crédit téléphonique seront envoyés aujourd'hui";
  $("etat").textContent = "Exemple chargé. Vérifiez-le, ou modifiez-le d'abord.";
};
