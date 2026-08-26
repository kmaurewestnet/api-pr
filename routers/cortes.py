"""Endpoint de detección de cortes por número de cliente."""
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Path

import config
import db
from models import ERROR_RATE_LIMIT, ERRORES_AUTENTICACION, CorteResponse, error
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
    summary="Determina la interrupción de servicio de un cliente y su alcance",
    response_description=(
        "Los tres booleanos del diagnóstico: tecnología del plan, disponibilidad "
        "del servicio y alcance de zona de la falla"
    ),
    responses={
        200: {
            "description": (
                "Diagnóstico resuelto. Una verificación individual fallida no "
                "invalida la respuesta: se registra como 'no evaluable', queda "
                "asentada en el log y no computa como falla en la decisión."
            )
        },
        **ERRORES_AUTENTICACION,
        404: error(
            "El cliente no existe, no registra contrato activo, o su plan no "
            "corresponde a las categorías contempladas (fibra o wireless).",
            "No se encontró el cliente 302381 con un contrato activo en las "
            "categorías contempladas",
        ),
        422: error(
            f"`numero_cliente` no es numérico o excede los {MAX_LARGO_CLIENTE} "
            "dígitos.",
            f"El número de cliente debe ser numérico y de hasta "
            f"{MAX_LARGO_CLIENTE} dígitos",
        ),
        # ponytail: el 422 de este endpoint siempre lo emite la validación
        # propia (el parámetro de ruta es str, así que FastAPI no lo rechaza),
        # por eso acá va un solo ejemplo y no dos como en analytics.
        **ERROR_RATE_LIMIT,
        500: error(
            "Error inesperado durante la resolución del diagnóstico.",
            "Error al procesar la detección de corte",
        ),
        503: error(
            "La base de administración, o el sistema de monitoreo correspondiente "
            "a la tecnología del cliente, no responde. Sin ese dato la respuesta "
            "carecería de respaldo, por lo que se rechaza el request en lugar de "
            "devolver booleanos no verificados.",
            "La base gestion no está disponible",
        ),
        504: error(
            f"La detección superó los {config.CORTES_TIMEOUT_SEG}s "
            "(`CORTES_TIMEOUT_SEG`). Habitualmente indica latencia elevada en una "
            "OLT o en una base de datos, o una cola de requests superior al tope "
            "de concurrencia.",
            f"La detección superó los {config.CORTES_TIMEOUT_SEG}s",
        ),
    },
)
async def detectar_corte(
    numero_cliente: str = Path(
        ...,
        description=(
            f"Número de cliente: solo dígitos, hasta {MAX_LARGO_CLIENTE} "
            "caracteres. Corresponde al código de cliente del sistema de "
            "administración."
        ),
    ),
    consumidor: str = Depends(limitar_tasa),
):
    """Diagnostica en tiempo real la interrupción de servicio de un cliente.

    Orientado a atención al cliente: resuelve en una sola llamada las dos
    preguntas que determinan el curso de la consulta —si el cliente tiene
    servicio y si la falla es individual o de zona— con un contrato de salida
    cerrado de tres booleanos.

    Parte del número de cliente en la **base de administración**, de la que
    obtiene la tecnología del plan y la IP asignada, y a partir de ahí bifurca:

    * **Fibra (FTTH)** — consulta el sistema de monitoreo de fibra por la NAP, la
      OLT y su IP; verifica el estado de la ONT y el de la NAP (por consulta, o
      por `snmpget` directo contra la OLT en el caso de las OLT "Solar"), y
      consulta el inventario de red por el switch del nodo.
    * **Wireless** — consulta el sistema de monitoreo de wireless por el Access
      Point y el RouterBoard del nodo.

    Las verificaciones del último paso (pings, `snmpget` y consultas de estado)
    son independientes entre sí y se ejecutan en paralelo.

    **Parámetros**

    * `numero_cliente` (ruta): solo dígitos, hasta 12 caracteres. Corresponde al
      código de cliente del sistema de administración. La restricción no es
      cosmética: el valor se incorpora a una consulta MySQL, a una PostgreSQL y a
      los logs, y limitarlo a dígitos lo vuelve inocuo en los tres contextos.

    **Devuelve** exactamente tres campos, sin envoltorio ni metadata:

    * `isFtth` — el plan activo es de fibra (`category_id` 16). `false`
      corresponde a wireless (17).
    * `isOnline` — el cliente tiene servicio. En fibra es `false` **únicamente
      si** no responde al ping *y* la ONT reporta LOS/Offline; en wireless se
      determina por el ping al cliente.
    * `isZoneIncident` — la falla afecta a más de un cliente. En fibra: la NAP
      está en corte, o la OLT no responde. En wireless: el AP o el RouterBoard
      del nodo no responden.

    La combinación de los dos últimos campos define la interpretación operativa:

    | `isOnline` | `isZoneIncident` | Interpretación |
    |---|---|---|
    | `false` | `true` | Corte de zona: existen otros afectados; hay incidente registrado o cuadrilla asignada |
    | `false` | `false` | Falla individual: ONT, corte de energía en el domicilio o cableado |
    | `true` | `true` | El cliente tiene servicio, pero hay un incidente próximo; puede presentar degradación |
    | `true` | `false` | Sin anomalías detectables desde la red |

    El detalle de cada verificación individual se registra en el **log del
    servidor**, no en la respuesta. Una verificación fallida no invalida el
    diagnóstico: se registra como "no evaluable" y no computa como falla.

    Este endpoint aplica **rate limit por consumidor** (`429` con `Retry-After`)
    y un tope de concurrencia: superado ese tope los requests se encolan y, de no
    resolverse a tiempo, responden `504`. El motivo es la protección de la red,
    no de la API: un único request puede originar decenas de consultas SNMP
    contra una OLT en producción.
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
