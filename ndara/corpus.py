"""Corpus vocal : ce qui s'accumule à chaque appel.

Trois principes non négociables, implémentés ici et pas seulement affichés :

1. **Aucun octet d'audio n'est écrit sur le disque sans consentement explicite
   et distinct** (``consent_corpus == granted``). Le refus n'a aucune autre
   conséquence : la personne participe et reçoit la même incitation.
2. **Aucune donnée identifiante** : le numéro est haché en amont, la
   transcription est expurgée (``redact_text``) avant tout stockage.
3. **Droit de retrait effectif** : un code permet d'effacer les fichiers ET
   les lignes de manifeste. Un droit qu'on ne peut pas exercer n'existe pas.

Le corpus n'est pas un produit : il est destiné à être publié en bien commun,
copublié avec l'institution partenaire du pays.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import CorpusItem, Interview, Turn, new_id

# --------------------------------------------------------------------------
# Expurgation
# --------------------------------------------------------------------------

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("TEL", re.compile(r"(?<!\d)(?:\+?\d[\s.\-]?){6,}\d(?!\d)")),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("NOM", re.compile(
        r"(?:je m'?appelle|mon nom est|c'est|my name is|ខ្ញុំឈ្មោះ)\s+([^\s,.;]+(?:\s+[^\s,.;]+)?)",
        re.IGNORECASE)),
]


def redact_text(text: str) -> str:
    """Remplace les identifiants directs par des marqueurs, avant tout stockage.

    Le seuil sur les chiffres est à 7 chiffres consécutifs : un prix
    (« 1500 ») passe, un numéro de téléphone (9 chiffres au Cameroun) non.
    """
    if not text:
        return ""
    out = text
    for tag, pattern in _PATTERNS:
        if tag == "NOM":
            out = pattern.sub(lambda m: m.group(0).replace(m.group(1), f"[{tag}]"), out)
        else:
            out = pattern.sub(f"[{tag}]", out)
    return out


def count_redactions(original: str, redacted: str) -> int:
    return len(re.findall(r"\[(?:TEL|EMAIL|NOM)\]", redacted or ""))


# --------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------

class CorpusWriter:
    def __init__(self, store, root: str | Path = "data/corpus") -> None:
        self.store = store
        self.root = Path(root)
        (self.root / "audio").mkdir(parents=True, exist_ok=True)

    # -- audio --

    def store_audio(self, iv: Interview, step, audio_bytes: bytes, ext: str = "webm") -> str | None:
        """Écrit le fichier audio. Renvoie None si le consentement n'est pas accordé."""
        from .models import Consent

        if iv.consent_corpus != Consent.GRANTED.value:
            return None
        if not getattr(step, "corpus_eligible", True):
            return None
        d = self.root / "audio" / iv.language
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{iv.id}_{step.id}.{ext}"
        path.write_bytes(audio_bytes)
        return str(path.as_posix())

    # -- manifeste --

    def register(self, iv: Interview, step, turn: Turn) -> CorpusItem | None:
        from .models import Consent

        if iv.consent_corpus != Consent.GRANTED.value or not turn.audio_path:
            return None
        demo = self._demographics(iv)
        transcript = redact_text(turn.raw_text or "")
        item = CorpusItem(
            id=new_id("cx"),
            interview_id=iv.id,
            respondent_hash=iv.respondent_hash,
            language=iv.language,
            step_id=step.id,
            audio_path=turn.audio_path,
            duration_ms=int(turn.duration_ms or 0),
            transcript=transcript,
            stratum=iv.stratum,
            weight=iv.weight,
            demographics=demo,
            consent_version=iv.consent_version,
            redactions=count_redactions(turn.raw_text or "", transcript),
        )
        self.store.save_corpus_item(item)
        return item

    def _demographics(self, iv: Interview) -> dict[str, Any]:
        """Variables grossières uniquement : région, sexe, tranche d'âge."""
        keep = {"region", "sex", "age_group"}
        out: dict[str, Any] = {}
        for t in self.store.turns(iv.id):
            if t.step_id in keep and t.code and not t.code.startswith("__"):
                out[t.step_id] = t.code
        return out

    # -- retrait --

    def withdraw(self, respondent_hash: str) -> int:
        items = self.store.corpus_items(respondent_hash)
        for it in items:
            p = Path(it["audio_path"])
            if p.exists():
                p.unlink()
        return self.store.delete_corpus_for(respondent_hash)

    # -- lecture --

    def stats(self) -> dict[str, Any]:
        items = self.store.corpus_items()
        total_ms = sum(i["duration_ms"] for i in items)
        by_lang: dict[str, dict[str, Any]] = {}
        for i in items:
            b = by_lang.setdefault(i["language"], {"segments": 0, "ms": 0})
            b["segments"] += 1
            b["ms"] += i["duration_ms"]
        for b in by_lang.values():
            b["minutes"] = round(b["ms"] / 60000, 2)
        return {
            "segments": len(items),
            "minutes": round(total_ms / 60000, 2),
            "redactions": sum(i["redactions"] for i in items),
            "by_language": by_lang,
            "speakers": len({i["respondent_hash"] for i in items}),
        }

    def export_manifest(self, out_dir: str | Path = "data/corpus/export") -> dict[str, Any]:
        """Produit le manifeste JSONL + la fiche descriptive (datasheet)."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        items = self.store.corpus_items()
        manifest = out / "manifest.jsonl"
        with manifest.open("w", encoding="utf-8") as fh:
            for it in items:
                fh.write(json.dumps({
                    "id": it["id"],
                    "audio": it["audio_path"],
                    "language": it["language"],
                    "transcript": it["transcript"],
                    "duration_ms": it["duration_ms"],
                    "stratum": it["stratum"],
                    "weight": it["weight"],
                    "demographics": it["demographics"],
                    "consent_version": it["consent_version"],
                    "licence": it["licence"],
                }, ensure_ascii=False) + "\n")
        stats = self.stats()
        (out / "DATASHEET.md").write_text(_datasheet(stats), encoding="utf-8")
        return {"manifest": str(manifest), **stats}


def _datasheet(stats: dict[str, Any]) -> str:
    return f"""# Corpus vocal NDARA — fiche descriptive

