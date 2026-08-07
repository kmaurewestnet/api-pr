"""Endpoint de detección de cortes por número de cliente."""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Path

import db
from models import CorteResponse
from security import verificar_api_key
from services import cortes as svc

log = logging.getLogger(__name__)

router = APIRouter(tags=["cortes"], dependencies=[Depends(verificar_api_key)])

# El número de cliente entra a tres lugares distintos: un `=` en MySQL, un `~*`
# en PostgreSQL y los logs. Restringirlo a dígitos lo vuelve inofensivo en los
# tres: no puede aportar comillas, ni metacaracteres de regex, ni saltos de
# línea. El tope de largo evita que un valor absurdo llegue a la base.
MAX_LARGO_CLIENTE = 12
_SOLO_DIGITOS = re.compile(rf"^\d{{1,{MAX_LARGO_CLIENTE}}}$")


@router.get(
    "/api/v1/cortes/{numero_cliente}",
    response_model=CorteResponse,
    summary="Detecta si un cliente está caído y si el corte es de zona",
)
def detectar_corte(
    numero_cliente: str = Path(
        ...,
        description=(
            f"Número de cliente: solo dígitos, hasta {MAX_LARGO_CLIENTE} "
            "caracteres (campo `customer.code` de Gestión)"
        ),
    ),
):
    numero_cliente = numero_cliente.strip()
    if not _SOLO_DIGITOS.match(numero_cliente):
        raise HTTPException(
            status_code=422,
            detail=(
                "El número de cliente debe ser numérico y de hasta "
                f"{MAX_LARGO_CLIENTE} dígitos"
            ),
        )

    try:
        return svc.detectar(numero_cliente)
    except svc.ClienteNoEncontrado:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No se encontró el cliente {numero_cliente} con un contrato "
                "activo en las categorías contempladas"
            ),
        )
    except db.DatabaseUnavailable as e:
        log.error("Cliente %s: %s", numero_cliente, e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        log.exception("Error detectando el corte del cliente %s: %s",
                      numero_cliente, e)
        raise HTTPException(
            status_code=500, detail="Error al procesar la detección de corte"
        )
