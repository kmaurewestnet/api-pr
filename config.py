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


def _codigos(nombre: str, default: str = "") -> frozenset:
    """Lista de códigos SNMP enteros separados por coma."""
    codigos = set()
    for parte in os.getenv(nombre, default).split(","):
        parte = parte.strip()
        if not parte:
            continue
        try:
            codigos.add(int(parte))
        except ValueError:
            raise ConfigError(
                f"{nombre} debe ser una lista de enteros separados por coma"
            )
    return frozenset(codigos)


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


def gestion_dsn() -> dict:
    """DSN de Gestión (MySQL). Es la base de clientes/contratos, distinta de
    napear. Resolución diferida, igual que soldef_dsn."""
    return {
        "host": _require("DB_GESTION_HOST"),
        "port": _int("DB_GESTION_PORT", 3306),
        "database": _require("DB_GESTION_NAME"),
        "user": _require("DB_GESTION_USER"),
        "password": _require("DB_GESTION_PASS"),
        "connection_timeout": 10,
    }


def zbx_wireless_dsn() -> dict:
    """DSN del Zabbix de Wireless (PostgreSQL). Es una instancia separada de la
    de fibra: mismo esquema, otro servidor."""
    return {
        "host": _require("DB_ZBX_WIFI_HOST"),
        "port": _int("DB_ZBX_WIFI_PORT", 5432),
        "dbname": _require("DB_ZBX_WIFI_NAME"),
        "user": _require("DB_ZBX_WIFI_USER"),
        "password": _require("DB_ZBX_WIFI_PASS"),
        "connect_timeout": 10,
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


# --- Detección de cortes (endpoint /api/v1/cortes) ---

# Categorías de plan de Gestión (`category.category_id`). Son las dos únicas
# tecnologías que contempla el endpoint y las dos únicas que trae la consulta:
# van juntas acá para que el `IN` del SQL y la decisión de isFtth no puedan
# quedar desalineados.
CATEGORIA_FTTH_ID = _int("CATEGORIA_FTTH_ID", 16)
CATEGORIA_WIRELESS_ID = _int("CATEGORIA_WIRELESS_ID", 17)

# Una NAP se considera caída cuando TODOS sus clientes reportan LOS. En NAPs de
# más de este tamaño se tolera un cliente sin reportar (puede estar de baja o con
# el item deshabilitado).
NAP_TOLERANCIA_DESDE = _int("NAP_TOLERANCIA_DESDE", 3)

# Timeout propio de las consultas del endpoint de cortes: tiene que responder en
# segundos, no puede heredar los 120 s de las consultas de analíticas.
CORTES_STATEMENT_TIMEOUT_MS = _int("CORTES_STATEMENT_TIMEOUT_MS", 15000)
# Verificaciones (pings, snmpget y queries) que corren en paralelo por request.
CORTES_MAX_WORKERS = _int("CORTES_MAX_WORKERS", 6)
# Techo de OIDs a consultar por SNMP al evaluar una NAP. Protege contra un
# `nap` mal extraído que devuelva cientos de items: cada OID es un subproceso.
CORTES_MAX_OIDS_NAP = _int("CORTES_MAX_OIDS_NAP", 64)

# --- Utilidades del sistema: ping ICMP y SNMP ---

PING_PATH = os.getenv("PING_PATH", "/bin/ping")
PING_COUNT = _int("PING_COUNT", 2)
PING_TIMEOUT_SEG = _int("PING_TIMEOUT_SEG", 2)

SNMPGET_PATH = os.getenv("SNMPGET_PATH", "/usr/bin/snmpget")
SNMP_COMMUNITY = os.getenv("SNMP_COMMUNITY", "")
SNMP_VERSION = os.getenv("SNMP_VERSION", "2c")
SNMP_TIMEOUT_SEG = _int("SNMP_TIMEOUT_SEG", 3)
SNMP_RETRIES = _int("SNMP_RETRIES", 1)

# Traducción de los enteros que devuelve un snmpget crudo. Zabbix guarda en su
# historial el texto ya mapeado ('LOS', 'No Alarm', 'Offline'), pero preguntarle
# directo a la OLT devuelve el código numérico del MIB.
#
# Verificado contra las OLT del parque:
#     hwGponDeviceOntAlarmLOSi            1 = No Alarm   2 = LOS / LOSi
#     hwGponDeviceOntEthernetOnlineState  1 = Online     2 = Offline
#
# Solo hay que tocarlas si aparece una OLT de otro vendor. Un código que no esté
# en ninguna de las dos listas de su métrica queda como "no evaluable" y se
# loguea: nunca se asume alarma sobre un valor que no se sabe leer. La primera
# versión traducía con `!= 0`, así que el 1 de "No Alarm" daba LOS y toda ONT
# sana —y por arrastre su NAP entera— se reportaba caída.
SNMP_COD_LOS = _codigos("SNMP_COD_LOS", "2")
SNMP_COD_SIN_LOS = _codigos("SNMP_COD_SIN_LOS", "1")
SNMP_COD_OFFLINE = _codigos("SNMP_COD_OFFLINE", "2")
SNMP_COD_ONLINE = _codigos("SNMP_COD_ONLINE", "1")


def setup_logging() -> None:
    nivel = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, nivel, logging.INFO),
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
