"""
Recolector de precios de Cazaprecios.

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
import urllib.request
import urllib.parse

from playwright.async_api import async_playwright

RAIZ = Path(__file__).resolve().parent.parent
CANASTA = json.loads((RAIZ / "recolector" / "canasta.json").read_text(encoding="utf-8"))
SALIDA = RAIZ / "datos" / "precios.json"
HISTORIAL = RAIZ / "datos" / "historial"
DEPURACION = RAIZ / "datos" / "depuracion"  # se publica para poder revisar bloqueos
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
        return max(candidatos)  # p. ej. "104 g drenado, 160 g neto" → 160 g
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
                    (DEPURACION / f"{tienda['id']}.txt").write_text(pagina.url + "\n\n" + (await pagina.inner_text("body"))[:5000], encoding="utf-8")
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
                # el texto "Antes $X" puede venir antes o después del precio, según el diseño de la página
                antes, nombre = None, lineas[i - 1]
                if re.match(r"^Antes", nombre, re.I):
                    antes = re.match(r"^Antes:?\s*\$\s?([\d.]+)", nombre)
                    nombre = lineas[i - 2] if i > 1 else ""
                elif i + 1 < len(lineas):
                    antes = re.match(r"^Antes:?\s*\$\s?([\d.]+)", lineas[i + 1])
                if not nombre or nombre.startswith("$") or re.match(r"^Antes", nombre, re.I):
                    continue
                por_kg = (m.group(2) or "").lower() == "kg"
                ofertas.append({"nombre": nombre, "precio": a_entero(m.group(1)), "normal": a_entero(antes.group(1)) if antes else None,
                                "url": SUPER10["url"], "tamano": 1.0 if por_kg else None})
        log.append(f"Super10: {len(ofertas)} ofertas leídas")
        for o in ofertas:
            log.append(f"Super10 · oferta: {o['nombre']} ${o['precio']}")
        for prod in CANASTA["productos"]:
            e = elegir(prod, ofertas)
            if e:
                resultado["productos"][prod["id"]] = e
    except Exception as e:
        resultado["estado"] = "error"
        log.append(f"Super10: error {e}")
    await contexto.close()
    return resultado


def agregar_historial(productos: dict, hoy) -> int:
    """Agrega a cada precio cómo estaba hace 7 días y el mínimo de los últimos 30 días."""
    archivos = {}
    for f in sorted(HISTORIAL.glob("*.json")) if HISTORIAL.exists() else []:
        try:
            d = datetime.strptime(f.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if 0 < (hoy - d).days <= 30:
            archivos[d] = f
    if not archivos:
        return 1
    # referencia "hace una semana": el último día con datos que tenga 7 días o más; si aún no hay, el más antiguo
    antiguos = [d for d in archivos if (hoy - d).days >= 7]
    ref = max(antiguos) if antiguos else min(archivos)
    historia: dict[tuple, list] = {}
    for d, f in archivos.items():
        try:
            datos = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in datos.get("productos", []):
            for tid, x in p.get("precios", {}).items():
                if x.get("fecha") == d.isoformat():  # solo precios realmente leídos ese día
                    historia.setdefault((p["id"], tid), []).append((d, x["precio"]))
    for pid, p in productos.items():
        for tid, x in p["precios"].items():
            h = historia.get((pid, tid), [])
            en_ref = [pr for d, pr in h if d == ref]
            if en_ref:
                x["antes"] = {"precio": en_ref[0], "fecha": ref.isoformat()}
            if h:
                d_min, pr_min = min(h + [(hoy, x["precio"])], key=lambda t: t[1])
                x["min_30d"] = {"precio": pr_min, "fecha": d_min.isoformat()}
    return len(archivos) + 1


def totales_canasta(datos: dict) -> tuple[list[dict], list[str]]:
    """Totales comparables: solo se suman los productos que tienen todas las tiendas comparadas."""
    canasta = datos["canasta_basica"]
    prods = {p["id"]: p for p in datos["productos"]}
    tiendas = [t for t in datos["tiendas"] if t["productos"] and not t.get("solo_ofertas")]
    # se dejan fuera las tiendas a las que les falta más del 20% de la canasta
    tiendas = [t for t in tiendas if sum(1 for pid in canasta if t["id"] in prods.get(pid, {}).get("precios", {})) >= 0.8 * len(canasta)]
    comunes = [pid for pid in canasta if pid in prods and all(t["id"] in prods[pid]["precios"] for t in tiendas)]
    filas = [{"nombre": t["nombre"], "id": t["id"], "total": sum(prods[pid]["precios"][t["id"]]["precio"] * canasta[pid] for pid in comunes)} for t in tiendas]
    filas.sort(key=lambda f: f["total"])
    return filas, comunes


async def generar_imagen(datos: dict):
    """Crea datos/canasta-hoy.png: una imagen lista para compartir en redes o WhatsApp."""
    filas, comunes = totales_canasta(datos)
    if len(filas) < 2:
        return
    ganador, mas_caro = filas[0], filas[-1]
    maximo = max(f["total"] for f in filas)
    fmt = lambda v: "$" + f"{round(v):,}".replace(",", ".")
    fecha = datetime.fromisoformat(datos["actualizado"])
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    n_items = sum(datos["canasta_basica"][pid] for pid in comunes)
    barras = "".join(
        f'<div class="fila{" win" if f is ganador else ""}"><span class="nm">{f["nombre"]}</span>'
        f'<span class="tr"><span style="width:{f["total"] / maximo * 100:.1f}%"></span></span>'
        f'<span class="tt">{fmt(f["total"])}</span></div>' for f in filas)
    # dónde conviene cada proteína y básico clave hoy
    prods = {p["id"]: p for p in datos["productos"]}
    claves = ["trutro-pollo", "pechuga-pollo", "carne-molida", "jurel", "huevos", "arroz", "aceite", "detergente"]
    nombres_t = {t["id"]: t["nombre"] for t in datos["tiendas"]}
    # agrupado por supermercado: qué conviene comprar en cada uno
    grupos: dict[str, list] = {}
    for pid in claves:
        pr = prods.get(pid, {}).get("precios", {})
        if pr:
            tid, x = min(pr.items(), key=lambda kv: kv[1]["precio"])
            grupos.setdefault(tid, []).append(
                f'<div class="li"><span>{prods[pid]["nombre"]}<small> · {str(prods[pid]["cantidad"]).replace(".", ",")} {prods[pid]["unidad"]}</small></span><b>{fmt(x["precio"])}</b></div>')
    orden = sorted(grupos.items(), key=lambda kv: -len(kv[1]))
    items = "".join(f'<div class="it"><em>{nombres_t.get(tid, tid)}</em>{"".join(lis)}</div>' for tid, lis in orden)
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@600;800&family=Figtree:wght@500;600&family=Archivo+Narrow:wght@700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0}} body{{width:1080px;height:1350px;background:#f4f4f7;color:#23232b;font-family:Figtree,sans-serif;padding:60px 64px;display:flex;flex-direction:column;gap:24px}}
h1{{font-family:'Public Sans',sans-serif;font-weight:800;font-size:84px;line-height:1}} h1 span{{background:#cdb8ff;padding:0 12px;border-radius:8px}}
.sub{{font-size:34px;color:#63636f}} .hero{{background:#cdb8ff;border-radius:24px;padding:28px 36px}}
.hero .l{{font-size:30px;font-weight:600;opacity:.8}}
.hero .b{{font-family:'Public Sans',sans-serif;font-weight:800;font-size:72px;line-height:1.1}}
.hero .b em{{font-style:normal;font-family:'Archivo Narrow',sans-serif}} .hero p{{font-size:32px;margin-top:10px}}
.filas{{display:flex;flex-direction:column;gap:16px}} .fila{{display:grid;grid-template-columns:260px 1fr 190px;gap:20px;align-items:center;font-size:34px;font-weight:600}}
.tr{{height:44px;background:#e0dfe6;border-radius:10px;overflow:hidden}} .tr span{{display:block;height:100%;background:#c4bcd9;border-radius:10px}}
.win .tr span{{background:#6a4cd6}} .tt{{text-align:right;font-family:'Archivo Narrow',sans-serif;font-size:42px}}
.pie{{margin-top:auto;font-size:26px;color:#63636f;display:flex;flex-direction:column;gap:6px}}
h2{{font-family:'Public Sans',sans-serif;font-size:36px}} .its{{display:grid;grid-template-columns:1fr 1fr;gap:16px 24px;align-items:start}}
.it{{display:flex;flex-direction:column;gap:8px;background:#fff;border:2px solid #e0dfe6;border-radius:16px;padding:16px 20px;font-size:24px;font-weight:600}}
.li{{display:flex;justify-content:space-between;align-items:baseline;gap:10px}}
.it small{{font-size:19px;color:#63636f;font-weight:500}} .it b{{font-family:'Archivo Narrow',sans-serif;font-size:30px;white-space:nowrap}}
.it em{{font-style:normal;font-family:'Public Sans',sans-serif;font-weight:800;font-size:28px;color:#6a4cd6}}
.pie b{{color:#23232b}}
</style></head><body>
<h1>Caza<span>precios</span></h1>
<div class="sub">Canasta básica de {n_items} productos · {fecha.day} de {meses[fecha.month - 1]}</div>
<div class="hero"><div class="l">Más barata hoy</div><div class="b">{ganador["nombre"]} · <em>{fmt(ganador["total"])}</em></div>
{f'<p>Ahorras <b>{fmt(mas_caro["total"] - ganador["total"])}</b> frente a {mas_caro["nombre"]}</p>' if mas_caro else ''}</div>
<div class="filas">{barras}</div>
<h2>Qué conviene comprar en cada supermercado</h2><div class="its">{items}</div>
<div class="pie"><span>Precios de las tiendas online de cada supermercado; en el local pueden ser distintos.</span><b>cazaprecios.cl</b></div>
</body></html>"""
    async with async_playwright() as p:
        nav = await p.chromium.launch()
        pagina = await nav.new_page(viewport={"width": 1080, "height": 1350})
        await pagina.set_content(html, wait_until="networkidle")
        await pagina.screenshot(path=str(RAIZ / "datos" / "canasta-hoy.png"))
        await nav.close()


