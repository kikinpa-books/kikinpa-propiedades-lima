"""
tools/run_all.py
────────────────
Runner principal: ejecuta todos los scrapers, une los resultados,
deduplica y exporta JSON + CSV.

Uso:
    python tools/run_all.py

Variables de entorno opcionales (via .env):
    SKIP_BBVA=true      # omitir propiedadesenremate.pe
    SKIP_BANBIF=true    # omitir BanBif
    SKIP_REMAJU=true    # omitir REMAJU
    REMAJU_MAX=50       # maximo de items de REMAJU (default: 50)
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

# Cargar .env si existe
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OUTPUT_JSON = Path(__file__).parent.parent / "docs" / "data" / "propiedades.json"
OUTPUT_CSV  = Path(__file__).parent.parent / "remates_bbva.csv"
DB_PATH     = Path(os.getenv("DB_PATH", "propiedades.db"))

CSV_COLUMNS = [
    "fuente_origen", "titulo", "precio_original", "moneda", "precio_usd",
    "distrito", "direccion", "medidas_m2", "url_detalle",
    "estado_inmueble", "partida_registral", "fecha_remate", "es_oportunidad",
]

EXCHANGE_RATE = 3.75


# ── Fingerprint para deduplicacion ────────────────────────────────────────────

def fingerprint(r: dict) -> str:
    key = "|".join([
        str(r.get("fuente_origen", "")),
        str(r.get("url_detalle",  "")),
        str(r.get("titulo",       "")),
        str(r.get("distrito",     "")),
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ── Persistencia SQLite ────────────────────────────────────────────────────────

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS propiedades (
    fingerprint      TEXT PRIMARY KEY,
    fuente_origen    TEXT,
    titulo           TEXT,
    precio_usd       REAL,
    moneda           TEXT,
    precio_original  TEXT,
    distrito         TEXT,
    direccion        TEXT,
    medidas_m2       REAL,
    url_detalle      TEXT,
    estado_inmueble  TEXT,
    partida_registral TEXT,
    fecha_remate     TEXT,
    es_oportunidad   INTEGER,
    raw_json         TEXT,
    created_at       TEXT,
    updated_at       TEXT
);
"""

UPSERT_SQL = """
INSERT INTO propiedades VALUES (
    :fp,:fuente_origen,:titulo,:precio_usd,:moneda,:precio_original,
    :distrito,:direccion,:medidas_m2,:url_detalle,:estado_inmueble,
    :partida_registral,:fecha_remate,:es_oportunidad,:raw_json,:now,:now
)
ON CONFLICT(fingerprint) DO UPDATE SET
    precio_usd=excluded.precio_usd,
    estado_inmueble=excluded.estado_inmueble,
    raw_json=excluded.raw_json,
    updated_at=excluded.updated_at;
"""


