"""Sortie console lisible partout.

La console Windows utilise cp1252 par défaut : les accents et les flèches y
provoquent un ``UnicodeEncodeError`` qui fait planter un script par ailleurs
correct. On force UTF-8 une fois pour toutes — un outil destiné à Yaoundé,
Bangui et Phnom Penh doit s'afficher correctement sur la machine de terrain,
pas seulement sur celle du développeur.
"""
from __future__ import annotations

import sys


def setup() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
