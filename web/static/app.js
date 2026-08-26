/* NDARA, canal de démonstration navigateur.
 *
 * Le canal réel est le téléphone. Ce fichier ne contient aucune logique
 * d'enquête : les libellés, les filtres, les relances, le codage et la
 * décision de basculer sur le clavier viennent tous du serveur. Le
 * navigateur ne fait que restituer une invite et renvoyer une réponse -
 * exactement comme le fera le canal téléphonique.
 */

const $ = (id) => document.getElementById(id);
const state = {
  interviewId: null,
  questionnaire: null,
  language: "fr",
  step: null,
  askedAt: null,
  caps: null,
  recorder: null,
  recog: null,
  chunks: [],
  micReady: null,
  history: [],
  handsFree: true,   // NDARA écoute dès qu'il a fini de parler
  silences: 0,       // silences consécutifs sur l'étape en cours
};

// ---------------------------------------------------------------- capacités

async function loadCaps() {
  const caps = await (await fetch("/api/capabilities")).json();
  state.caps = caps;
  // Arrivée depuis un dépôt d'enquête : on présélectionne l'instrument
  // fraîchement déposé, pour que le client passe son premier entretien
  // sans avoir à le chercher dans une liste.
  const demande = new URLSearchParams(location.search).get("questionnaire");

  const qSel = $("questionnaire");
  qSel.innerHTML = "";
  caps.questionnaires.forEach((q) => {
    const o = document.createElement("option");
    o.value = q.id;
    o.textContent = `${q.id}, ${q.country}, ${q.steps} questions${q.draft ? " (brouillon)" : ""}`;
    qSel.appendChild(o);
  });
  if (demande && caps.questionnaires.some((q) => q.id === demande)) {
    qSel.value = demande;
  }
  qSel.onchange = fillLanguages;
  fillLanguages();

  const badges = [
    { txt: `transcription : ${caps.asr}`, cls: caps.asr_live ? "live" : "off" },
    { txt: `codage : ${caps.coder}`, cls: caps.coder === "llm" ? "live" : "off" },
    { txt: `téléphonie : ${caps.telephony ? "branchée" : "non branchée"}`,
      cls: caps.telephony ? "live" : "off" },
  ];
  const draft = caps.questionnaires.find((q) => q.draft);
  if (draft) badges.push({ txt: `${draft.id} : traduction non validée`, cls: "draft" });
  $("caps").innerHTML = badges
    .map((b) => `<span class="cap ${b.cls}">${b.txt}</span>`)
    .join("");
}

function fillLanguages() {
  const qid = $("questionnaire").value;
  const q = state.caps.questionnaires.find((x) => x.id === qid);
  const sel = $("language");
  sel.innerHTML = "";
  (q ? q.languages : ["fr"]).forEach((l) => {
    const o = document.createElement("option");
    o.value = l;
    o.lang = l;
    o.textContent = { fr: "Français", en: "English", km: "ភាសាខ្មែរ" }[l] || l;
    sel.appendChild(o);
  });
}

// ---------------------------------------------------------------- restitution

function speak(prompt, onEnd) {
  // onEnd est appelé quand NDARA a fini de parler, quel que soit le chemin
  // emprunté, y compris en cas d'échec : les mains libres ne doivent jamais
  // rester bloquées à attendre une fin qui ne vient pas.
  let rendu = false;
  const fini = () => { if (!rendu) { rendu = true; if (onEnd) onEnd(); } };

  const say = () => {
    if (!("speechSynthesis" in window)) return fini();
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(prompt.text + (prompt.note ? " " + prompt.note : ""));
    u.lang = { fr: "fr-FR", en: "en-US", km: "km-KH" }[state.language] || "fr-FR";
    u.rate = 0.95;
    u.onend = fini;
    u.onerror = fini;
    window.speechSynthesis.speak(u);
    // Certaines synthèses de navigateur n'émettent jamais onend. Filet de
    // sécurité proportionnel à la longueur du texte.
    setTimeout(fini, 2000 + prompt.text.length * 90);
  };

  if (prompt.audio_url) {
    // Audio pré-synthétisé : stimulus identique pour tous les répondants.
    const a = new Audio(prompt.audio_url);
    a.onended = fini;
    a.onerror = say;
    a.play().catch(say);
    state.lastAudio = a;
  } else {
    say();
  }
}

