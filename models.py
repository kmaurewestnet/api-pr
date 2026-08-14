"""Modelos de respuesta de los endpoints (documentan /docs)."""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# --- Respuestas de error ------------------------------------------------------
# FastAPI serializa toda HTTPException como {"detail": ...}. El modelo existe
# para que /docs muestre esa forma en vez de un objeto vacío.


class ErrorDetalle(BaseModel):
    """Cuerpo de cualquier respuesta de error de la API."""

    detail: str = Field(
        description="Motivo del rechazo, en texto legible. Es el mismo valor que "
                    "viaja en el `detail` de la HTTPException del servidor.",
        examples=["No autorizado. API Key inválida o ausente en la cabecera X-API-Key."],
    )


def _error(descripcion: str, ejemplo: str) -> dict:
    """Entrada del dict `responses` de un endpoint, con su ejemplo."""
    return {
        "model": ErrorDetalle,
        "description": descripcion,
        "content": {"application/json": {"example": {"detail": ejemplo}}},
    }


# Los tres errores que puede devolver cualquier endpoint autenticado, sin
# importar su lógica. Se reparten con `**ERRORES_AUTENTICACION` para no repetir
# el mismo bloque en cada decorador.
ERRORES_AUTENTICACION = {
    403: _error(
        "API Key ausente o inválida en la cabecera `X-API-Key`.",
        "No autorizado. API Key inválida o ausente en la cabecera X-API-Key.",
    ),
}

ERROR_RATE_LIMIT = {
    429: _error(
        "Rate limit del consumidor alcanzado. La respuesta incluye la cabecera "
        "`Retry-After` con los segundos a esperar.",
        "Límite de 60 consultas por minuto alcanzado",
    ),
}


# --- Analíticas por empresa ---------------------------------------------------


class RangoTiempo(BaseModel):
    """Ventana temporal efectivamente consultada."""

    desde_timestamp: Optional[int] = Field(
        default=None,
        description="Epoch del inicio de la ventana. `null` cuando no se acotó la "
                    "antigüedad (sin el parámetro `horas`).",
        examples=[None],
    )
    hasta_timestamp: int = Field(
        description="Epoch del momento en que se resolvió la consulta.",
        examples=[1785180027],
    )
    horas_consultadas: Optional[int] = Field(
        default=None,
        description="Valor del parámetro `horas` con el que se resolvió la "
                    "consulta. `null` si no se envió.",
        examples=[None],
    )


class Paginacion(BaseModel):
    """Estado del recorte aplicado al listado `dispositivos`.

    Es `null` cuando se pidió `full=true`, porque en ese modo se devuelve el
    listado entero por streaming.
    """

    page: int = Field(description="Página devuelta.", examples=[1])
    limit: int = Field(description="Tamaño de página solicitado.", examples=[20])
    total_items: int = Field(
        description="Dispositivos tras aplicar el filtro `?estado`.",
        examples=[251],
    )
    total_paginas: int = Field(
        description="Páginas necesarias para recorrer `total_items`. Con 0 items "
                    "es 1 (una página vacía), no 0.",
        examples=[13],
    )


class Metadata(BaseModel):
    """Contexto de la consulta: qué empresa, cuántos registros trajo cada base
    y con qué recorte se devuelve el listado."""

    empresa_id: int = Field(
        description="ID de empresa consultado, tal como llegó en la ruta.",
        examples=[34],
    )
    empresa: Optional[str] = Field(
        default=None,
        description="Razón social según napear. `null` si la empresa no tiene "
                    "nombre cargado.",
        examples=["CMC Network"],
    )
    total_seriales_napear: int = Field(
        description="Seriales devueltos por napear.", examples=[252]
    )
    total_onus: int = Field(
        description="ONUs encontradas en soldef. Suele ser menor que "
                    "`total_seriales_napear`: hay seriales sin boca asociada.",
        examples=[251],
    )
    rango_tiempo: RangoTiempo
    paginacion: Optional[Paginacion] = Field(
        default=None,
        description="Recorte aplicado al listado. `null` cuando `full=true`.",
    )


class OrigenEstado(BaseModel):
    """De dónde salió el estado del parque, en conteos.

    Sirve para juzgar la muestra: mayoría de `los` significa que el estado es un
    proxy de la fibra, no del servicio.
    """

    onlinestate: int = Field(
        description="Equipos cuyo estado se tomó del item `OnlineState` "
                    "(fuente autoritativa).",
        examples=[490],
    )
    los: int = Field(
        description="Equipos cuyo estado se derivó de la alarma óptica, porque "
                    "no tienen item `OnlineState`.",
        examples=[0],
    )
    sin_datos: int = Field(
        description="Equipos sin ninguna de las dos fuentes.", examples=[1]
    )


class Resumen(BaseModel):
    """Reparto excluyente: los cinco contadores suman `total`.

    Se calcula siempre sobre el parque completo, sin importar la página ni el
    filtro `?estado`.
    """

    total: int = Field(
        description="ONUs evaluadas. Es la suma de las cinco categorías.",
        examples=[491],
    )
    online: int = Field(description="La ONT reporta `Online`.", examples=[446])
    offline: int = Field(
        description="Caída sin dying-gasp reciente ni LOS vigente.", examples=[34]
    )
    los: int = Field(
        description="Caída con alarma óptica de menos de 7 días "
                    "(`LOS_VIGENTE_DIAS`). Corte de fibra: necesita cuadrilla.",
        examples=[6],
    )
    powerfail: int = Field(
        description="Caída con Dying-gasp a menos de 15 min del corte "
                    "(`VENTANA_POWERFAIL_SEG`). Corte de energía en el domicilio.",
        examples=[4],
    )
    sin_datos: int = Field(
        description="Sin item de estado ni de LOS con lecturas.", examples=[1]
    )
    porcentaje_online: Optional[float] = Field(
        default=None,
        description="`online` sobre `total`, redondeado a dos decimales. `null` "
                    "si no hay ningún equipo.",
        examples=[90.84],
    )
    origen_estado: OrigenEstado


