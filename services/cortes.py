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

log = logging.getLogger(__name__)


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
    workers = min(len(tareas), config.CORTES_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futuros = {pool.submit(fn): nombre for nombre, fn in tareas.items()}
        for futuro, nombre in futuros.items():
            try:
                resultados[nombre] = futuro.result()
            except Exception as e:
                log.warning("La verificación '%s' falló: %s", nombre, e)
                resultados[nombre] = None
    return resultados


# --- Acceso a datos -----------------------------------------------------------

def _pg(conexion, sql, params):
    with conexion() as conn:
        with db.cursor_pg(conn, config.CORTES_STATEMENT_TIMEOUT_MS) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


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


def _ont_por_consulta(nro_cliente: str) -> bool | None:
    """Caso A1: LOS y Online State desde el historial de Zabbix."""
    filas = _pg(db.zabbix_conn, q.Q_ZBX_ESTADO_CLIENTE, (nro_cliente,))
    los = next((f["valor"] for f in filas if f["metrica"] == "los"), None)
    estado = next((f["valor"] for f in filas if f["metrica"] == "estado"), None)
    return ont_caida(los, estado)


def _nap_por_consulta(nap: str) -> bool | None:
    """Caso A1: corte de caja calculado sobre el último log de cada ONT."""
    if not nap:
        return None
    filas = _pg(db.zabbix_conn, q.Q_ZBX_ESTADO_NAP, (nap,))
    if not filas:
        log.info("La NAP %s no tiene ocupación declarada en nap_ocupacion", nap)
        return None
    fila = filas[0]
    return nap_caida(fila.get("clientes_caidos"), fila.get("total_clientes"))


def _oids(sql, clave: str):
    """OIDs de SNMP para un cliente o una NAP.

    Sin clave no se consulta: `<expresión> = ''` matchearía todos los items
    cuyo nombre no tenga NAP, y cada OID devuelto es un subproceso snmpget.
    """
    if not clave:
        return []
    filas = _pg(db.zabbix_conn, sql, (clave,))
    return [f["oid"] for f in filas if f.get("oid")]


def _ocupacion_nap(nap: str) -> int | None:
    if not nap:
        return None
    filas = _pg(db.zabbix_conn, q.Q_ZBX_OCUPACION_NAP, (nap,))
    return filas[0].get("total_clientes") if filas else None


def _leer_snmp(olt_ip: str, oids: list, interprete, metrica: str) -> list:
    """snmpget en paralelo sobre una lista de OIDs, ya interpretados.

    Devuelve solo los que se pudieron leer y traducir; los códigos desconocidos
    quedan afuera en vez de contarse como alarma.
    """
    oids = (oids or [])[: config.CORTES_MAX_OIDS_NAP]
    if not olt_ip or not oids:
        return []
    valores = _en_paralelo(
        {oid: partial(red.snmpget, olt_ip, oid) for oid in oids}
    )
    lecturas = [(oid, v, interprete(v)) for oid, v in valores.items()]
    leidos = [e for _, _, e in lecturas if e is not None]
    # Valor crudo junto a su interpretación: es la única forma de ver desde el
    # log si la OLT contesta lo que el intérprete cree que contesta.
    log.info(
        "snmp %s %s: %d/%d OIDs interpretados, %d positivos | %s",
        olt_ip, metrica, len(leidos), len(oids), sum(1 for e in leidos if e),
        "; ".join(f"{oid}={v!r}->{e}" for oid, v, e in lecturas[:6]),
    )
    return leidos


def _ont_por_snmp(olt_ip: str, oids_los: list, oids_online: list) -> bool | None:
    """Caso A2: estado de la ONT preguntándole a la OLT en vivo.

    Combina las dos señales igual que _ont_por_consulta —una en alarma alcanza
    para dar la ONT por caída—, ahora que el documento actualizado provee la
    consulta de OIDs de OnlineState además de la de LOS.
    """
    señales = [
        any(lecturas)
        for lecturas in (
            _leer_snmp(olt_ip, oids_los, hay_los, "LOS"),
            _leer_snmp(olt_ip, oids_online, esta_offline, "OnlineState"),
        )
        if lecturas
    ]
    return any(señales) if señales else None


def _nap_por_snmp(olt_ip: str, oids: list, ocupacion) -> bool | None:
    """Caso A2: corte de caja contando los LOS que devuelve la OLT contra la
    ocupación declarada. Sin ocupación el resultado queda en 'no evaluable': no
    se usa la cantidad de OIDs leídos como total, porque en una NAP de un solo
    cliente eso convertiría cualquier caída individual en un corte de zona."""
    leidos = _leer_snmp(olt_ip, oids, hay_los, "LOS NAP")
    if not leidos:
        return None
    if not ocupacion:
        log.info("Sin ocupación declarada para la NAP: no se evalúa corte de caja")
        return None
    return nap_caida(sum(1 for e in leidos if e), ocupacion)


def _evaluar_ftth(nro_cliente: str, cliente: dict) -> dict:
    topo = _obligatoria("zabbix", _topologia_ftth, nro_cliente)
    nap, olt_ip = topo["nap"], topo["olt_ip"]
    es_solar = "solar" in (topo["olt_nombre"] or "").lower()

    if not nap and not olt_ip:
        log.warning(
            "El cliente %s no tiene ONT en Zabbix Fibra: solo se evalúa por ping",
            nro_cliente,
        )

    # Primera tanda: todo lo que solo depende de la topología ya resuelta.
    tareas = {
        "ping_cliente": partial(red.ping, cliente["ip"], "cliente"),
        "ping_olt": partial(red.ping, olt_ip, "olt"),
        "switch": partial(_switch_del_nodo, olt_ip),
    }
    if es_solar:
        tareas["oids_los"] = partial(_oids, q.Q_ZBX_OID_LOS_CLIENTE, nro_cliente)
        tareas["oids_online"] = partial(_oids, q.Q_ZBX_OID_ONLINE_CLIENTE, nro_cliente)
        tareas["oids_nap"] = partial(_oids, q.Q_ZBX_OIDS_LOS_NAP, nap or "")
        tareas["ocupacion"] = partial(_ocupacion_nap, nap)
    else:
        tareas["ont"] = partial(_ont_por_consulta, nro_cliente)
        tareas["nap"] = partial(_nap_por_consulta, nap)
    r = _en_paralelo(tareas)

    # Segunda tanda: lo que necesitaba el resultado de la primera.
    switch = r.get("switch") or {}
    tareas2 = {"ping_switch": partial(red.ping, switch.get("ip"), "switch")}
    if es_solar:
        tareas2["ont"] = partial(
            _ont_por_snmp, olt_ip, r.get("oids_los"), r.get("oids_online")
        )
        tareas2["nap"] = partial(
            _nap_por_snmp, olt_ip, r.get("oids_nap"), r.get("ocupacion")
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

    r = _en_paralelo(
        {
            "ping_cliente": partial(red.ping, cliente["ip"], "cliente"),
            "ping_ap": partial(red.ping, topo["ap_ip"], "ap"),
            "ping_rb": partial(red.ping, topo["rb_ip"], "routerboard"),
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
