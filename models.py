"""Modelos de respuesta del endpoint de analíticas (documentan /docs)."""
from typing import List, Optional

from pydantic import BaseModel, Field


class RangoTiempo(BaseModel):
    desde_timestamp: Optional[int] = Field(
        default=None, description="null cuando no se acotó la antigüedad"
    )
    hasta_timestamp: int
    horas_consultadas: Optional[int] = None


class Paginacion(BaseModel):
    page: int
    limit: int
    total_items: int = Field(description="Dispositivos tras aplicar el filtro ?estado")
    total_paginas: int


class Metadata(BaseModel):
    empresa_id: int
    empresa: Optional[str] = None
    total_seriales_napear: int = Field(description="Seriales devueltos por napear")
    total_onus: int = Field(description="ONUs encontradas en soldef")
    rango_tiempo: RangoTiempo
    paginacion: Optional[Paginacion] = None


class OrigenEstado(BaseModel):
    onlinestate: int = Field(description="Estado tomado del item OnlineState")
    los: int = Field(description="Estado derivado de la alarma LOS")
    sin_datos: int


class Resumen(BaseModel):
    total: int
    online: int
    offline: int
    sin_datos: int = Field(description="Sin item de estado ni de LOS con lecturas")
    con_alarma_los: int
    porcentaje_online: Optional[float] = None
    origen_estado: OrigenEstado


class Dispositivo(BaseModel):
    serial: Optional[int] = Field(
        default=None, description="external_connector_id de napear = boca de soldef"
    )
    nombre: Optional[str] = Field(default=None, description="nap_tag de napear")
    mac: Optional[str] = Field(
        default=None, description="Informativa: no participa del cruce con Zabbix"
    )
    precinto: Optional[str] = Field(
        default=None, description="Clave de cruce contra items.name en Zabbix"
    )
    status: Optional[str] = None
    status_timestamp: Optional[int] = None
    los: Optional[str] = None
    los_timestamp: Optional[int] = None
    ldc: Optional[str] = Field(
        default=None, description="Última causa de caída, ej. 'Dying-gasp'"
    )
    ldc_timestamp: Optional[int] = Field(
        default=None,
        description="Epoch del momento de la caída, parseado del propio valor. "
                    "null si la fecha embebida viene malformada.",
    )
    estado: str = Field(description="online | offline | sin_datos")
    origen_estado: Optional[str] = Field(
        default=None,
        description="De dónde salió 'estado': 'onlinestate' (item autoritativo) o "
                    "'los' (derivado de la alarma óptica). null si es sin_datos.",
    )
    con_los: bool


class AnalyticsResponse(BaseModel):
    status: str
    metadata: Metadata
    resumen: Resumen
    dispositivos: List[Dispositivo]
