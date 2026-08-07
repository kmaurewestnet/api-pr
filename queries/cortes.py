"""Consultas del endpoint de detección de cortes.

Todas las consultas de `documentacion_api_cortes_v1.md` se pasaron a parámetros
bind (`%s`): el documento original interpolaba `$code`, `$ip`, `$oltip` y `$nap`
directamente en el string, que es inyección SQL servida.

Tres cambios respecto del documento, todos deliberados:

1. `$__unixEpochGroupAlias(h.clock,'1m')` es una macro de Grafana, no SQL. Se
   reemplazó por `floor(h.clock/60)*60 AS time`, que es su expansión y la misma
   expresión que ya usa queries/precinto.py.
2. Las consultas de estado LOS y de estado Online eran idénticas salvo por
   `i.key_`, así que se unificaron en una sola con una columna `metrica`. Un
   viaje a la base en vez de dos.
3. La NAP se extrae del nombre del item con UNA sola expresión (`_NAP_EXTRAIDA`).
   El documento usaba una versión corta para la topología y una larga para el
   estado de NAP, y después comparaba una contra la otra: cualquier NAP cuyo
   nombre incluyera '-AP', 'au', '1084M' o '288e' se extraía distinto en cada
   lado y el estado nunca podía matchear. Se usa la larga en ambos lados.

Todos los identificadores de cliente que entran a un `~*` vienen validados como
dígitos (ver routers/cortes.py), así que no pueden aportar metacaracteres de
regex. La IP se escapa explícitamente en services/cortes.py.
"""

# --- Fragmentos reutilizados --------------------------------------------------

# Precinto/NAP embebido en items.name. Es la expresión larga del documento: la
# corta más las normalizaciones '-AP', 'au', '1084M' y '288e'.
_NAP_EXTRAIDA = (
    "replace(replace(split_part(split_part(split_part(split_part(split_part("
    "replace(replace(replace(replace(replace(replace("
    "split_part(split_part(split_part(i.name,'odb_',2),'_aut',1),'extid',1),"
    "'_',''),'-SP-16',''),'-SP-08',''),'-SP-8',''),'-SP-24',''),'-SP8',''),"
    "'lat', 1),'SP', '1'),'-PISO', 1),'-AP',1),'au',1),'1084M', '1084'),"
    "'288e','288')"
)

# El nro de cliente viaja en items.name antes de '_zone'. El regex exige que no
# esté pegado a otro dígito, para que 3023 no matchee con 30231.
_MATCH_CODE = (
    "split_part(i.name, '_zone', 1) ~* ('(^|[^0-9])' || %s || '([^0-9]|$)')"
)

# En las OLT Solar el nombre del item tiene otro formato y el cliente hay que
# sacarlo de después de 'ONU LOSi'. Es la expresión del documento actualizado; lo
# único que se le cambió es el `~* '$code'` pelado por el mismo regex con
# frontera de dígitos que usa _MATCH_CODE: con el match suelto, el cliente 8870
# daba positivo contra los items del 88704.
_MATCH_CODE_SOLAR = (
    "replace(replace(replace(split_part(split_part(i.name,'ONU LOSi',2),"
    "'_zone',1),'_',' '),'(',''),')','')"
    " ~* ('(^|[^0-9])' || %s || '([^0-9]|$)')"
)


# Filtro de key_ **anclado al principio**, que es lo único que puede entrar por
# índice. Las keys de Zabbix tienen la forma:
#
#     hwGponDeviceOntAlarmLOSi.[{#SNMPINDEX}]
#     hwGponDeviceOntAlarmLOSi.[4194312192.0]
#
# o sea el patrón es un prefijo, no una subcadena. Con `LIKE 'patrón%%'` y el
# índice `items_key_pattern_idx` (ver README) se evita recorrer las ~156.000
# filas de `items`, que era el costo dominante de las tres consultas del camino
# FTTH: medido, 616 ms de los 645 ms de la consulta de estado.
#
# Se pasó por dos etapas y las dos están medidas: `~*` (811 ms de scan) ->
# `ILIKE '%%patrón%%'` (616 ms, saca el motor de regex pero sigue escaneando
# todo) -> `LIKE 'patrón%%'` (entra por índice).
#
# LIKE es sensible a mayúsculas, a diferencia del `~*` original. Es correcto
# porque la capitalización de estas keys está verificada contra la base; si
# alguna vez cambia, la consulta devuelve cero filas en silencio y el estado de
# ONT queda en "no evaluable". El chequeo está en el README.
_KEY_LOS = "i.key_ LIKE 'hwGponDeviceOntAlarmLOSi%%'"
_KEY_ONLINE = "i.key_ LIKE 'hwGponDeviceOntEthernetOnlineState%%'"


