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
import psycopg2.pool
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_pools: dict = {}


# El cuerpo del 503, igual para las cinco bases y para el pool agotado. Que
# falle napear, gestion o el pool no le incumbe a quien consume la API: es
# topología interna, y a `/cortes` llegan claves externas. Cuál falló se
# responde en `/health` y se registra en el log, que es donde sirve.
SERVICIO_NO_DISPONIBLE = (
    "Servicio temporalmente no disponible. Reintentá en unos minutos."
)


class DatabaseUnavailable(RuntimeError):
    """No se pudo crear el pool o conectar a la base indicada.

    `str(e)` es lo que sale por el cuerpo del 503, así que es un texto fijo que
    no nombra nada interno. El nombre de la base viaja aparte, en `.nombre`,
    para las líneas de log: quien opera necesita saber cuál se cayó, quien
    consume no.
    """

    def __init__(self, nombre: str, mensaje: str = None):
        self.nombre = nombre
        super().__init__(mensaje or SERVICIO_NO_DISPONIBLE)

    def __repr__(self) -> str:
        """Para el log: acá sí va el nombre de la base.

        `str()` es el cuerpo del 503 y se queda genérico; `repr()` es lo que
        loguean quienes solo tienen la excepción y no el contexto, como el
        caché al servir un valor vencido.
        """
        return f"{type(self).__name__}('{self.nombre}')"


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
                raise DatabaseUnavailable(nombre) from e
        return _pools[nombre]


# --- Context managers ---

class PoolAgotado(DatabaseUnavailable):
    """Todas las conexiones del pool están en uso.

    Hereda de DatabaseUnavailable a propósito: es un 503, no un "no se pudo
    verificar". La diferencia importa —el problema es de capacidad propia, no de
    la red que se está midiendo— pero la respuesta al cliente es la misma:
    devolver un resultado calculado sobre datos que nunca se consultaron sería
    inventarlo.
    """


@contextmanager
def _conexion_pg(nombre: str):
    pool = _pool(nombre)
    try:
        conn = pool.getconn()
    except psycopg2.pool.PoolError as e:
        log.error("Pool de %s agotado (POOL_MAX=%s): %s", nombre, config.POOL_MAX, e)
        raise PoolAgotado(nombre) from e
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
    # Import diferido, igual que el de la fábrica: para cuando se llega acá el
    # pool ya existe, así que mysql-connector está importado.
    from mysql.connector.errors import PoolError

    pool = _pool(nombre)
    try:
        conn = pool.get_connection()
    except PoolError as e:
        log.error("Pool de %s agotado (POOL_MAX=%s): %s", nombre, config.POOL_MAX, e)
        raise PoolAgotado(nombre) from e
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
