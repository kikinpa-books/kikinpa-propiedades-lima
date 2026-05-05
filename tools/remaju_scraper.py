"""
tools/remaju_scraper.py
────────────────────────
Extrae remates judiciales de REMAJU (Poder Judicial del Peru).
URL: https://remaju.pj.gob.pe/remaju/

Mecanismo: Los items del carousel tienen tipo, ciudad y fecha.
El boton "Detalle" usa PrimeFaces AJAX; al hacer click abre un dialogo
con informacion adicional del expediente.

Uso standalone:
    python tools/remaju_scraper.py
"""

from __future__ import annotations

import re
import time
import random
from pathlib import Path

from playwright.sync_api import sync_playwright, Page
from bs4 import BeautifulSoup

SOURCE_NAME = "REMAJU"
BASE_URL    = "https://remaju.pj.gob.pe/remaju/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

EXCHANGE_RATE = 3.75


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_price(raw: str) -> tuple[float | None, str]:
    if not raw:
        return None, ""
    raw_up = raw.upper()
    moneda = "PEN" if any(k in raw_up for k in ("S/", "PEN", "SOL")) else "USD"
    cleaned = re.sub(r"[A-Za-z/\$\s]", "", raw).strip().replace(",", "")
    try:
        amount = float(cleaned)
    except ValueError:
        return None, moneda
    precio_usd = round(amount / EXCHANGE_RATE, 2) if moneda == "PEN" else amount
    return precio_usd, moneda


