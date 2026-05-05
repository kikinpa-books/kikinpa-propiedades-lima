"""
tools/bbva_scraper.py
─────────────────────
Extrae propiedades en remate de propiedadesenremate.pe (portal BBVA Peru).

Uso:
    python tools/bbva_scraper.py

Requisitos previos:
    pip install playwright beautifulsoup4 lxml
    playwright install chromium

Salida:
    remates_bbva.csv  (directorio raiz del repo)
"""

from __future__ import annotations

import csv
import random
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

# ── Configuracion ─────────────────────────────────────────────────────────────

LISTING_URL   = "https://www.propiedadesenremate.pe/propiedades/"
OUTPUT_CSV    = Path(__file__).parent.parent / "remates_bbva.csv"

EXCHANGE_RATE = 3.75  # S/ -> USD (actualizar segun necesidad)

DISTRITOS_OPORTUNIDAD = {"miraflores", "san isidro", "surco"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

TIMEOUT_MS = 25_000

OUTPUT_JSON   = Path(__file__).parent.parent / "docs" / "data" / "propiedades.json"

CSV_COLUMNS = [
    "titulo",
    "precio_original",
    "moneda",
    "precio_usd",
    "distrito",
    "direccion",
    "url_detalle",
    "estado_inmueble",
    "partida_registral",
    "es_oportunidad",
]


# ── Limpieza de precio ────────────────────────────────────────────────────────

def clean_price(raw: str) -> tuple[float | None, str]:
    """
    Convierte texto de precio a (precio_usd, moneda).

    "S/1,012,400"    -> (269973.33, "PEN")
    "USD 185,000"    -> (185000.0,  "USD")
    "$ 75.500,50"    -> (75500.5,   "USD")   # notacion europea
    """
    if not raw:
        return None, ""

    raw_upper = raw.upper()
    moneda = "PEN" if any(k in raw_upper for k in ("S/", "PEN", "SOL")) else "USD"

    cleaned = re.sub(r"[A-Za-z/\$\s]", "", raw).strip()

    # Notacion europea: 1.234.567,89
    if re.search(r"\d+\.\d{3},\d{2}$", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")

    try:
        amount = float(cleaned)
    except ValueError:
        return None, moneda

    precio_usd = round(amount / EXCHANGE_RATE, 2) if moneda == "PEN" else amount
    return precio_usd, moneda


# ── Parsear distrito de la direccion completa ─────────────────────────────────

def parse_district(address: str) -> str:
    """
    Extrae el distrito de la direccion formateada por Houzez.
    Formato: "Calle X, Urbanizacion, Distrito, Provincia, Region, Pais, CP"
    El distrito esta en la posicion 2 (indice 2).
    """
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        return parts[2]
    if len(parts) >= 2:
        return parts[1]
    return address.strip()


# ── Extraccion del listado ────────────────────────────────────────────────────

def scrape_listing(page: Page) -> list[dict]:
    """Navega al listado y extrae datos basicos de cada propiedad."""
    print(f"Navegando a {LISTING_URL} ...")
    page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    time.sleep(3)  # esperar que cargue JavaScript del listado

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    cards = soup.find_all("div", class_=lambda c: c and "item-listing-wrap" in c)
    print(f"  {len(cards)} propiedades encontradas en el listado.")

    if not cards:
        print(
            "\nAVISO: No se encontraron propiedades.\n"
            "El sitio puede haber cambiado su HTML.\n"
            "Abre " + LISTING_URL + " en Chrome -> click derecho -> Inspeccionar\n"
            "y actualiza el selector 'item-listing-wrap' en el script."
        )

    records: list[dict] = []

    for card in cards:
        # Precio
        price_el = card.find("span", class_="price")
        precio_original = price_el.get_text(strip=True) if price_el else ""

        # Titulo y URL: primer <a> con /propiedad/ en href que no sea "Detalles"
        title_link = next(
            (
                a for a in card.find_all("a", href=True)
                if "/propiedad/" in a.get("href", "")
                and a.get_text(strip=True)
                and "Detalles" not in a.get_text(strip=True)
            ),
            None,
        )
        titulo = title_link.get_text(strip=True) if title_link else "No especificado"
        url = title_link["href"] if title_link else ""

        # Direccion completa
        addr_el = card.find("address", class_=lambda c: c and "item-address" in c)
        if not addr_el:
            addr_el = card.find(class_=lambda c: c and "item-address" in str(c))
        direccion = addr_el.get_text(strip=True) if addr_el else ""

        distrito = parse_district(direccion) if direccion else "No especificado"

        records.append(
            {
                "titulo":          titulo,
                "precio_original": precio_original,
                "distrito":        distrito,
                "direccion":       direccion,
                "url_detalle":     url,
            }
        )

    return records


# ── Extraccion de detalle por propiedad ──────────────────────────────────────

def scrape_detail(page: Page, url: str) -> dict:
    """
    Abre la pagina de detalle y busca Estado del Inmueble y Partida Registral
    en el texto libre de la descripcion.
    Retorna 'No especificado' si no encuentra el dato.
    """
    defaults = {
        "estado_inmueble":  "No especificado",
        "partida_registral": "No especificado",
    }

    if not url:
        return defaults

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        time.sleep(random.uniform(1.0, 2.5))
    except Exception as e:
        print(f"  ERROR cargando {url}: {e}")
        return defaults

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    # Extraer texto plano de la pagina (sin scripts ni estilos)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    page_text = soup.get_text("\n", strip=True)

    result = dict(defaults)

    # Buscar estado del inmueble (Ocupado / Desocupado) en texto libre
    state_match = re.search(
        r"(?i)estado\s+del\s+inmueble[:\s]*([^\n\r,\.]{3,40})",
        page_text,
    )
    if state_match:
        val = state_match.group(1).strip()
        if val:
            result["estado_inmueble"] = val

    # Si no encontro, buscar keywords directos
    if result["estado_inmueble"] == "No especificado":
        if re.search(r"(?i)\bdesocupado\b", page_text):
            result["estado_inmueble"] = "Desocupado"
        elif re.search(r"(?i)\bocupado\b", page_text):
            result["estado_inmueble"] = "Ocupado"

    # Buscar partida registral
    partida_match = re.search(
        r"(?i)partida\s+registral[:\s]*([^\n\r,\.]{3,40})",
        page_text,
    )
    if partida_match:
        val = partida_match.group(1).strip()
        if val and len(val) > 2:
            result["partida_registral"] = val

    return result


# ── Enriquecimiento ───────────────────────────────────────────────────────────

def enrich(records: list[dict]) -> list[dict]:
    """Agrega precio_usd, moneda y es_oportunidad."""
    for rec in records:
        precio_usd, moneda = clean_price(rec.get("precio_original", ""))
        rec["precio_usd"] = precio_usd
        rec["moneda"] = moneda

        distrito_lower = rec.get("distrito", "").lower()
        rec["es_oportunidad"] = bool(
            precio_usd is not None
            and precio_usd < 100_000
            and any(d in distrito_lower for d in DISTRITOS_OPORTUNIDAD)
        )

    return records


# ── Exportar CSV ──────────────────────────────────────────────────────────────

def export_csv(records: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"CSV exportado: {path}")
    print("CONSEJO: En Excel usa Datos -> Desde texto/CSV para evitar que las")
    print("Partidas Registrales largas se conviertan a notacion cientifica.")


def export_json(records: list[dict], path: Path) -> None:
    import json
    from datetime import datetime, timezone
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ultima_actualizacion": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(records),
        "propiedades": [{k: r.get(k) for k in CSV_COLUMNS} for r in records],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"JSON exportado: {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="es-PE",
        )
        page = ctx.new_page()

        # Fase 1: listado
        records = scrape_listing(page)

        if not records:
            browser.close()
            return

        # Fase 2: detalle por propiedad
        print(f"\nExtrayendo detalles de {len(records)} propiedades...")
        for i, rec in enumerate(records, 1):
            url = rec.get("url_detalle", "")
            print(f"  [{i:2}/{len(records)}] {rec.get('titulo', '')[:60]}")
            detail = scrape_detail(page, url)
            rec.update(detail)

        browser.close()

    # Fase 3: enriquecimiento y exportacion
    records = enrich(records)
    oportunidades = [r for r in records if r.get("es_oportunidad")]

    export_csv(records, OUTPUT_CSV)
    export_json(records, OUTPUT_JSON)

    print(f"\n{'='*55}")
    print(f"Total propiedades : {len(records)}")
    print(f"Oportunidades     : {len(oportunidades)}")
    print(f"  (precio < $100K USD en Miraflores / San Isidro / Surco)")
    if oportunidades:
        print("\nOportunidades encontradas:")
        for r in oportunidades:
            print(f"  - {r['titulo'][:55]} | ${r['precio_usd']:,.0f} | {r['distrito']}")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
