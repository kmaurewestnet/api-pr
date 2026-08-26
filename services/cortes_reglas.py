"""Reglas de decisión de cortes: sin I/O, sin estado, sin dependencias del resto
del paquete.

Es el módulo que comparten los otros dos: `services/cortes_io.py` las aplica
sobre las filas y los valores SNMP que trae de la red, y `services/cortes.py`
las aplica sobre las señales ya recolectadas. Que vivan una sola vez es lo que
impide que el camino por consulta y el camino por SNMP diverjan.
"""
import logging

import config
from services import red

log = logging.getLogger(__name__)


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
