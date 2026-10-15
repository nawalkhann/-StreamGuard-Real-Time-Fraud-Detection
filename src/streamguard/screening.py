"""Sanctions / counter-terrorism-financing name screening.

Loads public designation lists you download yourself (OFAC SDN, UN Security Council consolidated list) or any
custom CSV, and screens counterparty names. This produces *candidate matches for human review*, never a legal
determination: confirm hits against the source list and follow your institution's blocking/reporting procedure.
"""
from __future__ import annotations

import csv
import logging
import re
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from defusedxml import ElementTree as ET

log = logging.getLogger(__name__)

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_STOP = {"the", "of", "al", "el", "bin", "ibn", "abu", "and", "co", "ltd", "llc", "inc", "limited", "company"}


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join(_PUNCT.sub(" ", s).split())


def _tokens(norm: str) -> list[str]:
    return [t for t in norm.split() if t not in _STOP and len(t) > 1]


@dataclass(frozen=True, slots=True)
class Entry:
    uid: str
    list_name: str
    name: str  # original display name
    norm: str
    sorted_norm: str


@dataclass(frozen=True, slots=True)
class Match:
    entry: Entry
    score: float
    exact: bool


class Screener:
    def __init__(self, fuzzy_threshold: float = 0.90):
        self.threshold = fuzzy_threshold
        self.entries: list[Entry] = []
        self._by_norm: dict[str, list[int]] = defaultdict(list)
        self._by_token: dict[str, list[int]] = defaultdict(list)
        self.sources: dict[str, dict] = {}

    def __len__(self) -> int:
        return len(self.entries)

    def add(self, uid: str, list_name: str, name: str) -> None:
        norm = normalize(name)
        toks = _tokens(norm)
        if not toks:
            return
        e = Entry(uid, list_name, name.strip(), norm, " ".join(sorted(toks)))
        i = len(self.entries)
        self.entries.append(e)
        self._by_norm[norm].append(i)
        for t in set(toks):
            self._by_token[t].append(i)

    def screen(self, name: str | None) -> Match | None:
        """Best match for ``name`` or None. Exact (normalised) matches win; fuzzy needs >= 2 tokens."""
        if not name:
            return None
        norm = normalize(name)
        if not norm:
            return None
        if norm in self._by_norm:
            return Match(self.entries[self._by_norm[norm][0]], 1.0, True)
        toks = _tokens(norm)
        if len(toks) < 2:
            return None  # single-word fuzzy matching is far too noisy
        sorted_norm = " ".join(sorted(toks))
        cands: set[int] = set()
        for t in toks:
            ids = self._by_token.get(t)
            if ids and len(ids) <= 2000:  # skip pathologically common tokens
                cands.update(ids)
        best: Match | None = None
        for i in cands:
            e = self.entries[i]
            if len(e.sorted_norm.split()) < 2:
                continue
            # token-sorted comparison tolerates word order ("DOE JOHN" vs "John Doe") and minor spelling variants
            score = SequenceMatcher(None, sorted_norm, e.sorted_norm).ratio()
            if score >= self.threshold and (best is None or score > best.score):
                best = Match(e, round(score, 3), False)
        return best

    # ---- loading -------------------------------------------------------------------------
    @classmethod
    def from_dir(cls, directory: str | Path, fuzzy_threshold: float = 0.90) -> "Screener":
        """Auto-detect ``sdn.csv`` (+ ``alt.csv``), ``consolidated.xml`` and ``custom*.csv`` in a directory."""
        sc = cls(fuzzy_threshold)
        d = Path(directory)
        if not d.is_dir():
            return sc
        loaders = []
        if (d / "sdn.csv").exists():
            loaders.append(("OFAC_SDN", d / "sdn.csv", lambda s, p=d: load_ofac_sdn(s, p / "sdn.csv", p / "alt.csv")))
        if (d / "consolidated.xml").exists():
            loaders.append(("UN_CONSOLIDATED", d / "consolidated.xml", lambda s, p=d: load_un_xml(s, p / "consolidated.xml")))
        for f in sorted(d.glob("custom*.csv")):
            loaders.append((f"CUSTOM:{f.stem}", f, lambda s, f=f: load_generic_csv(s, f, f"CUSTOM:{f.stem}")))
        for label, path, fn in loaders:
            before = len(sc)
            try:
                fn(sc)
            except Exception:
                log.exception("failed to load sanctions list %s", path)
                continue
            sc.sources[label] = {"entries": len(sc) - before, "file": path.name,
                                 "age_days": round((time.time() - path.stat().st_mtime) / 86400, 1)}
        return sc


def load_ofac_sdn(sc: Screener, sdn_csv: Path, alt_csv: Path | None = None) -> None:
    """OFAC SDN: header-less CSV. col0 ent_num, col1 name ("LAST, FIRST" for people), col2 type. alt.csv: ent_num, alt_num, type, alt_name."""
    def fix(name: str, typ: str) -> str:
        if typ.strip().lower() == "individual" and "," in name:
            last, _, first = name.partition(",")
            return f"{first.strip()} {last.strip()}"
        return name

    types: dict[str, str] = {}
    with open(sdn_csv, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) < 3 or not row[0].strip().isdigit():
                continue
            uid, name, typ = row[0].strip(), row[1].strip(), row[2].strip()
            types[uid] = typ
            sc.add(uid, "OFAC_SDN", fix(name, typ))
    if alt_csv and Path(alt_csv).exists():
        with open(alt_csv, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) >= 4 and row[0].strip().isdigit():
                    sc.add(row[0].strip(), "OFAC_SDN", fix(row[3].strip(), types.get(row[0].strip(), "")))


def load_un_xml(sc: Screener, path: Path) -> None:
    """UN Security Council consolidated list (XML). Individuals: FIRST..FOURTH_NAME; entities: FIRST_NAME; plus aliases."""
    root = ET.parse(str(path)).getroot()
    for tag, alias_tag in (("INDIVIDUAL", "INDIVIDUAL_ALIAS"), ("ENTITY", "ENTITY_ALIAS")):
        for node in root.iter(tag):
            uid = (node.findtext("REFERENCE_NUMBER") or node.findtext("DATAID") or "").strip()
            parts = [node.findtext(k) for k in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
            name = " ".join(p.strip() for p in parts if p and p.strip())
            if name:
                sc.add(uid, "UN_CONSOLIDATED", name)
            for a in node.findall(alias_tag):
                alias = (a.findtext("ALIAS_NAME") or "").strip()
                if alias:
                    sc.add(uid, "UN_CONSOLIDATED", alias)


def load_generic_csv(sc: Screener, path: Path, list_name: str) -> None:
    """Any list as CSV with header ``name`` and optional ``id`` / ``list`` columns (EU, OpenSanctions export, internal watchlist)."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if row.get("name"):
                sc.add(row.get("id") or str(i), row.get("list") or list_name, row["name"])
