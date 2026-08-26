"""Series históricas de una ONU, identificada por su precinto.

Las cuatro consultas van sobre UNA sola conexión: comparten la resolución del
precinto contra `items` y abrirlas por separado multiplicaría el consumo del pool
de zabbix por request, que también usan cortes y analytics.

El módulo devuelve el payload completo. El router valida `horas`, delega y mapea
errores a 503/500; nada de la forma de la respuesta se arma allá.
"""
import logging
import time

import config
import db
from queries.precinto import QUERY_ESTADO, QUERY_LOGS, QUERY_OLT_RX, QUERY_ONU_RX

log = logging.getLogger(__name__)

SIN_IDENTIFICAR = "No identificado"


def nombre_cliente(onu_rx: list, onu_olt_rx: list) -> str:
    """El nombre sale del propio nombre del item, así que solo aparece si alguna
    de las dos series de RX trajo lecturas.

    Las series de logs y de estado no sirven de respaldo: `QUERY_LOGS` y
    `QUERY_ESTADO` no seleccionan la columna `cliente`.
    """
    for filas in (onu_rx, onu_olt_rx):
        if filas:
            return filas[0].get("cliente") or SIN_IDENTIFICAR
    return SIN_IDENTIFICAR


def armar_respuesta(codigo_precinto, horas, desde, hasta, series) -> dict:
    """Payload publicado. `series` son las cuatro listas en el orden en que se
    consultan: onu_rx, onu_olt_rx, logs, estados."""
    onu_rx, onu_olt_rx, logs, estados = series
    return {
        "status": "success",
        "metadata": {
            "precinto": codigo_precinto,
            "cliente": nombre_cliente(onu_rx, onu_olt_rx),
            "rango_tiempo_rx": {
                "desde_timestamp": desde,
                "hasta_timestamp": hasta,
                "horas_consultadas": horas,
            },
        },
        "metricas": {
            "onu_rx": onu_rx,
            "onu_olt_rx": onu_olt_rx,
            "logs": logs,
            "estados": estados,
        },
    }


def series_de_precinto(codigo_precinto: str, horas: int) -> dict:
    """Las cuatro series de la ONU. Un precinto sin lecturas no es un error: la
    respuesta sale con las cuatro listas vacías."""
    hasta = int(time.time())
    desde = hasta - (horas * 3600)
    tope = config.PRECINTO_MAX_FILAS
    t0 = time.perf_counter()

    with db.zabbix_conn() as conn:
        # El timeout es propio: sin él la consulta hereda el default del servidor
        # y una colgada se queda con la conexión tomada.
        with db.cursor_pg(conn, config.STATEMENT_TIMEOUT_MS) as cursor:
            # El filtro de tiempo aplica solo a las dos series de RX.
            cursor.execute(QUERY_ONU_RX, (desde, hasta, codigo_precinto, tope))
            onu_rx = cursor.fetchall()

            cursor.execute(QUERY_OLT_RX, (desde, hasta, codigo_precinto, tope))
            onu_olt_rx = cursor.fetchall()

            cursor.execute(QUERY_LOGS, (codigo_precinto, tope))
            logs = cursor.fetchall()

            cursor.execute(QUERY_ESTADO, (codigo_precinto, tope))
            estados = cursor.fetchall()

    log.info(
        "precinto=%s horas=%s | onu_rx=%d olt_rx=%d logs=%d estados=%d en %.2fs",
        codigo_precinto, horas, len(onu_rx), len(onu_olt_rx), len(logs),
        len(estados), time.perf_counter() - t0,
    )
    return armar_respuesta(
        codigo_precinto, horas, desde, hasta, (onu_rx, onu_olt_rx, logs, estados)
    )