def save_sqlite(records: list[dict]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    inserted = updated = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(CREATE_SQL)
        for r in records:
            fp = r.get("_fingerprint", "")
            exists = conn.execute(
                "SELECT 1 FROM propiedades WHERE fingerprint=?", (fp,)
            ).fetchone()
            conn.execute(UPSERT_SQL, {
                "fp": fp,
                "fuente_origen":    r.get("fuente_origen"),
                "titulo":           r.get("titulo"),
                "precio_usd":       r.get("precio_usd"),
                "moneda":           r.get("moneda"),
                "precio_original":  r.get("precio_original"),
                "distrito":         r.get("distrito"),
                "direccion":        r.get("direccion"),
                "medidas_m2":       r.get("medidas_m2"),
                "url_detalle":      r.get("url_detalle"),
                "estado_inmueble":  r.get("estado_inmueble"),
                "partida_registral":r.get("partida_registral"),
                "fecha_remate":     r.get("fecha_remate"),
                "es_oportunidad":   int(bool(r.get("es_oportunidad"))),
                "raw_json":         json.dumps(r, ensure_ascii=False),
                "now":              now,
            })
            if exists:
                updated += 1
            else:
                inserted += 1
        conn.commit()
    return {"inserted": inserted, "updated": updated}


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    t0 = time.time()
    all_records: list[dict] = []

    # ── BBVA (propiedadesenremate.pe) ─────────────────────────────────────────
    if os.getenv("SKIP_BBVA", "").lower() != "true":
        try:
            import sys, pathlib
            sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
            from tools.bbva_scraper import scrape_listing, scrape_detail, enrich
            from playwright.sync_api import sync_playwright
            import time as _time

            print("\n[BBVA] Iniciando...")
            UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(user_agent=UA, locale="es-PE")
                page = ctx.new_page()
                recs = scrape_listing(page)
                for i, rec in enumerate(recs):
                    url = rec.get("url_detalle", "")
                    print(f"  [{i+1}/{len(recs)}] {rec.get('titulo','')[:55]}")
                    rec.update(scrape_detail(page, url))
                    _time.sleep(0.8)
                browser.close()
            recs = enrich(recs)
            for r in recs:
                r.setdefault("fuente_origen", "BBVA")
            all_records.extend(recs)
            print(f"[BBVA] {len(recs)} propiedades.")
        except Exception as e:
            print(f"[BBVA] ERROR: {e}")

    # ── BanBif ────────────────────────────────────────────────────────────────
    if os.getenv("SKIP_BANBIF", "").lower() != "true":
        try:
            print("\n[BanBif] Iniciando...")
            import sys, pathlib
            sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
            from tools.banbif_scraper import scrape as banbif_scrape
            recs = banbif_scrape()
            all_records.extend(recs)
            print(f"[BanBif] {len(recs)} propiedades.")
        except Exception as e:
            print(f"[BanBif] ERROR: {e}")

    # ── REMAJU ────────────────────────────────────────────────────────────────
    if os.getenv("SKIP_REMAJU", "").lower() != "true":
        try:
            print("\n[REMAJU] Iniciando...")
            import sys, pathlib
            sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
            from tools.remaju_scraper import scrape as remaju_scrape
            max_r = int(os.getenv("REMAJU_MAX", "50"))
            recs = remaju_scrape(max_items=max_r)
            all_records.extend(recs)
            print(f"[REMAJU] {len(recs)} remates.")
        except Exception as e:
            print(f"[REMAJU] ERROR: {e}")

    if not all_records:
        print("\nNo se obtuvieron datos de ninguna fuente.")
        return

    # ── Deduplicacion ─────────────────────────────────────────────────────────
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in all_records:
        fp = fingerprint(r)
        if fp not in seen:
            seen.add(fp)
            r["_fingerprint"] = fp
            deduped.append(r)

    print(f"\nTotal: {len(all_records)} registros -> {len(deduped)} tras deduplicacion.")

    # ── SQLite ────────────────────────────────────────────────────────────────
    db_result = save_sqlite(deduped)
    print(f"SQLite: {db_result['inserted']} insertados, {db_result['updated']} actualizados.")

    # ── JSON (para el dashboard web) ──────────────────────────────────────────
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ultima_actualizacion": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(deduped),
        "propiedades": [{k: r.get(k) for k in CSV_COLUMNS} for r in deduped],
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"JSON: {OUTPUT_JSON}")

    # ── CSV ───────────────────────────────────────────────────────────────────
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(deduped)
    print(f"CSV:  {OUTPUT_CSV}")

    # ── Resumen ───────────────────────────────────────────────────────────────
    by_source = {}
    for r in deduped:
        s = r.get("fuente_origen", "?")
        by_source[s] = by_source.get(s, 0) + 1

    oportunidades = sum(1 for r in deduped if r.get("es_oportunidad"))
    elapsed = round(time.time() - t0, 1)

    print(f"\n{'='*55}")
    print(f"Tiempo total     : {elapsed}s")
    print(f"Total propiedades: {len(deduped)}")
    print(f"Oportunidades    : {oportunidades}")
    print(f"Por fuente:")
    for src, cnt in sorted(by_source.items()):
        print(f"  {src:<20} {cnt}")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
