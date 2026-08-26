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
from concurrent.futures import ThreadPoolExecutor

import config
import db
from queries import analytics as q

log = logging.getLogger(__name__)

# Conexiones de zabbix que un request toma a la vez: `obtener_metricas` corre
# estado+LOS y LDC en paralelo, una conexión cada una. Eran tres hasta que
# estado y LOS se unificaron en una sola consulta. Ver main.py.
CONEXIONES_ZABBIX_POR_REQUEST = 2

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

def _guardar(destino, clave, clock, valor):
    """Se queda con la lectura más reciente por precinto.

    La query ya deduplica por itemid; esto cubre el caso de un precinto que mapee
    a más de un item. El clock va último en la tupla para no depender del largo.
    """
    anterior = destino.get(clave)
    if anterior is None or clock >= anterior[-1]:
        destino[clave] = valor + (clock,)


def _metricas_estado(precintos, desde):
    """Estado y LOS: una sola query, comparten la resolución precinto -> itemid."""
    resultado = {"status": {}, "los": {}}
    with db.zabbix_conn() as conn:
        with db.cursor_pg(conn, config.STATEMENT_TIMEOUT_MS) as cur:
            for lote in _chunks(precintos, config.CHUNK_SIZE):
                cur.execute(q.Q_ZBX_METRICAS, (lote, desde, desde))
                for fila in cur.fetchall():
                    destino = resultado.get(fila["metrica"])
                    clave = normalizar_precinto(fila["precinto"])
                    if destino is None or not clave:
                        continue
                    _guardar(destino, clave, fila["clock"] or 0, (fila["valor"],))
    return resultado


def _metricas_ldc(precintos, desde):
    """Última causa de caída. Query aparte: vive en history_text, no history_str."""
    resultado = {}
    with db.zabbix_conn() as conn:
        with db.cursor_pg(conn, config.STATEMENT_TIMEOUT_MS) as cur:
            for lote in _chunks(precintos, config.CHUNK_SIZE):
                cur.execute(q.Q_ZBX_LDC, (lote, desde, desde))
                for fila in cur.fetchall():
                    clave = normalizar_precinto(fila["precinto"])
                    if not clave:
                        continue
                    _guardar(
                        resultado, clave, fila["clock"] or 0,
                        (fila["ldc"], fila["ldc_timestamp"]),
                    )
    return {"ldc": resultado}


