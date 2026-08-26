"""Fuente de señales de corte: todo el I/O del endpoint detrás de una interfaz.

`FuenteReal` es el adapter de producción —consultas, pings, snmpget y el caché
del estado de zona— y en las pruebas ocupa su lugar una fuente en memoria. El
seam va acá abajo, debajo de las primitivas, y no por encima de la orquestación:
lo que puede romperse en silencio es el armado de las tandas y el mapeo de
señales de `services/cortes.py`, así que eso tiene que quedar del lado testeable.

El caché vive adentro del adapter, no en el dict de tareas del llamador: la regla
"solo se cachea lo que comparten todos los clientes de una caja" queda expresada
en la interfaz —`ping_cliente` no cachea, `ping_zona` sí— en vez de depender de
que cada call site recuerde envolver la llamada.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import config
import db
from queries import cortes as q
from services import red
from services.cache import CacheTTL
from services.cortes_reglas import (
    esta_offline,
    hay_los,
    ip_routerboard,
    nap_caida,
    ont_caida,
)

log = logging.getLogger(__name__)

# Conexiones de zabbix que un request toma A LA VEZ: el estado de la NAP y el de
# la ONT del cliente corren en paralelo en la primera tanda. La topología va
# antes y sola. Lo declara este módulo y no config porque el número lo fija el
# armado de las tandas de services/cortes.py, y un consumidor nuevo se agrega
# acá: main.py lo suma al arrancar para verificar que el pool alcance.
CONEXIONES_ZABBIX_POR_REQUEST = 2

# Estado que es idéntico para todos los clientes de una misma caja. Ver
# services/cache.py: lo que se cachea es el resultado final (el booleano de NAP,
# el del ping), no las consultas intermedias, así que un hit ahorra el walk SNMP
# entero. Nada por cliente entra acá.
_zona = CacheTTL(
    config.CACHE_ZONA_TTL_SEG,
    config.CACHE_MAX_ENTRADAS,
    nombre="zona",
    stale_max_seg=config.CACHE_STALE_MAX_SEG,
)


# --- Ejecución concurrente ----------------------------------------------------

def _en_paralelo(tareas: dict) -> dict:
    """Corre {nombre: callable} en hilos. Una tarea que falla queda en None y no
    arrastra a las demás.

    El pool se crea por llamada a propósito, y no conviene compartir uno global:
    estas llamadas se anidan. `estado_nap` corre como tarea acá adentro y por
    dentro vuelve a entrar por `_nap_por_snmp` -> `_valores_snmp` -> acá. Con un
    pool compartido y acotado, las tareas de afuera ocupan todos los slots
    esperando a las de adentro, que nunca consiguen uno: deadlock.
    """
    if not tareas:
        return {}
    resultados = {}
    sin_capacidad = None
    workers = min(len(tareas), config.CORTES_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futuros = {pool.submit(fn): nombre for nombre, fn in tareas.items()}
        for futuro, nombre in futuros.items():
            try:
                resultados[nombre] = futuro.result()
            except db.PoolAgotado as e:
                # No es "la red no contestó", es "no llegué a preguntar". Dejarlo
                # como no evaluable haría que la API devolviera 200 con una
                # respuesta calculada sobre datos que nunca se consultaron: bajo
                # carga mentiría en silencio en vez de fallar. Corta el request.
                sin_capacidad = sin_capacidad or e
                resultados[nombre] = None
            except Exception as e:
                log.warning("La verificación '%s' falló: %s", nombre, e)
                resultados[nombre] = None
    if sin_capacidad:
        raise sin_capacidad
    return resultados


# --- Acceso a datos -----------------------------------------------------------

def _pg_multi(conexion, consultas):
    """Varias consultas sobre UNA sola conexión. Devuelve una lista de filas por
    consulta, en orden.

    Cada tarea paralela que abría su propia conexión multiplicaba el consumo del
    pool por request: el camino Solar tomaba 4 conexiones de zabbix a la vez, así
    que con POOL_MAX=10 la tercera request simultánea ya se quedaba sin. Estas
    consultas entran por índice sobre `items`; secuenciarlas cuesta milisegundos
    frente a los segundos que tarda un ping.
    """
    resultados = []
    with conexion() as conn:
        with db.cursor_pg(conn, config.CORTES_STATEMENT_TIMEOUT_MS) as cur:
            for sql, params in consultas:
                cur.execute(sql, params)
                resultados.append(cur.fetchall())
    return resultados


def _pg(conexion, sql, params):
    return _pg_multi(conexion, [(sql, params)])[0]


def _regex_ip(ip: str) -> str:
    """La IP se usa como patrón `~*` contra items.name. Los puntos se escapan
    para que 10.1.1.1 no matchee 10X1Y1Z1."""
    return ip.replace(".", r"\.")


# --- SNMP ---------------------------------------------------------------------

def _valores_snmp(olt_ip: str, oids: list) -> dict:
    """{oid: valor crudo} leyendo todos los OIDs en la menor cantidad de
    invocaciones posible: un snmpget por lote de SNMP_OIDS_POR_CONSULTA."""
    oids = (oids or [])[: config.CORTES_MAX_OIDS_NAP]
    if not olt_ip or not oids:
        return {}
    tamano = max(1, config.SNMP_OIDS_POR_CONSULTA)
    lotes = [oids[i : i + tamano] for i in range(0, len(oids), tamano)]
    respuestas = _en_paralelo(
        {i: partial(red.snmpget, olt_ip, lote) for i, lote in enumerate(lotes)}
    )
    valores = {}
    for parcial in respuestas.values():
        valores.update(parcial or {})
    return valores


def _interpretar(oids, valores, interprete, metrica, olt_ip) -> list:
    """Traduce los valores crudos y loguea el par valor→interpretación.

    Ese log es la única forma de ver si la OLT contesta lo que el intérprete cree
    que contesta: es lo que destapó que el código 1 ('No Alarm') se estaba
    leyendo como alarma. Los códigos desconocidos quedan afuera de la lista, no
    se cuentan como positivos.
    """
    if not oids:
        return []
    lecturas = [(o, valores.get(o), interprete(valores.get(o))) for o in oids]
    leidos = [e for _, _, e in lecturas if e is not None]
    log.info(
        "snmp %s %s: %d/%d OIDs interpretados, %d positivos | %s",
        olt_ip, metrica, len(leidos), len(oids), sum(1 for e in leidos if e),
        "; ".join(f"{o}={v!r}->{e}" for o, v, e in lecturas[:6]),
    )
    return leidos


# --- La fuente ----------------------------------------------------------------

class FuenteReal:
    """Las nueve señales que puede pedir la evaluación de un corte.

    Las que llevan caché lo dicen en el nombre: `ping_zona` es infraestructura
    compartida por la caja, `ping_cliente` es la respuesta puntual de un cliente
    y tiene que ser fresca siempre.

    El caché entra por parámetro y no se lee del global: en producción es el
    singleton compartido por todos los requests, y una prueba construye el suyo
    sin ensuciarlo.
    """

    def __init__(self, cache=None):
        self._cache = cache if cache is not None else _zona

    # --- Pings ---

    def ping_cliente(self, ip) -> bool | None:
        return red.ping(ip, "cliente")

    def ping_zona(self, ip, rol) -> bool | None:
        return self._cache.obtener(("ping", ip), partial(red.ping, ip, rol))

    # --- Topología ---

    def topologia_ftth(self, nro_cliente: str) -> dict:
        filas = _pg(db.zabbix_conn, q.Q_ZBX_TOPOLOGIA_FTTH, (nro_cliente,))
        if not filas:
            return {"nap": None, "olt_nombre": None, "olt_ip": ""}
        fila = next((f for f in filas if f.get("olt_ip")), filas[0])
        return {
            "nap": (fila.get("nap") or "").strip() or None,
            "olt_nombre": fila.get("olt_nombre"),
            "olt_ip": red.ip_valida(fila.get("olt_ip")),
        }

    def topologia_wireless(self, ip: str) -> dict:
        """AP y RouterBoard del cliente. Sin IP no se consulta: `i.name ~* ''`
        matchearía todos los items del Zabbix."""
        if not ip:
            return {"ap_ip": "", "ap": None, "rb_ip": "", "rb": None}

        rb_ip = ip_routerboard(ip)
        filas_ap = _pg(db.zabbix_wireless_conn, q.Q_ZBX_WIFI_AP, (_regex_ip(ip),))
        filas_rb = (
            _pg(db.zabbix_wireless_conn, q.Q_ZBX_WIFI_RB, (rb_ip,)) if rb_ip else []
        )
        ap = next((f for f in filas_ap if red.ip_valida(f.get("ip"))), None)
        return {
            "ap": (ap or {}).get("host"),
            "ap_ip": red.ip_valida((ap or {}).get("ip")),
            "rb": (filas_rb[0].get("host") if filas_rb else None),
            # El RB se pingea por su IP calculada aunque Zabbix no lo tenga dado
            # de alta: la IP es determinística, el alta en Zabbix puede faltar.
            "rb_ip": rb_ip,
        }

    def switch_del_nodo(self, olt_ip: str) -> dict | None:
        """Soldef. Devuelve None si no hay IP de OLT o si la base no responde: el
        ping al switch queda en 'no evaluable' y la matriz no se altera."""
        return self._cache.obtener(
            ("switch", olt_ip), partial(self._switch_del_nodo, olt_ip)
        )

    def _switch_del_nodo(self, olt_ip: str) -> dict | None:
        if not olt_ip:
            return None
        filas = _pg(db.soldef_conn, q.Q_SOLDEF_SWITCH, (olt_ip,))
        fila = next((f for f in filas if red.ip_valida(f.get("ip"))), None)
        if not fila:
            return None
        return {"nombre": fila.get("nombre"), "ip": red.ip_valida(fila.get("ip"))}

    # --- Estado de la ONT del cliente (sin caché: es su respuesta puntual) ---

    def estado_ont(self, nro_cliente: str) -> bool | None:
        """Caso A1: estado de la ONT (LOS + Online State)."""
        filas = _pg(db.zabbix_conn, q.Q_ZBX_ESTADO_CLIENTE, (nro_cliente,))
        los = next((f["valor"] for f in filas if f["metrica"] == "los"), None)
        estado = next((f["valor"] for f in filas if f["metrica"] == "estado"), None)
        return ont_caida(los, estado)

    def oids_solar(self, nro_cliente: str) -> dict:
        """Caso A2: OIDs de LOS y de OnlineState de ESTE cliente."""
        r = _pg_multi(
            db.zabbix_conn,
            [
                (q.Q_ZBX_OID_LOS_CLIENTE, (nro_cliente,)),
                (q.Q_ZBX_OID_ONLINE_CLIENTE, (nro_cliente,)),
            ],
        )
        return {
            "oids_los": [f["oid"] for f in r[0] if f.get("oid")],
            "oids_online": [f["oid"] for f in r[1] if f.get("oid")],
        }

    def ont_por_snmp(self, olt_ip: str, oids_los: list, oids_online: list) -> bool | None:
        """Caso A2: estado de la ONT preguntándole a la OLT en vivo.

        Los OIDs de LOS y de OnlineState van en la MISMA invocación: son pocos,
        del mismo host, y ahora que el estado de la NAP está cacheado esta es la
        única consulta SNMP que queda sin cachear en cada request.

        Combina las dos señales igual que el caso A1: una en alarma alcanza para
        dar la ONT por caída.
        """
        oids_los = list(oids_los or [])
        oids_online = list(oids_online or [])
        valores = _valores_snmp(olt_ip, oids_los + oids_online)
        señales = [
            any(leidos)
            for leidos in (
                _interpretar(oids_los, valores, hay_los, "LOS", olt_ip),
                _interpretar(
                    oids_online, valores, esta_offline, "OnlineState", olt_ip
                ),
            )
            if leidos
        ]
        return any(señales) if señales else None

    # --- Estado de la caja (cacheado: misma respuesta para todos sus clientes) ---

    def estado_nap(self, olt_ip: str, nap: str, es_solar: bool) -> bool | None:
        """El caché envuelve el resultado final, así que un hit ahorra también el
        walk SNMP entero."""
        if not nap:
            return None
        return self._cache.obtener(
            ("nap", olt_ip, nap),
            (lambda: self._nap_por_snmp(olt_ip, nap)) if es_solar
            else (lambda: self._nap_por_consulta(nap)),
        )

    def _nap_por_consulta(self, nap: str) -> bool | None:
        """Caso A1: corte de caja sobre el último log de cada ONT de la NAP."""
        filas = _pg(db.zabbix_conn, q.Q_ZBX_ESTADO_NAP, (nap,))
        if not filas:
            log.info("La NAP %s no tiene ocupación declarada en nap_ocupacion", nap)
            return None
        return nap_caida(
            filas[0].get("clientes_caidos"), filas[0].get("total_clientes")
        )

    def _nap_por_snmp(self, olt_ip: str, nap: str) -> bool | None:
        """Caso A2: corte de caja preguntándole a la OLT en vivo.

        Sin ocupación declarada el resultado queda en "no evaluable": no se usa
        la cantidad de OIDs leídos como total, porque en una NAP de un solo
        cliente eso convertiría cualquier caída individual en un corte de zona.
        """
        r = _pg_multi(
            db.zabbix_conn,
            [(q.Q_ZBX_OIDS_LOS_NAP, (nap,)), (q.Q_ZBX_OCUPACION_NAP, (nap,))],
        )
        oids = [f["oid"] for f in r[0] if f.get("oid")]
        ocupacion = r[1][0].get("total_clientes") if r[1] else None

        leidos = _interpretar(
            oids, _valores_snmp(olt_ip, oids), hay_los, "LOS NAP", olt_ip
        )
        if not leidos:
            return None
        if not ocupacion:
            log.info("Sin ocupación declarada para la NAP %s: no se evalúa corte", nap)
            return None
        return nap_caida(sum(1 for e in leidos if e), ocupacion)
