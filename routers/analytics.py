"""Endpoint de analíticas agregadas por empresa."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

import db
from models import (
    ERROR_RATE_LIMIT,
    ERRORES_AUTENTICACION_INTERNA,
    AnalyticsResponse,    error,
)
from security import limitar_tasa_interna
from services import analytics as svc

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["analiticas"], dependencies=[Depends(limitar_tasa_interna)]
)

ESTADOS = svc.CATEGORIAS


def _filtrar(dispositivos, estado):
    """Filtra por categoría, la misma taxonomía excluyente que usa el resumen."""
    if not estado:
        return dispositivos
    return [d for d in dispositivos if d["categoria"] == estado]


def _stream(metadata, resumen, dispositivos):
    """Serializa la respuesta por partes para no construir un JSON de 40.000
    registros entero en memoria."""
    cabecera = {"status": "success", "metadata": metadata, "resumen": resumen}
    yield json.dumps(cabecera, default=str)[:-1]  # quita el '}' de cierre
    yield ', "dispositivos": ['
    for i, d in enumerate(dispositivos):
        yield ("," if i else "") + json.dumps(d, default=str)
    yield "]}"


@router.get(
    "/api/v1/empresa/{empresa_id}/analytics",
    response_model=AnalyticsResponse,
    summary="Estado agregado del parque de ONUs de una empresa",
    response_description=(
        "Resumen por categoría sobre el parque completo, más el listado de "
        "dispositivos, paginado salvo que se solicite `full=true`"
    ),
    responses={
        200: {
            "description": (
                "Analíticas resueltas. Con `full=true` el cuerpo mantiene la misma "
                "estructura, pero se transmite por streaming y "
                "`metadata.paginacion` se devuelve en `null`."
            )
        },
        **ERRORES_AUTENTICACION_INTERNA,
        **ERROR_RATE_LIMIT,
        404: error(
            "La empresa no registra dispositivos asociados en el sistema de "
            "reservas.",
            "La empresa 34 no tiene dispositivos asociados en napear",
        ),
        422: {
            "description": (
                "Un parámetro está fuera de rango, o `estado` no corresponde a "
                "ninguna de las cinco categorías válidas. El cuerpo adopta dos "
                "formas según el origen del rechazo: texto cuando la validación "
                "es del endpoint, lista de errores cuando la realiza FastAPI."
            ),
            "content": {
                "application/json": {
                    "examples": {
                        "estado_invalido": {
                            "summary": "Categoría inexistente (validación del endpoint)",
                            "value": {
                                "detail": "El parámetro 'estado' debe ser uno de: "
                                          "online, offline, los, powerfail, sin_datos"
                            },
                        },
                        "parametro_fuera_de_rango": {
                            "summary": "limit fuera del rango 1–5000 (validación de FastAPI)",
                            "value": {
                                "detail": [
                                    {
                                        "loc": ["query", "limit"],
                                        "msg": "Input should be less than or equal to 5000",
                                        "type": "less_than_equal",
                                    }
                                ]
                            },
                        },
                    }
                }
            },
        },
        500: error(
            "Error inesperado durante el cálculo de las analíticas.",
            "Error al procesar las analíticas de la empresa",
        ),
        501: error(
            "Faltan definir consultas SQL en `queries/analytics.py`. Constituye "
            "un error de despliegue, no del request.",
            "Faltan definir las consultas SQL en queries/analytics.py: "
            "Q_NAPEAR_ONTS_POR_EMPRESA",
        ),
        503: error(
            "Alguna de las tres bases involucradas (reservas, inventario de red o "
            "monitoreo de fibra) no responde. `/health` identifica cuál.",
            "La base napear no está disponible",
        ),
    },
)
def analiticas_empresa(
    empresa_id: int = Path(
        ..., description="Identificador de empresa en el sistema de reservas"
    ),
    horas: Optional[int] = Query(
        default=None, ge=1, le=8760,
        description="Antigüedad máxima admitida para el último valor de estado y "
                    "LOS. Sin este parámetro se devuelve el último valor registrado "
                    "de cada ONU, con independencia de su fecha; los campos "
                    "'status_timestamp' y 'los_timestamp' permiten evaluar su "
                    "vigencia. Acotar la ventana solo descarta lecturas antiguas, no "
                    "reduce el tiempo de consulta: ya se resuelve por índice.",
    ),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=500, ge=1, le=5000),
    full: bool = Query(
        default=False,
        description="Devuelve el listado completo por streaming, ignorando `page` "
                    "y `limit`.",
    ),
    estado: Optional[str] = Query(
        default=None,
        description="Filtra el listado por categoría: online | offline | los | "
                    "powerfail | sin_datos. El resumen se calcula siempre sobre el "
                    "parque completo, con independencia de este filtro.",
    ),
):
    """Estado del parque completo de ONUs de una empresa, en forma agregada y
    detallada.

    Cruza en memoria tres bases que no admiten JOIN entre sí: el **sistema de
    reservas** aporta los seriales de la empresa, el **inventario de red**
    traduce cada serial a su precinto, y el **sistema de monitoreo de fibra**
    aporta el último estado, la alarma óptica y la última causa de caída de cada
    precinto.

    Cada equipo se clasifica en **una sola** de cinco categorías excluyentes, que
    se evalúan en el orden indicado; prevalece la primera que se cumple:

    | # | Condición | Categoría |
    |---|---|---|
    | 1 | La ONT reporta `Online` | `online` |
    | 2 | Caída, con `Dying-gasp` a menos de 15 min del corte | `powerfail` |
    | 3 | Caída, con alarma LOS de menos de 7 días | `los` |
    | 4 | Caída (incluye LOS de 7 días o más) | `offline` |
    | 5 | Sin estado ni LOS registrados | `sin_datos` |

    `powerfail` se evalúa antes que `los` de forma deliberada: un corte de
    energía apaga la ONT y eso genera LOS en la OLT, por lo que ambas señales se
    presentan de manera simultánea y el dying-gasp es la más específica. La
    distinción tiene consecuencias operativas: `powerfail` se resuelve por sí
    solo, mientras que `los` requiere intervención de cuadrilla.

    **Parámetros**

    * `empresa_id` (ruta): identificador de empresa en el sistema de reservas.
    * `horas` (1–8760, opcional): descarta lecturas de estado y LOS con
      antigüedad superior a N horas. Sin este parámetro se devuelve el último
      valor registrado de cada ONU, con independencia de su fecha. Acotar la
      ventana **no reduce el tiempo de consulta**; únicamente descarta lecturas
      antiguas, a costa de un mayor número de `sin_datos`.
    * `page` (≥1, valor por defecto 1) y `limit` (1–5000, valor por defecto 500):
      recorte del listado. **La paginación no reduce el tiempo de consulta**: las
      tres bases se consultan íntegramente y el recorte se aplica en memoria al
      final. Su función es evitar la transferencia de 40.000 registros por HTTP.
    * `full` (valor por defecto `false`): devuelve el listado completo por
      streaming e ignora `page` y `limit`. En ese modo `metadata.paginacion` se
      devuelve en `null`.
    * `estado` (opcional): filtra el listado por categoría (`online`, `offline`,
      `los`, `powerfail`, `sin_datos`). **El `resumen` se calcula siempre sobre
      el parque completo**, con independencia de este filtro y de la paginación,
      de modo que su valor no varíe según la forma de consulta.

    **Devuelve** `status`, `metadata` (empresa, totales por base, rango temporal
    y paginación), `resumen` (los cinco contadores excluyentes, el porcentaje
    online y el origen del estado) y `dispositivos` (el listado ya filtrado y
    recortado).

    Advertencia sobre la interpretación de los resultados: la mayoría de las ONUs
    no dispone del item `OnlineState`, y en su ausencia el estado se deriva de la
    alarma óptica. Ese mecanismo detecta **fibra caída, no servicio caído**: una
    ONU con fibra en condiciones pero sin servicio figura como `online`. El campo
    `origen_estado` de cada dispositivo y el conteo `resumen.origen_estado`
    indican la composición de la muestra.
    """
    if estado and estado not in ESTADOS:
        raise HTTPException(
            status_code=422,
            detail=f"El parámetro 'estado' debe ser uno de: {', '.join(ESTADOS)}",
        )

    try:
        metadata, resumen, dispositivos = svc.analitica_empresa(empresa_id, horas)
    except svc.QueriesNoConfiguradas as e:
        log.error(str(e))
        raise HTTPException(status_code=501, detail=str(e))
    except db.DatabaseUnavailable as e:
        log.error("Base no disponible: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        log.exception("Error en las analíticas de la empresa %s: %s", empresa_id, e)
        raise HTTPException(
            status_code=500, detail="Error al procesar las analíticas de la empresa"
        )

    if metadata is None:
        raise HTTPException(
            status_code=404,
            detail=f"La empresa {empresa_id} no tiene dispositivos asociados en napear",
        )

    filtrados = _filtrar(dispositivos, estado)

    if full:
        metadata["paginacion"] = None
        return StreamingResponse(
            _stream(metadata, resumen, filtrados), media_type="application/json"
        )

    total = len(filtrados)
    inicio = (page - 1) * limit
    metadata["paginacion"] = {
        "page": page,
        "limit": limit,
        "total_items": total,
        # Con 0 items hay 1 página vacía, no 0.
        "total_paginas": max(1, -(-total // limit)),
    }

    return {
        "status": "success",
        "metadata": metadata,
        "resumen": resumen,
        "dispositivos": filtrados[inicio : inicio + limit],
    }
