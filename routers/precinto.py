"""Endpoint de consulta por precinto. Contrato idéntico al original."""
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import db
from queries.precinto import QUERY_ESTADO, QUERY_LOGS, QUERY_OLT_RX, QUERY_ONU_RX
from security import verificar_api_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["precinto"], dependencies=[Depends(verificar_api_key)])


@router.get("/api/v1/precinto/{codigo_precinto}")
def obtener_datos_completos_precinto(
    codigo_precinto: str,
    horas: Optional[int] = Query(
        default=6,
        ge=1,
        le=168,
        description="Cantidad de horas hacia atrás a consultar (solo aplica a RX y OLT RX)",
    ),
):
    now = int(time.time())
    start_time = now - (horas * 3600)

    try:
        with db.zabbix_conn() as conn:
            with db.cursor_pg(conn) as cursor:
                # 1. Ejecutar ONU RX (Usa tiempo y precinto)
                cursor.execute(QUERY_ONU_RX, (start_time, now, codigo_precinto))
                onu_rx_data = cursor.fetchall()

                # 2. Ejecutar ONU OLT RX (Usa tiempo y precinto)
                cursor.execute(QUERY_OLT_RX, (start_time, now, codigo_precinto))
                onu_olt_rx_data = cursor.fetchall()

                # 3. Ejecutar LOGS (Solo usa precinto)
                cursor.execute(QUERY_LOGS, (codigo_precinto,))
                logs_data = cursor.fetchall()

                # 4. Ejecutar ESTADO (Solo usa precinto)
                cursor.execute(QUERY_ESTADO, (codigo_precinto,))
                estado_data = cursor.fetchall()
    except db.DatabaseUnavailable as e:
        log.error("Zabbix no disponible: %s", e)
        raise HTTPException(
            status_code=503, detail="Error interno de conexión a la base de datos"
        )
    except Exception as e:
        log.exception("Error al ejecutar las consultas del precinto %s: %s",
                      codigo_precinto, e)
        raise HTTPException(status_code=500, detail="Error al procesar los datos en Zabbix")

    # Extraer el nombre del cliente si las consultas de RX trajeron algo
    nombre_cliente = "No identificado"
    if onu_rx_data:
        nombre_cliente = onu_rx_data[0]["cliente"]
    elif onu_olt_rx_data:
        nombre_cliente = onu_olt_rx_data[0]["cliente"]

    return {
        "status": "success",
        "metadata": {
            "precinto": codigo_precinto,
            "cliente": nombre_cliente,
            "rango_tiempo_rx": {
                "desde_timestamp": start_time,
                "hasta_timestamp": now,
                "horas_consultadas": horas,
            },
        },
        "metricas": {
            "onu_rx": onu_rx_data,
            "onu_olt_rx": onu_olt_rx_data,
            "logs": logs_data,
            "estados": estado_data,
        },
    }
