"""
tools/banbif_scraper.py
────────────────────────
Extrae bienes adjudicados de BanBif Peru.
URL: https://banbif.com.pe/bienesadjudicados

Mecanismo: cada card abre un modal dinamico con los datos completos.
Se usa Playwright para hacer click en cada card y leer el modal.

Uso standalone:
    python tools/banbif_scraper.py
"""

from __future__ import annotations

import html as htmllib
import re
import time
import random
from pathlib import Path

from playwright.sync_api import sync_playwright, Page
from bs4 import BeautifulSoup

SOURCE_NAME = "BanBif"
BASE_URL    = "https://banbif.com.pe/bienesadjudicados"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

EXCHANGE_RATE = 3.75  # S/ -> USD


# ── Precio ────────────────────────────────────────────────────────────────────

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


# ── Extraccion del modal de detalle ──────────────────────────────────────────

def extract_modal_data(page: Page) -> dict:
    """
    Lee el contenido del modal activo post-click.
    BanBif carga el HTML del detalle dentro de un div oculto que se vuelve
    visible; el contenido esta HTML-escapado dentro de un atributo o innerHTML.
    """
    defaults = {
        "titulo":          "No especificado",
        "direccion":       "No especificado",
        "area_m2":         None,
        "precio_original": "",
        "precio_usd":      None,
        "moneda":          "USD",
        "ambientes":       "No especificado",
    }

    try:
        # Esperar que el modal sea visible
        page.wait_for_selector(".modal.show, .modal-dialog, #myModal", timeout=5000)
    except Exception:
        pass

    # Leer el HTML completo y buscar el contenido HTML-escapado del detalle
    raw_html = page.content()

    # El detalle esta HTML-escapado dentro del DOM; desescaparlo
    unescaped = htmllib.unescape(raw_html)
    soup = BeautifulSoup(unescaped, "lxml")

    # Titulo
    h2 = soup.find("h2", class_=lambda c: c and "titleprin" in str(c))
    if not h2:
        h2 = soup.find("h2", style=lambda s: s and "0099ff" in str(s))
    titulo = h2.get_text(strip=True) if h2 else defaults["titulo"]

    # Extraer campos de los parrafos con chevron icon
    fields = {}
    for p in soup.find_all("p", style=True):
        text = p.get_text(strip=True)
        if ":" in text:
            key, _, val = text.partition(":")
            fields[key.strip().lower()] = val.strip()

    precio_raw = fields.get("precio", "")
    direccion  = fields.get("dirección", fields.get("direccion", defaults["direccion"]))
    area_raw   = fields.get("área total", fields.get("area total", ""))
    ambientes  = fields.get("ambientes", defaults["ambientes"])

    # Area m2
    area_m2 = None
    area_match = re.search(r"([\d\.]+)", area_raw)
    if area_match:
        try:
            area_m2 = float(area_match.group(1))
        except ValueError:
            pass

    precio_usd, moneda = clean_price(precio_raw)

    return {
        "titulo":          titulo,
        "direccion":       direccion,
        "area_m2":         area_m2,
        "precio_original": precio_raw,
        "precio_usd":      precio_usd,
        "moneda":          moneda,
        "ambientes":       ambientes,
    }


# ── Scraper principal ─────────────────────────────────────────────────────────

def scrape() -> list[dict]:
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
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=25000)
        time.sleep(3)

        # Datos base de cada card (visibles sin click)
        cards_data = page.evaluate("""() => {
            const cards = [];
            document.querySelectorAll('a.redirectAction').forEach(a => {
                const input  = a.querySelector('input.itemBienAdjudicado');
                const locEl  = a.querySelector('.capitalizeSpan');
                const priceEl = a.querySelector('p.adicional-inmueble strong');
                cards.push({
                    id:       input ? input.value : '',
                    location: locEl  ? locEl.innerText.trim()  : '',
                    price:    priceEl ? priceEl.innerText.trim() : '',
                });
            });
            return cards;
        }""")

        print(f"[{SOURCE_NAME}] {len(cards_data)} propiedades encontradas.")

        for i, card_info in enumerate(cards_data):
            item_id = card_info.get("id", "")
            print(f"  [{i+1}/{len(cards_data)}] {card_info.get('location','')} (id={item_id})")

            try:
                # Navegar a la pagina fresca y hacer click en el card por su ID oculto.
                # Esto evita el problema de elementos DOM obsoletos entre clicks.
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20000)
                time.sleep(2)
                # Click en el card que tiene el input con el ID correspondiente
                selector = f"a.redirectAction:has(input.itemBienAdjudicado[value='{item_id}'])"
                page.wait_for_selector(selector, timeout=5000)
                page.click(selector)
                time.sleep(1.5)
                modal_data = extract_modal_data(page)
            except Exception as e:
                print(f"    ERROR extrayendo detalle: {e}")
                modal_data = {
                    "titulo": card_info.get("location", "No especificado"),
                    "direccion": "No especificado",
                    "area_m2": None,
                    "precio_original": card_info.get("price", ""),
                    "precio_usd": None,
                    "moneda": "USD",
                    "ambientes": "No especificado",
                }

            # Parsear distrito de la ubicacion del card
            loc = card_info.get("location", "")
            parts = [p.strip() for p in loc.split("-")]
            distrito = parts[-1].title() if parts else "No especificado"

            # Precio de respaldo desde el card si el modal no lo tiene
            if not modal_data.get("precio_usd"):
                p_usd, mon = clean_price(card_info.get("price", ""))
                modal_data["precio_usd"]      = p_usd
                modal_data["moneda"]          = mon
                modal_data["precio_original"] = card_info.get("price", "")

            record = {
                "fuente_origen":    SOURCE_NAME,
                "url_detalle":      BASE_URL,
                "titulo":           modal_data["titulo"],
                "precio_original":  modal_data["precio_original"],
                "moneda":           modal_data["moneda"],
                "precio_usd":       modal_data["precio_usd"],
                "distrito":         distrito,
                "direccion":        modal_data["direccion"],
                "medidas_m2":       modal_data["area_m2"],
                "estado_inmueble":  "No especificado",
                "partida_registral":"No especificado",
                "es_oportunidad":   bool(
                    modal_data.get("precio_usd") and
                    modal_data["precio_usd"] < 100_000 and
                    distrito.lower() in {"miraflores", "san isidro", "surco"}
                ),
            }
            records.append(record)
            time.sleep(random.uniform(0.8, 1.8))

        browser.close()

    return records


if __name__ == "__main__":
    import json
    data = scrape()
    print(f"\n{len(data)} registros extraidos de {SOURCE_NAME}")
    print(json.dumps(data[:2], ensure_ascii=False, indent=2))