**Ce corpus n'est pas vendu.** Il est destiné à être publié sous licence ouverte
(CC-BY-4.0), copublié avec l'institution partenaire du pays d'enquête.

## Ce qui le distingue

Les corpus vocaux ouverts existants reposent sur des **contributeurs volontaires** :
urbains, jeunes, scolarisés, équipés d'un smartphone. Les locuteurs de ce corpus-ci
sont **tirés au sort** dans une base de sondage et chaque segment porte la
**pondération** qui dit quelle part de la population il représente.

## Contenu à ce jour

- Segments : {stats['segments']}
- Durée totale : {stats['minutes']} minutes
- Locuteurs distincts : {stats['speakers']}
- Expurgations appliquées (identifiants retirés) : {stats['redactions']}
- Par langue : {json.dumps(stats['by_language'], ensure_ascii=False)}

## Consentement

Chaque segment provient d'une personne ayant accordé un consentement **distinct**
de celui de l'enquête, explicitement refusable sans perte de l'incitation.
Version du protocole de consentement enregistrée avec chaque segment.

## Vie privée

- Numéros de téléphone : jamais stockés (empreinte HMAC-SHA256 salée).
- Transcriptions : expurgées automatiquement (téléphone, courriel, nom déclaré).
- Démographie : région, sexe, tranche d'âge uniquement — jamais d'identifiant fin.
- Droit de retrait : effacement des fichiers et du manifeste sur présentation du code.

## Limites connues — à lire avant usage

- Parole **téléphonique** : bande passante réduite (8 kHz), bruit de ligne.
- Réponses **courtes et guidées** par un questionnaire : ce corpus ne convient pas
  à l'entraînement d'un modèle de parole spontanée longue.
- La pondération corrige la structure démographique, **pas** le biais de couverture
  des personnes sans téléphone.
"""
