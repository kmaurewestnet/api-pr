"""Modelos de respuesta de los endpoints. Definen el esquema publicado en
/docs, /redoc y /openapi.json."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# --- Respuestas de error ------------------------------------------------------
# FastAPI serializa toda HTTPException como {"detail": ...}. El modelo existe
# para que /docs muestre esa forma en vez de un objeto vacío.


class ErrorDetalle(BaseModel):
    """Cuerpo de cualquier respuesta de error de la API."""

    detail: str = Field(
        description="Motivo del rechazo, en texto legible. Corresponde al valor "
                    "del campo `detail` de la HTTPException emitida por el servidor.",
        examples=["No autorizado. API Key inválida o ausente en la cabecera X-API-Key."],
    )


def error(descripcion: str, ejemplo: str) -> dict:
    """Entrada del dict `responses` de un endpoint, con su ejemplo.

    Toda respuesta de error de la API tiene la misma forma —`{"detail": ...}`—
    así que la forma se escribe una vez acá y no en cada decorador.
    """
    return {
        "model": ErrorDetalle,
        "description": descripcion,
        "content": {"application/json": {"example": {"detail": ejemplo}}},
    }


# Los tres errores que puede devolver cualquier endpoint autenticado, sin
# importar su lógica. Se reparten con `**ERRORES_AUTENTICACION` para no repetir
# el mismo bloque en cada decorador.
ERRORES_AUTENTICACION = {
    403: error(
        "API Key ausente o inválida en la cabecera `X-API-Key`.",
        "No autorizado. API Key inválida o ausente en la cabecera X-API-Key.",
    ),
}

# Precinto y analiticas suman un segundo motivo de 403: la clave es valida pero
# es la de un consumidor externo, limitado a /cortes.
ERRORES_AUTENTICACION_INTERNA = {
    403: error(
        "API Key ausente o inválida, o válida pero correspondiente a un "
        "consumidor externo: `/precinto` y `/analytics` son endpoints internos.",
        "Esta clave solo tiene acceso al endpoint de cortes.",
    ),
}

ERROR_RATE_LIMIT = {
    429: error(
        "Rate limit del consumidor alcanzado. La respuesta incluye la cabecera "
        "`Retry-After` con los segundos a esperar. Los `X-RateLimit-*` van en "
        "toda respuesta, no solo en esta.",
        "Límite de 60 consultas por minuto alcanzado",
    ),
}


# --- Analíticas por empresa ---------------------------------------------------


class RangoTiempo(BaseModel):
    """Ventana temporal efectivamente consultada."""

    desde_timestamp: Optional[int] = Field(
        default=None,
        description="Epoch de inicio de la ventana. `null` cuando no se acotó la "
                    "antigüedad, es decir, sin el parámetro `horas`.",
        examples=[None],
    )
    hasta_timestamp: int = Field(
        description="Epoch del momento en que se resolvió la consulta.",
        examples=[1785180027],
    )
    horas_consultadas: Optional[int] = Field(
        default=None,
        description="Valor del parámetro `horas` aplicado en la consulta. `null` "
                    "si no se envió.",
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
        description="Dispositivos resultantes tras aplicar el filtro `?estado`.",
        examples=[251],
    )
    total_paginas: int = Field(
        description="Páginas necesarias para recorrer `total_items`. Con 0 items "
                    "su valor es 1, correspondiente a una página vacía, no 0.",
        examples=[13],
    )


class Metadata(BaseModel):
    """Contexto de la consulta: empresa consultada, registros aportados por cada
    base y recorte aplicado al listado."""

    empresa_id: int = Field(
        description="ID de empresa consultado, tal como llegó en la ruta.",
        examples=[34],
    )
    empresa: Optional[str] = Field(
        default=None,
        description="Razón social registrada en el sistema de reservas. `null` si "
                    "la empresa no tiene nombre cargado.",
        examples=["Empresa Ejemplo S.A."],
    )
    total_seriales_napear: int = Field(
        description="Seriales aportados por el sistema de reservas.", examples=[252]
    )
    total_onus: int = Field(
        description="ONUs localizadas en el inventario de red. Habitualmente es "
                    "menor que `total_seriales_napear`, dado que existen seriales "
                    "sin boca asociada.",
        examples=[251],
    )
    rango_tiempo: RangoTiempo
    paginacion: Optional[Paginacion] = Field(
        default=None,
        description="Recorte aplicado al listado. `null` cuando `full=true`.",
    )


class OrigenEstado(BaseModel):
    """Origen del estado del parque, expresado en conteos.

    Permite evaluar la composición de la muestra: un predominio de `los` indica
    que el estado es un proxy del enlace de fibra, no del servicio.
    """

    onlinestate: int = Field(
        description="Equipos cuyo estado se obtuvo del item `OnlineState`, la "
                    "fuente autoritativa.",
        examples=[490],
    )
    los: int = Field(
        description="Equipos cuyo estado se derivó de la alarma óptica, por no "
                    "disponer del item `OnlineState`.",
        examples=[0],
    )
    sin_datos: int = Field(
        description="Equipos que no disponen de ninguna de las dos fuentes.",
        examples=[1],
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
        description="Caída sin dying-gasp reciente ni alarma LOS vigente.",
        examples=[34],
    )
    los: int = Field(
        description="Caída con alarma óptica de menos de 7 días "
                    "(`LOS_VIGENTE_DIAS`). Corte de fibra: requiere intervención "
                    "de cuadrilla.",
        examples=[6],
    )
    powerfail: int = Field(
        description="Caída con Dying-gasp a menos de 15 min del corte "
                    "(`VENTANA_POWERFAIL_SEG`). Corresponde a un corte de energía "
                    "en el domicilio.",
        examples=[4],
    )
    sin_datos: int = Field(
        description="Sin lecturas en el item de estado ni en el de LOS.",
        examples=[1],
    )
    porcentaje_online: Optional[float] = Field(
        default=None,
        description="`online` sobre `total`, redondeado a dos decimales. `null` "
                    "si no hay ningún equipo.",
        examples=[90.84],
    )
    origen_estado: OrigenEstado


class Dispositivo(BaseModel):
    """Una ONU del parque, con los datos ya cruzados entre las tres bases de
    origen."""

    serial: Optional[int] = Field(
        default=None,
        description="Identificador de conector del sistema de reservas, "
                    "equivalente a la boca del inventario de red.",
        examples=[2464596],
    )
    nombre: Optional[str] = Field(
        default=None,
        description="Etiqueta de NAP registrada en el sistema de reservas.",
        examples=["JN-018"],
    )
    mac: Optional[str] = Field(
        default=None,
        description="Informativa: no participa del cruce con el sistema de "
                    "monitoreo.",
        examples=["48575443ED3C75AA"],
    )
    precinto: Optional[str] = Field(
        default=None,
        description="Clave de cruce contra el nombre del item de monitoreo.",
        examples=["JES0037"],
    )
    status: Optional[str] = Field(
        default=None,
        description="Último valor del item `OnlineState`. `null` si la ONU no "
                    "dispone de ese item.",
        examples=["Online"],
    )
    status_timestamp: Optional[int] = Field(
        default=None,
        description="Epoch de la lectura de `status`. Permite evaluar la vigencia "
                    "del dato.",
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
        description="Última causa de caída informada por el equipo, por ejemplo "
                    "'Dying-gasp'.",
        examples=["Dying-gasp"],
    )
    ldc_timestamp: Optional[int] = Field(
        default=None,
        description="Epoch del momento de la caída, derivado del propio valor. "
                    "`null` si la fecha embebida está malformada.",
        examples=[1784326271],
    )
    categoria: str = Field(
        description="Reparto excluyente: online | offline | los | powerfail | sin_datos",
        examples=["online"],
    )
    estado: str = Field(
        description="online | offline | sin_datos. Se mantiene por compatibilidad "
                    "con la primera versión de la API; `categoria` ofrece mayor "
                    "granularidad.",
        examples=["online"],
    )
    origen_estado: Optional[str] = Field(
        default=None,
        description="Origen del campo 'estado': 'onlinestate' (item autoritativo) "
                    "o 'los' (derivado de la alarma óptica). `null` cuando la "
                    "categoría es sin_datos.",
        examples=["onlinestate"],
    )
    con_los: bool = Field(
        description="Alarma óptica activa, con independencia de su antigüedad. A "
                    "diferencia de la categoría `los`, no aplica el umbral de "
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
                    "empresa": "Empresa Ejemplo S.A.",
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
        description="Siempre 'success' en una respuesta 200. Las respuestas de "
                    "error no utilizan este modelo.",
        examples=["success"],
    )
    metadata: Metadata
    resumen: Resumen
    dispositivos: List[Dispositivo] = Field(
        description="Página del listado, o el parque completo si se solicitó "
                    "`full=true`."
    )


# --- Detección de cortes ------------------------------------------------------


class CorteResponse(BaseModel):
    """Salida del endpoint de cortes. Contrato cerrado: exactamente estos tres
    campos, sin envoltorio ni metadata. El detalle de cada verificación se
    registra en el log del servidor, no en la respuesta."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"isFtth": True, "isOnline": False, "isZoneIncident": True}
        }
    )

    isFtth: bool = Field(
        description="El plan activo es de fibra (`category_id` 16). El valor 17 "
                    "corresponde a wireless.",
        examples=[True],
    )
    isOnline: bool = Field(
        description="`false` únicamente si el ping al cliente falla y la ONT "
                    "reporta LOS/Offline. En wireless se determina por el ping al "
                    "cliente.",
        examples=[False],
    )
    isZoneIncident: bool = Field(
        description="Fibra: la NAP está en corte, o la OLT no responde. Wireless: "
                    "el AP o el RouterBoard del nodo no responden.",
        examples=[True],
    )