# ---------------------------------------------------------------- locales (OpenStreetMap)

LOCALES = RAIZ / "datos" / "locales.json"
VERSION_LOCALES = 2  # subir este número obliga a descargar los locales de nuevo
MARCAS = {  # nombre en OpenStreetMap -> id de la cadena en Cazaprecios
    "jumbo": "jumbo", "santa isabel": "santaisabel", "unimarc": "unimarc", "alvi": "alvi",
    "super 10": "super10", "super10": "super10", "mayorista 10": "super10",
    "lider": "lider", "express de lider": "lider", "super lider": "lider", "hiper lider": "lider", "lider express": "lider",
    "tottus": "tottus", "hiper tottus": "tottus",
    "acuenta": "acuenta", "superbodega acuenta": "acuenta", "super bodega acuenta": "acuenta",
}
CONSULTA_OSM = """[out:json][timeout:180];
area["ISO3166-1"="CL"][admin_level=2]->.cl;
(
  nwr["shop"~"supermarket|wholesale|convenience"]["brand"~"^(Jumbo|Santa Isabel|Unimarc|Alvi|Super ?10|Mayorista 10|L[ií]der|Express de L[ií]der|Tottus|a?Cuenta|Super ?Bodega a?Cuenta)$",i](area.cl);
  nwr["shop"~"supermarket|wholesale|convenience"]["name"~"^(Jumbo|Santa Isabel|Unimarc|Alvi|Super ?10|Mayorista 10|(Super |Hiper |Express de )?L[ií]der|(Hiper )?Tottus|(Super ?Bodega )?a?Cuenta)( |$)",i](area.cl);
);
out center tags;"""


