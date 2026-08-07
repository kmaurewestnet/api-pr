"""Pools de conexión a las bases de datos.

Los pools se crean de forma diferida (al primer uso) y no al importar el módulo:
así la API arranca y el endpoint de precinto sigue funcionando aunque las
credenciales de Soldef, Napear, Gestión o Zabbix Wireless todavía no estén
configuradas.

    zabbix           PostgreSQL  Zabbix de fibra (items, hosts, history, nap_ocupacion)
    zabbix_wireless  PostgreSQL  Zabbix de wireless (instancia separada)
    soldef           PostgreSQL  inventario: bocas, precintos y aparatos de nodo
    napear           MySQL       reservas/empresas
    gestion          MySQL       clientes, contratos y conexiones
"""
import logging
import threading
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_pools: dict = {}


class DatabaseUnavailable(RuntimeError):
    """No se pudo crear el pool o conectar a la base indicada."""


# --- Creación de pools ---

def _crear_pool_pg(nombre: str, dsn: dict) -> ThreadedConnectionPool:
    return ThreadedConnectionPool(
        config.POOL_MIN,
        config.POOL_MAX,
        cursor_factory=RealDictCursor,
        **dsn,
    )


def _crear_pool_mysql(nombre: str, dsn: dict):
    # Import diferido: sin estos endpoints, mysql-connector-python no hace falta.
    from mysql.connector import pooling

    # pool_name tiene que ser único por proceso: napear y gestion son dos bases
    # MySQL distintas y cada una necesita su propio pool.
    return pooling.MySQLConnectionPool(
        pool_name=nombre,
        pool_size=min(config.POOL_MAX, 32),
        **dsn,
    )


_FABRICAS = {
    "zabbix": lambda: _crear_pool_pg("zabbix", config.ZBX_DSN),
    "zabbix_wireless": lambda: _crear_pool_pg(
        "zabbix_wireless", config.zbx_wireless_dsn()
    ),
    "soldef": lambda: _crear_pool_pg("soldef", config.soldef_dsn()),
    "napear": lambda: _crear_pool_mysql("napear", config.nap_dsn()),
    "gestion": lambda: _crear_pool_mysql("gestion", config.gestion_dsn()),
}

# Las que hablan MySQL: el context manager y el ping son distintos de los de PG.
_MYSQL = ("napear", "gestion")


def _pool(nombre: str):
    pool = _pools.get(nombre)
    if pool is not None:
        return pool
    with _lock:
        # Otro hilo pudo haberlo creado mientras esperábamos el lock.
        if nombre not in _pools:
            try:
                _pools[nombre] = _FABRICAS[nombre]()
                log.info("Pool de %s inicializado", nombre)
            except Exception as e:
                log.error("No se pudo inicializar el pool de %s: %s", nombre, e)
                raise DatabaseUnavailable(
                    f"Base de datos '{nombre}' no disponible: {e}"
                ) from e
        return _pools[nombre]


# --- Context managers ---

@contextmanager
def _conexion_pg(nombre: str):
    pool = _pool(nombre)
    conn = pool.getconn()
    try:
        yield conn
    finally:
        try:
            # Solo hacemos SELECTs; el rollback cierra la transacción implícita
            # para que la conexión vuelva limpia al pool.
            conn.rollback()
        except Exception:
            log.warning("Fallo el rollback en %s, se descarta la conexión", nombre)
            pool.putconn(conn, close=True)
        else:
            pool.putconn(conn)


@contextmanager
def zabbix_conn():
    with _conexion_pg("zabbix") as conn:
        yield conn


@contextmanager
def soldef_conn():
    with _conexion_pg("soldef") as conn:
        yield conn


@contextmanager
def zabbix_wireless_conn():
    with _conexion_pg("zabbix_wireless") as conn:
        yield conn


@contextmanager
def _conexion_mysql(nombre: str):
    conn = _pool(nombre).get_connection()
    try:
        yield conn
    finally:
        # En mysql-connector, close() devuelve la conexión al pool.
        conn.close()


@contextmanager
def napear_conn():
    with _conexion_mysql("napear") as conn:
        yield conn


@contextmanager
def gestion_conn():
    with _conexion_mysql("gestion") as conn:
        yield conn


@contextmanager
def cursor_pg(conn, statement_timeout_ms: int = None):
    """Cursor de PostgreSQL con statement_timeout opcional, cerrado siempre."""
    cur = conn.cursor()
    try:
        if statement_timeout_ms:
            # SET es un comando de utilidad: no admite parámetros bind, hay que
            # interpolar. El int() garantiza que no entre nada más que un número.
            cur.execute(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}")
        yield cur
    finally:
        cur.close()


def ping(nombre: str) -> bool:
    """SELECT 1 contra la base indicada. Usado por /health."""
    if nombre in _MYSQL:
        with _conexion_mysql(nombre) as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT 1")
                cur.fetchall()
            finally:
                cur.close()
        return True
    with _conexion_pg(nombre) as conn:
        with cursor_pg(conn) as cur:
            cur.execute("SELECT 1")
            cur.fetchall()
    return True


def close_all() -> None:
    with _lock:
        for nombre, pool in _pools.items():
            try:
                if hasattr(pool, "closeall"):
                    pool.closeall()
                log.info("Pool de %s cerrado", nombre)
            except Exception as e:
                log.warning("Error cerrando el pool de %s: %s", nombre, e)
        _pools.clear()