/* Mains libres : NDARA écoute dès qu'il a fini de parler, comme au téléphone.
 * Personne n'appuie sur rien. Le repli sur les touches reste entier, et
 * l'écoute s'arrête d'elle-même après deux silences pour ne pas enfermer
 * quelqu'un dans une boucle. */
function mainsLibres() {
  return !!(state.handsFree && RECOG);
}

function ecouterSiPossible(prompt) {
  if (!mainsLibres()) return;
  if (!prompt || prompt.done || !prompt.allow_voice) return;
  if (state.silences >= 2) {
    $("mic-hint").textContent =
      "Je n'ai rien entendu. Utilisez les touches, ou appuyez sur le micro pour reprendre.";
    return;
  }
  startRecognition();
}

const KIND_LABEL = {
  announce: "Annonce, identification de l'agent",
  consent: "Consentement",
  question: "Question",
  end: "Fin de l'entretien",
};

/* Les trois etats de l'appel. Ils existaient dans la logique et nulle part
   sur la surface : en mains libres, personne n'appuie sur rien, et rien ne
   disait quand le micro etait ouvert. */
function etat(mode, texte) {
  document.body.classList.toggle("parle", mode === "parle");
  document.body.classList.toggle("ecoute", mode === "ecoute");
  $("etat").textContent = texte;
}

/* Le lecteur d'ecran doit changer de voix en meme temps que NDARA. */
function langueDuDocument() {
  const l = state.language || "fr";
  document.documentElement.lang = l;
  $("speech").lang = l;
}

function render(prompt) {
  // Nouvelle question : le compteur de silences repart de zéro. Il ne compte
  // que les silences sur UNE étape, sinon un répondant lent finirait muet
  // pour tout le reste de l'entretien.
  langueDuDocument();
  if (!state.step || state.step.step_id !== prompt.step_id) state.silences = 0;
  state.step = prompt;
  state.askedAt = performance.now();
  $("screen-home").style.display = "none";
  $("screen-iv").style.display = "";

  const pct = Math.round(prompt.progress * 100);
  $("bar").style.width = pct + "%";
  $("progress").setAttribute("aria-valuenow", String(pct));
  $("kind").textContent = KIND_LABEL[prompt.kind] || prompt.kind;
  $("speech").textContent = prompt.text;

  const note = $("note");
  if (prompt.note) { note.style.display = ""; note.textContent = prompt.note; }
  else { note.style.display = "none"; }

  const panel = $("screen-iv");
  panel.classList.toggle("consent", prompt.kind === "consent");
  panel.classList.toggle("optional", prompt.step_id === "__consent_corpus__");

  buildInputs(prompt);
  $("btn-mic").style.display = prompt.allow_voice ? "" : "none";
  $("btn-replay").style.display = prompt.done ? "none" : "";
  $("mic-hint").textContent = prompt.allow_voice ? micHint() : "";

  $("hf-wrap").style.display = RECOG ? "" : "none";
  etat("parle", "NDARA parle");
  speak(prompt, () => {
    // La reponse n'est rendue disponible qu'a la fin de la parole, et c'est
    // seulement la que le focus arrive. Il partait auparavant soixante
    // millisecondes apres l'affichage, donc pendant le premier mot.
    etat(prompt.done ? "" : "attend", prompt.done ? "" : "À vous");
    const libre = $("free");
    if (libre) libre.focus();
    ecouterSiPossible(prompt);
  });
  drawHood();
}

