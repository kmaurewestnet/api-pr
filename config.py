"""Configuración central: credenciales de las 3 bases, API key y logging."""
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Falta una variable de entorno obligatoria."""


def _require(nombre: str) -> str:
    valor = os.getenv(nombre)
    if not valor:
        raise ConfigError(f"Falta la variable de entorno {nombre}")
    return valor


def _int(nombre: str, default: int) -> int:
    try:
        return int(os.getenv(nombre, default))
    except ValueError:
        raise ConfigError(f"La variable {nombre} debe ser un número entero")


# --- API ---

API_KEY_NAME = "X-API-Key"
API_KEY_SECRETA = os.getenv("API_KEY_SECRETA", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# --- Bases de datos ---
# Zabbix conserva los nombres de variable originales para no romper el .env en
# producción. Soldef y Napear usan la convención DB_<BASE>_* de obras/.env.example.

ZBX_DSN = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": _int("DB_PORT", 5432),
    "dbname": os.getenv("DB_NAME", "zabbix"),
    "user": os.getenv("DB_USER", "zabbix"),
    "password": os.getenv("DB_PASS", ""),
    "connect_timeout": 10,
}


def soldef_dsn() -> dict:
    """DSN de Soldef. Se resuelve al crear el pool, no al importar el módulo,
    para que la API arranque aunque estas credenciales todavía no estén cargadas."""
    return {
        "host": _require("DB_SOLDEF_HOST"),
        "port": _int("DB_SOLDEF_PORT", 5432),
        "dbname": _require("DB_SOLDEF_NAME"),
        "user": _require("DB_SOLDEF_USER"),
        "password": _require("DB_SOLDEF_PASS"),
        "connect_timeout": 10,
    }


def nap_dsn() -> dict:
    """DSN de Napear (MySQL). Igual que soldef_dsn: resolución diferida."""
    return {
        "host": _require("DB_NAP_HOST"),
        "port": _int("DB_NAP_PORT", 3306),
        "database": _require("DB_NAP_NAME"),
        "user": _require("DB_NAP_USER"),
        "password": _require("DB_NAP_PASS"),
        "connection_timeout": 10,
    }


# --- Límites de las consultas pesadas ---

# --- Clasificación de las caídas ---

# Un Dying-gasp dentro de esta ventana respecto del momento de la caída se
# interpreta como corte de energía. Tiene que absorber el desfase entre cuándo la
# ONT reportó el evento y cuándo Zabbix lo registró (intervalo de sondeo).
VENTANA_POWERFAIL_SEG = _int("VENTANA_POWERFAIL_SEG", 900)
# Una alarma LOS más vieja que esto se considera vencida: el equipo cuenta como
# offline común en vez de como corte de fibra vigente.
LOS_VIGENTE_SEG = _int("LOS_VIGENTE_DIAS", 7) * 86400

# Tamaño de lote al cruzar listas grandes de precintos contra Zabbix.
# Alto a propósito: el escaneo de `items` se repite entero en cada lote, así que
# conviene que una empresa grande entre en uno solo.
CHUNK_SIZE = _int("QUERY_CHUNK_SIZE", 50000)
# Corta una consulta colgada en Zabbix en vez de dejar la conexión tomada.
STATEMENT_TIMEOUT_MS = _int("STATEMENT_TIMEOUT_MS", 120000)
# Tamaño de los pools de PostgreSQL. El endpoint de analíticas usa 3 conexiones
# simultáneas de zabbix (RX, estado y LOS en paralelo).
POOL_MIN = _int("POOL_MIN", 1)
POOL_MAX = _int("POOL_MAX", 10)


def setup_logging() -> None:
    nivel = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, nivel, logging.INFO),
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
