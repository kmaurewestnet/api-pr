"""Consultas del endpoint de analíticas por empresa.

Napear y soldef son las consultas provistas en query.txt, con los valores
hardcodeados pasados a parámetros %s.

La consulta a Zabbix se reescribió: el original cruzaba por MAC contra
history_text.value, una columna sin índice, escaneando la tabla entera en cada
request y por cada métrica. Ahora se cruza por precinto contra items.name, que
contiene el mismo dato y vive en una tabla chica e indexada. Ver el comentario
de Q_ZBX_METRICAS.

La consulta de RX quedó fuera de alcance por pedido del usuario.
"""

# --- Paso 1: MySQL napear -----------------------------------------------------
# Params  : (empresa_id,)
# Columnas: empresa_id, empresa, nap_tag, serial
#
# `serial` es external_connector_id, que cruza contra dispositivos_bocas.id en
# soldef. estado_id = 3 es el estado instalado.
Q_NAPEAR_ONTS_POR_EMPRESA = """
SELECT r.empresa_id, e.nombre as empresa, rr.nap_tag, rr.external_connector_id as serial
FROM registros r
INNER JOIN registro_reservas rr ON r.id = rr.registro_id
INNER JOIN empresas e ON r.empresa_id = e.id
INNER JOIN installation_sheets i ON r.id = i.reservation
WHERE r.empresa_id = %s
  AND r.estado_id = 3
  AND rr.external_connector_id IS NOT NULL
ORDER BY r.id
"""

# --- Paso 2: PostgreSQL soldef ------------------------------------------------
# Params  : (0, lista_de_seriales)  -> el primer parámetro es el flag "traer todo"
# Columnas: serial, precinto, mac
#
# La MAC se deriva de dispositivos.nro_serie traduciendo el prefijo de fabricante
# (HWTC, ZTEG, ...) a su representación hexadecimal, que es el formato con el que
# Zabbix guarda el serial de la ONT.
Q_SOLDEF_ONUS_POR_IDS = """
SELECT
b.id as serial,
p.etiqueta as precinto,
replace(replace(replace(replace(replace(replace(replace(replace(replace(d.nro_serie, 'HWTC', '48575443'), 'ZTEG', '5A544547'), 'VSOL', '56534F4C'), 'RTEG', '52544547'), 'TPLG', '54504C47'), 'NBEL', '4E42454C'), 'MSTC', '4D535443'), 'GPON', '47504F4E'), 'ASKY', '41534B59') as mac
FROM dispositivos_bocas b
INNER JOIN dispositivos_onuses o ON b.onu_id = o.id
LEFT JOIN dispositivos_precintos p ON b.precinto_id = p.id
INNER JOIN dispositivos d ON o.dispositivo_id = d.id
WHERE (%s = 0) OR (b.id = ANY(%s))
ORDER BY b.id
"""

# --- Paso 3: PostgreSQL zabbix ------------------------------------------------
# Params  : (lista_de_precintos, desde_epoch, desde_epoch)
#           desde_epoch = 0 desactiva el filtro temporal (mismo patrón de flag
#           que usa la consulta de soldef).
# Columnas: precinto, metrica ('status' | 'los'), valor, clock
#
# El filtro de tiempo es opcional porque ya no hace falta para el rendimiento:
# al estar acotada por itemid, `DISTINCT ON (itemid) ORDER BY itemid, clock DESC`
# entra por el índice (itemid, clock) y trae el último valor sin recorrer todo.
# Con ventana fija se perdían ONUs cuyo último dato era más viejo que la ventana.
#
# El precinto viaja embebido en items.name, entre 'descr_' y '_odb':
#   (WESTNET) 302381 - LAURA MARCELA DIAZ_zone_CIUDAD_descr_WN12753_odb_GLL-...
#                                                          ^^^^^^^ precinto
# El replace('_',' ') final es el que convierte 'descr_PR_W3165_odb' en
# 'PR W3165', que es el formato exacto en que soldef devuelve el precinto.
# Es la misma expresión que usa queries/precinto.py.
#
# Resolver la identidad contra `items` (chica, indexada por key_/hostid) en vez
# de contra history_text.value (sin índice, millones de filas) es lo que hace
# viable la consulta con 40.000 dispositivos. El acceso a la historia queda por
# itemid, que entra por la PK (itemid, clock).
Q_ZBX_METRICAS = """
WITH precintos AS (
    -- unnest va en el FROM: PostgreSQL 10+ no admite funciones que devuelven
    -- conjuntos anidadas dentro de otra función en la lista del SELECT.
    SELECT DISTINCT upper(trim(regexp_replace(t.p, '\\s+', ' ', 'g'))) AS precinto
    FROM unnest(%s::text[]) AS t(p)
),
items_metrica AS (
    SELECT i.itemid,
           upper(trim(regexp_replace(replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' '), '\\s+', ' ', 'g'))) AS precinto,
           CASE WHEN i.key_ LIKE '%%OnlineState%%' THEN 'status' ELSE 'los' END AS metrica
    FROM items i
    INNER JOIN hosts h ON h.hostid = i.hostid
    WHERE (i.key_ LIKE '%%OnlineState%%' OR i.key_ LIKE '%%hwGponDeviceOntAlarmLOSi%%')
      AND i.status = 0
      AND h.status = 0
      AND i.name LIKE '%%descr\\_%%'
)
SELECT DISTINCT ON (im.itemid)
    im.precinto,
    im.metrica,
    hs.value AS valor,
    hs.clock AS clock
FROM precintos p
INNER JOIN items_metrica im ON im.precinto = p.precinto
INNER JOIN history_str hs ON hs.itemid = im.itemid
WHERE (%s = 0 OR hs.clock >= %s)
ORDER BY im.itemid, hs.clock DESC
"""


