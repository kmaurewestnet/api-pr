import logging
from contextlib import asynccontextmanager

from urllib.parse import quote

from fastapi import Depends, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse

import config
import db
from models import ERRORES_AUTENTICACION
from routers import analytics, cortes, precinto
from services import red
from security import verificar_api_key, verificar_api_key_docs

config.setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Los pools se crean de forma diferida al primer uso (ver db.py), así que
    # acá solo hace falta liberarlos al apagar.
    yield
    cortes.cerrar_ejecutor()
    db.close_all()


DESCRIPCION = """
API de monitoreo de red de acceso: consulta el estado de las ONUs de fibra
(GPON) y de los enlaces wireless cruzando **cinco bases de datos** que no
admiten JOIN entre sí (Zabbix Fibra, Zabbix Wireless, Soldef, Napear y Gestión),
más verificaciones en vivo por `ping` ICMP y `snmpget` contra las OLT.

## Qué resuelve cada endpoint

| Endpoint | Alcance | Caso de uso típico |
|---|---|---|
| `GET /api/v1/precinto/{codigo}` | Una ONU | Diagnóstico fino: series históricas de potencia óptica, logs y estados |
| `GET /api/v1/empresa/{id}/analytics` | El parque completo de una empresa | Tableros, reportes de disponibilidad, auditoría de cobertura |
| `GET /api/v1/cortes/{numero_cliente}` | Un cliente | Atención al cliente: ¿está caído? ¿es un corte de zona? |
| `GET /health` | La API misma | Monitoreo del servicio y de sus dependencias |

## Autenticación

Todos los endpoints —incluido `/health`— exigen la cabecera `X-API-Key`.

```bash
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/api/v1/cortes/302381"
```

Cada consumidor tiene su propia clave. El nombre asociado aparece en cada línea
de log y es la unidad del **rate limit**, así que se le puede revocar o limitar a
uno sin afectar a los demás. Una clave inválida o ausente devuelve `403`.

La documentación misma está protegida: `/docs`, `/redoc` y `/openapi.json`
exigen la clave igual que los endpoints, pero **solo ellas la aceptan también
por `?key=`**, porque el navegador no puede mandar cabeceras al abrir una URL:

```bash
# Para bajar el esquema y generar un cliente
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/openapi.json"
```

Para abrir esta página en el navegador: `http://localhost:8000/docs?key=<clave>`.
Tené en cuenta que así la clave queda en el historial y en el log de accesos.

## Modelo de confianza

**No hay filtro por empresa, y es a propósito.** `/precinto` y `/analytics`
cruzan el parque de las 17 empresas porque esa es su función: son la vista del
NOC sobre toda la red de acceso, no un portal por cliente. No existe ni está
previsto un vínculo clave → empresa; si aparece un consumidor que sólo deba ver
lo suyo, ese recorte va del lado de quien consume, no acá.

Lo que sí se acota es **hasta dónde llega cada clave**. Las internas llegan a
todo. Las externas —hoy la centralita y el chatbot— se limitan a `/cortes` con
`API_KEYS_SOLO_CORTES`, porque `/cortes` responde por un cliente puntual y no
expone el parque de nadie. Con una clave así, cualquier otro endpoint devuelve
`403`.

| Endpoint | Claves que lo alcanzan | Qué expone |
|---|---|---|
| `GET /api/v1/cortes/{n}` | internas y externas | un cliente |
| `GET /api/v1/precinto/{c}` | sólo internas | una ONU (varias si el código es corto) |
| `GET /api/v1/empresa/{id}/analytics` | sólo internas | el parque completo de una empresa |
| `GET /health` | internas y externas | estado del servicio |

El precinto se compara como **texto literal**: los metacaracteres no significan
nada. Antes se compilaba como expresión regular y quien llamaba elegía el patrón,
no el valor.

## Rate limit

Cubeta de tokens **por consumidor**, con dos cuotas separadas:

| Endpoints | Variable | Por defecto |
|---|---|---|
| `/cortes` | `RATE_LIMIT_POR_MINUTO` | 60 por minuto |
| `/precinto` y `/analytics` | `RATE_LIMIT_INTERNO_POR_MINUTO` | 10 por minuto |

Van separadas porque no cuestan lo mismo: una analítica recorre el parque entero
de la empresa **aunque se pida `limit=1`**, y comparte con `/cortes` el pool de
conexiones a Zabbix. Que un tablero refrescando no pueda dejar sin atender a la
centralita es el motivo de que la cuota interna sea la más baja.

`RATE_LIMIT_POR_CONSUMIDOR` le da una cuota distinta a un consumidor puntual
(`chatbot:20,centralita:120`). Ajusta la cuota de `/cortes`; los endpoints
internos no se ajustan por consumidor, porque ahí sólo llegan claves internas.

Al agotarse, `429` con la cabecera `Retry-After` en segundos.

El límite importa más que en una API común: **un solo request de `/cortes` puede
disparar decenas de consultas SNMP contra una OLT de producción**, así que un
loop sobre números de cliente le hace DoS a la red, no a la API.

Es **en memoria y por proceso**: con N workers de uvicorn el límite efectivo es N
veces el configurado. Con un solo proceso —como lo levanta el README— el número
es exacto.

## Cómo leer los errores

| Código | Significado |
|---|---|
| `403` | API Key inválida o ausente, o válida pero sin alcance para ese endpoint |
| `404` | El recurso no existe o no tiene datos asociados |
| `422` | Un parámetro no cumple el formato o el rango esperado |
| `429` | Rate limit del consumidor alcanzado |
| `500` | Error inesperado al procesar |
| `503` | Alguna base de datos no responde — `/health` indica cuál |
| `504` | La detección de corte superó su tope de tiempo |

Todos los errores comparten el mismo cuerpo: `{"detail": "<motivo>"}`.

Un `503` distingue el caso en que **falta el dato sin el cual la respuesta sería
inventada**. Las verificaciones individuales que fallan (un ping, un `snmpget`)
no rompen la respuesta: quedan como "no evaluable", se loguean y no cuentan como
falla.

## Zona horaria y timestamps

Todos los campos `*_timestamp` son **epoch UNIX en segundos** (UTC). No hay
fechas en texto salvo dentro de los logs crudos que devuelve Zabbix.
"""

