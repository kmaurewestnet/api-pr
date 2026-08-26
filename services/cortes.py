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

Este módulo no habla con nadie: todo el I/O entra por `fuente`, que en producción
es `services.cortes_io.FuenteReal` y en las pruebas es una fuente en memoria. Lo
que queda acá es lo que puede romperse en silencio —el armado de las dos tandas y
el mapeo de cada señal a su lugar en la matriz— y por eso queda del lado
testeable del seam.

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
from functools import partial

import config
import db
from queries import cortes as q
from services import red
from services.cortes_io import FuenteReal, _en_paralelo, _regex_ip  # noqa: F401
from services.cortes_reglas import (  # noqa: F401
    TABLA_SNMP,
    decidir_ftth,
    decidir_wireless,
    es_ftth,
    esta_offline,
    hay_los,
    ip_routerboard,
    nap_caida,
    ont_caida,
)

log = logging.getLogger(__name__)

# La fuente de producción. Se pasa por parámetro y no se lee de un global dentro
# de las funciones: una prueba construye la suya y no toca estado de módulo.
_FUENTE = FuenteReal()


class ClienteNoEncontrado(LookupError):
    """El número de cliente no existe, no tiene contrato activo, o su plan no
    cae en las categorías 16/17."""


# --- Acceso a datos que no pasa por la fuente ---------------------------------

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

def _evaluar_ftth(nro_cliente: str, cliente: dict, fuente=None) -> dict:
    fuente = fuente or _FUENTE
    topo = _obligatoria("zabbix", fuente.topologia_ftth, nro_cliente)
    nap, olt_ip = topo["nap"], topo["olt_ip"]
    es_solar = "solar" in (topo["olt_nombre"] or "").lower()

    if not nap and not olt_ip:
        log.warning(
            "El cliente %s no tiene ONT en Zabbix Fibra: solo se evalúa por ping",
            nro_cliente,
        )

    # Primera tanda. Lo compartido por toda la caja va por `ping_zona`/`estado_nap`,
    # que cachean adentro de la fuente; el ping al cliente y el estado de su ONT,
    # no: son la respuesta puntual de este cliente.
    tareas = {
        "ping_cliente": partial(fuente.ping_cliente, cliente["ip"]),
        "ping_olt": partial(fuente.ping_zona, olt_ip, "olt"),
        "switch": partial(fuente.switch_del_nodo, olt_ip),
        "nap": partial(fuente.estado_nap, olt_ip, nap, es_solar),
    }
    if es_solar:
        tareas["oids"] = partial(fuente.oids_solar, nro_cliente)
    else:
        tareas["ont"] = partial(fuente.estado_ont, nro_cliente)
    r = _en_paralelo(tareas)

    # Segunda tanda: lo que necesitaba el resultado de la primera.
    switch = r["switch"] or {}
    sw_ip = switch.get("ip")
    tareas2 = {"ping_switch": partial(fuente.ping_zona, sw_ip, "switch")}
    if es_solar:
        d = r["oids"] or {}
        tareas2["ont"] = partial(
            fuente.ont_por_snmp, olt_ip, d.get("oids_los"), d.get("oids_online")
        )
    r.update(_en_paralelo(tareas2))

    # Subscript y no `.get()`: después de las dos tandas las seis señales existen
    # en las dos ramas, así que una clave ausente es un error de cableado y tiene
    # que romper acá, no degradar a None y salir por 200 como "no evaluable".
    is_online, is_zone = decidir_ftth(
        r["ping_cliente"], r["ont"], r["nap"], r["ping_olt"]
    )
    log.info(
        "cliente=%s ftth solar=%s nap=%s olt=%s(%s) sw=%s | ping_cli=%s ping_olt=%s "
        "ping_sw=%s ont_caida=%s nap_caida=%s -> online=%s zona=%s",
        nro_cliente, es_solar, nap, topo["olt_nombre"], olt_ip or "-",
        switch.get("ip") or "-", r["ping_cliente"], r["ping_olt"],
        r["ping_switch"], r["ont"], r["nap"], is_online, is_zone,
    )
    return {"isFtth": True, "isOnline": is_online, "isZoneIncident": is_zone}


# --- Wireless -----------------------------------------------------------------

def _evaluar_wireless(nro_cliente: str, cliente: dict, fuente=None) -> dict:
    fuente = fuente or _FUENTE
    if not cliente["ip"]:
        log.warning("El cliente wireless %s no tiene IP en Gestión", nro_cliente)
    topo = _obligatoria("zabbix_wireless", fuente.topologia_wireless, cliente["ip"])

    # El AP y el RouterBoard son del nodo, no del cliente: mismo caché que fibra.
    r = _en_paralelo(
        {
            "ping_cliente": partial(fuente.ping_cliente, cliente["ip"]),
            "ping_ap": partial(fuente.ping_zona, topo["ap_ip"], "ap"),
            "ping_rb": partial(fuente.ping_zona, topo["rb_ip"], "routerboard"),
        }
    )
    is_online, is_zone = decidir_wireless(
        r["ping_cliente"], r["ping_ap"], r["ping_rb"]
    )
    log.info(
        "cliente=%s wireless ip=%s ap=%s(%s) rb=%s(%s) | ping_cli=%s ping_ap=%s "
        "ping_rb=%s -> online=%s zona=%s",
        nro_cliente, cliente["ip"] or "-", topo["ap"], topo["ap_ip"] or "-",
        topo["rb"], topo["rb_ip"] or "-", r["ping_cliente"],
        r["ping_ap"], r["ping_rb"], is_online, is_zone,
    )
    return {"isFtth": False, "isOnline": is_online, "isZoneIncident": is_zone}


# --- Orquestación -------------------------------------------------------------

def detectar(nro_cliente: str, fuente=None) -> dict:
    """Punto de entrada. Devuelve {'isFtth', 'isOnline', 'isZoneIncident'}."""
    t0 = time.perf_counter()
    cliente = buscar_cliente(nro_cliente)
    if cliente["is_ftth"]:
        resultado = _evaluar_ftth(nro_cliente, cliente, fuente)
    else:
        resultado = _evaluar_wireless(nro_cliente, cliente, fuente)
    log.info(
        "cliente=%s categoria=%s(%s) resuelto en %.2fs",
        nro_cliente, cliente["categoria"], cliente["categoria_id"],
        time.perf_counter() - t0,
    )
    return resultado
