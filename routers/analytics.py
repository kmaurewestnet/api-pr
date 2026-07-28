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
