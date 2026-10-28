"""Répartition temporelle des événements mesurés (démo).

Les valeurs (tokens, latences) restent **réelles** : seul l'horodatage est
redistribué, exactement comme le fait la démo. Le but est que les surfaces du
dashboard (jour × heure) et les courbes journalières aient une vraie
profondeur temporelle au lieu d'un seul tas d'événements.

Deux profils déterministes (aucun hasard non reproductible) :

* **surface** : 7 jours × 24 heures avec un pic d'activité en journée —
  c'est la « montagne » en 3D et la carte topographique ;
* **courbe** : 14 jours avec une croissance douce — c'est la courbe des
  tokens économisés et la jauge d'économie.

Lancement : ``.venv\\Scripts\\python.exe tools\\spread_demo.py``
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEMO_DB = PROJECT / "var" / "demo.db"

#: Poids horaires (24 valeurs) : nuit calme, montée le matin, pic l'après-midi.
HOUR_WEIGHTS = (2, 1, 1, 1, 2, 4, 9, 20, 38, 55, 68, 78,
                85, 95, 100, 96, 88, 84, 80, 72, 58, 40, 22, 8)

#: Poids journaliers (14 jours) : semaine chargée, week-end creux, et une
#: légère croissance vers aujourd'hui. C'est ce qui donne au « volcan » des
#: reliefs réels plutôt qu'un plat curtain.
#:
#: L'ordre est **chronologique** : indice 0 = il y a 13 jours, indice 13 =
#: aujourd'hui. (Le tirage est ensuite inversé pour obtenir un décalage en
#: jours, car ``_stamp`` compte les jours en arrière.)
DAY_WEIGHTS = (
    110, 104, 78, 68, 118, 112, 116,   # J-13 → J-7  (jeu, ven, sam, dim…)
    112, 108, 80, 70, 120, 126, 118,   # J-6  → J-0
)

#: Nombre premier : garantit une répartition sans diagonale parasite.
STEP = 104729


def _cumulative(weights: tuple[int, ...]) -> list[int]:
    total = 0
    out: list[int] = []
    for weight in weights:
        total += weight
        out.append(total)
    return out


def _pick(position: int, cumulative: list[int]) -> int:
    """Index stable d'un élément, proportionnel à son poids."""
    slot = (position * STEP) % cumulative[-1]
    low, high = 0, len(cumulative) - 1
    while low < high:
        mid = (low + high) // 2
        if slot < cumulative[mid]:
            high = mid
        else:
            low = mid + 1
    return low


def _stamp(now: datetime, day_offset: int, hour: int, minute: int) -> str:
    """Horodatage réel (jamais dans le futur)."""
    moment = (now - timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    if moment > now:
        moment -= timedelta(days=1)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _hour_for(day_offset: int, hour: int, now: datetime) -> int:
    """Heure réelle pour un jour donné.

    Aujourd'hui, seules les heures **écoulées** existent : on comprime donc
    la plage horaire sur la portion déjà vécue au lieu de pushed les événements
    dans le futur — ce qui ferait déborder la veille et.Parallelement, la
    courbe journalière garde le même poids qu'un jour complet.
    """
    if day_offset > 0:
        return hour
    elapsed = now.hour
    if elapsed <= 0:
        return 0
    return min(elapsed, int(round(hour * elapsed / 23.0)))


def spread(db_path: Path = DEMO_DB) -> dict[str, int]:
    """Redistribue les horodatages de tous les événements mesurés."""
    if not db_path.exists():
        raise SystemExit(f"base absente : {db_path}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    hour_cum = _cumulative(HOUR_WEIGHTS)
    day_cum = _cumulative(DAY_WEIGHTS)
    counts: dict[str, int] = {}

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()

        # -- surface 3D : 14 jours × 24 heures, profil horaire en journée ----
        # Le jour et l'heure sont tirés par deux permutations différentes du
        # PRNG : chaque case (jour, heure) reçoit weight_day × weight_hour,
        # ce qui donne un relief varié au lieu d'un plateau uniforme.
        cur.execute("SELECT id FROM llm_events ORDER BY id")
        llm_ids = [row[0] for row in cur.fetchall()]
        for position, event_id in enumerate(llm_ids):
            hour = _pick(position * 7 + 3, hour_cum)
            # DAY_WEIGHTS est chronologique : on inverse pour obtenir un
            # décalage « il y a N jours » (0 = aujourd'hui).
            day = (len(DAY_WEIGHTS) - 1) - _pick(position, day_cum)
            hour = _hour_for(day, hour, now)
            cur.execute("UPDATE llm_events SET ts = ? WHERE id = ?",
                        (_stamp(now, day, hour, (position * 13) % 60), event_id))
        counts["llm_events"] = len(llm_ids)

        # -- courbe 14 jours : croissance douce -----------------------------
        cur.execute("SELECT id FROM ledger_entries ORDER BY id")
        ledger_ids = [row[0] for row in cur.fetchall()]
        for position, entry_id in enumerate(ledger_ids):
            day = (len(DAY_WEIGHTS) - 1) - _pick(position, day_cum)
            hour = _hour_for(day, 9 + (position * 5) % 12, now)
            cur.execute("UPDATE ledger_entries SET ts = ? WHERE id = ?",
                        (_stamp(now, day, hour, (position * 17) % 60),
                         entry_id))
        counts["ledger_entries"] = len(ledger_ids)

        # -- tax d'outils : 24 dernières heures ------------------------------
        cur.execute("SELECT id FROM tool_events ORDER BY id")
        tool_ids = [row[0] for row in cur.fetchall()]
        for position, event_id in enumerate(tool_ids):
            hour = _hour_for(position % 2, _pick(position, hour_cum), now)
            cur.execute("UPDATE tool_events SET ts = ? WHERE id = ?",
                        (_stamp(now, position % 2, hour, (position * 7) % 60), event_id))
        counts["tool_events"] = len(tool_ids)

        # -- contexte : 14 jours --------------------------------------------
        cur.execute("SELECT id FROM context_sources ORDER BY id")
        source_ids = [row[0] for row in cur.fetchall()]
        for position, source_id in enumerate(source_ids):
            day = (len(DAY_WEIGHTS) - 1) - _pick(position, day_cum)
            moment = now - timedelta(days=day)
            cur.execute("UPDATE context_sources SET created_at = ? WHERE id = ?",
                        (moment.timestamp(), source_id))
        counts["context_sources"] = len(source_ids)

        # -- cache : 12 dernières heures --------------------------------------
        cur.execute("SELECT key FROM cache_entries ORDER BY created_at")
        cache_keys = [row[0] for row in cur.fetchall()]
        for position, key in enumerate(cache_keys):
            moment = now - timedelta(hours=position % 12)
            cur.execute("UPDATE cache_entries SET created_at = ?, last_accessed = ?"
                        " WHERE key = ?",
                        (moment.strftime("%Y-%m-%d %H:%M:%S"),
                         (moment - timedelta(hours=position % 3)).strftime("%Y-%m-%d %H:%M:%S"),
                         key))
        counts["cache_entries"] = len(cache_keys)

        conn.commit()
    finally:
        conn.close()
    return counts


def main() -> int:
    started = time.time()
    counts = spread()
    print(f"base            : {DEMO_DB}")
    for table, count in counts.items():
        print(f"  {table:<18}: {count} événements répartis")
    print(f"durée           : {time.time() - started:.1f} s")
    print("les valeurs (tokens, latences) n'ont pas été touchées :")
    print("seul l'horodatage a été redistribué.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