def actualizar_locales(log: list):
    """Una vez por semana descarga de OpenStreetMap la ubicación de los locales de cada cadena."""
    if LOCALES.exists():
        try:
            previo = json.loads(LOCALES.read_text(encoding="utf-8"))
            edad = datetime.now(CHILE) - datetime.fromisoformat(previo["actualizado"])
            if edad < timedelta(days=7) and previo.get("locales") and previo.get("v") == VERSION_LOCALES:
                return
        except Exception:
            pass
    try:
        datos = urllib.parse.urlencode({"data": CONSULTA_OSM}).encode()
        req = urllib.request.Request("https://overpass-api.de/api/interpreter", data=datos,
                                     headers={"User-Agent": "Cazaprecios/1.0 (https://cazaprecios.cl)"})
        with urllib.request.urlopen(req, timeout=240) as r:
            elementos = json.loads(r.read().decode("utf-8"))["elements"]
    except Exception as e:
        log.append(f"Locales: no se pudo consultar OpenStreetMap ({e}); se mantienen los anteriores")
        return
    locales, vistos = [], set()
    for el in elementos:
        t = el.get("tags", {})
        marca = normalizar(t.get("brand") or t.get("name") or "")
        tienda = next((v for k, v in MARCAS.items() if marca == k or marca.startswith(k + " ")), None)
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if not tienda or lat is None or lon is None:
            continue
        clave = (tienda, round(lat, 3), round(lon, 3))
        if clave in vistos:
            continue
        vistos.add(clave)
        direccion = " ".join(x for x in [t.get("addr:street"), t.get("addr:housenumber")] if x)
        comuna = t.get("addr:city") or t.get("addr:municipality") or ""
        locales.append({"t": tienda, "n": t.get("name") or t.get("brand"), "d": direccion, "c": comuna,
                        "lat": round(lat, 5), "lon": round(lon, 5)})
    if len(locales) < 50:  # algo salió mal: se conservan los datos anteriores
        log.append(f"Locales: OpenStreetMap devolvió solo {len(locales)} locales; se mantienen los anteriores")
        return
    LOCALES.write_text(json.dumps({"v": VERSION_LOCALES, "actualizado": datetime.now(CHILE).isoformat(timespec="minutes"),
                                   "fuente": "© OpenStreetMap contributors (ODbL)", "locales": locales},
                                  ensure_ascii=False), encoding="utf-8")
    por = {}
    for l in locales:
        por[l["t"]] = por.get(l["t"], 0) + 1
    log.append(f"Locales actualizados desde OpenStreetMap: {por}")


