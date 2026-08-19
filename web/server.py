"""Serveur NDARA — bibliothèque standard uniquement.

Pourquoi pas de framework : le dossier doit tourner sur n'importe quelle
machine, sans installation, sans réseau, y compris pour un évaluateur qui
clone le dépôt et lance une commande. Zéro dépendance = zéro excuse.

Deux surfaces :
  * ``/``           l'entretien, dans le navigateur — c'est ce que le jury utilise
  * ``/dashboard``  le tableau de bord : terrain, estimations, qualité, corpus

Et une surface dormante, prête pour l'opérateur :
  * ``/twiml/*``    traduction des invites en instructions téléphoniques
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ndara.console import setup as setup_console  # noqa: E402
from ndara.analysis import estimate_all  # noqa: E402
from ndara.audit import quality_report  # noqa: E402
from ndara.coding import default_coder  # noqa: E402
from ndara.corpus import CorpusWriter  # noqa: E402
from ndara.engine import InterviewEngine  # noqa: E402
from ndara.providers.asr import MockASR, default_asr  # noqa: E402
from ndara.providers.telephony import prompt_to_twiml  # noqa: E402
from ndara.questionnaire import Questionnaire  # noqa: E402
from ndara.sampling import load_margins  # noqa: E402
from ndara.storage import Store  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
AUDIO_ROOT = ROOT / "data" / "audio"


class App:
    """État partagé du serveur."""

    def __init__(self, db: str = "data/ndara.db") -> None:
        self.store = Store(ROOT / db)
        self.coder = default_coder()
        self.asr = default_asr()
        self.corpus = CorpusWriter(self.store, ROOT / "data" / "corpus")
        qdir = ROOT / "data" / "questionnaires"
        self.questionnaires = {
            p.stem: Questionnaire.load(p) for p in sorted(qdir.glob("*.json"))
        }
        self.engines = {
            qid: InterviewEngine(self.store, q, self.coder, self.corpus)
            for qid, q in self.questionnaires.items()
        }
        self.margins = {
            "prix_denrees_cm": load_margins(ROOT / "data" / "margins" / "cm_margins.json"),
        }

    @property
    def default_qid(self) -> str:
        return "prix_denrees_cm" if "prix_denrees_cm" in self.questionnaires \
            else next(iter(self.questionnaires))

    def capabilities(self) -> dict:
        """Ce qui est réellement branché. Affiché tel quel dans l'interface :
        un évaluateur doit voir sans ambiguïté ce qui tourne et ce qui est simulé."""
        return {
            "asr": self.asr.name,
            "asr_live": not isinstance(self.asr, MockASR),
            "coder": self.coder.name,
            "telephony": os.environ.get("TWILIO_ACCOUNT_SID") is not None,
            "questionnaires": [
                {"id": qid, "languages": q.languages, "country": q.country,
                 "version": q.version, "steps": len(q.steps),
                 "draft": q.version.endswith("draft")}
                for qid, q in self.questionnaires.items()
            ],
        }


APP: App | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "NDARA/0.1"

    # ---------------- utilitaires ----------------

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return dict(urllib.parse.parse_qsl(raw.decode("utf-8")))

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype)

    def log_message(self, fmt: str, *args) -> None:  # silence
        pass

    # ---------------- GET ----------------

    def do_GET(self) -> None:
        assert APP is not None
        parsed = urllib.parse.urlparse(self.path)
        route, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            return self._serve_file(STATIC / "index.html")
        if route == "/dashboard":
            return self._serve_file(STATIC / "dashboard.html")
        if route.startswith("/static/"):
            return self._serve_file(STATIC / route[len("/static/"):])
        if route.startswith("/audio/"):
            return self._serve_file(AUDIO_ROOT / route[len("/audio/"):])

        if route in ("/health", "/healthz"):
            # Les hébergeurs sondent cette route. Elle sert aussi de diagnostic
            # à distance : elle dit ce qui est branché sans ouvrir l'interface.
            caps = APP.capabilities()
            return self._json({
                "ok": True,
                "asr": caps["asr"],
                "asr_live": caps["asr_live"],
                "coder": caps["coder"],
                "telephony": caps["telephony"],
                "questionnaires": [q["id"] for q in caps["questionnaires"]],
            })

        if route == "/api/capabilities":
            return self._json(APP.capabilities())

        if route == "/api/dashboard":
            qid = (qs.get("questionnaire") or [APP.default_qid])[0]
            q = APP.questionnaires[qid]
            margins = APP.margins.get(qid, {})
            data = estimate_all(APP.store, q, margins)
            data["corpus"] = APP.corpus.stats()
            data["capabilities"] = APP.capabilities()
            return self._json(data)

        if route == "/api/quality":
            qid = (qs.get("questionnaire") or [APP.default_qid])[0]
            q = APP.questionnaires[qid]
            ivs = APP.store.interviews()
            turns = {iv.id: APP.store.turns(iv.id) for iv in ivs}
            return self._json(quality_report(q, ivs, turns))

        if route == "/api/corpus":
            return self._json(APP.corpus.stats())

        return self._send(404, b"not found", "text/plain")

    # ---------------- POST ----------------

    def do_POST(self) -> None:
        assert APP is not None
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route == "/api/start":
            body = self._read_json()
            qid = body.get("questionnaire") or APP.default_qid
            engine = APP.engines[qid]
            prompt = engine.start(
                language=body.get("language") or engine.q.languages[0],
                stratum=body.get("stratum") or "WEB",
                channel=body.get("channel") or "web",
            )
            out = prompt.to_dict()
            out["questionnaire"] = qid
            return self._json(out)

        if route == "/api/answer":
            body = self._read_json()
            qid = body.get("questionnaire") or APP.default_qid
            engine = APP.engines[qid]
            audio_bytes = None
            transcript = body.get("text")
            asr_conf = None
            if body.get("audio_b64"):
                audio_bytes = base64.b64decode(body["audio_b64"])
                transcript, asr_conf = APP.asr.transcribe(
                    audio_bytes, body.get("language") or "fr",
                    body.get("audio_ext") or "webm")
            try:
                prompt = engine.submit(
                    body["interview_id"],
                    text=transcript,
                    dtmf=body.get("dtmf"),
                    audio_bytes=audio_bytes,
                    audio_ext=body.get("audio_ext") or "webm",
                    asr_confidence=asr_conf,
                    duration_ms=body.get("duration_ms"),
                )
            except KeyError as exc:
                return self._json({"error": str(exc)}, 404)
            out = prompt.to_dict()
            out["questionnaire"] = qid
            out["transcript"] = transcript
            out["asr_confidence"] = asr_conf
            return self._json(out)

        if route == "/api/withdraw":
            body = self._read_json()
            engine = APP.engines[APP.default_qid]
            return self._json(engine.withdraw((body.get("code") or "").strip().upper()))

        # ---- surface téléphonique (dormante tant qu'aucun opérateur n'est branché) ----

        if route.startswith("/twiml/"):
            return self._handle_twiml(route, parsed)

        return self._send(404, b"not found", "text/plain")

    def _handle_twiml(self, route: str, parsed) -> None:
        assert APP is not None
        qs = urllib.parse.parse_qs(parsed.query)
        form = self._read_form()
        qid = (qs.get("questionnaire") or [APP.default_qid])[0]
        engine = APP.engines[qid]
        base = os.environ.get("NDARA_PUBLIC_URL", "").rstrip("/")

        if route == "/twiml/start":
            prompt = engine.start(language=(qs.get("lang") or ["fr"])[0],
                                  stratum=(qs.get("stratum") or ["MTN"])[0],
                                  channel="phone",
                                  msisdn=form.get("To"))
            action = f"{base}/twiml/step?interview_id={prompt.interview_id}&questionnaire={qid}"
            xml = prompt_to_twiml(prompt.to_dict(), action_url=action, audio_base=base)
            return self._send(200, xml.encode("utf-8"), "text/xml")

        if route == "/twiml/step":
            interview_id = (qs.get("interview_id") or [""])[0]
            prompt = engine.submit(
                interview_id,
                text=form.get("SpeechResult"),
                dtmf=form.get("Digits"),
                duration_ms=int(float(form.get("RecordingDuration", 0)) * 1000) or None,
            )
            action = f"{base}/twiml/step?interview_id={interview_id}&questionnaire={qid}"
            xml = prompt_to_twiml(prompt.to_dict(), action_url=action, audio_base=base)
            return self._send(200, xml.encode("utf-8"), "text/xml")

        if route == "/twiml/status":
            APP.store.log("telephony_status", (qs.get("interview_id") or [None])[0], **form)
            return self._send(200, b"", "text/plain")

        return self._send(404, b"not found", "text/plain")


def main() -> None:
    setup_console()
    global APP
    ap = argparse.ArgumentParser(description="Serveur NDARA")
    # Un hébergeur impose son port et écoute sur toutes les interfaces. Sans
    # cette lecture d'environnement, le service démarre et reste injoignable.
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--db", default="data/ndara.db")
    args = ap.parse_args()

    APP = App(args.db)
    caps = APP.capabilities()
    print("NDARA — serveur d'entretien")
    print(f"  entretien       http://{args.host}:{args.port}/")
    print(f"  tableau de bord http://{args.host}:{args.port}/dashboard")
    print(f"  transcription   {caps['asr']} ({'branchée' if caps['asr_live'] else 'non branchée → saisie/clavier'})")
    print(f"  codage          {caps['coder']}")
    print(f"  questionnaires  {', '.join(q['id'] for q in caps['questionnaires'])}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
