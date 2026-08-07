import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

import config
import db
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


app = FastAPI(
    title="Zabbix Precintos API",
    description=(
        "API para consultar métricas de ONUs por precinto (RX y OLT RX con tiempo, "
        "Logs y Estado históricos), analíticas agregadas por empresa y detección "
        "de cortes por número de cliente."
    ),
    version="2.1.0",
    lifespan=lifespan,
)

app.include_router(precinto.router)
app.include_router(analytics.router)
app.include_router(cortes.router)


@app.get("/health", tags=["infra"], dependencies=[Depends(verificar_api_key)])
def health():
    """Estado de conexión de cada base y de las utilidades del sistema."""
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