class Dispositivo(BaseModel):
    """Una ONU del parque, ya cruzada entre napear, soldef y Zabbix."""

    serial: Optional[int] = Field(
        default=None,
        description="`external_connector_id` de napear = boca de soldef.",
        examples=[2464596],
    )
    nombre: Optional[str] = Field(
        default=None, description="`nap_tag` de napear.", examples=["JN-018"]
    )
    mac: Optional[str] = Field(
        default=None,
        description="Informativa: no participa del cruce con Zabbix.",
        examples=["48575443ED3C75AA"],
    )
    precinto: Optional[str] = Field(
        default=None,
        description="Clave de cruce contra `items.name` en Zabbix.",
        examples=["JES0037"],
    )
    status: Optional[str] = Field(
        default=None,
        description="Último valor del item `OnlineState`. `null` si la ONU no "
                    "tiene ese item.",
        examples=["Online"],
    )
    status_timestamp: Optional[int] = Field(
        default=None,
        description="Epoch de la lectura de `status`. Sirve para juzgar qué tan "
                    "fresco es el dato.",
        examples=[1784886153],
    )
    los: Optional[str] = Field(
        default=None,
        description="Último valor de la alarma óptica `hwGponDeviceOntAlarmLOSi`.",
        examples=["No Alarm"],
    )
    los_timestamp: Optional[int] = Field(
        default=None,
        description="Epoch de la lectura de `los`.",
        examples=[1784885561],
    )
    ldc: Optional[str] = Field(
        default=None,
        description="Última causa de caída, ej. 'Dying-gasp'.",
        examples=["Dying-gasp"],
    )
    ldc_timestamp: Optional[int] = Field(
        default=None,
        description="Epoch del momento de la caída, parseado del propio valor. "
                    "null si la fecha embebida viene malformada.",
        examples=[1784326271],
    )
    categoria: str = Field(
        description="Reparto excluyente: online | offline | los | powerfail | sin_datos",
        examples=["online"],
    )
    estado: str = Field(
        description="online | offline | sin_datos. Se mantiene por compatibilidad "
                    "con la primera versión; `categoria` es más específico.",
        examples=["online"],
    )
    origen_estado: Optional[str] = Field(
        default=None,
        description="De dónde salió 'estado': 'onlinestate' (item autoritativo) o "
                    "'los' (derivado de la alarma óptica). null si es sin_datos.",
        examples=["onlinestate"],
    )
    con_los: bool = Field(
        description="Alarma óptica activa, sin importar su antigüedad. A "
                    "diferencia de la categoría `los`, no aplica el corte de "
                    "7 días.",
        examples=[False],
    )


class AnalyticsResponse(BaseModel):
    """Estado del parque completo de una empresa: contexto, resumen agregado y
    listado de dispositivos."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "success",
                "metadata": {
                    "empresa_id": 34,
                    "empresa": "CMC Network",
                    "total_seriales_napear": 252,
                    "total_onus": 251,
                    "rango_tiempo": {
                        "desde_timestamp": None,
                        "hasta_timestamp": 1785180027,
                        "horas_consultadas": None,
                    },
                    "paginacion": {
                        "page": 1,
                        "limit": 20,
                        "total_items": 251,
                        "total_paginas": 13,
                    },
                },
                "resumen": {
                    "total": 491,
                    "online": 446,
                    "offline": 34,
                    "los": 6,
                    "powerfail": 4,
                    "sin_datos": 1,
                    "porcentaje_online": 90.84,
                    "origen_estado": {"onlinestate": 490, "los": 0, "sin_datos": 1},
                },
                "dispositivos": [
                    {
                        "serial": 2464596,
                        "nombre": "JN-018",
                        "mac": "48575443ED3C75AA",
                        "precinto": "JES0037",
                        "status": "Online",
                        "status_timestamp": 1784886153,
                        "los": "No Alarm",
                        "los_timestamp": 1784885561,
                        "ldc": "Dying-gasp",
                        "ldc_timestamp": 1784326271,
                        "categoria": "online",
                        "estado": "online",
                        "origen_estado": "onlinestate",
                        "con_los": False,
                    }
                ],
            }
        }
    )

    status: str = Field(
        description="Siempre 'success' en un 200. Los errores no usan este modelo.",
        examples=["success"],
    )
    metadata: Metadata
    resumen: Resumen
    dispositivos: List[Dispositivo] = Field(
        description="Página del listado, o el parque entero si se pidió `full=true`."
    )


# --- Detección de cortes ------------------------------------------------------


class CorteResponse(BaseModel):
    """Salida del endpoint de cortes. Contrato cerrado: exactamente estos tres
    campos, sin envoltorio ni metadata. El detalle de cada verificación queda en
    el log del servidor, no en la respuesta."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"isFtth": True, "isOnline": False, "isZoneIncident": True}
        }
    )

    isFtth: bool = Field(
        description="El plan activo es de fibra: category_id 16. 17 es wireless",
        examples=[True],
    )
    isOnline: bool = Field(
        description="False solo si el ping al cliente falla y la ONT reporta "
                    "LOS/Offline. En wireless, es el ping al cliente.",
        examples=[False],
    )
    isZoneIncident: bool = Field(
        description="Fibra: NAP en corte, o la OLT no responde. "
                    "Wireless: el AP o el RouterBoard del nodo no responden.",
        examples=[True],
    )
