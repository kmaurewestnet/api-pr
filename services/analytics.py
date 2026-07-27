"""Orquestación del cruce napear -> soldef -> zabbix.

Las tres bases son motores distintos y no admiten JOIN entre sí, así que el
cruce final se hace en memoria indexando por precinto normalizado (O(n)).

Cadena de identificadores:
    napear.registro_reservas.external_connector_id  (= "serial")
      -> soldef.dispositivos_bocas.id
      -> soldef.dispositivos_precintos.etiqueta     (= "precinto")
      -> zabbix: items.name, que lo trae entre 'descr_' y '_odb'

La MAC sigue viniendo de soldef y se devuelve en la respuesta, pero ya no
participa del cruce: resolverla contra history_text era el cuello de botella.
"""
import logging
import re
import time

import config
import db
from queries import analytics as q

log = logging.getLogger(__name__)

_ESPACIOS = re.compile(r"\s+")


class QueriesNoConfiguradas(RuntimeError):
    def __init__(self, faltantes):
        self.faltantes = faltantes
        super().__init__(
            "Faltan definir las consultas SQL en queries/analytics.py: "
            + ", ".join(faltantes)
        )


def normalizar_precinto(precinto) -> str:
    """Clave de cruce entre soldef y zabbix.

    Replica del lado de Python el mismo `upper(trim(...))` que aplica la query a
    ambos lados del join, y colapsa espacios repetidos: el precinto sale de
    items.name reemplazando '_' por ' ', así que un doble guión bajo produciría
    un doble espacio.
    """
    if not precinto:
        return ""
    return _ESPACIOS.sub(" ", str(precinto).strip().upper())


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# --- Paso 1: napear -----------------------------------------------------------

def obtener_ont_ids(empresa_id):
    """Seriales de la empresa. Devuelve (seriales, nombre_empresa, nap_tag_por_serial)."""
    with db.napear_conn() as conn:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(q.Q_NAPEAR_ONTS_POR_EMPRESA, (empresa_id,))
            filas = cur.fetchall()
        finally:
            cur.close()

    seriales = []
    nap_tags = {}
    for f in filas:
        serial = f.get("serial")
        if serial is None:
            continue
        if serial not in nap_tags:
            # El join con installation_sheets puede repetir el mismo registro.
            seriales.append(serial)
        nap_tags.setdefault(serial, f.get("nap_tag"))

    nombre_empresa = filas[0].get("empresa") if filas else None
    return seriales, nombre_empresa, nap_tags


# --- Paso 2: soldef -----------------------------------------------------------

def obtener_onus(seriales):
    """ONUs con serial, precinto y mac para los seriales dados."""
    onus = []
    with db.soldef_conn() as conn:
        with db.cursor_pg(conn, config.STATEMENT_TIMEOUT_MS) as cur:
            for lote in _chunks(seriales, config.CHUNK_SIZE):
                # El primer parámetro es el flag "traer todo": mandamos 1 para
                # que aplique el filtro por lista.
                cur.execute(q.Q_SOLDEF_ONUS_POR_IDS, (1, lote))
                onus.extend(cur.fetchall())
    return onus


# --- Paso 3: zabbix -----------------------------------------------------------

def obtener_metricas(precintos, desde):
    """Estado y LOS en una sola consulta.

    Devuelve {'status': {precinto: (valor, clock)}, 'los': {...}}. Una sola query
    alcanza porque ambas métricas comparten la resolución precinto -> itemid.
    """
    resultado = {"status": {}, "los": {}}
    with db.zabbix_conn() as conn:
        with db.cursor_pg(conn, config.STATEMENT_TIMEOUT_MS) as cur:
            for lote in _chunks(precintos, config.CHUNK_SIZE):
                cur.execute(q.Q_ZBX_METRICAS, (lote, desde))
                for fila in cur.fetchall():
                    destino = resultado.get(fila["metrica"])
                    if destino is None:
                        continue
                    clave = normalizar_precinto(fila["precinto"])
                    if not clave:
                        continue
                    clock = fila["clock"] or 0
                    anterior = destino.get(clave)
                    # La query deduplica por itemid; si un precinto mapeara a más
                    # de un item, nos quedamos con la lectura más reciente.
                    if anterior is None or clock >= anterior[1]:
                        destino[clave] = (fila["valor"], clock)
    return resultado


