import logging
from contextlib import asynccontextmanager

from urllib.parse import quote

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse

import config
import db
from models import ERRORES_AUTENTICACION
from routers import analytics, cortes, precinto
from services import (
    analytics as servicio_analytics,
    cortes_io,
    precinto as servicio_precinto,
    red,
)
from security import es_externo, verificar_api_key, verificar_api_key_docs

config.setup_logging()
log = logging.getLogger(__name__)


# Los tres consumidores del pool de zabbix, con lo que toma cada uno a la vez.
# Cada módulo declara el suyo: la cuenta se hacía en un comentario de config.py
# que nombraba a un solo endpoint y con un número que ya se había quedado viejo.
# Qué endpoints monta cada perfil. `completo` es la unión, no una lista aparte:
# repetirla ya sería la forma de que un endpoint nuevo se olvide en un perfil.
ENDPOINTS_POR_PERFIL = {
    "critico": ("cortes",),
    "interno": ("precinto", "analytics"),
}
ENDPOINTS_POR_PERFIL["completo"] = tuple(
    sorted(set(ENDPOINTS_POR_PERFIL["critico"] + ENDPOINTS_POR_PERFIL["interno"]))
)

ROUTERS = {"cortes": cortes, "analytics": analytics, "precinto": precinto}

# Cada uno con lo que toma a la vez y su tope de admisión. Los tres tienen
# ahora un tope propio: hasta que precinto y analytics no lo tuvieron, su
# demanda la fijaba el threadpool de FastAPI y esta cuenta no se podía hacer.
TODOS_LOS_CONSUMIDORES = {
    "cortes": (
        cortes_io.CONEXIONES_ZABBIX_POR_REQUEST,
        config.CORTES_MAX_CONCURRENTES,
        "CORTES_MAX_CONCURRENTES",
    ),
    "analytics": (
        servicio_analytics.CONEXIONES_ZABBIX_POR_REQUEST,
        config.ANALYTICS_MAX_CONCURRENTES,
        "ANALYTICS_MAX_CONCURRENTES",
    ),
    "precinto": (
        servicio_precinto.CONEXIONES_ZABBIX_POR_REQUEST,
        config.PRECINTO_MAX_CONCURRENTES,
        "PRECINTO_MAX_CONCURRENTES",
    ),
}


# El presupuesto es el de ESTA instancia: la crítica no paga por analytics
# porque no lo monta. Es lo que hace que POOL_MAX se pueda dimensionar por
# fleet en vez de tener que cubrir la suma de todos los endpoints en todos lados.
ENDPOINTS = ENDPOINTS_POR_PERFIL[config.API_PERFIL]
CONSUMIDORES_ZABBIX = {
    n: v for n, v in TODOS_LOS_CONSUMIDORES.items() if n in ENDPOINTS
}