def _nap_normalizada(col: str) -> str:
    """Rellena con ceros la parte numérica de la NAP para poder compararla con
    `nap_ocupacion.nap`, que la guarda con ancho fijo. Tal cual el documento."""
    return f"""CASE
        WHEN LENGTH(split_part({col},'-',2))=2 AND {col} ~* '-0' THEN replace({col},'-0','-00')
        WHEN LENGTH(split_part({col},'-',2))=2 AND {col} !~* '-0' THEN replace({col},'-','-0')
        WHEN LENGTH(split_part({col},'-',2))=3 AND {col} !~* '-0' THEN replace({col},'-','-0')
        WHEN LENGTH(split_part({col},'-',2))=3 AND {col} ~* '-0' THEN replace({col},'-0','-00')
        ELSE {col}
    END"""


# --- Paso 1: MySQL Gestión ----------------------------------------------------
# Params  : (nro_cliente, categoria_ftth_id, categoria_wireless_id)
# Columnas: nro_cliente, categoria_id, categoria, ip
#
# `categoria_id` no estaba en el documento, que devolvía solo `cat.name`: es el
# dato con el que se decide isFtth (16 = fibra, 17 = wireless). El nombre queda
# igual en la respuesta de la consulta, pero solo para el log.
#
# Los dos IDs entran como parámetros en vez de literales para que salgan del
# mismo lugar que usa services/cortes.es_ftth() — ver config.CATEGORIA_FTTH_ID.
Q_GESTION_CLIENTE = """
SELECT DISTINCT
    CAST(cus.code AS SIGNED) AS nro_cliente,
    cat.category_id AS categoria_id,
    cat.name AS categoria,
    INET_NTOA(conn.ip4_1) AS ip
FROM customer cus
LEFT JOIN contract cont ON cus.customer_id = cont.customer_id
LEFT JOIN connection conn ON conn.contract_id = cont.contract_id
LEFT JOIN contract_detail cd ON cd.contract_id = cont.contract_id
LEFT JOIN product p ON cd.product_id = p.product_id
LEFT JOIN product_has_category pc ON p.product_id = pc.product_id
LEFT JOIN category cat ON pc.category_id = cat.category_id
WHERE cus.code = %s
  AND cont.status = 'active'
  AND p.type = 'plan'
  AND cat.category_id IN (%s, %s)
"""


# --- Paso 2A: topología de fibra (Zabbix Fibra) -------------------------------
# Params  : (nro_cliente,)
# Columnas: nap, olt_nombre, olt_ip
Q_ZBX_TOPOLOGIA_FTTH = f"""
SELECT DISTINCT
    {_NAP_EXTRAIDA} AS nap,
    h.host AS olt_nombre,
    intf.ip AS olt_ip
FROM items i
LEFT JOIN hosts h ON i.hostid = h.hostid
LEFT JOIN interface intf ON h.hostid = intf.hostid AND intf.main = 1
WHERE i.key_ ~* 'rx.ont'
  AND i.name !~* 'bigway'
  AND {_MATCH_CODE}
"""

# --- Paso 2A bis: switch del nodo de la OLT (Soldef) --------------------------
# Params  : (olt_ip,)
# Columnas: nombre, ip, nodo_id
#
# rol = 3 es el switch. El `!~*` descarta la IP de gestión del propio nodo.
Q_SOLDEF_SWITCH = """
SELECT a.nombre, a.ip, a.nodo_id
FROM aparatos a
INNER JOIN (SELECT nodo_id FROM aparatos WHERE ip = %s) AS n
        ON a.nodo_id = n.nodo_id
WHERE a.rol = 3
  AND a.ip !~* concat('10.', a.nodo_id)
"""


# --- Paso 3A1: estado de la ONT del cliente (OLT no Solar) --------------------
# Params  : (nro_cliente,)
# Columnas: metrica ('los' | 'estado'), valor, time
#
# Unifica las dos consultas del documento: son la misma salvo por i.key_.
Q_ZBX_ESTADO_CLIENTE = f"""
SELECT DISTINCT ON (i.itemid)
    CASE WHEN {_KEY_LOS} THEN 'los' ELSE 'estado' END AS metrica,
    h.value AS valor,
    floor((h.clock)/60)*60 AS time
FROM items i
JOIN hosts ho ON i.hostid = ho.hostid
JOIN history_str h ON i.itemid = h.itemid
WHERE ({_KEY_LOS} OR {_KEY_ONLINE})
  AND i.name !~* 'bigway'
  AND {_MATCH_CODE}
ORDER BY i.itemid, h.clock DESC
"""