def parse_date(raw: str) -> str | None:
    """Convierte DD/MM/YYYY a ISO8601."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}T00:00:00"
    return None


# ── Extraccion del dialogo de detalle (PrimeFaces) ───────────────────────────

def extract_dialog(page: Page) -> dict:
    """
    Despues de hacer click en el boton Detalle de un remate,
    PrimeFaces muestra un dialogo. Esperamos que aparezca y
    extraemos su contenido.
    """
    defaults = {
        "titulo":           "No especificado",
        "precio_original":  "",
        "precio_usd":       None,
        "moneda":           "PEN",
        "partida_registral":"No especificado",
        "tipo_bien":        "No especificado",
        "expediente":       "No especificado",
        "estado_inmueble":  "No especificado",
        "medidas_m2":       None,
    }

    try:
        # El dialogo de PrimeFaces tiene clase ui-dialog o similar
        page.wait_for_selector(
            ".ui-dialog-content, div[id*='dlg'], div[id*='dialog']",
            timeout=6000,
            state="visible",
        )
        time.sleep(0.8)
    except Exception:
        return defaults

    soup = BeautifulSoup(page.content(), "lxml")

    # Buscar el dialogo activo
    dialog = soup.find(class_=lambda c: c and "ui-dialog" in str(c) and "ui-widget" in str(c))
    if not dialog:
        dialog = soup.find(id=lambda i: i and "dlg" in str(i).lower())
    if not dialog:
        return defaults

    text = dialog.get_text(" ", strip=True)

    result = dict(defaults)

    # Extraer campos clave del texto del dialogo
    for line in text.splitlines():
        line = line.strip()
        lower = line.lower()

        if "expediente" in lower:
            m = re.search(r"expediente[:\s]*([^\s,\.]+)", line, re.IGNORECASE)
            if m:
                result["expediente"] = m.group(1).strip()

        if "partida" in lower:
            m = re.search(r"partida[:\s]*([^\s,\.]+)", line, re.IGNORECASE)
            if m and len(m.group(1)) > 2:
                result["partida_registral"] = m.group(1).strip()

        if re.search(r"s/|pen|precio|tasaci", lower):
            m = re.search(r"(s/|pen|usd|us\$)\s*([\d,\.]+)", line, re.IGNORECASE)
            if m:
                result["precio_original"] = m.group(0)
                result["precio_usd"], result["moneda"] = clean_price(m.group(0))

        if re.search(r"m2|area|superficie|metros", lower):
            m = re.search(r"([\d\.]+)\s*m2", line, re.IGNORECASE)
            if m:
                try:
                    result["medidas_m2"] = float(m.group(1))
                except ValueError:
                    pass

        if re.search(r"desocupado|libre|vac", lower):
            result["estado_inmueble"] = "Desocupado"
        elif re.search(r"ocupado|inquilino", lower):
            result["estado_inmueble"] = "Ocupado"

    # Titulo: primer texto sustancial del dialogo
    for el in dialog.find_all(["h2", "h3", "h4", "strong", "b"]):
        t = el.get_text(strip=True)
        if t and len(t) > 5 and not re.match(r"^\d+$", t):
            result["titulo"] = t
            break

    return result


# ── Cerrar dialogo ────────────────────────────────────────────────────────────

def close_dialog(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
        time.sleep(0.5)
    except Exception:
        pass


# ── Scraper principal ─────────────────────────────────────────────────────────

def scrape(max_items: int = 50) -> list[dict]:
    """
    Extrae hasta max_items remates de REMAJU.
    El carousel tiene cientos de items; limitamos para evitar
    tiempo de ejecucion excesivo en el CI.
    """
    records: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="es-PE",
        )
        page = ctx.new_page()

        print(f"[{SOURCE_NAME}] Navegando a {BASE_URL} ...")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)  # REMAJU carga el carousel via JS, esperar mas tiempo

        # Leer datos base de todos los sliders del DOM.
        # Extraemos tambien los IDs de convocatoria y remate del onclick del boton
        # para armar la URL de detalle sin necesidad de hacer click (JSF navega la pagina).
        sliders_data = page.evaluate("""() => {
            const result = [];
            document.querySelectorAll('div.remate-slider').forEach((s, i) => {
                const spans = s.querySelectorAll('.titulo span, .titulo');
                let tipo = '', ciudad = '', fecha = '';
                spans.forEach(sp => {
                    const t = sp.innerText ? sp.innerText.trim() : '';
                    if (t === 'REMATE SIMPLE' || t === 'REMATE MULTIPLE' || t === 'REMATE M\\u00dALTIPLE') tipo = t;
                    else if (t && !t.includes('REMATE') && t.length > 1 && !t.includes('*')) ciudad = t;
                });
                const fechaEl = s.querySelector('.fecha');
                if (fechaEl) fecha = fechaEl.innerText.trim();

                // Extraer IDs del onclick (sin hacer click)
                const btn = s.querySelector('button[onclick]');
                let convocatoria = '', remate = '', tipoConv = '';
                if (btn) {
                    const oc = btn.getAttribute('onclick') || '';
                    const cm = oc.match(/"convocatoria",value:"(\\d+)"/);
                    const rm = oc.match(/"remate",value:"(\\d+)"/);
                    const tm = oc.match(/"tipoConvocatoria",value:"(\\d+)"/);
                    if (cm) convocatoria = cm[1];
                    if (rm) remate = rm[1];
                    if (tm) tipoConv = tm[1];
                }

                result.push({ idx: i, tipo, ciudad, fecha, convocatoria, remate, tipoConv });
            });
            return result;
        }""")

        total = len(sliders_data)
        limit = min(total, max_items)
        print(f"[{SOURCE_NAME}] {total} remates encontrados. Procesando {limit}...")

        for i, item in enumerate(sliders_data[:limit]):
            ciudad       = item.get("ciudad", "").title()
            tipo         = item.get("tipo", "")
            fecha        = item.get("fecha", "")
            convocatoria = item.get("convocatoria", "")
            remate_id    = item.get("remate", "")

            safe_ciudad = ciudad.encode('ascii', 'replace').decode()
            print(f"  [{i+1}/{limit}] {tipo} - {safe_ciudad} - {fecha} (remate={remate_id})")

            # URL de detalle: pagina principal con parametro de remate
            url_detalle = (
                f"{BASE_URL}?convocatoria={convocatoria}&remate={remate_id}"
                if remate_id else BASE_URL
            )

            fecha_iso = parse_date(fecha)

            record = {
                "fuente_origen":    SOURCE_NAME,
                "url_detalle":      url_detalle,
                "titulo":           f"{tipo} - {ciudad}",
                "precio_original":  "",
                "moneda":           "PEN",
                "precio_usd":       None,
                "distrito":         ciudad,
                "direccion":        ciudad,
                "medidas_m2":       None,
                "estado_inmueble":  "No especificado",
                "partida_registral":"No especificado",
                "fecha_remate":     fecha_iso,
                "tipo_remate":      tipo,
                "convocatoria":     convocatoria,
                "expediente":       remate_id,
                "es_oportunidad":   False,
            }
            records.append(record)

        browser.close()

    return records


if __name__ == "__main__":
    import json
    data = scrape(max_items=10)
    print(f"\n{len(data)} registros extraidos de {SOURCE_NAME}")
    print(json.dumps(data[:2], ensure_ascii=False, indent=2))
