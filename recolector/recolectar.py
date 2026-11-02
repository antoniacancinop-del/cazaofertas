"""
Recolector de precios de Cazaofertas.

Abre las páginas públicas de búsqueda de cada supermercado con un navegador
automático (como lo haría una persona), lee los precios de los productos de la
canasta y guarda el resultado en ../datos/precios.json.

Se ejecuta una vez al día desde GitHub Actions. Para correrlo a mano:
    pip install playwright && playwright install chromium
    python recolector/recolectar.py
"""
import asyncio
import json
import random
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

from playwright.async_api import async_playwright

RAIZ = Path(__file__).resolve().parent.parent
CANASTA = json.loads((RAIZ / "recolector" / "canasta.json").read_text(encoding="utf-8"))
SALIDA = RAIZ / "datos" / "precios.json"
HISTORIAL = RAIZ / "datos" / "historial"
DEPURACION = RAIZ / "depuracion"
CHILE = timezone(timedelta(hours=-3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# ---------------------------------------------------------------- utilidades

def normalizar(t: str) -> str:
    t = unicodedata.normalize("NFD", t or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower()).strip()


def a_entero(s) -> int | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(round(s))
    d = re.sub(r"[^\d]", "", str(s))
    return int(d) if d else None


UNIDADES = {
    "kg": ("masa", 1), "kilo": ("masa", 1), "kilos": ("masa", 1), "k": ("masa", 1),
    "g": ("masa", 0.001), "gr": ("masa", 0.001), "grs": ("masa", 0.001), "gramos": ("masa", 0.001),
    "l": ("vol", 1), "lt": ("vol", 1), "lts": ("vol", 1), "litro": ("vol", 1), "litros": ("vol", 1),
    "ml": ("vol", 0.001), "cc": ("vol", 0.001), "cm3": ("vol", 0.001),
    "un": ("cant", 1), "u": ("cant", 1), "und": ("cant", 1), "unid": ("cant", 1), "unidades": ("cant", 1),
    "rollos": ("cant", 1), "rollo": ("cant", 1), "bolsitas": ("cant", 1), "sobres": ("cant", 1),
}
PAT_UNI = r"(kg|kilos?|k|grs|gramos|gr|g|litros?|lts|lt|l|ml|cc|cm3|unidades|unid|und|un|u|rollos?|bolsitas|sobres)\b"
NUM = r"(\d+(?:[.,]\d+)?)"


def a_numero(s: str) -> float:
    return float(s.replace(".", "").replace(",", ".")) if re.fullmatch(r"\d{1,3}(\.\d{3})+", s) else float(s.replace(",", "."))


def tamano(nombre: str, tipo: str) -> float | None:
    """Devuelve el tamaño del envase en la unidad base del tipo pedido (kg, l o unidades)."""
    n = normalizar(nombre)
    # Packs "6 x 1 l", "3 x 90 g"
    for m in re.finditer(rf"(\d+)\s*x\s*{NUM}\s*{PAT_UNI}", n):
        cuantos, valor, uni = int(m.group(1)), a_numero(m.group(2)), m.group(3)
        t, f = UNIDADES[uni]
        if tipo == "cant":
            return float(cuantos)
        if t == tipo:
            return cuantos * valor * f
    candidatos = []
    for m in re.finditer(rf"{NUM}\s*{PAT_UNI}", n):
        valor, uni = a_numero(m.group(1)), m.group(2)
        t, f = UNIDADES[uni]
        if t == tipo:
            candidatos.append(valor * f)
    if candidatos:
        return candidatos[-1]
    if tipo == "cant":
        m = re.search(r"(?:pack|bandeja|caja)\s*(?:de\s*)?(\d+)", n)
        if m:
            return float(m.group(1))
        if re.search(r"\b(barra|jabon)\b", n):
            return 1.0
    if tipo == "masa" and re.search(r"\b(granel|x kg|por kg|kg aprox|aprox)\b", n):
        return 1.0
    return None


def coincide_palabra(texto: str, palabra: str) -> bool:
    palabra = normalizar(palabra)
    if len(palabra) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(palabra)}(?![a-z0-9])", texto) is not None
    return re.search(rf"(?<![a-z0-9]){re.escape(palabra)}", texto) is not None


def es_candidato(prod: dict, nombre: str) -> bool:
    t = normalizar(nombre)
    if not all(coincide_palabra(t, p) for p in prod.get("debe", [])):
        return False
    if prod.get("alguna") and not any(coincide_palabra(t, p) for p in prod["alguna"]):
        return False
    if any(normalizar(p) in t for p in prod.get("excluir", [])):
        return False
    return True


def referencia(prod: dict) -> tuple[str, float]:
    t, f = UNIDADES[prod["unidad"]]
    return t, prod["cantidad"] * f


def elegir(prod: dict, resultados: list[dict]) -> dict | None:
    """De todos los resultados de una tienda, elige el más barato que cumple las reglas."""
    tipo, ref = referencia(prod)
    mejor = None
    for r in resultados:
        if not r.get("precio") or not es_candidato(prod, r["nombre"]):
            continue
        tam = r.get("tamano") or tamano(r["nombre"] + " " + (r.get("formato") or ""), tipo)
        if not tam:
            continue
        lo, hi = prod.get("rango", [0.5, 2])
        if not (lo * ref <= tam <= hi * ref):
            continue
        # precio que paga cualquier persona (sin tarjeta ni club)
        base = r["normal"] if r.get("tarjeta") and r.get("normal") else r["precio"]
        cand = {
            "nombre_producto": r["nombre"],
            "url": r.get("url"),
            "formato": r.get("formato"),
            "tamano": round(tam, 4),
            "precio_envase": base,
            "normal_envase": r.get("normal") if r.get("normal") and r["normal"] > base else None,
            "precio": round(base / tam * ref),
            "precio_tarjeta": round(r["precio"] / tam * ref) if r.get("tarjeta") and r["precio"] < base else None,
        }
        if mejor is None or cand["precio"] < mejor["precio"]:
            mejor = cand
    return mejor


# ---------------------------------------------------------------- extractores (se ejecutan dentro de la página)

JS_CENCOSUD = r"""
() => {
  const out = [];
  const lists = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map(s => { try { return JSON.parse(s.textContent) } catch (e) { return null } })
    .filter(j => j && j['@type'] === 'ItemList');
  for (const l of lists) for (const e of (l.itemListElement || [])) {
    const it = e.item || {}; const of = it.offers || {};
    let normal = null, tarjeta = false;
    try {
      const path = new URL(it.url || e.url).pathname;
      const a = document.querySelector(`a[href$="${path}"]`);
      let c = a; for (let i = 0; i < 7 && c; i++) { if ((c.innerText || '').match(/\$/g)?.length >= 1 && /Agregar/.test(c.innerText || '')) break; c = c.parentElement; }
      if (c) {
        const txt = c.innerText;
        const montos = [...txt.matchAll(/\$\s?([\d.]+)(?!\s*x\s)/g)].map(m => +m[1].replace(/\./g, ''));
        if (montos.length >= 2) { const mx = Math.max(...montos); if (mx > of.price) normal = mx; }
        tarjeta = /tarjeta cencosud|prime/i.test(txt);
      }
    } catch (err) {}
    out.push({ nombre: it.name || e.name, precio: of.price, normal, url: it.url || e.url, tarjeta: false, marca: it.brand && it.brand.name });
  }
  return out;
}
"""

JS_TOTTUS = r"""
() => {
  const nd = document.getElementById('__NEXT_DATA__'); if (!nd) return [];
  const res = (JSON.parse(nd.textContent).props.pageProps || {}).results || [];
  const n = s => s ? +String(Array.isArray(s) ? s[0] : s).replace(/[^\d]/g, '') : null;
  return res.map(r => {
    const ps = r.prices || [];
    const normalP = ps.find(p => p.crossed);
    const sinTarjeta = ps.filter(p => !p.crossed && !/cmr/i.test(p.type || ''));
    const conTarjeta = ps.filter(p => !p.crossed && /cmr/i.test(p.type || ''));
    const precio = sinTarjeta.length ? Math.min(...sinTarjeta.map(p => n(p.price))) : (normalP ? n(normalP.price) : null);
    const tarj = conTarjeta.length ? Math.min(...conTarjeta.map(p => n(p.price))) : null;
    return { nombre: r.displayName, marca: r.brand, formato: (r.measurements || {}).format, url: r.url,
             precio: tarj && tarj < precio ? tarj : precio, normal: tarj && tarj < precio ? precio : (normalP ? n(normalP.price) : null),
             tarjeta: !!(tarj && tarj < precio) };
  }).map(x => (x.normal === x.precio ? { ...x, normal: null } : x));
}
"""

JS_LIDER = r"""
() => {
  const nd = document.getElementById('__NEXT_DATA__'); if (!nd) return [];
  const out = [], seen = new Set();
  const n = s => s ? +String(s).replace(/[^\d]/g, '') : null;
  (function walk(o, d) {
    if (!o || typeof o !== 'object' || d > 40) return;
    if (Array.isArray(o)) { o.forEach(x => walk(x, d + 1)); return; }
    if (o.name && o.priceInfo && typeof o.priceInfo === 'object') {
      const p = o.priceInfo;
      const precio = n(p.linePrice) || n(p.itemPrice) || n(p.currentPrice && p.currentPrice.price);
      const key = o.usItemId || o.id || o.name;
      if (precio && !seen.has(key)) {
        seen.add(key);
        const url = o.canonicalUrl ? new URL(o.canonicalUrl, location.origin).href : null;
        out.push({ nombre: o.name, precio, normal: n(p.wasPrice) || null, url, tarjeta: false });
      }
    }
    for (const k in o) walk(o[k], d + 1);
  })(JSON.parse(nd.textContent), 0);
  return out;
}
"""

# Unimarc y Alvi (grupo SMU) dibujan los productos en la página después de cargar.
JS_SMU = r"""
() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href^="/product/"]')) {
    const href = a.getAttribute('href'); if (seen.has(href)) continue;
    let c = a; for (let i = 0; i < 9 && c; i++) { const t = c.innerText || ''; if (/\$/.test(t) && /Agregar/.test(t)) break; c = c.parentElement; }
    if (!c) continue; seen.add(href);
    const partes = c.innerText.split(/\n+/).map(s => s.trim()).filter(Boolean);
    const txt = partes.join(' | ');
    const nombre = a.getAttribute('title') || a.innerText;
    let precio = null, normal = null, tarjeta = false;
    const reg = txt.match(/Precio regular \| \$([\d.]+)/);
    if (reg) { // Alvi: precio regular para quien compra una unidad sin ser socio
      precio = +reg[1].replace(/\./g, '');
    } else { // Unimarc: montos seguidos de su precio por kg/L, ignorando los "2 x $3.000"
      const montos = [];
      for (let i = 0; i < partes.length; i++) {
        const m = partes[i].match(/^\$([\d.]+)(\s*c\/u)?$/);
        if (m && i + 1 < partes.length && /^\(\$/.test(partes[i + 1]) && !/^\d+\s*x\s*\$/.test(partes[i - 1] || '')) montos.push(+m[1].replace(/\./g, ''));
      }
      if (!montos.length) continue;
      precio = montos[0]; normal = montos.length > 1 ? montos[montos.length - 1] : null;
      if (normal && normal <= precio) normal = null;
      tarjeta = !!normal && /Club Unimarc|Diamante|Socio/i.test(txt);
    }
    const formato = partes.find(p => /^\d+([.,]\d+)?\s*(kg|g|gr|l|lt|ml|cc|un)\b/i.test(p)) || null;
    out.push({ nombre, precio, normal, tarjeta, formato, url: new URL(href, location.origin).href });
  }
  return out;
}
"""

TIENDAS = [
    {"id": "jumbo", "nombre": "Jumbo", "url": "https://www.jumbo.cl/busqueda?ft={q}", "js": JS_CENCOSUD, "esperar": 'script[type="application/ld+json"]'},
    {"id": "santaisabel", "nombre": "Santa Isabel", "url": "https://www.santaisabel.cl/busqueda?ft={q}", "js": JS_CENCOSUD, "esperar": 'script[type="application/ld+json"]'},
    {"id": "tottus", "nombre": "Tottus", "url": "https://www.tottus.cl/tottus-cl/buscar?Ntt={q}", "js": JS_TOTTUS, "esperar": "#__NEXT_DATA__"},
    {"id": "lider", "nombre": "Lider", "url": "https://super.lider.cl/search?q={q}", "js": JS_LIDER, "esperar": "#__NEXT_DATA__"},
    {"id": "unimarc", "nombre": "Unimarc", "url": "https://www.unimarc.cl/search?q={q}", "js": JS_SMU, "esperar": 'a[href^="/product/"]'},
    {"id": "alvi", "nombre": "Alvi", "url": "https://www.alvi.cl/search?q={q}", "js": JS_SMU, "esperar": 'a[href^="/product/"]'},
]
SUPER10 = {"id": "super10", "nombre": "Super10 (ex Mayorista 10)", "url": "https://www.super10.cl/ofertas"}


# ---------------------------------------------------------------- recolección

async def permitido(contexto, url: str) -> bool:
    """Respeta el archivo robots.txt de cada sitio."""
    p = urlparse(url)
    robots = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        r = await contexto.request.get(robots, timeout=15000)
        if r.status >= 400:
            return True
        rp = RobotFileParser()
        rp.parse((await r.text()).splitlines())
        return rp.can_fetch("*", url)
    except Exception:
        return True


async def recolectar_tienda(navegador, tienda: dict, log: list) -> dict:
    contexto = await navegador.new_context(user_agent=UA, locale="es-CL", timezone_id="America/Santiago", viewport={"width": 1366, "height": 900})
    pagina = await contexto.new_page()
    resultado = {"estado": "ok", "productos": {}, "errores": 0}
    ejemplo = tienda["url"].format(q="arroz")
    if not await permitido(contexto, ejemplo):
        log.append(f"{tienda['nombre']}: robots.txt no permite la búsqueda, se omite")
        await contexto.close()
        return {"estado": "omitida", "motivo": "robots.txt", "productos": {}}
    vacios = 0
    for prod in CANASTA["productos"]:
        url = tienda["url"].format(q=quote(prod["buscar"]))
        try:
            await pagina.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await pagina.wait_for_selector(tienda["esperar"], state="attached", timeout=20000)
            except Exception:
                pass
            await pagina.wait_for_timeout(1500)
            items = await pagina.evaluate(tienda["js"])
        except Exception as e:
            items = []
            resultado["errores"] += 1
            log.append(f"{tienda['nombre']} · {prod['id']}: error {type(e).__name__}: {str(e)[:120]}")
        if not items:
            vacios += 1
            if vacios == 3 and not resultado["productos"]:
                DEPURACION.mkdir(exist_ok=True)
                try:
                    (DEPURACION / f"{tienda['id']}.html").write_text(await pagina.content(), encoding="utf-8")
                    await pagina.screenshot(path=str(DEPURACION / f"{tienda['id']}.png"))
                except Exception:
                    pass
                log.append(f"{tienda['nombre']}: 3 búsquedas seguidas sin resultados, se detiene (posible bloqueo)")
                resultado["estado"] = "bloqueada"
                break
        elegido = elegir(prod, items) if items else None
        if elegido:
            resultado["productos"][prod["id"]] = elegido
        log.append(f"{tienda['nombre']} · {prod['id']}: {len(items)} resultados → {elegido['nombre_producto'] + ' $' + str(elegido['precio']) if elegido else 'sin coincidencia'}")
        await asyncio.sleep(random.uniform(2.0, 4.0))  # pausa amable entre búsquedas
    await contexto.close()
    if resultado["estado"] == "ok" and not resultado["productos"]:
        resultado["estado"] = "sin datos"
    return resultado


async def recolectar_super10(navegador, log: list) -> dict:
    contexto = await navegador.new_context(user_agent=UA, locale="es-CL")
    pagina = await contexto.new_page()
    resultado = {"estado": "ok", "productos": {}, "solo_ofertas": True}
    try:
        if not await permitido(contexto, SUPER10["url"]):
            return {"estado": "omitida", "motivo": "robots.txt", "productos": {}}
        await pagina.goto(SUPER10["url"], wait_until="networkidle", timeout=60000)
        for _ in range(6):
            await pagina.mouse.wheel(0, 3000)
            await pagina.wait_for_timeout(800)
        lineas = [l.strip() for l in (await pagina.inner_text("body")).split("\n") if l.strip()]
        ofertas = []
        for i, l in enumerate(lineas):
            m = re.match(r"^\$\s?([\d.]+)\s*(kg|c/u|un)?$", l, re.I)
            if m and i > 0:
                antes = re.match(r"^Antes:\s*\$\s?([\d.]+)", lineas[i + 1]) if i + 1 < len(lineas) else None
                nombre = lineas[i - 1]
                if (m.group(2) or "").lower() == "kg" and not re.search(r"\bkg\b", nombre, re.I):
                    nombre += " 1 kg"
                ofertas.append({"nombre": nombre, "precio": a_entero(m.group(1)), "normal": a_entero(antes.group(1)) if antes else None, "url": SUPER10["url"]})
        log.append(f"Super10: {len(ofertas)} ofertas leídas")
        for prod in CANASTA["productos"]:
            e = elegir(prod, ofertas)
            if e:
                resultado["productos"][prod["id"]] = e
    except Exception as e:
        resultado["estado"] = "error"
        log.append(f"Super10: error {e}")
    await contexto.close()
    return resultado


async def principal():
    ahora = datetime.now(CHILE)
    anterior = json.loads(SALIDA.read_text(encoding="utf-8")) if SALIDA.exists() else None
    log: list[str] = []
    solo = set(sys.argv[1:])  # opcional: recolectar solo algunas tiendas
    async with async_playwright() as p:
        navegador = await p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        tareas = {t["id"]: recolectar_tienda(navegador, t, log) for t in TIENDAS if not solo or t["id"] in solo}
        if not solo or "super10" in solo:
            tareas["super10"] = recolectar_super10(navegador, log)
        resultados = dict(zip(tareas.keys(), await asyncio.gather(*tareas.values())))
        await navegador.close()

    nombres = {t["id"]: t["nombre"] for t in TIENDAS + [SUPER10]}
    tiendas = []
    productos = {p["id"]: {k: p[k] for k in ("id", "nombre", "categoria", "cantidad", "unidad")} | {"precios": {}} for p in CANASTA["productos"]}
    fecha = ahora.strftime("%Y-%m-%d")
    for tid, nombre in nombres.items():
        r = resultados.get(tid)
        previo = {pid: pr["precios"][tid] for pid, pr in ((anterior or {}).get("productos") or {}).items() if tid in pr.get("precios", {})}
        if r and r["productos"]:
            for pid, dato in r["productos"].items():
                productos[pid]["precios"][tid] = dato | {"fecha": fecha}
            tiendas.append({"id": tid, "nombre": nombre, "estado": "ok", "fecha": fecha, "productos": len(r["productos"]), "solo_ofertas": r.get("solo_ofertas", False)})
        elif previo:  # si hoy falló, se mantienen los precios del último día que funcionó
            for pid, dato in previo.items():
                productos[pid]["precios"][tid] = dato
            ult = max(d.get("fecha", "") for d in previo.values())
            tiendas.append({"id": tid, "nombre": nombre, "estado": "desactualizada", "fecha": ult, "productos": len(previo), "motivo": (r or {}).get("estado")})
        else:
            tiendas.append({"id": tid, "nombre": nombre, "estado": (r or {}).get("estado", "no revisada"), "fecha": None, "productos": 0})

    datos = {
        "actualizado": ahora.isoformat(timespec="minutes"),
        "tiendas": tiendas,
        "productos": list(productos.values()),
        "canasta_basica": CANASTA["canasta_basica"],
    }
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    HISTORIAL.mkdir(parents=True, exist_ok=True)
    (HISTORIAL / f"{fecha}.json").write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    (RAIZ / "datos" / "registro.txt").write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))
    print("\nResumen:")
    for t in tiendas:
        print(f"  {t['nombre']}: {t['estado']} ({t['productos']} productos)")


if __name__ == "__main__":
    asyncio.run(principal())