TAGS_METADATA = [
    {
        "name": "precinto",
        "description": (
            "Series históricas de **una** ONU identificada por su precinto: "
            "potencia óptica recibida por la ONU y por la OLT, log de causas de "
            "caída y cambios de estado. Es la vista de diagnóstico fino. "
            "**Interno**: no lo alcanzan las claves externas."
        ),
    },
    {
        "name": "analiticas",
        "description": (
            "Estado agregado del **parque completo** de una empresa, con resumen "
            "por categoría y listado paginado de dispositivos. Pensado para "
            "tableros y reportes de disponibilidad. **Interno**: no lo "
            "alcanzan las claves externas."
        ),
    },
    {
        "name": "cortes",
        "description": (
            "Diagnóstico de **un cliente** por su número de contrato: si está "
            "navegando y si el corte afecta a la zona. Contrato de salida cerrado "
            "de tres booleanos, pensado para consumirlo desde un CRM o un bot de "
            "atención. Es el único que alcanzan las claves externas, y el que "
            "amplifica contra la red: un request dispara consultas SNMP contra "
            "una OLT de producción."
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
    title="Zabbix Precintos API",
    description=DESCRIPCION,
    summary=(
        "Estado de ONUs de fibra y enlaces wireless: métricas por precinto, "
        "analíticas por empresa y detección de cortes por cliente."
    ),
    version="2.1.0",
    contact={
        "name": "Equipo de Redes / NOC",
        "email": "noc@westnet.com.ar",
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

app.include_router(precinto.router)
app.include_router(analytics.router)
app.include_router(cortes.router)


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
        "Estado global ('success' o 'degraded') más el detalle por base de datos "
        "y por utilidad del sistema"
    ),
    dependencies=[Depends(verificar_api_key)],
    responses={
        200: {
            "description": (
                "Chequeo realizado. **Siempre 200, incluso en 'degraded'**: el "
                "código HTTP indica que el chequeo se pudo hacer, no que todo "
                "esté sano. Hay que mirar el campo `status`."
            ),
            "content": {
                "application/json": {
                    "example": {
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
                    }
                }
            },
        },
        **ERRORES_AUTENTICACION,
    },
)
def health():
    """Verifica que la API pueda hablar con todo aquello de lo que depende.

    Hace un `ping` de conexión contra cada una de las cinco bases de datos y
    comprueba que estén disponibles los dos binarios del sistema que usa la
    detección de cortes.

    **Parámetros:** ninguno. Solo requiere la cabecera `X-API-Key`.

    **Devuelve** un objeto con:

    * `status`: `"success"` si todo responde, `"degraded"` si falla al menos una
      base o una utilidad.
    * `environment`: entorno declarado en la variable `ENVIRONMENT`.
    * `bases`: por cada base (`zabbix`, `zabbix_wireless`, `soldef`, `napear`,
      `gestion`), un `{"ok": bool}` y, cuando falla, el `error` devuelto por el
      driver.
    * `utilidades`: idem para `ping` y `snmpget`.

    `ping` y `snmpget` son binarios del sistema, no dependencias de Python: si
    faltan, el endpoint de cortes responde igual pero con todas las
    verificaciones de red en "no evaluable". Verlo acá evita tener que deducirlo
    a partir de resultados raros.

    El endpoint responde **200 en ambos casos**: un `status: "degraded"` no es un
    error del request. Los monitores tienen que alertar sobre el campo `status`,
    no sobre el código HTTP.
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
    return {
        "status": "success" if todo_ok else "degraded",
        "environment": config.ENVIRONMENT,
        "bases": resultado,
        "utilidades": utilidades,
    }
