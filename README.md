# Cazaofertas Chile

Sitio gratuito que compara cada día dónde sale más barata la canasta básica en los supermercados de Chile, con prioridad en proteínas (pollo, carne, pescado, huevos), alimentos no perecibles y productos de limpieza.

## Cómo funciona

1. Todos los días a las 06:17 (hora de Chile), GitHub ejecuta el proceso **Actualizar precios** (`.github/workflows/actualizar-precios.yml`).
2. El recolector (`recolector/recolectar.py`) abre las páginas públicas de búsqueda de cada supermercado, como lo haría una persona, con pausas entre búsquedas y respetando el `robots.txt` de cada sitio.
3. Para cada producto de `recolector/canasta.json`, elige en cada tienda el más barato que cumple la descripción (cualquier marca) y lleva su precio al mismo formato para comparar de forma justa.
4. Los resultados quedan en `datos/precios.json` y el historial diario en `datos/historial/`.
5. El sitio (`index.html`) lee esos datos y se publica con GitHub Pages.

Si un día una tienda no responde, se mantienen sus últimos precios y el sitio muestra la fecha de esos precios.

## Cambiar los productos de la canasta

Edita `recolector/canasta.json` directamente en GitHub (botón del lápiz). Cada producto tiene:

- `buscar`: lo que se escribe en el buscador del supermercado.
- `debe`: palabras que el nombre del producto tiene que contener.
- `excluir`: palabras que descartan el producto.
- `cantidad` y `unidad`: el formato de referencia para comparar.

## Correr la actualización a mano

En la pestaña **Actions** → **Actualizar precios** → **Run workflow**.

## Tiendas

Jumbo, Santa Isabel, Tottus, Lider, Unimarc, Alvi y Super10 (ex Mayorista 10, solo publica sus ofertas). Acuenta está pendiente.

Los precios son referenciales: corresponden a lo publicado en cada sitio web y pueden variar según el local.