# --- Precinto -----------------------------------------------------------------
# Se tipa el envoltorio y la metadata, que es lo que puede divergir en silencio
# del ejemplo publicado. Las cuatro series quedan como listas de objetos sin
# tipar a propósito: son lecturas crudas de Zabbix, y tipar cada fila
# convertiría un valor inesperado en un 500 sobre un endpoint de diagnóstico.


class RangoTiempoRx(BaseModel):
    """Ventana efectivamente aplicada. Solo afecta a las series de RX."""

    desde_timestamp: int = Field(
        description="Inicio de la ventana, epoch UNIX en segundos.",
        examples=[1785158427],
    )
    hasta_timestamp: int = Field(
        description="Fin de la ventana, epoch UNIX en segundos. Es el momento en "
                    "que se resolvió la consulta.",
        examples=[1785180027],
    )
    horas_consultadas: int = Field(
        description="Ventana pedida, en horas.", examples=[6]
    )


class MetadataPrecinto(BaseModel):
    """Identidad de la consulta y del equipo consultado."""

    precinto: str = Field(
        description="Precinto tal como se recibió en la ruta.", examples=["JES0037"]
    )
    cliente: str = Field(
        description="Nombre derivado del nombre del item en el sistema de "
                    "monitoreo. Es `No identificado` cuando ninguna de las dos "
                    "series de RX trajo lecturas.",
        examples=["JES0037"],
    )
    rango_tiempo_rx: RangoTiempoRx


class MetricasPrecinto(BaseModel):
    """Las cuatro series. Vacías si el precinto no registró lecturas."""

    onu_rx: List[Dict[str, Any]] = Field(
        description="Potencia óptica recibida por la ONU, truncada al minuto."
    )
    onu_olt_rx: List[Dict[str, Any]] = Field(
        description="Potencia óptica con que la OLT recibe a esa ONU."
    )
    logs: List[Dict[str, Any]] = Field(
        description="Registros de última causa de caída, con su fecha embebida."
    )
    estados: List[Dict[str, Any]] = Field(
        description="Estado operativo reportado por la ONU."
    )


class PrecintoResponse(BaseModel):
    """Respuesta de `GET /api/v1/precinto/{codigo_precinto}`."""

    status: str = Field(
        description="Resultado de la consulta.", examples=["success"]
    )
    metadata: MetadataPrecinto
    metricas: MetricasPrecinto