def obtener_metricas(precintos, desde):
    """Estado, LOS y causa de caída.

    `desde` en 0 desactiva el filtro temporal y trae el último valor real de cada
    item, sin importar su antigüedad. Devuelve
    {'status': {...}, 'los': {...}, 'ldc': {...}} indexado por precinto.

    Las dos consultas son independientes (distinta tabla de historia, distintos
    items), así que corren en paralelo con una conexión cada una.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        futuros = [
            pool.submit(_metricas_estado, precintos, desde),
            pool.submit(_metricas_ldc, precintos, desde),
        ]
        resultado = {}
        for f in futuros:
            resultado.update(f.result())
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


def _es_dying_gasp(causa) -> bool:
    """La ONT avisó que se quedaba sin energía justo antes de morir."""
    return bool(causa) and "dying" in str(causa).lower()


def _categoria(estado, con_los, los_ts, ldc, ldc_ts, caida_ts, ahora):
    """Reparto excluyente: cada equipo cae en una sola categoría.

    El orden importa. Powerfail se evalúa ANTES que LOS porque un corte de
    energía apaga la ONT y eso genera LOS en la OLT: las dos señales aparecen
    juntas, y el dying-gasp es la más específica. Si LOS ganara, no se
    detectaría ningún powerfail.
    """
    if estado == "online":
        return "online"
    if estado == "sin_datos":
        return "sin_datos"

    # Caída. La cercanía se mide contra el momento de la caída, no contra ahora.
    if (
        _es_dying_gasp(ldc)
        and ldc_ts
        and caida_ts
        and abs(caida_ts - ldc_ts) <= config.VENTANA_POWERFAIL_SEG
    ):
        return "powerfail"

    # Una alarma LOS vencida ya no describe la caída actual.
    if con_los and los_ts and (ahora - los_ts) < config.LOS_VIGENTE_SEG:
        return "los"

    return "offline"


def cruzar(onus, metricas, nap_tags, ahora=None):
    """Une cada ONU de soldef con sus métricas de zabbix. Devuelve la lista completa."""
    status, los, ldc = metricas["status"], metricas["los"], metricas["ldc"]
    ahora = ahora if ahora is not None else int(time.time())
    dispositivos = []

    for onu in onus:
        clave = normalizar_precinto(onu.get("precinto"))
        st_v = status.get(clave) if clave else None
        los_v = los.get(clave) if clave else None
        ldc_v = ldc.get(clave) if clave else None
        serial = onu.get("serial")
        con_los = _tiene_los(los_v[0]) if los_v else False

        # La mayoría de las ONUs no tiene el item hwGponDeviceOntEthernetOnlineState:
        # solo algunas plantillas lo incluyen. Cuando falta, la alarma LOS es el
        # mejor proxy disponible (pérdida de señal óptica = ONU caída). El origen
        # queda expuesto en la respuesta para que el dato sea interpretable.
        if st_v:
            estado, origen = ("online" if _es_online(st_v[0]) else "offline"), "onlinestate"
        elif los_v:
            estado, origen = ("offline" if con_los else "online"), "los"
        else:
            estado, origen = "sin_datos", None

        # Momento de la caída: el registro del estado si existe, y si no el de la
        # alarma, que es de donde se dedujo que el equipo está caído.
        caida_ts = st_v[1] if st_v else (los_v[1] if los_v else None)
        categoria = _categoria(
            estado, con_los,
            los_v[1] if los_v else None,
            ldc_v[0] if ldc_v else None,
            ldc_v[1] if ldc_v else None,
            caida_ts, ahora,
        )

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
                "ldc": ldc_v[0] if ldc_v else None,
                "ldc_timestamp": ldc_v[1] if ldc_v else None,
                "estado": estado,
                "origen_estado": origen,
                "categoria": categoria,
                "con_los": con_los,
            }
        )
    return dispositivos


CATEGORIAS = ("online", "offline", "los", "powerfail", "sin_datos")


def resumir(dispositivos):
    """Reparto excluyente por categoría, en un solo pase.

    Los cinco contadores suman el total: cada equipo está en uno y solo uno.
    """
    conteo = {c: 0 for c in CATEGORIAS}
    # Cuántos estados salen del item autoritativo y cuántos del proxy de LOS:
    # una categoría derivada de un proxy vale menos que una tomada del item real.
    origenes = {"onlinestate": 0, "los": 0, "sin_datos": 0}

    for d in dispositivos:
        conteo[d["categoria"]] += 1
        origenes[d["origen_estado"] or "sin_datos"] += 1

    total = len(dispositivos)
    return {
        "total": total,
        **conteo,
        "porcentaje_online": round(100 * conteo["online"] / total, 2) if total else None,
        "origen_estado": origenes,
    }


# --- Orquestación -------------------------------------------------------------

def analitica_empresa(empresa_id, horas):
    """Ejecuta los tres pasos y devuelve (metadata, resumen, dispositivos)."""
    pendientes = q.faltantes()
    if pendientes:
        raise QueriesNoConfiguradas(pendientes)

    hasta = int(time.time())
    # horas=None trae el último valor de cada item sin importar su antigüedad.
    desde = hasta - (horas * 3600) if horas else 0

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
        obtener_metricas(precintos, desde)
        if precintos
        else {"status": {}, "los": {}, "ldc": {}}
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
            "desde_timestamp": desde or None,
            "hasta_timestamp": hasta,
            "horas_consultadas": horas,
        },
    }
    return metadata, resumen, dispositivos