function micHint() {
  if (state.caps.asr_live) {
    return "Micro branché sur la transcription du serveur (" + state.caps.asr + ").";
  }
  if (RECOG) {
    return (state.handsFree
        ? "Mains libres : répondez à voix haute dès que j'ai fini de parler, sans rien toucher. "
        : "Appuyez sur le micro pour répondre à la voix. ")
      + "Répondez comme vous voulez, en une phrase entière si c'est plus naturel : "
      + "la réponse est cherchée dedans. Reconnaissance du navigateur, vraie "
      + "transcription et pas une simulation, mais votre voix part chez l'éditeur "
      + "du navigateur : bon pour cette démonstration, jamais pour une collecte réelle.";
  }
  return "Ce navigateur n'a pas de reconnaissance vocale et aucun moteur n'est "
       + "branché sur le serveur : le micro enregistre, mais la réponse doit "
       + "passer par les touches. On ne fabrique jamais une transcription.";
}

function buildInputs(prompt) {
  const box = $("inputs");
  box.innerHTML = "";

  if (prompt.kind === "announce") {
    const b = document.createElement("button");
    b.className = "act";
    b.textContent = "Continuer";
    b.onclick = () => submit({});
    box.appendChild(wrapRow(b));
    return;
  }

  if (prompt.done) {
    const b = document.createElement("button");
    b.className = "act ghost";
    b.textContent = "Revenir à l'accueil";
    b.onclick = () => location.reload();
    box.appendChild(wrapRow(b));
    return;
  }

  if (prompt.options && prompt.options.length) {
    const pad = document.createElement("div");
    pad.className = "keypad";
    prompt.options.forEach((o) => {
      const b = document.createElement("button");
      b.className = "key";
      b.innerHTML = `<b>${o.dtmf ?? "·"}</b><span></span>`;
      b.querySelector("span").textContent = o.label;
      b.onclick = () => submit({ dtmf: o.dtmf });
      if (o.dtmf != null) b.setAttribute("aria-label", "Touche " + o.dtmf + ", " + o.label);
      pad.appendChild(b);
    });
    box.appendChild(pad);
  }

  if (prompt.input_type === "number" || prompt.input_type === "open") {
    const inp = document.createElement("input");
    inp.className = "txt";
    inp.id = "free";
    inp.placeholder = prompt.input_type === "number"
      ? `Montant${prompt.unit ? " en " + prompt.unit : ""}`
      : "Votre réponse";
    inp.setAttribute("aria-label", prompt.text);
    inp.onkeydown = (e) => { if (e.key === "Enter") send(); };
    const b = document.createElement("button");
    b.className = "act";
    b.textContent = "Envoyer";
    b.onclick = send;
    box.appendChild(wrapRow(inp, b));

    function send() {
      const v = inp.value.trim();
      if (v) submit({ text: v });
    }
  } else if (!prompt.options.length) {
    const inp = document.createElement("input");
    inp.className = "txt";
    inp.placeholder = "Dites oui ou non";
    inp.setAttribute("aria-label", prompt.text);
    inp.onkeydown = (e) => { if (e.key === "Enter" && inp.value.trim()) submit({ text: inp.value.trim() }); };
    box.appendChild(wrapRow(inp));
  }
}

function wrapRow(...els) {
  const r = document.createElement("div");
  r.className = "row";
  els.forEach((e) => r.appendChild(e));
  return r;
}

// ---------------------------------------------------------------- sous le capot

function drawHood() {
  const tb = $("hood-table").querySelector("tbody");
  tb.innerHTML = state.history
    .map((h) => `<tr>
        <td class="k">${h.step}</td>
        <td>${h.transcript ? escapeHtml(h.transcript) : "-"}</td>
        <td>${h.asr == null ? "-" : h.asr.toFixed(2)}</td>
        <td>${h.code ?? "-"}</td>
        <td>${h.method}</td>
        <td>${h.relances}</td>
      </tr>`)
    .join("");
}

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ---------------------------------------------------------------- échanges

