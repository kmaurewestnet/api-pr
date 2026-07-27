"""Endpoint de analíticas agregadas por empresa."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

import db
from models import AnalyticsResponse
from security import verificar_api_key
from services import analytics as svc

log = logging.getLogger(__name__)

router = APIRouter(tags=["analiticas"], dependencies=[Depends(verificar_api_key)])

ESTADOS = ("online", "offline", "sin_datos", "los")


def _filtrar(dispositivos, estado):
    if not estado:
        return dispositivos
    if estado == "los":
        return [d for d in dispositivos if d["con_los"]]
    return [d for d in dispositivos if d["estado"] == estado]


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
)
def analiticas_empresa(
    empresa_id: int = Path(..., description="ID de empresa en la base napear"),
    horas: int = Query(
        default=168, ge=1, le=720,
        description="Ventana hacia atrás para buscar el último valor de estado y LOS "
                    "en Zabbix. El default son 7 días: el estado operativo solo se "
                    "escribe al cambiar, así que una ventana corta deja muchas ONUs "
                    "en 'sin_datos'.",
    ),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=500, ge=1, le=5000),
    full: bool = Query(
        default=False,
        description="Devuelve el listado completo por streaming, ignorando page y limit",
    ),
    estado: Optional[str] = Query(
        default=None,
        description="Filtra el listado: online | offline | sin_datos | los. "
                    "El resumen siempre se calcula sobre el total.",
    ),
):
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