async def principal():
    ahora = datetime.now(CHILE)
    anterior = json.loads(SALIDA.read_text(encoding="utf-8")) if SALIDA.exists() else None
    log: list[str] = []
    if DEPURACION.exists():  # se borra la depuración del día anterior
        for f in DEPURACION.iterdir():
            f.unlink()
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
        previo = {pr["id"]: pr["precios"][tid] for pr in ((anterior or {}).get("productos") or [])
                  if tid in pr.get("precios", {}) and pr["id"] in productos}
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

    actualizar_locales(log)
    dias_historial = agregar_historial(productos, ahora.date())
    datos = {
        "actualizado": ahora.isoformat(timespec="minutes"),
        "dias_historial": dias_historial,
        "tiendas": tiendas,
        "productos": list(productos.values()),
        "canasta_basica": CANASTA["canasta_basica"],
    }
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    HISTORIAL.mkdir(parents=True, exist_ok=True)
    (HISTORIAL / f"{fecha}.json").write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    (RAIZ / "datos" / "registro.txt").write_text("\n".join(log), encoding="utf-8")
    try:
        await generar_imagen(datos)
    except Exception as e:  # la imagen es un extra: si falla, los precios igual se publican
        print(f"No se pudo generar la imagen: {e}")
    print("\n".join(log))
    print("\nResumen:")
    for t in tiendas:
        print(f"  {t['nombre']}: {t['estado']} ({t['productos']} productos)")


if __name__ == "__main__":
    asyncio.run(principal())
