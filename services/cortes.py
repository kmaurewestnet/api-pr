"""Detección de cortes de cliente: orquestación y matriz de decisión.

Flujo (documentacion_api_cortes_v1.md):

    Gestión (MySQL)  ->  tecnología + IP del cliente
      |
      +-- FTTH      -> Zabbix Fibra: NAP, OLT y su IP
      |                   +-- OLT normal : estado de ONT y de NAP por consulta
      |                   +-- OLT "Solar": estado de ONT y de NAP por snmpget
      |                 Soldef: switch del nodo de la OLT
      |
      +-- Wireless  -> Zabbix Wireless: Access Point y RouterBoard

Las verificaciones del último paso (pings, snmpget y consultas de estado) son
independientes entre sí y corren en un ThreadPoolExecutor. El endpoint se declara
`def`, así que FastAPI ya lo ejecuta en su threadpool: el event loop nunca se
bloquea, ni por el subproceso de ping ni por psycopg2/mysql-connector, que son
drivers sincrónicos.

Política de errores:

* Gestión caída, o la consulta de topología caída  -> 503. Sin eso no hay
  respuesta posible: `isFtth` y `isZoneIncident` serían inventados.
* Cualquier verificación individual que falle      -> queda en `None`
  ("no evaluable"), se loguea, y no cuenta como falla en la matriz.
* Soldef caída                                     -> solo se pierde el ping al
  switch, que según la matriz nunca cambia el resultado.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import config
import db
from queries import cortes as q
from services import red
from services.cache import CacheTTL

log = logging.getLogger(__name__)

# Estado que es idéntico para todos los clientes de una misma caja. Ver
# services/cache.py: lo que se cachea es el resultado final (el booleano de NAP,
# el del ping), no las consultas intermedias, así que un hit ahorra el walk SNMP
# entero. Nada por cliente entra acá.
_zona = CacheTTL(
    config.CACHE_ZONA_TTL_SEG,
    config.CACHE_MAX_ENTRADAS,
    nombre="zona",
    stale_max_seg=config.CACHE_STALE_MAX_SEG,
)


class ClienteNoEncontrado(LookupError):
    """El número de cliente no existe, no tiene contrato activo, o su plan no
    cae en las categorías 16/17."""


# --- Reglas puras (sin I/O): son las que se testean --------------------------

def es_ftth(categoria_id) -> bool:
    """isFtth por `category.category_id`: 16 es fibra, 17 es wireless.

    Se decide sobre el ID y no sobre `category.name` —que es lo que el documento
    llamaba "categoria"— porque el nombre es texto libre y cambia con cada plan
    nuevo. El nombre se sigue trayendo, pero solo va al log.
    """
    if categoria_id is None:
        return False
    try:
        return int(categoria_id) == config.CATEGORIA_FTTH_ID
    except (TypeError, ValueError):
        log.warning("category_id no numérico: %r", categoria_id)
        return False


def _codigo_snmp(texto, codigos_si, codigos_no, metrica) -> bool | None:
    """Traduce el entero crudo de un snmpget a booleano.

    Un código que no esté en ninguna de las dos listas queda en None y se
    loguea con su valor. La regla anterior era `int(valor) != 0`, que daba
    alarma para *cualquier* código no nulo: si la OLT contesta con un enum
    (1 = normal, 2 = alarma, o al revés), toda ONT sana se reportaba caída y
    de ahí la NAP entera. Un código desconocido tiene que degradar, no afirmar.
    """
    codigo = int(texto)
    if codigo in codigos_si:
        return True
    if codigo in codigos_no:
        return False
    log.warning(
        "Código SNMP %s sin traducir para %s: agregalo a SNMP_COD_* en el .env",
        codigo, metrica,
    )
    return None


def hay_los(valor) -> bool | None:
    """Alarma óptica activa.

    Acepta el texto ya mapeado que guarda Zabbix en su historial ('LOS',
    'No Alarm') y el entero crudo que devuelve un snmpget directo a la OLT.
    """
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    bajo = texto.lower()
    if "no alarm" in bajo or "noalarm" in bajo:
        return False
    if "los" in bajo:
        return True
    if texto.lstrip("-").isdigit():
        return _codigo_snmp(texto, config.SNMP_COD_LOS, config.SNMP_COD_SIN_LOS, "LOS")
    return False


def esta_offline(estado) -> bool | None:
    """Item hwGponDeviceOntEthernetOnlineState, por historial o por SNMP crudo.
    Mismo criterio que services/analytics._es_online, invertido."""
    if estado is None:
        return None
    texto = str(estado).strip()
    if not texto:
        return None
    bajo = texto.lower()
    if "offline" in bajo:
        return True
    if "online" in bajo:
        return False
    if texto.lstrip("-").isdigit():
        return _codigo_snmp(
            texto, config.SNMP_COD_OFFLINE, config.SNMP_COD_ONLINE, "OnlineState"
        )
    return False


def ont_caida(los, estado) -> bool | None:
    """Estado de la ONT combinando las dos señales. None si no hay ninguna."""
    señales = [s for s in (hay_los(los), esta_offline(estado)) if s is not None]
    if not señales:
        return None
    return any(señales)


def nap_caida(clientes_caidos, total_clientes) -> bool | None:
    """Corte de caja: todos los clientes de la NAP reportando LOS.

    En NAPs de más de NAP_TOLERANCIA_DESDE clientes se acepta uno sin reportar
    (puede estar de baja o con el item deshabilitado). Es el mismo umbral que
    traía el CASE del documento, acá una sola vez para que el camino por
    consulta y el camino por SNMP no puedan divergir.
    """
    if clientes_caidos is None or total_clientes is None:
        return None
    caidos, total = int(clientes_caidos), int(total_clientes)
    if total <= 0:
        return None
    umbral = total - 1 if total > config.NAP_TOLERANCIA_DESDE else total
    return caidos >= umbral


def decidir_ftth(ping_cliente, ont, nap, ping_olt):
    """Matriz del punto 4 del documento.

    isOnline: False solo si el ping falla Y la ONT reporta LOS/Offline. Si no hay
    dato de ONT se cae al ping, que es la única señal real disponible: decir
    "online" sin ninguna evidencia sería peor que decir "offline".

    isZoneIncident: la tabla se reduce a `NAP caída OR OLT sin responder`. El
    ping al switch no cambia el resultado en ninguna de sus cuatro filas (con la
    NAP arriba y la OLT respondiendo el resultado es FALSE responda o no el
    switch), pero se ejecuta igual porque está en el procedimiento y queda
    registrado en el log para diagnóstico.
    """
    is_online = bool(ping_cliente) or ont is False
    is_zone_incident = nap is True or ping_olt is False
    return is_online, is_zone_incident


def decidir_wireless(ping_cliente, ping_ap, ping_rb):
    """El documento define para wireless los tres pings pero no la matriz.

    isOnline es el ping al cliente, que es la única señal del escenario B.
    isZoneIncident se toma como "la infraestructura compartida no responde": si
    se cae el AP o el RouterBoard del nodo, el corte no es de este cliente solo.
    """
    return bool(ping_cliente), (ping_ap is False or ping_rb is False)


def ip_routerboard(ip) -> str:
    """10.<segundo octeto de la IP del cliente>.0.1 — la expresión que el
    documento resolvía dentro del SQL con split_part."""
    partes = str(ip or "").split(".")
    if len(partes) != 4:
        return ""
    return red.ip_valida(f"10.{partes[1]}.0.1")


def _regex_ip(ip: str) -> str:
    """La IP se usa como patrón `~*` contra items.name. Los puntos se escapan
    para que 10.1.1.1 no matchee 10X1Y1Z1."""
    return ip.replace(".", r"\.")


# --- Ejecución concurrente ----------------------------------------------------

def _en_paralelo(tareas: dict) -> dict:
    """Corre {nombre: callable} en hilos. Una tarea que falla queda en None y no
    arrastra a las demás."""
    if not tareas:
        return {}
    resultados = {}
    sin_capacidad = None
    workers = min(len(tareas), config.CORTES_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futuros = {pool.submit(fn): nombre for nombre, fn in tareas.items()}
        for futuro, nombre in futuros.items():
            try:
                resultados[nombre] = futuro.result()
            except db.PoolAgotado as e:
                # No es "la red no contestó", es "no llegué a preguntar". Dejarlo
                # como no evaluable haría que la API devolviera 200 con una
                # respuesta calculada sobre datos que nunca se consultaron: bajo
                # carga mentiría en silencio en vez de fallar. Corta el request.
                sin_capacidad = sin_capacidad or e
                resultados[nombre] = None
            except Exception as e:
                log.warning("La verificación '%s' falló: %s", nombre, e)
                resultados[nombre] = None
    if sin_capacidad:
        raise sin_capacidad
    return resultados


# --- Acceso a datos -----------------------------------------------------------

def _pg_multi(conexion, consultas):
    """Varias consultas sobre UNA sola conexión. Devuelve una lista de filas por
    consulta, en orden.

    Cada tarea paralela que abría su propia conexión multiplicaba el consumo del
    pool por request: el camino Solar tomaba 4 conexiones de zabbix a la vez, así
    que con POOL_MAX=10 la tercera request simultánea ya se quedaba sin. Estas
    consultas entran por índice sobre `items`; secuenciarlas cuesta milisegundos
    frente a los segundos que tarda un ping.
    """
    resultados = []
    with conexion() as conn:
        with db.cursor_pg(conn, config.CORTES_STATEMENT_TIMEOUT_MS) as cur:
            for sql, params in consultas:
                cur.execute(sql, params)
                resultados.append(cur.fetchall())
    return resultados


def _pg(conexion, sql, params):
    return _pg_multi(conexion, [(sql, params)])[0]


def _mysql(conexion, sql, params):
    with conexion() as conn:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()


def _obligatoria(base, fn, *args):
    """Consulta sin la cual no hay respuesta posible: cualquier error se traduce
    a 503 con el detalle completo en el log."""
    try:
        return fn(*args)
    except db.DatabaseUnavailable:
        raise
    except Exception as e:
        log.exception("Fallo la consulta obligatoria a %s: %s", base, e)
        raise db.DatabaseUnavailable(
            f"Base de datos '{base}' no disponible"
        ) from e


def buscar_cliente(nro_cliente: str) -> dict:
    """Paso 1. Levanta ClienteNoEncontrado si no hay contrato activo."""
    filas = _obligatoria(
        "gestion", _mysql, db.gestion_conn, q.Q_GESTION_CLIENTE,
        (nro_cliente, config.CATEGORIA_FTTH_ID, config.CATEGORIA_WIRELESS_ID),
    )
    if not filas:
        raise ClienteNoEncontrado(nro_cliente)

    if len({f.get("categoria_id") for f in filas}) > 1:
        log.warning(
            "El cliente %s tiene fibra y wireless activos a la vez (%s): se "
            "resuelve como fibra",
            nro_cliente,
            ", ".join(f"{f.get('categoria')}={f.get('categoria_id')}" for f in filas),
        )
    # Orden determinístico: primero fibra, y dentro de la tecnología elegida la
    # fila que trae IP (sin ella no se puede pingear nada). Sin este orden, un
    # cliente con las dos tecnologías activas daría una respuesta u otra según
    # en qué orden devolviera las filas MySQL.
    fila = sorted(
        filas, key=lambda f: (not es_ftth(f.get("categoria_id")), not f.get("ip"))
    )[0]
    return {
        "nro_cliente": fila.get("nro_cliente"),
        "categoria": fila.get("categoria"),
        "categoria_id": fila.get("categoria_id"),
        "ip": red.ip_valida(fila.get("ip")),
        "is_ftth": es_ftth(fila.get("categoria_id")),
    }


# --- FTTH ---------------------------------------------------------------------

def _topologia_ftth(nro_cliente: str) -> dict:
    filas = _pg(db.zabbix_conn, q.Q_ZBX_TOPOLOGIA_FTTH, (nro_cliente,))
    if not filas:
        return {"nap": None, "olt_nombre": None, "olt_ip": ""}
    fila = next((f for f in filas if f.get("olt_ip")), filas[0])
    return {
        "nap": (fila.get("nap") or "").strip() or None,
        "olt_nombre": fila.get("olt_nombre"),
        "olt_ip": red.ip_valida(fila.get("olt_ip")),
    }


def _switch_del_nodo(olt_ip: str) -> dict | None:
    """Soldef. Devuelve None si no hay IP de OLT o si la base no responde: el
    ping al switch queda en 'no evaluable' y la matriz no se altera."""
    if not olt_ip:
        return None
    filas = _pg(db.soldef_conn, q.Q_SOLDEF_SWITCH, (olt_ip,))
    fila = next((f for f in filas if red.ip_valida(f.get("ip"))), None)
    if not fila:
        return None
    return {"nombre": fila.get("nombre"), "ip": red.ip_valida(fila.get("ip"))}


def _estado_por_consulta(nro_cliente: str) -> bool | None:
    """Caso A1: estado de la ONT (LOS + Online State). Por cliente, sin caché."""
    filas = _pg(db.zabbix_conn, q.Q_ZBX_ESTADO_CLIENTE, (nro_cliente,))
    los = next((f["valor"] for f in filas if f["metrica"] == "los"), None)
    estado = next((f["valor"] for f in filas if f["metrica"] == "estado"), None)
    return ont_caida(los, estado)


def _nap_por_consulta(nap: str) -> bool | None:
    """Caso A1: corte de caja sobre el último log de cada ONT de la NAP."""
    filas = _pg(db.zabbix_conn, q.Q_ZBX_ESTADO_NAP, (nap,))
    if not filas:
        log.info("La NAP %s no tiene ocupación declarada en nap_ocupacion", nap)
        return None
    return nap_caida(filas[0].get("clientes_caidos"), filas[0].get("total_clientes"))


def _oids_cliente_solar(nro_cliente: str) -> dict:
    """Caso A2: OIDs de LOS y de OnlineState de ESTE cliente. Sin caché."""
    r = _pg_multi(
        db.zabbix_conn,
        [
            (q.Q_ZBX_OID_LOS_CLIENTE, (nro_cliente,)),
            (q.Q_ZBX_OID_ONLINE_CLIENTE, (nro_cliente,)),
        ],
    )
    return {
        "oids_los": [f["oid"] for f in r[0] if f.get("oid")],
        "oids_online": [f["oid"] for f in r[1] if f.get("oid")],
    }


def _nap_por_snmp(olt_ip: str, nap: str) -> bool | None:
    """Caso A2: corte de caja preguntándole a la OLT en vivo.

    Sin ocupación declarada el resultado queda en "no evaluable": no se usa la
    cantidad de OIDs leídos como total, porque en una NAP de un solo cliente eso
    convertiría cualquier caída individual en un corte de zona.
    """
    r = _pg_multi(
        db.zabbix_conn,
        [(q.Q_ZBX_OIDS_LOS_NAP, (nap,)), (q.Q_ZBX_OCUPACION_NAP, (nap,))],
    )
    oids = [f["oid"] for f in r[0] if f.get("oid")]
    ocupacion = r[1][0].get("total_clientes") if r[1] else None

    leidos = _interpretar(
        oids, _valores_snmp(olt_ip, oids), hay_los, "LOS NAP", olt_ip
    )
    if not leidos:
        return None
    if not ocupacion:
        log.info("Sin ocupación declarada para la NAP %s: no se evalúa corte", nap)
        return None
    return nap_caida(sum(1 for e in leidos if e), ocupacion)


def _estado_nap(olt_ip: str, nap: str, es_solar: bool) -> bool | None:
    """Estado de la caja, cacheado: es la misma respuesta para todos sus
    clientes, y durante un corte real llegan todos juntos. El caché envuelve el
    resultado final, así que un hit ahorra también el walk SNMP."""
    if not nap:
        return None
    return _zona.obtener(
        ("nap", olt_ip, nap),
        (lambda: _nap_por_snmp(olt_ip, nap)) if es_solar
        else (lambda: _nap_por_consulta(nap)),
    )


def _valores_snmp(olt_ip: str, oids: list) -> dict:
    """{oid: valor crudo} leyendo todos los OIDs en la menor cantidad de
    invocaciones posible: un snmpget por lote de SNMP_OIDS_POR_CONSULTA."""
    oids = (oids or [])[: config.CORTES_MAX_OIDS_NAP]
    if not olt_ip or not oids:
        return {}
    tamano = max(1, config.SNMP_OIDS_POR_CONSULTA)
    lotes = [oids[i : i + tamano] for i in range(0, len(oids), tamano)]
    respuestas = _en_paralelo(
        {i: partial(red.snmpget, olt_ip, lote) for i, lote in enumerate(lotes)}
    )
    valores = {}
    for parcial in respuestas.values():
        valores.update(parcial or {})
    return valores


def _interpretar(oids, valores, interprete, metrica, olt_ip) -> list:
    """Traduce los valores crudos y loguea el par valor→interpretación.

    Ese log es la única forma de ver si la OLT contesta lo que el intérprete cree
    que contesta: es lo que destapó que el código 1 ('No Alarm') se estaba
    leyendo como alarma. Los códigos desconocidos quedan afuera de la lista, no
    se cuentan como positivos.
    """
    if not oids:
        return []
    lecturas = [(o, valores.get(o), interprete(valores.get(o))) for o in oids]
    leidos = [e for _, _, e in lecturas if e is not None]
    log.info(
        "snmp %s %s: %d/%d OIDs interpretados, %d positivos | %s",
        olt_ip, metrica, len(leidos), len(oids), sum(1 for e in leidos if e),
        "; ".join(f"{o}={v!r}->{e}" for o, v, e in lecturas[:6]),
    )
    return leidos


def _ont_por_snmp(olt_ip: str, oids_los: list, oids_online: list) -> bool | None:
    """Caso A2: estado de la ONT preguntándole a la OLT en vivo.

    Los OIDs de LOS y de OnlineState van en la MISMA invocación: son pocos, del
    mismo host, y ahora que el estado de la NAP está cacheado esta es la única
    consulta SNMP que queda sin cachear en cada request.

    Combina las dos señales igual que el caso A1: una en alarma alcanza para dar
    la ONT por caída.
    """
    oids_los = list(oids_los or [])
    oids_online = list(oids_online or [])
    valores = _valores_snmp(olt_ip, oids_los + oids_online)
    señales = [
        any(leidos)
        for leidos in (
            _interpretar(oids_los, valores, hay_los, "LOS", olt_ip),
            _interpretar(oids_online, valores, esta_offline, "OnlineState", olt_ip),
        )
        if leidos
    ]
    return any(señales) if señales else None


def _evaluar_ftth(nro_cliente: str, cliente: dict) -> dict:
    topo = _obligatoria("zabbix", _topologia_ftth, nro_cliente)
    nap, olt_ip = topo["nap"], topo["olt_ip"]
    es_solar = "solar" in (topo["olt_nombre"] or "").lower()

    if not nap and not olt_ip:
        log.warning(
            "El cliente %s no tiene ONT en Zabbix Fibra: solo se evalúa por ping",
            nro_cliente,
        )

    # Primera tanda. Lo compartido por toda la caja va por caché; el ping al
    # cliente y el estado de su ONT, no: son la respuesta puntual de este cliente.
    tareas = {
        "ping_cliente": partial(red.ping, cliente["ip"], "cliente"),
        "ping_olt": partial(
            _zona.obtener, ("ping", olt_ip), partial(red.ping, olt_ip, "olt")
        ),
        "switch": partial(
            _zona.obtener, ("switch", olt_ip), partial(_switch_del_nodo, olt_ip)
        ),
        "nap": partial(_estado_nap, olt_ip, nap, es_solar),
    }
    if es_solar:
        tareas["oids"] = partial(_oids_cliente_solar, nro_cliente)
    else:
        tareas["ont"] = partial(_estado_por_consulta, nro_cliente)
    r = _en_paralelo(tareas)

    # Segunda tanda: lo que necesitaba el resultado de la primera.
    switch = r.get("switch") or {}
    sw_ip = switch.get("ip")
    tareas2 = {
        "ping_switch": partial(
            _zona.obtener, ("ping", sw_ip), partial(red.ping, sw_ip, "switch")
        )
    }
    if es_solar:
        d = r.get("oids") or {}
        tareas2["ont"] = partial(
            _ont_por_snmp, olt_ip, d.get("oids_los"), d.get("oids_online")
        )
    r.update(_en_paralelo(tareas2))

    is_online, is_zone = decidir_ftth(
        r.get("ping_cliente"), r.get("ont"), r.get("nap"), r.get("ping_olt")
    )
    log.info(
        "cliente=%s ftth solar=%s nap=%s olt=%s(%s) sw=%s | ping_cli=%s ping_olt=%s "
        "ping_sw=%s ont_caida=%s nap_caida=%s -> online=%s zona=%s",
        nro_cliente, es_solar, nap, topo["olt_nombre"], olt_ip or "-",
        switch.get("ip") or "-", r.get("ping_cliente"), r.get("ping_olt"),
        r.get("ping_switch"), r.get("ont"), r.get("nap"), is_online, is_zone,
    )
    return {"isFtth": True, "isOnline": is_online, "isZoneIncident": is_zone}


# --- Wireless -----------------------------------------------------------------

def _topologia_wireless(ip: str) -> dict:
    """AP y RouterBoard del cliente. Sin IP no se consulta: `i.name ~* ''`
    matchearía todos los items del Zabbix."""
    if not ip:
        return {"ap_ip": "", "ap": None, "rb_ip": "", "rb": None}

    rb_ip = ip_routerboard(ip)
    filas_ap = _pg(db.zabbix_wireless_conn, q.Q_ZBX_WIFI_AP, (_regex_ip(ip),))
    filas_rb = (
        _pg(db.zabbix_wireless_conn, q.Q_ZBX_WIFI_RB, (rb_ip,)) if rb_ip else []
    )
    ap = next((f for f in filas_ap if red.ip_valida(f.get("ip"))), None)
    return {
        "ap": (ap or {}).get("host"),
        "ap_ip": red.ip_valida((ap or {}).get("ip")),
        "rb": (filas_rb[0].get("host") if filas_rb else None),
        # El RB se pingea por su IP calculada aunque Zabbix no lo tenga dado de
        # alta: la IP es determinística, el alta en Zabbix puede faltar.
        "rb_ip": rb_ip,
    }


def _evaluar_wireless(nro_cliente: str, cliente: dict) -> dict:
    if not cliente["ip"]:
        log.warning("El cliente wireless %s no tiene IP en Gestión", nro_cliente)
    topo = _obligatoria("zabbix_wireless", _topologia_wireless, cliente["ip"])

    # El AP y el RouterBoard son del nodo, no del cliente: mismo caché que fibra.
    r = _en_paralelo(
        {
            "ping_cliente": partial(red.ping, cliente["ip"], "cliente"),
            "ping_ap": partial(
                _zona.obtener, ("ping", topo["ap_ip"]),
                partial(red.ping, topo["ap_ip"], "ap"),
            ),
            "ping_rb": partial(
                _zona.obtener, ("ping", topo["rb_ip"]),
                partial(red.ping, topo["rb_ip"], "routerboard"),
            ),
        }
    )
    is_online, is_zone = decidir_wireless(
        r.get("ping_cliente"), r.get("ping_ap"), r.get("ping_rb")
    )
    log.info(
        "cliente=%s wireless ip=%s ap=%s(%s) rb=%s(%s) | ping_cli=%s ping_ap=%s "
        "ping_rb=%s -> online=%s zona=%s",
        nro_cliente, cliente["ip"] or "-", topo["ap"], topo["ap_ip"] or "-",
        topo["rb"], topo["rb_ip"] or "-", r.get("ping_cliente"),
        r.get("ping_ap"), r.get("ping_rb"), is_online, is_zone,
    )
    return {"isFtth": False, "isOnline": is_online, "isZoneIncident": is_zone}


# --- Orquestación -------------------------------------------------------------

def detectar(nro_cliente: str) -> dict:
    """Punto de entrada. Devuelve {'isFtth', 'isOnline', 'isZoneIncident'}."""
    t0 = time.perf_counter()
    cliente = buscar_cliente(nro_cliente)
    if cliente["is_ftth"]:
        resultado = _evaluar_ftth(nro_cliente, cliente)
    else:
        resultado = _evaluar_wireless(nro_cliente, cliente)
    log.info(
        "cliente=%s categoria=%s(%s) resuelto en %.2fs",
        nro_cliente, cliente["categoria"], cliente["categoria_id"],
        time.perf_counter() - t0,
    )
    return resultado
