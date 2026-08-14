import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

import config
import db
from models import ERRORES_AUTENTICACION
from routers import analytics, cortes, precinto
from services import red
from security import verificar_api_key

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

En Swagger UI, usá el botón **Authorize** para cargar la clave una vez y que
viaje en todas las pruebas.

## Rate limit

El endpoint de cortes descuenta un token por request (cubeta de tokens por
consumidor, por defecto 60 por minuto). Al agotarse devuelve `429` con la
cabecera `Retry-After` en segundos.

El límite importa más que en una API común: **un solo request puede disparar
decenas de consultas SNMP contra una OLT de producción**, así que un loop sobre
números de cliente le hace DoS a la red, no a la API.

## Cómo leer los errores

| Código | Significado |
|---|---|
| `403` | API Key inválida o ausente |
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
            "caída y cambios de estado. Es la vista de diagnóstico fino."
        ),
    },
    {
        "name": "analiticas",
        "description": (
            "Estado agregado del **parque completo** de una empresa, con resumen "
            "por categoría y listado paginado de dispositivos. Pensado para "
            "tableros y reportes de disponibilidad."
        ),
    },
    {
        "name": "cortes",
        "description": (
            "Diagnóstico de **un cliente** por su número de contrato: si está "
            "navegando y si el corte afecta a la zona. Contrato de salida cerrado "
            "de tres booleanos, pensado para consumirlo desde un CRM o un bot de "
            "atención. Es el único grupo con rate limit propio."
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
)

app.include_router(precinto.router)
app.include_router(analytics.router)
app.include_router(cortes.router)


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