def revisar_presupuesto_zabbix(pool_max, consumidores):
    """Aviso si la demanda máxima de los tres endpoints no entra en el pool.

    Antes verificaba solo cortes, porque era el único con control de admisión y
    el único cuya demanda máxima era un número nuestro. Ahora los tres tienen
    tope, así que la cuenta es la suma: es lo que puede pedirse a la vez si los
    tres están llenos al mismo tiempo, que es exactamente el pico que importa.

    Un tope en 0 significa "sin límite" y vuelve la suma incalculable: se avisa
    aparte en vez de fingir un número.

    Devuelve el aviso, o None si los números cierran. No aborta el arranque: un
    operador que sube la concurrencia durante un incidente no debería quedarse
    sin API por eso.
    """
    sin_tope = [n for n, (_, c, _) in consumidores.items() if c <= 0]
    if sin_tope:
        return (
            f"{', '.join(sorted(sin_tope))} no tiene tope de admisión, así que su "
            f"demanda sobre el pool de zabbix (POOL_MAX={pool_max}) no está "
            f"acotada. Bajo carga se van a agotar las conexiones y los requests "
            f"van a salir por 503."
        )

    demanda = sum(por_req * conc for por_req, conc, _ in consumidores.values())
    if demanda <= pool_max:
        return None
    detalle = " + ".join(
        f"{n} {conc}x{por_req}"
        for n, (por_req, conc, _) in sorted(consumidores.items())
    )
    return (
        f"La demanda máxima de zabbix es {detalle} = {demanda} conexiones, y "
        f"POOL_MAX={pool_max}. Bajo carga se va a agotar el pool y los requests "
        f"van a salir por 503: subí POOL_MAX a {demanda}, o bajá el tope de "
        f"alguno de los tres. El primero a bajar es analytics, que es un tablero."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "Perfil %s: monta %s", config.API_PERFIL, ", ".join(ENDPOINTS) or "nada"
    )
    aviso = revisar_presupuesto_zabbix(config.POOL_MAX, CONSUMIDORES_ZABBIX)
    if aviso:
        log.warning("Presupuesto del pool de zabbix: %s", aviso)
    log.info(
        "Pool de zabbix: POOL_MAX=%d | admisión x conexiones por request: %s",
        config.POOL_MAX,
        ", ".join(
            f"{n}={conc}x{por_req}"
            for n, (por_req, conc, _) in sorted(CONSUMIDORES_ZABBIX.items())
        ),
    )
    # Los pools se crean de forma diferida al primer uso (ver db.py), así que
    # al apagar solo hace falta liberarlos.
    yield
    if "cortes" in ENDPOINTS:
        cortes.cerrar_ejecutor()
    db.close_all()


