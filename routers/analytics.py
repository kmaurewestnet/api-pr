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
    AnalyticsResponse,
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
    summary="Analíticas de todas las ONUs de una empresa",
    response_description=(
        "Resumen por categoría sobre el parque completo más el listado de "
        "dispositivos, paginado salvo que se pida `full=true`"
    ),
    responses={
        200: {
            "description": (
                "Analíticas resueltas. Con `full=true` el cuerpo es el mismo "
                "objeto pero enviado por streaming y con `metadata.paginacion` "
                "en `null`."
            )
        },
        **ERRORES_AUTENTICACION_INTERNA,
        **ERROR_RATE_LIMIT,
        404: {
            "description": "La empresa no tiene dispositivos asociados en napear.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "La empresa 34 no tiene dispositivos asociados en napear"
                    }
                }
            },
        },
        422: {
            "description": (
                "Un parámetro está fuera de rango, o `estado` no es una de las "
                "cinco categorías válidas. El cuerpo tiene dos formas según quién "
                "rechace: texto cuando lo valida el endpoint, lista de errores "
                "cuando lo valida FastAPI."
            ),
            "content": {
                "application/json": {
                    "examples": {
                        "estado_invalido": {
                            "summary": "Categoría inexistente (validado por el endpoint)",
                            "value": {
                                "detail": "El parámetro 'estado' debe ser uno de: "
                                          "online, offline, los, powerfail, sin_datos"
                            },
                        },
                        "parametro_fuera_de_rango": {
                            "summary": "limit fuera de 1–5000 (validado por FastAPI)",
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
        500: {
            "description": "Error inesperado al procesar las analíticas.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Error al procesar las analíticas de la empresa"
                    }
                }
            },
        },
        501: {
            "description": (
                "Faltan definir consultas SQL en `queries/analytics.py`. Es un "
                "error de despliegue, no del request."
            ),
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Faltan definir las consultas SQL en "
                                  "queries/analytics.py: Q_NAPEAR_ONTS_POR_EMPRESA"
                    }
                }
            },
        },
        503: {
            "description": (
                "Alguna de las tres bases (napear, soldef o zabbix) no responde. "
                "`/health` indica cuál."
            ),
            "content": {
                "application/json": {
                    "example": {"detail": "La base napear no está disponible"}
                }
            },
        },
    },
)
def analiticas_empresa(
    empresa_id: int = Path(..., description="ID de empresa en la base napear"),
    horas: Optional[int] = Query(
        default=None, ge=1, le=8760,
        description="Antigüedad máxima aceptada para el último valor de estado y LOS. "
                    "Sin este parámetro se trae el último valor real de cada ONU, sin "
                    "importar cuándo se registró; usá 'status_timestamp' y "
                    "'los_timestamp' para juzgar qué tan fresco es. Acotar la ventana "
                    "solo sirve para descartar datos viejos, no para acelerar: la "
                    "consulta ya entra por índice.",
    ),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=500, ge=1, le=5000),
    full: bool = Query(
        default=False,
        description="Devuelve el listado completo por streaming, ignorando page y limit",
    ),
    estado: Optional[str] = Query(
        default=None,
        description="Filtra el listado por categoría: online | offline | los | "
                    "powerfail | sin_datos. El resumen siempre se calcula sobre "
                    "el total, sin importar este filtro.",
    ),
):
    """Estado del parque completo de ONUs de una empresa, resumido y detallado.

    Cruza tres bases que no admiten JOIN entre sí y arma el resultado en memoria:
    **napear** aporta los seriales de la empresa, **soldef** traduce cada serial a
    su precinto, y **zabbix** aporta el último estado, la alarma óptica y la
    última causa de caída de cada precinto.

    Cada equipo se clasifica en **una sola** de cinco categorías excluyentes, que
    se evalúan en este orden y gana la primera que aplica:

    | # | Condición | Categoría |
    |---|---|---|
    | 1 | La ONT reporta `Online` | `online` |
    | 2 | Caída, con `Dying-gasp` a menos de 15 min del corte | `powerfail` |
    | 3 | Caída, con alarma LOS de menos de 7 días | `los` |
    | 4 | Caída (incluye LOS de 7 días o más) | `offline` |
    | 5 | Sin estado ni LOS en Zabbix | `sin_datos` |

    `powerfail` se evalúa antes que `los` a propósito: un corte de energía apaga
    la ONT y eso genera LOS en la OLT, así que ambas señales aparecen juntas y el
    dying-gasp es la más específica. Operativamente la diferencia importa:
    `powerfail` se resuelve solo, `los` necesita cuadrilla.

    **Parámetros**

    * `empresa_id` (ruta): ID de empresa en la base napear.
    * `horas` (1–8760, opcional): descarta lecturas de estado y LOS más viejas
      que N horas. Sin este parámetro se trae el último valor real de cada ONU,
      sea de hoy o de hace meses. Acotar la ventana **no acelera** la consulta,
      solo descarta datos viejos a costa de más `sin_datos`.
    * `page` (≥1, por defecto 1) y `limit` (1–5000, por defecto 500): recorte del
      listado. **Paginar no acelera nada**: las tres bases se consultan enteras
      igual y el recorte se hace en memoria al final. Sirve para no mover 40.000
      registros por HTTP.
    * `full` (por defecto `false`): devuelve el listado completo por streaming e
      ignora `page` y `limit`. En ese modo `metadata.paginacion` viene en `null`.
    * `estado` (opcional): filtra el listado por categoría (`online`, `offline`,
      `los`, `powerfail`, `sin_datos`). **El `resumen` siempre se calcula sobre
      el parque completo**, sin importar este filtro ni la paginación, para que
      no cambie según cómo se lo consulte.

    **Devuelve** `status`, `metadata` (empresa, totales por base, rango temporal
    y paginación), `resumen` (los cinco contadores excluyentes, el porcentaje
    online y el origen del estado) y `dispositivos` (el listado ya filtrado y
    recortado).

    Una advertencia al interpretar los números: la mayoría de las ONUs no tiene
    el item `OnlineState`, y cuando falta el estado se deriva de la alarma
    óptica. Eso detecta **fibra caída, no servicio caído**: una ONU con fibra
    sana pero servicio muerto figura `online`. El campo `origen_estado` de cada
    dispositivo y el conteo `resumen.origen_estado` dicen qué tan mezclada viene
    la muestra.
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