async function start() {
  const body = {
    questionnaire: $("questionnaire").value,
    language: $("language").value,
    stratum: "WEB",
    channel: "web",
  };
  state.language = body.language;
  langueDuDocument();
  state.questionnaire = body.questionnaire;
  const p = await postJSON("/api/start", body);
  state.interviewId = p.interview_id;
  state.history = [];
  render(p);
}

async function submit({ text, dtmf, audio_b64, audio_ext, asr, asr_confidence }) {
  const prev = state.step;
  const duration = state.askedAt ? Math.round(performance.now() - state.askedAt) : null;
  if (state.lastAudio) { try { state.lastAudio.pause(); } catch (_) {} }
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();

  const p = await postJSON("/api/answer", {
    interview_id: state.interviewId,
    questionnaire: state.questionnaire,
    language: state.language,
    text, dtmf, audio_b64, audio_ext, asr, asr_confidence,
    duration_ms: duration,
  });

  if (prev && prev.kind === "question") {
    state.history.push({
      step: prev.step_id,
      transcript: p.transcript || text || null,
      asr: p.asr_confidence,
      code: dtmf ? `touche ${dtmf}` : (p.step_id === prev.step_id ? "non compris" : "codé"),
      method: dtmf ? "clavier" : ((audio_b64 || asr) ? "voix" : "saisie"),
      relances: p.step_id === prev.step_id ? "+1" : 0,
    });
  }
  render(p);
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

// ---------------------------------------------------------------- micro
//
// Deux chemins, et l'écran dit toujours lequel tourne.
//
// 1. Le navigateur embarque un moteur de reconnaissance. On l'utilise : c'est
//    une transcription réelle par un moteur réel, pas une simulation. Elle ne
//    sert qu'à la démonstration, jamais à une collecte réelle, parce que la
//    parole part alors chez l'éditeur du navigateur : c'est écrit à l'écran.
// 2. Sinon, le micro enregistre et la transcription revient au serveur, qui
//    la refuse tant qu'aucun moteur n'est branché. Le repli est le clavier.
//
// Dans les deux cas la règle tient : on ne fabrique jamais une transcription.

const RECOG = window.SpeechRecognition || window.webkitSpeechRecognition || null;
const LOCALE = { fr: "fr-FR", en: "en-US", km: "km-KH" };

function micLabel(t) { $("mic-label").textContent = t; }

function startRecognition() {
  const btn = $("btn-mic");
  const r = new RECOG();
  r.lang = LOCALE[state.language] || "fr-FR";
  r.interimResults = true;
  r.continuous = false;
  r.maxAlternatives = 1;

  let final = "";
  let conf = null;
  state.recog = r;

  r.onstart = () => {
    btn.classList.add("rec"); micLabel("J'écoute, parlez");
    etat("ecoute", "J'écoute, parlez");
  };
  r.onresult = (e) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const res = e.results[i];
      if (res.isFinal) { final += res[0].transcript; conf = res[0].confidence; }
      else interim += res[0].transcript;
    }
    $("mic-hint").textContent = (final + interim).trim()
      ? "Vous dites : « " + (final + interim).trim() + " »"
      : "J'écoute.";
  };
  let muet = false;
  r.onerror = (e) => {
    muet = e.error === "no-speech";
    btn.classList.remove("rec");
    micLabel("Répondre à la voix");
    $("mic-hint").textContent = muet
      ? "Je n'ai rien entendu."
      : "La reconnaissance a échoué (" + e.error + "). Utilisez les touches.";
  };
  r.onend = () => {
    btn.classList.remove("rec");
    micLabel("Répondre à la voix");
    if (state.step && !state.step.done) etat("attend", "À vous");
    state.recog = null;
    const dit = final.trim();
    if (!dit) {
      // Silence. En mains libres, c'est un tour sans réponse : le moteur
      // relance avec son libellé fixe, puis bascule sur les touches. C'est
      // exactement ce que fait un vrai appel.
      if (mainsLibres() && muet && state.step && state.step.allow_voice) {
        state.silences += 1;
        submit({ text: "", asr: "navigateur", asr_confidence: 0 });
      }
      return;
    }
    state.silences = 0;
    // Une confiance est toujours transmise : c'est elle qui fait enregistrer
    // le tour comme une réponse parlée et non comme une saisie au clavier.
    submit({ text: dit, asr: "navigateur", asr_confidence: conf == null ? 0.5 : conf });
  };

  try { r.start(); } catch (_) { /* déjà en cours */ }
}

