"""Endpoint de consulta de series históricas por precinto."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import db
from models import ERROR_RATE_LIMIT, ERRORES_AUTENTICACION_INTERNA, PrecintoResponse
from security import limitar_tasa_interna
from services import precinto as svc

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
    response_model=PrecintoResponse,
    summary="Series históricas de una ONU por precinto",
    response_description=(
        "Metadata de la consulta y las cuatro series de métricas de la ONU "
        "(RX, OLT RX, registros de caída y estados)"
    ),
    responses={
        200: {
            "description": (
                "Consulta resuelta. **Un precinto inexistente también devuelve "
                "200**, con las cuatro listas vacías y `cliente` en "
                "'No identificado': la API no distingue entre un precinto "
                "inexistente y uno registrado que no reportó lecturas."
            ),
            "content": {"application/json": {"example": EJEMPLO_RESPUESTA}},
        },
        **ERRORES_AUTENTICACION_INTERNA,
        **ERROR_RATE_LIMIT,
        500: {
            "description": (
                "Error inesperado durante el procesamiento de los datos "
                "obtenidos del sistema de monitoreo."
            ),
            "content": {
                "application/json": {
                    "example": {"detail": "Error al procesar los datos en Zabbix"}
                }
            },
        },
        503: {
            "description": (
                "La base del sistema de monitoreo de fibra no responde. "
                "`/health` detalla el estado de cada dependencia."
            ),
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
        description="Ventana temporal hacia atrás, en horas. Aplica únicamente a "
                    "las series RX y OLT RX.",
    ),
):
    """Devuelve las series históricas registradas para una ONU, identificada por
    su precinto.

    Resuelve cuatro consultas contra la base del sistema de monitoreo de fibra y
    las agrupa en una única respuesta, evitando que el consumidor deba emitir
    cuatro requests:

    1. **`onu_rx`** — potencia óptica recibida *por la ONU*, con granularidad de
       un minuto.
    2. **`onu_olt_rx`** — potencia óptica que la *OLT* recibe de esa ONU. La
       comparación entre ambas series permite distinguir un problema de bajada de
       uno de subida.
    3. **`logs`** — última causa de caída informada por el equipo
       (`hwGponDeviceOntControlLastDownCause`), deduplicada por valor.
    4. **`estados`** — histórico del item `hwGponDeviceOntEthernetOnlineState`.

    **Parámetros**

    * `codigo_precinto` (ruta): precinto de la ONU. Se compara contra el nombre
      del item de monitoreo de forma **insensible a mayúsculas y por coincidencia
      parcial**, por lo que un precinto abreviado puede devolver datos de varias
      ONUs; se recomienda enviar el código completo. La comparación es de texto
      literal: los metacaracteres carecen de significado especial.
    * `horas` (query, 1–168, valor por defecto 6): ventana temporal hacia atrás
      aplicada a las series de potencia. **Afecta únicamente a `onu_rx` y
      `onu_olt_rx`**; `logs` y `estados` se devuelven completos, sin recorte
      temporal.

    **Devuelve** un objeto con `status`, `metadata` (precinto consultado, nombre
    de cliente derivado de las series de RX y rango temporal efectivamente
    aplicado) y `metricas` con las cuatro listas.

    Los campos `time` de `onu_rx`, `onu_olt_rx` y `estados` son epoch UNIX en
    segundos, truncados al minuto. El campo `time` de `logs` es un timestamp
    derivado del propio texto del evento.

    Un precinto sin lecturas **no constituye un error**: la respuesta es 200 con
    las cuatro listas vacías y `cliente` en `"No identificado"`.
    """
    try:
        return svc.series_de_precinto(codigo_precinto, horas)
    except db.DatabaseUnavailable as e:
        log.error("La base del sistema de monitoreo no responde: %s", e)
        raise HTTPException(
            status_code=503, detail="Error interno de conexión a la base de datos"
        )
    except Exception as e:
        log.exception("Error al ejecutar las consultas del precinto %s: %s",
                      codigo_precinto, e)
        raise HTTPException(
            status_code=500, detail="Error al procesar los datos en Zabbix"
        )
