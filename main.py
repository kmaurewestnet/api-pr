import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

import config
import db
from routers import analytics, precinto
from security import verificar_api_key

config.setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Los pools se crean de forma diferida al primer uso (ver db.py), así que
    # acá solo hace falta liberarlos al apagar.
    yield
    db.close_all()


app = FastAPI(
    title="Zabbix Precintos API",
    description=(
        "API para consultar métricas de ONUs por precinto (RX y OLT RX con tiempo, "
        "Logs y Estado históricos) y analíticas agregadas por empresa."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(precinto.router)
app.include_router(analytics.router)


@app.get("/health", tags=["infra"], dependencies=[Depends(verificar_api_key)])
def health():
    """Estado de conexión de cada base por separado."""
    resultado = {}
    for nombre in ("zabbix", "soldef", "napear"):
        try:
            db.ping(nombre)
            resultado[nombre] = {"ok": True}
        except Exception as e:
            resultado[nombre] = {"ok": False, "error": str(e)}
    todo_ok = all(v["ok"] for v in resultado.values())
    return {
        "status": "success" if todo_ok else "degraded",
        "environment": config.ENVIRONMENT,
        "bases": resultado,
    }
