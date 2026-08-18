"""Endpoint de consulta por precinto. Contrato idéntico al original."""
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import config
import db
from models import ERROR_RATE_LIMIT, ERRORES_AUTENTICACION_INTERNA
from queries.precinto import QUERY_ESTADO, QUERY_LOGS, QUERY_OLT_RX, QUERY_ONU_RX
from security import limitar_tasa_interna

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["precinto"], dependencies=[Depends(limitar_tasa_interna)]
)


EJEMPLO_RESPUESTA = {
    "status": "success",
    "metadata": {
        "precinto": "JES0037",
        "cliente": "JES0037",
        "rango_tiempo_rx": {
            "desde_timestamp": 1785158427,
            "hasta_timestamp": 1785180027,
            "horas_consultadas": 6,
        },
    },
    "metricas": {
        "onu_rx": [{"cliente": "JES0037", "onu_rx": "-21.35", "time": 1785179940}],
        "onu_olt_rx": [
            {"cliente": "JES0037", "onu_olt_rx": "-19.80", "time": 1785179940}
        ],
        "logs": [{"log": "Dying-gasp", "time": "2026-07-15T03:11:11"}],
        "estados": [{"status": "Online", "time": 1785179880}],
    },
}


@router.get(
    "/api/v1/precinto/{codigo_precinto}",
    summary="Series históricas de una ONU por precinto",
    response_description=(
        "Metadata de la consulta y las cuatro series de métricas de la ONU "
        "(RX, OLT RX, logs y estados)"
    ),
    responses={
        200: {
            "description": (
                "Consulta resuelta. **Un precinto inexistente también devuelve "
                "200**, con las cuatro listas vacías y `cliente` en "
                "'No identificado': la API no distingue 'no existe' de 'existe "
                "pero no reportó nada'."
            ),
            "content": {"application/json": {"example": EJEMPLO_RESPUESTA}},
        },
        **ERRORES_AUTENTICACION_INTERNA,
        **ERROR_RATE_LIMIT,
        500: {
            "description": "Error inesperado al procesar los datos de Zabbix.",
            "content": {
                "application/json": {
                    "example": {"detail": "Error al procesar los datos en Zabbix"}
                }
            },
        },
        503: {
            "description": "La base de Zabbix no responde. `/health` indica el estado.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Error interno de conexión a la base de datos"
                    }
                }
            },
        },
    },
)
def obtener_datos_completos_precinto(
    codigo_precinto: str,
    horas: Optional[int] = Query(
        default=6,
        ge=1,
        le=168,
        description="Cantidad de horas hacia atrás a consultar (solo aplica a RX y OLT RX)",
    ),
):
    """Devuelve todo lo que Zabbix sabe de una ONU, identificada por su precinto.

    Resuelve cuatro consultas contra la base de Zabbix de fibra y las agrupa en
    una sola respuesta, para no obligar al consumidor a hacer cuatro requests:

    1. **`onu_rx`** — potencia óptica recibida *por la ONU*, minuto a minuto.
    2. **`onu_olt_rx`** — potencia óptica que la *OLT* recibe de esa ONU. Comparar
       ambas es lo que separa un problema de bajada de uno de subida.
    3. **`logs`** — última causa de caída informada por el equipo
       (`hwGponDeviceOntControlLastDownCause`), deduplicada por valor.
    4. **`estados`** — histórico del item `hwGponDeviceOntEthernetOnlineState`.

    **Parámetros**

    * `codigo_precinto` (ruta): el precinto de la ONU. Se compara contra el
      nombre del item en Zabbix de forma **insensible a mayúsculas y como
      coincidencia parcial**, así que un precinto corto puede traer datos de
      varias ONUs. Enviá el código completo para evitarlo. Se compara como
      texto literal: los metacaracteres no tienen ningún significado especial.
    * `horas` (query, 1–168, por defecto 6): ventana hacia atrás de las series de
      potencia. **Solo afecta a `onu_rx` y `onu_olt_rx`**; `logs` y `estados` se
      devuelven siempre completos, sin corte temporal.

    **Devuelve** un objeto con `status`, `metadata` (precinto consultado, nombre
    del cliente deducido de las series de RX y el rango temporal efectivamente
    usado) y `metricas` con las cuatro listas.

    Los campos `time` de `onu_rx`, `onu_olt_rx` y `estados` son epoch UNIX en
    segundos, truncados al minuto. El `time` de `logs` es un timestamp parseado
    del propio texto del evento.

    Un precinto sin datos **no es un error**: devuelve 200 con las cuatro listas
    vacías y `cliente` en `"No identificado"`.
    """
    now = int(time.time())
    start_time = now - (horas * 3600)

    try:
        with db.zabbix_conn() as conn:
            with db.cursor_pg(conn) as cursor:
                tope = config.PRECINTO_MAX_FILAS

                # 1. Ejecutar ONU RX (Usa tiempo y precinto)
                cursor.execute(
                    QUERY_ONU_RX, (start_time, now, codigo_precinto, tope)
                )
                onu_rx_data = cursor.fetchall()

                # 2. Ejecutar ONU OLT RX (Usa tiempo y precinto)
                cursor.execute(
                    QUERY_OLT_RX, (start_time, now, codigo_precinto, tope)
                )
                onu_olt_rx_data = cursor.fetchall()

                # 3. Ejecutar LOGS (Solo usa precinto)
                cursor.execute(QUERY_LOGS, (codigo_precinto, tope))
                logs_data = cursor.fetchall()

                # 4. Ejecutar ESTADO (Solo usa precinto)
                cursor.execute(QUERY_ESTADO, (codigo_precinto, tope))
                estado_data = cursor.fetchall()
    except db.DatabaseUnavailable as e:
        log.error("Zabbix no disponible: %s", e)
        raise HTTPException(
            status_code=503, detail="Error interno de conexión a la base de datos"
        )
    except Exception as e:
        log.exception("Error al ejecutar las consultas del precinto %s: %s",
                      codigo_precinto, e)
        raise HTTPException(status_code=500, detail="Error al procesar los datos en Zabbix")

    # Extraer el nombre del cliente si las consultas de RX trajeron algo
    nombre_cliente = "No identificado"
    if onu_rx_data:
        nombre_cliente = onu_rx_data[0]["cliente"]
    elif onu_olt_rx_data:
        nombre_cliente = onu_olt_rx_data[0]["cliente"]

    return {
        "status": "success",
        "metadata": {
            "precinto": codigo_precinto,
            "cliente": nombre_cliente,
            "rango_tiempo_rx": {
                "desde_timestamp": start_time,
                "hasta_timestamp": now,
                "horas_consultadas": horas,
            },
        },
        "metricas": {
            "onu_rx": onu_rx_data,
            "onu_olt_rx": onu_olt_rx_data,
            "logs": logs_data,
            "estados": estado_data,
        },
    }
