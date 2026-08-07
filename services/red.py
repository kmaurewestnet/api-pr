"""Capa de red: ping ICMP y snmpget.

Las dos son llamadas a binarios del sistema, no librerías. Se invocan siempre con
lista de argumentos (nunca `shell=True`) y con la IP validada con `ipaddress`
antes de llegar al subproceso, así que no hay superficie de inyección de
comandos.

Convención de retorno, usada por toda la matriz de decisión:

    True   verificación hecha, resultado positivo (responde / valor leído)
    False  verificación hecha, resultado negativo (no responde)
    None   no se pudo verificar (falta el dato, el binario no está, error raro)

`None` no es lo mismo que `False`: services/cortes.py nunca lo cuenta como falla,
porque un timeout de ping SÍ significa "caído" pero un binario faltante no.
"""
import ipaddress
import logging
import re
import shutil
import subprocess

import config

log = logging.getLogger(__name__)

# Un OID es solo dígitos y puntos. Los items de Zabbix pueden traer macros sin
# resolver ({#SNMPINDEX}) o cadenas vacías: esas se descartan.
_OID_VALIDO = re.compile(r"^\.?\d+(\.\d+)*$")

# Respuestas de net-snmp que son un error con código de salida 0.
_SNMP_SIN_DATO = ("no such object", "no such instance", "no more variables")


def ip_valida(ip) -> str:
    """Devuelve la IP normalizada, o '' si no es una IP. Frontera de confianza:
    todo lo que sale de una base de datos pasa por acá antes de ir a un
    subproceso."""
    if not ip:
        return ""
    try:
        return str(ipaddress.ip_address(str(ip).strip()))
    except ValueError:
        log.warning("Se descartó un valor que no es una IP: %r", ip)
        return ""


def _ejecutar(argv, timeout, que):
    """subprocess.run acotado. Devuelve (returncode, stdout) o None si el
    comando no se pudo ejecutar. Nunca propaga."""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # El binario se colgó por encima de su propio timeout.
        log.warning("%s excedió el timeout de %ss", que, timeout)
        return None
    except (FileNotFoundError, PermissionError) as e:
        # Error de configuración del host, no del cliente consultado.
        log.error("No se pudo ejecutar %s (%s): revisá PING_PATH/SNMPGET_PATH",
                  argv[0], e)
        return None
    except OSError as e:
        log.error("Fallo al ejecutar %s: %s", que, e)
        return None
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def ping(ip, etiqueta="") -> bool | None:
    """Ping ICMP. True responde, False no responde, None no se pudo evaluar.

    Un `ping` que agota el tiempo devuelve código != 0: eso es False, que es
    justamente la señal que la matriz necesita.
    """
    destino = ip_valida(ip)
    if not destino:
        return None

    deadline = max(1, config.PING_COUNT * config.PING_TIMEOUT_SEG)
    argv = [
        config.PING_PATH,
        "-n",                              # sin resolución inversa de DNS
        "-q",                              # solo el resumen
        "-c", str(config.PING_COUNT),
        "-W", str(config.PING_TIMEOUT_SEG),  # espera por respuesta
        "-w", str(deadline),                 # tope total del comando
        destino,
    ]
    # +2s de colchón: si el propio ping ignora -w, lo corta subprocess.
    res = _ejecutar(argv, deadline + 2, f"ping a {etiqueta or destino}")
    if res is None:
        return None
    responde = res[0] == 0
    log.debug("ping %s (%s): %s", destino, etiqueta, "OK" if responde else "sin respuesta")
    return responde


def snmpget(host, oids) -> dict:
    """Lee varios OIDs del host en UNA sola invocación de snmpget.

    Devuelve {oid: valor} solo con los que se pudieron leer; un OID sin dato
    simplemente no aparece. `oids` tiene que entrar en un PDU: el que chunkea es
    quien llama (services/cortes._leer_snmp, con SNMP_OIDS_POR_CONSULTA).

    Antes era un subproceso por OID. Una NAP de 64 clientes hacía 64 fork+exec
    y, con el pool de hilos en 6, tardaba hasta 11 rondas de timeout SNMP. En un
    solo PDU son 64 varbinds y un subproceso.

    La comunidad sale de SNMP_COMMUNITY y nunca se loguea: los mensajes de error
    solo mencionan host y OID.
    """
    destino = ip_valida(host)
    if not destino:
        return {}
    if not config.SNMP_COMMUNITY:
        log.error("SNMP_COMMUNITY no está configurada: no se puede consultar %s", destino)
        return {}

    # Validación en la frontera: lo que no es un OID no llega al subproceso.
    validos = []
    for oid in oids or []:
        limpio = str(oid or "").strip()
        if _OID_VALIDO.match(limpio):
            validos.append(limpio)
        else:
            log.warning("OID inválido o sin resolver, se omite: %r", oid)
    if not validos:
        return {}

    argv = [
        config.SNMPGET_PATH,
        "-v", config.SNMP_VERSION,
        "-c", config.SNMP_COMMUNITY,
        "-t", str(config.SNMP_TIMEOUT_SEG),
        "-r", str(config.SNMP_RETRIES),
        # -Oq: sin el "= INTEGER:"; -On: OID numérico. Juntos dan "<oid> <valor>"
        # por línea, así que el resultado se mapea por OID y no por posición: si
        # la OLT omite o reordena un varbind, no se corren todos los valores.
        "-Oqn",
        "-Ln",    # sin logging de net-snmp por stderr
        destino,
        *validos,
    ]
    tope = config.SNMP_TIMEOUT_SEG * (config.SNMP_RETRIES + 1) + 2
    res = _ejecutar(argv, tope, f"snmpget {destino} ({len(validos)} OIDs)")
    if res is None:
        return {}

    codigo, salida = res
    if codigo != 0 and not salida:
        log.warning("snmpget sin respuesta de %s para %d OIDs", destino, len(validos))
        return {}

    # Índice por OID sin punto inicial: se pide "1.3.6..." y net-snmp responde
    # ".1.3.6...".
    pedidos = {o.lstrip("."): o for o in validos}
    valores = {}
    for linea in salida.splitlines():
        partes = linea.strip().split(None, 1)
        if len(partes) != 2:
            continue
        oid_crudo, valor = partes
        oid = pedidos.get(oid_crudo.lstrip("."))
        if oid is None:
            continue
        if any(m in valor.lower() for m in _SNMP_SIN_DATO):
            log.warning("snmpget: %s no tiene el OID %s", destino, oid)
            continue
        valores[oid] = valor.strip().strip('"')

    faltantes = len(validos) - len(valores)
    if faltantes:
        log.warning("snmpget %s: %d de %d OIDs sin valor utilizable",
                    destino, faltantes, len(validos))
    log.debug("snmpget %s -> %s", destino, valores)
    return valores


def utilidades_disponibles() -> dict:
    """Estado de los binarios externos. Lo expone /health: sin ellos el endpoint
    de cortes responde igual, pero con todas las verificaciones en 'no evaluable'."""
    estado = {}
    for nombre, ruta in (("ping", config.PING_PATH), ("snmpget", config.SNMPGET_PATH)):
        # which() resuelve tanto una ruta absoluta como un nombre suelto del PATH.
        estado[nombre] = {"ok": bool(shutil.which(ruta)), "path": ruta}
    estado["snmpget"]["community_configurada"] = bool(config.SNMP_COMMUNITY)
    return estado
