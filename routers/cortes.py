"""Endpoint de detección de cortes por número de cliente."""
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Path

import config
import db
from models import ERROR_RATE_LIMIT, ERRORES_AUTENTICACION, CorteResponse
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
    response_description=(
        "Los tres booleanos del diagnóstico: tecnología del plan, si el cliente "
        "navega y si el corte afecta a la zona"
    ),
    responses={
        200: {
            "description": (
                "Diagnóstico resuelto. Una verificación suelta que falle no rompe "
                "la respuesta: queda como 'no evaluable', se loguea y no cuenta "
                "como falla en la decisión."
            )
        },
        **ERRORES_AUTENTICACION,
        404: {
            "description": (
                "El cliente no existe, no tiene contrato activo, o su plan no cae "
                "en las categorías contempladas (fibra o wireless)."
            ),
            "content": {
                "application/json": {
                    "example": {
                        "detail": "No se encontró el cliente 302381 con un contrato "
                                  "activo en las categorías contempladas"
                    }
                }
            },
        },
        422: {
            "description": (
                f"`numero_cliente` no es numérico o pasa los {MAX_LARGO_CLIENTE} "
                "dígitos."
            ),
            "content": {
                "application/json": {
                    "example": {
                        "detail": "El número de cliente debe ser numérico y de "
                                  f"hasta {MAX_LARGO_CLIENTE} dígitos"
                    }
                }
            },
        },
        # ponytail: el 422 de este endpoint siempre lo emite la validación
        # propia (el parámetro de ruta es str, así que FastAPI no lo rechaza),
        # por eso acá va un solo ejemplo y no dos como en analytics.
        **ERROR_RATE_LIMIT,
        500: {
            "description": "Error inesperado al procesar la detección.",
            "content": {
                "application/json": {
                    "example": {"detail": "Error al procesar la detección de corte"}
                }
            },
        },
        503: {
            "description": (
                "Gestión, o el Zabbix de la tecnología del cliente, no responde. "
                "Sin ese dato la respuesta sería inventada, así que se rechaza el "
                "request en vez de devolver booleanos sin respaldo."
            ),
            "content": {
                "application/json": {
                    "example": {"detail": "La base gestion no está disponible"}
                }
            },
        },
        504: {
            "description": (
                f"La detección superó los {config.CORTES_TIMEOUT_SEG}s "
                "(`CORTES_TIMEOUT_SEG`). Suele indicar una OLT o una base lentas, "
                "o que hay más requests en cola que el tope de concurrencia."
            ),
            "content": {
                "application/json": {
                    "example": {
                        "detail": f"La detección superó los "
                                  f"{config.CORTES_TIMEOUT_SEG}s"
                    }
                }
            },
        },
    },
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
    """Diagnostica en vivo si un cliente está sin servicio y si el corte es de zona.

    Pensado para atención al cliente: responde las dos preguntas que definen cómo
    seguir una consulta —¿el cliente está caído?, ¿es un problema suyo o de la
    zona?— en una sola llamada y con un contrato de salida cerrado de tres
    booleanos.

    Parte del número de cliente en **Gestión**, de donde saca la tecnología del
    plan y la IP, y a partir de ahí bifurca:

    * **Fibra (FTTH)** — consulta el Zabbix de fibra por la NAP, la OLT y su IP;
      verifica el estado de la ONT y el de la NAP (por consulta, o por `snmpget`
      directo contra la OLT si es una OLT "Solar"), y consulta Soldef por el
      switch del nodo.
    * **Wireless** — consulta el Zabbix de wireless por el Access Point y el
      RouterBoard del nodo.

    Las verificaciones del último paso (pings, `snmpget` y consultas de estado)
    son independientes entre sí y corren en paralelo.

    **Parámetros**

    * `numero_cliente` (ruta): solo dígitos, hasta 12 caracteres. Es el
      campo `customer.code` de Gestión. La restricción no es cosmética: el valor
      entra a una consulta MySQL, a una PostgreSQL y a los logs, y limitarlo a
      dígitos lo vuelve inofensivo en los tres.

    **Devuelve** exactamente tres campos, sin envoltorio ni metadata:

    * `isFtth` — el plan activo es de fibra (`category_id` 16). `false` =
      wireless (17).
    * `isOnline` — el cliente está navegando. En fibra es `false` **solo si** no
      responde al ping *y* la ONT reporta LOS/Offline; en wireless, es el ping al
      cliente.
    * `isZoneIncident` — el corte afecta a más gente. En fibra: la NAP está en
      corte o la OLT no responde. En wireless: el AP o el RouterBoard del nodo no
      responden.

    La combinación de los dos últimos es lo que se usa operativamente:

    | `isOnline` | `isZoneIncident` | Lectura |
    |---|---|---|
    | `false` | `true` | Corte de zona: hay otros afectados, ya hay incidente o cuadrilla |
    | `false` | `false` | Falla individual: ONT, corte de energía en el domicilio, cable |
    | `true` | `true` | El cliente navega pero hay un incidente cerca; puede estar degradado |
    | `true` | `false` | Todo normal desde la red |

    El detalle de cada verificación individual queda en el **log del servidor**,
    no en la respuesta. Una verificación que falla no rompe el diagnóstico: queda
    como "no evaluable" y no cuenta como falla.

    Este endpoint tiene **rate limit por consumidor** (`429` con `Retry-After`) y
    un tope de concurrencia: por encima de él los requests encolan y, si no
    llegan a tiempo, salen por `504`. El motivo es la red, no la API: un solo
    request puede disparar decenas de consultas SNMP contra una OLT de
    producción.
    """
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
