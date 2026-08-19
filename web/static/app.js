/* NDARA — canal de démonstration navigateur.
 *
 * Le canal réel est le téléphone. Ce fichier ne contient aucune logique
 * d'enquête : les libellés, les filtres, les relances, le codage et la
 * décision de basculer sur le clavier viennent tous du serveur. Le
 * navigateur ne fait que restituer une invite et renvoyer une réponse —
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
  chunks: [],
  micReady: null,
  history: [],
};

// ---------------------------------------------------------------- capacités

async function loadCaps() {
  const caps = await (await fetch("/api/capabilities")).json();
  state.caps = caps;

  const qSel = $("questionnaire");
  qSel.innerHTML = "";
  caps.questionnaires.forEach((q) => {
    const o = document.createElement("option");
    o.value = q.id;
    o.textContent = `${q.id} — ${q.country}, ${q.steps} questions${q.draft ? " (brouillon)" : ""}`;
    qSel.appendChild(o);
  });
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
    o.textContent = { fr: "Français", en: "English", km: "ភាសាខ្មែរ" }[l] || l;
    sel.appendChild(o);
  });
}

// ---------------------------------------------------------------- restitution

function speak(prompt) {
  const say = () => {
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(prompt.text + (prompt.note ? " " + prompt.note : ""));
    u.lang = { fr: "fr-FR", en: "en-US", km: "km-KH" }[state.language] || "fr-FR";
    u.rate = 0.95;
    window.speechSynthesis.speak(u);
  };
  if (prompt.audio_url) {
    // Audio pré-synthétisé : stimulus identique pour tous les répondants.
    const a = new Audio(prompt.audio_url);
    a.onerror = say;
    a.play().catch(say);
    state.lastAudio = a;
  } else {
    say();
  }
}

const KIND_LABEL = {
  announce: "Annonce — identification de l'agent",
  consent: "Consentement",
  question: "Question",
  end: "Fin de l'entretien",
};

function render(prompt) {
  state.step = prompt;
  state.askedAt = performance.now();
  $("screen-home").style.display = "none";
  $("screen-iv").style.display = "";

  $("bar").style.width = Math.round(prompt.progress * 100) + "%";
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
  $("mic-hint").textContent = prompt.allow_voice
    ? (state.caps.asr_live
        ? "Micro branché sur la transcription."
        : "Aucune transcription n'est branchée : le micro enregistre, mais la réponse doit passer par le clavier ou la saisie. On ne simule jamais une transcription.")
    : "";

  speak(prompt);
  drawHood();
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
    inp.onkeydown = (e) => { if (e.key === "Enter") send(); };
    const b = document.createElement("button");
    b.className = "act";
    b.textContent = "Envoyer";
    b.onclick = send;
    box.appendChild(wrapRow(inp, b));
    setTimeout(() => inp.focus(), 60);

    function send() {
      const v = inp.value.trim();
      if (v) submit({ text: v });
    }
  } else if (!prompt.options.length) {
    const inp = document.createElement("input");
    inp.className = "txt";
    inp.placeholder = "Dites oui ou non";
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
        <td>${h.transcript ? escapeHtml(h.transcript) : "—"}</td>
        <td>${h.asr == null ? "—" : h.asr.toFixed(2)}</td>
        <td>${h.code ?? "—"}</td>
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
  state.questionnaire = body.questionnaire;
  const p = await postJSON("/api/start", body);
  state.interviewId = p.interview_id;
  state.history = [];
  render(p);
}

async function submit({ text, dtmf, audio_b64, audio_ext }) {
  const prev = state.step;
  const duration = state.askedAt ? Math.round(performance.now() - state.askedAt) : null;
  if (state.lastAudio) { try { state.lastAudio.pause(); } catch (_) {} }
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();

  const p = await postJSON("/api/answer", {
    interview_id: state.interviewId,
    questionnaire: state.questionnaire,
    language: state.language,
    text, dtmf, audio_b64, audio_ext,
    duration_ms: duration,
  });

  if (prev && prev.kind === "question") {
    state.history.push({
      step: prev.step_id,
      transcript: p.transcript || text || null,
      asr: p.asr_confidence,
      code: dtmf ? `touche ${dtmf}` : (p.step_id === prev.step_id ? "non compris" : "codé"),
      method: dtmf ? "clavier" : (audio_b64 ? "voix" : "saisie"),
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

async function toggleMic() {
  const btn = $("btn-mic");
  if (state.recorder && state.recorder.state === "recording") {
    state.recorder.stop();
    return;
  }
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
      $("mic-label").textContent = "Répondre à la voix";
      const blob = new Blob(state.chunks, { type: "audio/webm" });
      const b64 = await blobToBase64(blob);
      submit({ audio_b64: b64, audio_ext: "webm" });
    };
    rec.start();
    btn.classList.add("rec");
    $("mic-label").textContent = "Arrêter et envoyer";
  } catch (err) {
    $("mic-hint").textContent =
      "Micro indisponible : " + err.message + " — utilisez le clavier ou la saisie.";
  }
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
$("btn-replay").onclick = () => state.step && speak(state.step);
$("btn-withdraw").onclick = withdraw;
loadCaps();
