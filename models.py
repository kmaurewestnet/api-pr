"""Modelos de respuesta del endpoint de analíticas (documentan /docs)."""
from typing import List, Optional

from pydantic import BaseModel, Field


class RangoTiempo(BaseModel):
    desde_timestamp: int
    hasta_timestamp: int
    horas_consultadas: int


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


class Resumen(BaseModel):
    total: int
    online: int
    offline: int
    sin_datos: int = Field(description="Sin lecturas en Zabbix dentro de la ventana")
    con_alarma_los: int
    porcentaje_online: Optional[float] = None


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
    estado: str = Field(description="online | offline | sin_datos")
    con_los: bool


class AnalyticsResponse(BaseModel):
    status: str
    metadata: Metadata
    resumen: Resumen
    dispositivos: List[Dispositivo]