DESCRIPCION = """
API de monitoreo de red de acceso. Expone el estado de las ONUs de fibra (GPON)
y de los enlaces wireless mediante el cruce de **cinco bases de datos** que no
admiten JOIN entre sí, complementado con verificaciones en vivo por `ping` ICMP
y `snmpget` contra las OLT.

## Endpoints

| Endpoint | Alcance | Caso de uso |
|---|---|---|
| `GET /api/v1/precinto/{codigo}` | Una ONU | Diagnóstico detallado: series históricas de potencia óptica, registros de caída y estados |
| `GET /api/v1/empresa/{id}/analytics` | Parque completo de una empresa | Tableros, reportes de disponibilidad y auditoría de cobertura |
| `GET /api/v1/cortes/{numero_cliente}` | Un cliente | Atención al cliente: determinar si el servicio está interrumpido y si la falla es de zona |
| `GET /health` | La API | Monitoreo del servicio y de sus dependencias |

## Autenticación

Todos los endpoints, incluido `/health`, requieren la cabecera `X-API-Key`.

> **Esta instancia puede no montarlos todos.** La API se despliega en dos fleets
> de la misma imagen —uno para `/cortes` y otro para los tableros— para que no
> compartan proceso ni pool de conexiones. El esquema de abajo lista únicamente
> los endpoints que **esta** instancia sirve; el resto responde `404` acá y vive
> detrás del mismo proxy, en la otra ruta.

```bash
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/api/v1/cortes/302381"
```

Cada consumidor dispone de una clave propia. El nombre asociado se registra en
cada línea de log y constituye la unidad del **rate limit**, lo que permite
revocar o limitar consumidores de forma individual. Una clave inválida o ausente
produce `403`.

La documentación también está protegida: `/docs`, `/redoc` y `/openapi.json`
requieren la clave en las mismas condiciones que el resto de los endpoints, con
la diferencia de que **únicamente estas tres la aceptan por `?key=`**, dado que
el navegador no envía cabeceras al abrir una URL.

Acceso desde el navegador: `http://localhost:8000/docs?key=<clave>`. Debe
tenerse en cuenta que, por esa vía, la clave queda registrada en el historial
del navegador y en el log de accesos del servidor.

## Modelo de acceso

**No se aplica filtro por empresa, y es una decisión deliberada.** `/precinto` y
`/analytics` abarcan el parque de las 17 empresas porque esa es su función:
constituyen la vista del equipo de operaciones sobre la totalidad de la red de
acceso, no un portal por cliente. No existe vinculación entre clave y empresa;
si un consumidor debe ver únicamente su propio parque, ese recorte corresponde
al sistema consumidor.

Lo que sí se acota es **el alcance de cada clave**. Las claves internas acceden
a la totalidad de los endpoints. Las externas quedan limitadas a `/cortes`
mediante `API_KEYS_SOLO_CORTES`, dado que `/cortes` responde por un cliente
puntual y no expone el parque de ninguna empresa. Con una clave externa,
cualquier otro endpoint responde `403`.

| Endpoint | Claves habilitadas | Alcance de la información |
|---|---|---|
| `GET /api/v1/cortes/{n}` | internas y externas | un cliente |
| `GET /api/v1/precinto/{c}` | sólo internas | una ONU (varias si el código es parcial) |
| `GET /api/v1/empresa/{id}/analytics` | sólo internas | el parque completo de una empresa |
| `GET /health` | internas y externas | estado del servicio. Las externas reciben **solo** `status`; el detalle por base es para las internas |

El precinto se compara como **texto literal**: los metacaracteres carecen de
significado especial.

## Rate limit

Cubeta de tokens **por consumidor**, con dos cuotas independientes:

| Endpoints | Variable | Valor por defecto |
|---|---|---|
| `/cortes` | `RATE_LIMIT_POR_MINUTO` | 60 por minuto |
| `/precinto` y `/analytics` | `RATE_LIMIT_INTERNO_POR_MINUTO` | 10 por minuto |

Las cuotas están separadas porque el costo de cada operación difiere: una
consulta analítica recorre el parque completo de la empresa **incluso cuando se
solicita `limit=1`**, y comparte con `/cortes` el pool de conexiones al sistema
de monitoreo. La cuota interna es la más baja para evitar que un tablero con
refresco automático degrade la atención del resto de los consumidores.

`RATE_LIMIT_POR_CONSUMIDOR` asigna una cuota diferenciada a un consumidor
puntual (`consumidor_a:20,consumidor_b:120`). Solo ajusta la cuota de `/cortes`;
los endpoints internos no admiten ajuste por consumidor, dado que únicamente los
alcanzan claves internas.

Al agotarse la cuota se responde `429` con la cabecera `Retry-After` expresada
en segundos.

**Toda respuesta** de un endpoint con cuota —no únicamente el `429`— incluye el
estado del límite, para que el consumidor pueda regularse antes de chocar contra
él y no después:

| Cabecera | Significado |
|---|---|
| `X-RateLimit-Limit` | Cuota del consumidor, en requests por minuto |
| `X-RateLimit-Remaining` | Requests disponibles **con el actual ya descontado** |
| `X-RateLimit-Reset` | Epoch UNIX en el que vuelve a haber al menos un request disponible |

Con la cuota desactivada (valor `0`) las tres cabeceras se omiten: su ausencia
indica que no hay límite que informar.

El límite tiene mayor relevancia que en una API convencional: **un único request
a `/cortes` puede originar decenas de consultas SNMP contra una OLT en
producción**, por lo que una iteración sobre números de cliente afecta a la red
antes que a la API.

La implementación es **en memoria y por proceso**: con N workers de uvicorn el
límite efectivo equivale a N veces el valor configurado. Con un único proceso el
valor es exacto.

## Control de admisión

Independiente del rate limit: el rate limit acota la **tasa** por consumidor, la
admisión acota los requests **simultáneos** de cada endpoint, sin importar quién
los haga. Es lo que mantiene acotada la demanda sobre el pool de conexiones al
sistema de monitoreo, que los tres endpoints comparten.

| Endpoint | Simultáneos | Conexiones por request | Al superarse |
|---|---|---|---|
| `/cortes` | `CORTES_MAX_CONCURRENTES` (5) | 2 | encola; `504` vencido `CORTES_TIMEOUT_SEG` |
| `/analytics` | `ANALYTICS_MAX_CONCURRENTES` (2) | 2 | encola; `503` vencido `INTERNO_ESPERA_SEG` |
| `/precinto` | `PRECINTO_MAX_CONCURRENTES` (2) | 1 | encola; `503` vencido `INTERNO_ESPERA_SEG` |

La suma —16— tiene que entrar en `POOL_MAX`, y la API la verifica al arrancar:
si no cierra, lo deja escrito en el log con el número al que hay que subirlo.

## Códigos de error

| Código | Significado |
|---|---|
| `403` | API Key inválida o ausente, o válida pero sin alcance sobre el endpoint |
| `404` | El recurso no existe o no tiene datos asociados |
| `422` | Un parámetro no cumple el formato o el rango esperado |
| `429` | Rate limit del consumidor alcanzado |
| `500` | Error inesperado durante el procesamiento |
| `503` | Una dependencia no responde. El cuerpo es siempre el mismo: cuál falló se consulta en `/health` y queda en el log, no viaja en la respuesta |
| `504` | La detección de corte superó su tope de tiempo |

Todas las respuestas de error comparten el mismo cuerpo: `{"detail": "<motivo>"}`.

El `503` identifica el caso en que **falta el dato sin el cual la respuesta
carecería de respaldo**. Las verificaciones individuales que fallan (un ping, un
`snmpget`) no invalidan la respuesta: se registran como "no evaluable", quedan
asentadas en el log y no computan como falla.

## Zona horaria y timestamps

Todos los campos `*_timestamp` son **epoch UNIX en segundos** (UTC). No se
devuelven fechas en texto, con excepción de las contenidas en los registros
crudos que provee el sistema de monitoreo.
"""

