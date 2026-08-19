"""Plan de sondage et taux de réponse.

Point de méthode qu'un développeur ne connaît pas et qu'un jury de
statisticiens vérifiera : **au Cameroun comme au Cambodge, les préfixes
mobiles ne sont pas géographiques.** On ne peut donc pas stratifier
géographiquement une base RDD mobile.

Conséquence assumée dans le code :
    * les strates sont les **opérateurs** (allocation proportionnelle à leur
      part de marché) ;
    * la **région est une question de filtrage** posée en début d'entretien ;
    * la représentativité géographique est rétablie **a posteriori** par
      calage sur marges (voir ``weighting.py``).

Aucune donnée d'abonné n'est utilisée : on tire des numéros au hasard dans
les plages de numérotation publiées par le régulateur. C'est la réponse à la
question « vous exploitez le fichier client de l'opérateur ? » — non.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import Disposition, SampleUnit, hash_msisdn, new_id

# --------------------------------------------------------------------------
# Plages de numérotation
# --------------------------------------------------------------------------
# ⚠️ À VÉRIFIER auprès du régulateur avant toute collecte réelle
#    (ART au Cameroun, TRC au Cambodge). Les parts de marché servent à
#    l'allocation des strates et doivent être sourcées dans le dossier.

NUMBERING_PLANS: dict[str, dict[str, Any]] = {
    "CM": {
        "country_code": "237",
        "national_length": 9,
        "strata": {
            "MTN": {"prefixes": ["650", "651", "652", "653", "654", "67", "680"],
                    "market_share": 0.45},
            "ORANGE": {"prefixes": ["655", "656", "657", "658", "659", "69"],
                       "market_share": 0.42},
            "CAMTEL": {"prefixes": ["620", "621", "622"], "market_share": 0.13},
        },
    },
    "KH": {
        "country_code": "855",
        "national_length": 9,
        "strata": {
            "METFONE": {"prefixes": ["88", "97", "71", "60"], "market_share": 0.40},
            "SMART": {"prefixes": ["10", "15", "16", "69", "70", "81", "86", "93", "96", "98"],
                      "market_share": 0.40},
            "CELLCARD": {"prefixes": ["11", "12", "14", "17", "61", "76", "77", "78", "85", "89", "92", "95", "99"],
                         "market_share": 0.20},
        },
    },
}


@dataclass
class DrawnNumber:
    """Un numéro tiré. Le clair ne sort jamais de la mémoire du composeur."""

    msisdn: str
    msisdn_hash: str
    stratum: str
    country: str


def draw_frame(country: str, n: int, seed: int | None = None) -> list[DrawnNumber]:
    """Tire ``n`` numéros par composition aléatoire, alloués par part de marché."""
    plan = NUMBERING_PLANS[country]
    rng = random.Random(seed)
    total_len = plan["national_length"]
    out: list[DrawnNumber] = []
    strata = plan["strata"]
    # Allocation proportionnelle, avec ajustement du reste sur la plus grande strate.
    alloc = {k: int(round(n * v["market_share"])) for k, v in strata.items()}
    biggest = max(strata, key=lambda k: strata[k]["market_share"])
    alloc[biggest] += n - sum(alloc.values())
    for stratum, count in alloc.items():
        prefixes = strata[stratum]["prefixes"]
        for _ in range(max(0, count)):
            pref = rng.choice(prefixes)
            rest = total_len - len(pref)
            number = pref + "".join(str(rng.randint(0, 9)) for _ in range(rest))
            out.append(DrawnNumber(msisdn=number, msisdn_hash=hash_msisdn(number),
                                   stratum=stratum, country=country))
    rng.shuffle(out)
    return out


def to_sample_units(drawn: Iterable[DrawnNumber]) -> list[SampleUnit]:
    return [SampleUnit(id=new_id("su"), msisdn_hash=d.msisdn_hash, stratum=d.stratum,
                       country=d.country) for d in drawn]


# --------------------------------------------------------------------------
# Taux de réponse
# --------------------------------------------------------------------------

@dataclass
class OutcomeCounts:
    complete: int = 0       # I
    partial: int = 0        # P
    refusal: int = 0        # R
    noncontact: int = 0     # NC
    other: int = 0          # O (abandon en cours)
    ineligible: int = 0     # non éligible
    unknown: int = 0        # UH + UO

    @property
    def eligible_known(self) -> int:
        return self.complete + self.partial + self.refusal + self.noncontact + self.other

    def eligibility_rate(self) -> float:
        """Méthode d'allocation proportionnelle (« e » d'AAPOR)."""
        denom = self.eligible_known + self.ineligible
        return (self.eligible_known / denom) if denom else 1.0

    def rr2(self) -> float:
        """Taux de réponse en supposant tous les inconnus éligibles (borne basse)."""
        denom = self.eligible_known + self.unknown
        return (self.complete + self.partial) / denom if denom else 0.0

    def rr3(self) -> float:
        """Taux de réponse avec estimation de l'éligibilité des inconnus."""
        e = self.eligibility_rate()
        denom = self.eligible_known + e * self.unknown
        return (self.complete + self.partial) / denom if denom else 0.0

    def cooperation(self) -> float:
        """Taux de coopération : parmi les personnes effectivement jointes."""
        denom = self.complete + self.partial + self.refusal
        return (self.complete + self.partial) / denom if denom else 0.0

    def contact(self) -> float:
        e = self.eligibility_rate()
        denom = self.eligible_known + e * self.unknown
        contacted = self.complete + self.partial + self.refusal + self.other
        return contacted / denom if denom else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": {
                "complete": self.complete, "partial": self.partial,
                "refusal": self.refusal, "noncontact": self.noncontact,
                "other": self.other, "ineligible": self.ineligible,
                "unknown": self.unknown,
            },
            "eligibility_rate_e": round(self.eligibility_rate(), 4),
            "response_rate_rr2": round(self.rr2(), 4),
            "response_rate_rr3": round(self.rr3(), 4),
            "cooperation_rate": round(self.cooperation(), 4),
            "contact_rate": round(self.contact(), 4),
        }


_DISPO_MAP = {
    Disposition.COMPLETE.value: "complete",
    Disposition.PARTIAL.value: "partial",
    Disposition.REFUSAL.value: "refusal",
    Disposition.NONCONTACT.value: "noncontact",
    Disposition.BREAKOFF.value: "other",
    Disposition.INELIGIBLE.value: "ineligible",
    Disposition.UNKNOWN_ELIGIBLE.value: "unknown",
    Disposition.IN_PROGRESS.value: "unknown",
}


def outcomes_from_units(units: Iterable[SampleUnit]) -> OutcomeCounts:
    c = OutcomeCounts()
    for u in units:
        field = _DISPO_MAP.get(u.disposition, "unknown")
        setattr(c, field, getattr(c, field) + 1)
    return c


def outcomes_from_interviews(interviews: Iterable[Any]) -> OutcomeCounts:
    c = OutcomeCounts()
    for iv in interviews:
        field = _DISPO_MAP.get(iv.disposition, "unknown")
        setattr(c, field, getattr(c, field) + 1)
    return c


def load_margins(path: str | Path) -> dict[str, dict[str, float]]:
    """Charge les marges de calage (proportions par modalité)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}
