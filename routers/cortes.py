"""Endpoint de detección de cortes por número de cliente."""
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Path

import config
import db
from models import CorteResponse
from security import limitar_tasa
from services import cortes as svc

log = logging.getLogger(__name__)

router = APIRouter(tags=["cortes"])

# El número de cliente entra a tres lugares distintos: un `=` en MySQL, un `~*`
# en PostgreSQL y los logs. Restringirlo a dígitos lo vuelve inofensivo en los
# tres: no puede aportar comillas, ni metacaracteres de regex, ni saltos de
# línea. El tope de largo evita que un valor absurdo llegue a la base.
MAX_LARGO_CLIENTE = 12
_SOLO_DIGITOS = re.compile(rf"^\d{{1,{MAX_LARGO_CLIENTE}}}$")

# Control de admisión. El endpoint se declara `async def` y todo el trabajo
# bloqueante (drivers sincrónicos y subprocesos de ping/snmpget) va acá: el
# event loop queda libre y la cantidad de requests que se procesan a la vez es
# un número explícito, no el tamaño del threadpool que le tocó a FastAPI.
# Por encima del tope, los requests encolan en el loop sin ocupar un hilo.
_ejecutor = ThreadPoolExecutor(
    max_workers=config.CORTES_MAX_CONCURRENTES, thread_name_prefix="cortes"
)


def cerrar_ejecutor() -> None:
    _ejecutor.shutdown(wait=False, cancel_futures=True)


@router.get(
    "/api/v1/cortes/{numero_cliente}",
    response_model=CorteResponse,
    summary="Detecta si un cliente está caído y si el corte es de zona",
)
async def detectar_corte(
    numero_cliente: str = Path(
        ...,
        description=(
            f"Número de cliente: solo dígitos, hasta {MAX_LARGO_CLIENTE} "
            "caracteres (campo `customer.code` de Gestión)"
        ),
    ),
    consumidor: str = Depends(limitar_tasa),
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
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _ejecutor, svc.detectar, numero_cliente
            ),
            timeout=config.CORTES_TIMEOUT_SEG,
        )
    except asyncio.TimeoutError:
        # El trabajo en curso no se puede matar: son subprocesos y drivers
        # sincrónicos. Termina solo, porque cada paso tiene su propio timeout;
        # lo que se corta acá es la espera del cliente, que si no quedaría
        # colgado sin tope.
        log.error(
            "Timeout de %ss resolviendo el cliente %s (consumidor=%s)",
            config.CORTES_TIMEOUT_SEG, numero_cliente, consumidor,
        )
        raise HTTPException(
            status_code=504,
            detail=f"La detección superó los {config.CORTES_TIMEOUT_SEG}s",
        )
    except svc.ClienteNoEncontrado:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No se encontró el cliente {numero_cliente} con un contrato "
                "activo en las categorías contempladas"
            ),
        )
    except db.DatabaseUnavailable as e:
        log.error("Cliente %s (consumidor=%s): %s", numero_cliente, consumidor, e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        log.exception(
            "Error detectando el corte del cliente %s (consumidor=%s): %s",
            numero_cliente, consumidor, e,
        )
        raise HTTPException(
            status_code=500, detail="Error al procesar la detección de corte"
        )