TAGS_METADATA = [
    {
        "name": "precinto",
        "description": (
            "Series históricas de **una** ONU identificada por su precinto: "
            "potencia óptica recibida por la ONU y por la OLT, log de causas de "
            "caída y cambios de estado. Constituye la vista de diagnóstico "
            "detallado. **Endpoint interno**: no accesible con claves externas."
        ),
    },
    {
        "name": "analiticas",
        "description": (
            "Estado agregado del **parque completo** de una empresa, con resumen "
            "por categoría y listado paginado de dispositivos. Orientado a "
            "tableros y reportes de disponibilidad. **Endpoint interno**: no "
            "accesible con claves externas."
        ),
    },
    {
        "name": "cortes",
        "description": (
            "Diagnóstico de **un cliente** identificado por su número de "
            "contrato: determina si el servicio está operativo y si la falla "
            "afecta a la zona. Contrato de salida cerrado de tres booleanos, "
            "orientado a su consumo desde un CRM o un asistente de atención. "
            "Es el único endpoint accesible con claves externas y el de mayor "
            "amplificación sobre la red: un request origina consultas SNMP "
            "contra una OLT en producción."
        ),
    },
    {
        "name": "infra",
        "description": (
            "Salud del servicio: conectividad de las cinco bases de datos y "
            "disponibilidad de las utilidades del sistema (`ping` y `snmpget`)."
        ),
    },
]