# --- Paso 3b: última causa de caída (LastDownCause) ---------------------------
# Params  : (lista_de_precintos, desde_epoch, desde_epoch)
# Columnas: precinto, ldc, ldc_timestamp, clock
#
# Va aparte de Q_ZBX_METRICAS porque hwGponDeviceOntControlLastDownCause es un
# item de TEXTO: sus valores viven en history_text, no en history_str.
#
# El valor llega como 'Dying-gasp-(2026-07-17 22:11:11)' y se parte en dos con el
# mismo criterio que usa queries/precinto.py: la causa antes de '-(' y la fecha
# adentro del paréntesis. Esa fecha no trae zona horaria, así que se interpreta
# con la de la base (current_setting('TimeZone')) y no como UTC, que correría el
# epoch varias horas.
#
# El CASE evita que un valor con fecha malformada aborte toda la consulta: en ese
# caso devuelve la causa con ldc_timestamp en NULL en vez de fallar.
Q_ZBX_LDC = """
WITH precintos AS (
    SELECT DISTINCT upper(trim(regexp_replace(t.p, '\\s+', ' ', 'g'))) AS precinto
    FROM unnest(%s::text[]) AS t(p)
),
items_ldc AS (
    SELECT i.itemid,
           upper(trim(regexp_replace(replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' '), '\\s+', ' ', 'g'))) AS precinto
    FROM items i
    INNER JOIN hosts h ON h.hostid = i.hostid
    WHERE i.key_ LIKE '%%hwGponDeviceOntControlLastDownCause%%'
      AND i.status = 0
      AND h.status = 0
      AND i.name LIKE '%%descr\\_%%'
)
SELECT DISTINCT ON (im.itemid)
    im.precinto,
    split_part(ht.value, '-(', 1) AS ldc,
    CASE WHEN replace(split_part(ht.value, '-(', 2), ')', '')
              ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$'
         THEN extract(epoch FROM (
                  replace(split_part(ht.value, '-(', 2), ')', '')::timestamp
                  AT TIME ZONE current_setting('TimeZone')))::bigint
    END AS ldc_timestamp,
    ht.clock AS clock
FROM precintos p
INNER JOIN items_ldc im ON im.precinto = p.precinto
INNER JOIN history_text ht ON ht.itemid = im.itemid
WHERE (%s = 0 OR ht.clock >= %s)
  AND replace(split_part(ht.value, '-(', 2), ')', '') <> ''
  AND split_part(ht.value, '-(', 1) <> 'Query-fails'
ORDER BY im.itemid, ht.clock DESC
"""


_REQUERIDAS = {
    "Q_NAPEAR_ONTS_POR_EMPRESA": "napear",
    "Q_SOLDEF_ONUS_POR_IDS": "soldef",
    "Q_ZBX_METRICAS": "zabbix",
    "Q_ZBX_LDC": "zabbix",
}


def faltantes() -> list:
    """Nombres de las queries que todavía no fueron definidas."""
    return [n for n in _REQUERIDAS if globals().get(n) is None]