async function recordForServer() {
  const btn = $("btn-mic");
  try {
    if (!state.micReady) {
      state.micReady = await navigator.mediaDevices.getUserMedia({ audio: true });
    }
    const rec = new MediaRecorder(state.micReady);
    state.recorder = rec;
    state.chunks = [];
    rec.ondataavailable = (e) => state.chunks.push(e.data);
    rec.onstop = async () => {
      btn.classList.remove("rec");
      micLabel("Répondre à la voix");
      const blob = new Blob(state.chunks, { type: "audio/webm" });
      submit({ audio_b64: await blobToBase64(blob), audio_ext: "webm" });
    };
    rec.start();
    btn.classList.add("rec");
    micLabel("Arrêter et envoyer");
  } catch (err) {
    $("mic-hint").textContent =
      "Micro indisponible : " + err.message + ". Utilisez le clavier ou la saisie.";
  }
}

function toggleMic() {
  if (state.recog) { state.recog.stop(); return; }
  if (state.recorder && state.recorder.state === "recording") {
    state.recorder.stop();
    return;
  }
  if (RECOG) return startRecognition();
  return recordForServer();
}

function blobToBase64(blob) {
  return new Promise((res) => {
    const fr = new FileReader();
    fr.onloadend = () => res(String(fr.result).split(",")[1]);
    fr.readAsDataURL(blob);
  });
}

// ---------------------------------------------------------------- retrait

async function withdraw() {
  const code = $("wcode").value.trim().toUpperCase();
  if (!code) return;
  const r = await postJSON("/api/withdraw", { code });
  $("wres").textContent = r.found
    ? `${r.deleted} enregistrement(s) effacé(s) définitivement.`
    : "Code inconnu.";
}

// ---------------------------------------------------------------- amorçage

$("btn-start").onclick = start;
$("btn-mic").onclick = toggleMic;
$("btn-replay").onclick = () => state.step && speak(state.step, () => ecouterSiPossible(state.step));
$("btn-withdraw").onclick = withdraw;
$("hf").onchange = (e) => {
  state.handsFree = e.target.checked;
  state.silences = 0;
  if (!state.handsFree && state.recog) state.recog.abort();
  $("mic-hint").textContent = state.step && state.step.allow_voice ? micHint() : "";
};
loadCaps();

/* Les chiffres du clavier physique composent, comme sur un telephone. La these
   du produit est qu'un appareil a dix touches suffit ; l'ecran ne le montrait
   pas, puisque la seule facon d'appuyer sur la touche 1 etait de viser un
   rectangle a la souris. On ne detourne jamais une frappe destinee a un champ
   de saisie, ni une combinaison avec une touche de commande. */
document.addEventListener("keydown", (e) => {
  if (e.altKey || e.ctrlKey || e.metaKey) return;
  const cible = e.target;
  if (cible && (cible.tagName === "INPUT" || cible.tagName === "TEXTAREA"
                || cible.isContentEditable)) return;
  if (!/^[0-9*#]$/.test(e.key)) return;
  const touches = document.querySelectorAll("#inputs button.key > b");
  for (const b of touches) {
    if (b.textContent.trim() === e.key) {
      e.preventDefault();
      const bouton = b.parentElement;
      bouton.classList.add("frappee");
      setTimeout(() => bouton.classList.remove("frappee"), 160);
      bouton.click();
      return;
    }
  }
});