# --- Cruce y agregados --------------------------------------------------------

def _es_online(status) -> bool:
    return bool(status) and "offline" not in str(status).lower()


def _tiene_los(valor) -> bool:
    # El valor sano es 'No alarm'; la alarma contiene 'LOS'.
    if not valor:
        return False
    texto = str(valor).lower()
    return "no alarm" not in texto and "los" in texto


def cruzar(onus, metricas, nap_tags):
    """Une cada ONU de soldef con sus métricas de zabbix. Devuelve la lista completa."""
    status, los = metricas["status"], metricas["los"]
    dispositivos = []

    for onu in onus:
        clave = normalizar_precinto(onu.get("precinto"))
        st_v = status.get(clave) if clave else None
        los_v = los.get(clave) if clave else None
        serial = onu.get("serial")

        dispositivos.append(
            {
                "serial": serial,
                "nombre": nap_tags.get(serial),
                "mac": onu.get("mac"),
                "precinto": onu.get("precinto"),
                "status": st_v[0] if st_v else None,
                "status_timestamp": st_v[1] if st_v else None,
                "los": los_v[0] if los_v else None,
                "los_timestamp": los_v[1] if los_v else None,
                "estado": ("online" if _es_online(st_v[0]) else "offline")
                if st_v
                else "sin_datos",
                "con_los": _tiene_los(los_v[0]) if los_v else False,
            }
        )
    return dispositivos


def resumir(dispositivos):
    """Agregados calculados en un solo pase sobre la lista."""
    online = offline = sin_datos = con_los = 0

    for d in dispositivos:
        if d["estado"] == "online":
            online += 1
        elif d["estado"] == "offline":
            offline += 1
        else:
            sin_datos += 1
        if d["con_los"]:
            con_los += 1

    total = len(dispositivos)
    return {
        "total": total,
        "online": online,
        "offline": offline,
        "sin_datos": sin_datos,
        "con_alarma_los": con_los,
        "porcentaje_online": round(100 * online / total, 2) if total else None,
    }


# --- Orquestación -------------------------------------------------------------

def analitica_empresa(empresa_id, horas):
    """Ejecuta los tres pasos y devuelve (metadata, resumen, dispositivos)."""
    pendientes = q.faltantes()
    if pendientes:
        raise QueriesNoConfiguradas(pendientes)

    hasta = int(time.time())
    desde = hasta - (horas * 3600)

    t0 = time.perf_counter()
    seriales, nombre_empresa, nap_tags = obtener_ont_ids(empresa_id)
    t_nap = time.perf_counter()

    if not seriales:
        return None, None, None

    onus = obtener_onus(seriales)
    t_sol = time.perf_counter()

    # En soldef el precinto entra por LEFT JOIN, así que puede venir en NULL.
    # Esas ONUs no se pueden cruzar, pero igual aparecen en la respuesta como
    # sin_datos: no se descartan, solo no se consultan.
    precintos = list({p for p in (o.get("precinto") for o in onus) if p})
    metricas = (
        obtener_metricas(precintos, desde) if precintos else {"status": {}, "los": {}}
    )
    t_zbx = time.perf_counter()

    dispositivos = cruzar(onus, metricas, nap_tags)
    resumen = resumir(dispositivos)

    log.info(
        "empresa=%s seriales=%d onus=%d precintos=%d | napear=%.2fs soldef=%.2fs "
        "zabbix=%.2fs total=%.2fs",
        empresa_id, len(seriales), len(onus), len(precintos),
        t_nap - t0, t_sol - t_nap, t_zbx - t_sol, time.perf_counter() - t0,
    )

    metadata = {
        "empresa_id": empresa_id,
        "empresa": nombre_empresa,
        "total_seriales_napear": len(seriales),
        "total_onus": len(dispositivos),
        "rango_tiempo": {
            "desde_timestamp": desde,
            "hasta_timestamp": hasta,
            "horas_consultadas": horas,
        },
    }
    return metadata, resumen, dispositivos