app = FastAPI(
    title="API de Monitoreo de Red de Acceso",
    description=DESCRIPCION,
    summary=(
        "Estado de ONUs de fibra y enlaces wireless: métricas por precinto, "
        "analíticas por empresa y detección de cortes por cliente."
    ),
    version="2.1.0",
    contact={
        "name": "Equipo de Operaciones de Red",
    },
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
    # FastAPI monta /docs, /redoc y /openapi.json como rutas propias, sin
    # dependencias: las de los routers no las alcanzan. Se apagan acá y se
    # vuelven a montar más abajo detrás de la API key, para que el esquema
    # —que enumera cada endpoint, parámetro y código de error— no quede
    # legible para cualquiera que llegue al puerto.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CORS solo si hay un panel web declarado. Sin CORS_ORIGINS no se monta nada:
# los consumidores server-to-server no pasan por el navegador y el middleware
# seria peso muerto.
#
# allow_credentials queda en False a proposito: la autenticacion es la cabecera
# X-API-Key, no una cookie, y ponerlo en True obligaria a jurar que el origen es
# exacto sin ganar nada. GET es el unico metodo que expone la API.
if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.CORS_ORIGINS),
        allow_methods=["GET"],
        allow_headers=[config.API_KEY_NAME],
        max_age=3600,
    )

@app.middleware("http")
async def poner_headers_de_limite(request: Request, call_next):
    """Copia los `X-RateLimit-*` que dejó el rate limit sobre la respuesta final.

    Va acá y no en la dependencia porque un Response construido a mano no hereda
    los headers del que FastAPI inyecta: lo reemplaza entero. Eso alcanza al
    streaming de analíticas y a **toda** respuesta de error, que se arman desde
    la HTTPException. Desde el middleware el header sale igual en un 200, en un
    503 y en el 429, que es la única forma de que un consumidor pueda leer
    siempre cuánta cuota le queda.
    """
    respuesta = await call_next(request)
    respuesta.headers.update(getattr(request.state, "headers_de_limite", {}))
    return respuesta


for _nombre in ENDPOINTS:
    app.include_router(ROUTERS[_nombre].router)


# --- Documentación, detrás de la misma API key que el resto -------------------
# Estas tres aceptan la clave por cabecera o por `?key=`, a diferencia de los
# endpoints de negocio, que siguen exigiendo la cabecera. El motivo es que el
# navegador no manda cabeceras al tipear una URL, ni el `fetch` con el que
# Swagger UI se trae el esquema: por header la UI no carga.

_RUTA_OPENAPI = "/openapi.json"


def _url_openapi(key) -> str:
    """URL del esquema, arrastrando la clave si vino por query string.

    Sin esto la UI carga pero queda vacía: el `fetch` del esquema saldría sin
    credencial y se comería un 403.
    """
    return f"{_RUTA_OPENAPI}?key={quote(key)}" if key else _RUTA_OPENAPI


@app.get(_RUTA_OPENAPI, include_in_schema=False,
         dependencies=[Depends(verificar_api_key_docs)])
def openapi_protegido():
    """Esquema OpenAPI. Requiere `X-API-Key` o `?key=`."""
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False,
         dependencies=[Depends(verificar_api_key_docs)])
def swagger_protegido(key: str = Query(default=None)):
    """Swagger UI. Requiere `X-API-Key` o `?key=`."""
    return get_swagger_ui_html(
        openapi_url=_url_openapi(key),
        title=f"{app.title} — Swagger UI",
        # Sin OAuth2 no hace falta la página de redirección que monta FastAPI
        # por defecto, y con docs_url=None ya no existe.
        oauth2_redirect_url=None,
    )


@app.get("/redoc", include_in_schema=False,
         dependencies=[Depends(verificar_api_key_docs)])
def redoc_protegido(key: str = Query(default=None)):
    """ReDoc. Requiere `X-API-Key` o `?key=`."""
    return get_redoc_html(openapi_url=_url_openapi(key), title=f"{app.title} — ReDoc")