# --- Paso 3A1: estado de la NAP (OLT no Solar) --------------------------------
# Params  : (nap,)
# Columnas: nap, total_clientes, clientes_caidos
#
# Devuelve los contadores crudos, no 'up'/'down' como el documento: el umbral
# vive una sola vez, en services/cortes.nap_caida(), y así lo comparte con el
# camino Solar (que lo resuelve por SNMP y no tiene esta consulta).
#
# El CTE `logs_filtrados` del documento se eliminó: repetía el filtro por NAP que
# `ultimos_logs` ya aplica.
Q_ZBX_ESTADO_NAP = f"""
WITH ultimos_logs AS (
    SELECT DISTINCT ON (i.itemid)
        {_NAP_EXTRAIDA} AS nap_extraida,
        g.value AS log
    FROM items i
    JOIN hosts h ON i.hostid = h.hostid
    JOIN history_str g ON i.itemid = g.itemid
    WHERE {_KEY_LOS}
      AND i.status = 0
      AND h.name !~* 'Solar'
      AND {_NAP_EXTRAIDA} = %s
    ORDER BY i.itemid, g.clock DESC
)
SELECT
    b.nap,
    b.clientes AS total_clientes,
    SUM(CASE WHEN a.log ~* 'los' THEN 1 ELSE 0 END) AS clientes_caidos
FROM nap_ocupacion b
INNER JOIN ultimos_logs a
        ON b.nap = ({_nap_normalizada('a.nap_extraida')})
GROUP BY b.nap, b.clientes
"""


# --- Paso 3A2: OIDs para consulta en vivo (OLT Solar) -------------------------
# Params  : (nro_cliente,)
# Columnas: nap_extraida, oid
#
# Las dos consultas individuales por ONT del documento actualizado. Son la misma
# salvo por `i.key_`, pero se dejan separadas porque cada una alimenta una señal
# distinta de `ont_caida`, igual que en el caso A1.
#
# Se les agregó `i.snmp_oid IS NOT NULL`: sin OID no hay nada que preguntarle a
# la OLT, y un NULL llegaría hasta el validador de OIDs para ser descartado ahí.
Q_ZBX_OID_LOS_CLIENTE = f"""
SELECT DISTINCT
    {_NAP_EXTRAIDA} AS nap_extraida,
    i.snmp_oid AS oid
FROM items i
JOIN hosts h ON i.hostid = h.hostid
WHERE {_KEY_LOS}
  AND h.name ~* 'Solar'
  AND {_MATCH_CODE_SOLAR}
  AND i.snmp_oid IS NOT NULL
"""

# Params  : (nro_cliente,)
# Columnas: nap_extraida, oid
Q_ZBX_OID_ONLINE_CLIENTE = f"""
SELECT DISTINCT
    {_NAP_EXTRAIDA} AS nap_extraida,
    i.snmp_oid AS oid
FROM items i
JOIN hosts h ON i.hostid = h.hostid
WHERE {_KEY_ONLINE}
  AND h.name ~* 'Solar'
  AND {_MATCH_CODE_SOLAR}
  AND i.snmp_oid IS NOT NULL
"""

# Params  : (nap,)
# Columnas: nap_extraida, oid
Q_ZBX_OIDS_LOS_NAP = f"""
SELECT DISTINCT
    {_NAP_EXTRAIDA} AS nap_extraida,
    i.snmp_oid AS oid
FROM items i
JOIN hosts h ON i.hostid = h.hostid
WHERE {_KEY_LOS}
  AND h.name ~* 'Solar'
  AND i.snmp_oid IS NOT NULL
  AND {_NAP_EXTRAIDA} = %s
"""

# Ocupación declarada de la NAP, para comparar contra los LOS que devuelva el
# SNMP. En el camino no-Solar este dato viene dentro de Q_ZBX_ESTADO_NAP.
# Params  : (nap,)
# Columnas: nap, total_clientes
Q_ZBX_OCUPACION_NAP = f"""
WITH entrada AS (SELECT %s::text AS nap_extraida)
SELECT b.nap, b.clientes AS total_clientes
FROM nap_ocupacion b, entrada n
WHERE b.nap = ({_nap_normalizada('n.nap_extraida')})
"""


# --- Paso 2B/3B: topología wireless (Zabbix Wireless) -------------------------
# Params  : (ip_cliente_escapada_como_regex,)
# Columnas: host, ip
Q_ZBX_WIFI_AP = """
SELECT DISTINCT h.host, intf.ip
FROM items i
LEFT JOIN hosts h ON i.hostid = h.hostid
LEFT JOIN interface intf ON h.hostid = intf.hostid AND intf.main = 1
WHERE i.key_ ~* 'ubntCPEmac'
  AND i.name ~* %s
"""

# Params  : (ip_del_routerboard,)
# Columnas: host, ip
#
# El documento calculaba la IP del RB dentro del SQL:
#   concat('10.', split_part(split_part('$ip','.',2),'.',1), '.0.', '1')
# es decir 10.<segundo octeto de la IP del cliente>.0.1. Se calcula en Python
# (services/cortes._ip_routerboard) para no meter la IP cruda en el string.
Q_ZBX_WIFI_RB = """
SELECT DISTINCT h.host, intf.ip
FROM items i
LEFT JOIN hosts h ON i.hostid = h.hostid
LEFT JOIN interface intf ON h.hostid = intf.hostid AND intf.main = 1
WHERE intf.ip = %s
"""