@app.get(
    "/health",
    tags=["infra"],
    summary="Estado de la API y de sus dependencias",
    response_description=(
        "Estado global ('success' o 'degraded'). Con una clave interna incluye "
        "además el detalle por base de datos y por utilidad del sistema"
    ),
    responses={
        200: {
            "description": (
                "Verificación realizada. **Siempre 200, incluso con 'degraded'**: "
                "el código HTTP indica que la verificación pudo ejecutarse, no "
                "que todas las dependencias estén operativas. El estado real se "
                "determina por el campo `status`.\n\n"
                "El cuerpo depende del alcance de la clave: **las externas "
                "reciben únicamente `status`**. Enumerar las bases y devolver el "
                "error del driver es describir la infraestructura, y para decidir "
                "si reintentar alcanza con saber que el servicio está degradado."
            ),
            "content": {
                "application/json": {
                    "examples": {
                      "interna": {
                        "summary": "Clave interna — detalle completo",
                        "value": {
                        "status": "degraded",
                        "environment": "production",
                        "bases": {
                            "zabbix": {"ok": True},
                            "zabbix_wireless": {"ok": True},
                            "soldef": {"ok": True},
                            "napear": {"ok": True},
                            "gestion": {
                                "ok": False,
                                "error": "Can't connect to MySQL server (timed out)",
                            },
                        },
                        "utilidades": {
                            "ping": {"ok": True},
                            "snmpget": {"ok": True},
                        },
                        },
                      },
                      "externa": {
                        "summary": "Clave externa — solo el estado global",
                        "value": {"status": "degraded"},
                      },
                    }
                }
            },
        },
        **ERRORES_AUTENTICACION,
    },
)
def health(consumidor: str = Depends(verificar_api_key)):
    """Verifica la conectividad de la API con la totalidad de sus dependencias.

    Ejecuta un `ping` de conexión contra cada una de las cinco bases de datos y
    comprueba la disponibilidad de los dos binarios del sistema que utiliza la
    detección de cortes.

    **Parámetros:** ninguno. Requiere únicamente la cabecera `X-API-Key`.

    **Devuelve** un objeto con:

    * `status`: `"success"` si todas las dependencias responden, `"degraded"` si
      falla al menos una base o una utilidad. **Es el único campo que reciben las
      claves externas**: la lista de bases y el mensaje del driver describen la
      infraestructura, y para decidir si reintentar no hacen falta.
    * `environment`: entorno declarado en la variable `ENVIRONMENT`.
    * `bases`: por cada base, un `{"ok": bool}` y, cuando falla, el `error`
      devuelto por el driver.
    * `utilidades`: idem para `ping` y `snmpget`.

    `ping` y `snmpget` son binarios del sistema, no dependencias de Python: si
    no están disponibles, el endpoint de cortes responde igualmente, pero con
    todas las verificaciones de red en "no evaluable". Exponerlo en este endpoint
    evita tener que inferirlo a partir de resultados anómalos.

    El endpoint responde **200 en ambos casos**: un `status: "degraded"` no
    constituye un error del request. Los sistemas de monitoreo deben alertar
    sobre el campo `status`, no sobre el código HTTP.
    """
    resultado = {}
    for nombre in ("zabbix", "zabbix_wireless", "soldef", "napear", "gestion"):
        try:
            db.ping(nombre)
            resultado[nombre] = {"ok": True}
        except Exception as e:
            resultado[nombre] = {"ok": False, "error": str(e)}

    # ping y snmpget son binarios del sistema, no dependencias de Python: si
    # faltan, el endpoint de cortes responde igual pero con todas las
    # verificaciones de red en "no evaluable". Mejor verlo acá que deducirlo.
    utilidades = red.utilidades_disponibles()
    todo_ok = all(v["ok"] for v in resultado.values()) and all(
        u["ok"] for u in utilidades.values()
    )
    estado = "success" if todo_ok else "degraded"
    if es_externo(consumidor):
        # Sí o no, sin el mapa: qué bases existen, cuáles se cayeron y qué
        # contesta el driver es infraestructura. Para reintentar alcanza con
        # saber que está degradado.
        return {"status": estado}
    return {
        "status": estado,
        "environment": config.ENVIRONMENT,
        "bases": resultado,
        "utilidades": utilidades,
    }
