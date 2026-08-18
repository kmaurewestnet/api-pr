"""Identidad del consumidor y rate limit.

Dos cosas que van juntas: sin saber quién llama no se le puede limitar a uno sin
limitarles a todos.

La clave única de la primera versión (`API_KEY_SECRETA`) sigue funcionando como
consumidor "legacy", para no romper a quien ya la tiene configurada.
"""
import hmac
import logging
import threading
import time

from fastapi import Depends, HTTPException, Query
from fastapi.security import APIKeyHeader

import config

log = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name=config.API_KEY_NAME, auto_error=False)


def _cargar_claves() -> dict:
    """{clave: nombre_del_consumidor}. Indexado por clave para poder resolver
    quién llama en una sola pasada."""
    claves = {}
    for entrada in config.API_KEYS_CRUDO.split(","):
        entrada = entrada.strip()
        if not entrada:
            continue
        nombre, sep, clave = entrada.partition(":")
        if not sep or not nombre.strip() or not clave.strip():
            log.error("Entrada mal formada en API_KEYS, se ignora: %r", nombre)
            continue
        claves[clave.strip()] = nombre.strip()
    if config.API_KEY_SECRETA:
        claves.setdefault(config.API_KEY_SECRETA, "legacy")
    return claves


def _cargar_solo_cortes(claves: dict) -> frozenset:
    """Nombres de los consumidores limitados al endpoint de cortes.

    Un nombre mal escrito no rompe nada visible: el consumidor queda sin
    restringir y nadie se entera hasta que pide el parque entero. Es justo la
    falla que esto viene a evitar, asi que se valida contra las claves cargadas
    y se grita al arrancar.
    """
    nombres = frozenset(
        n.strip() for n in config.CONSUMIDORES_SOLO_CORTES.split(",") if n.strip()
    )
    desconocidos = nombres - set(claves.values())
    if desconocidos:
        log.error(
            "API_KEYS_SOLO_CORTES nombra consumidores que no estan en API_KEYS, "
            "quedan SIN restringir: %s", ", ".join(sorted(desconocidos)),
        )
    return nombres


def _cargar_limites(claves: dict) -> dict:
    """{consumidor: consultas_por_minuto}. Lo no listado usa el limite general."""
    limites = {}
    for entrada in config.RATE_LIMIT_POR_CONSUMIDOR_CRUDO.split(","):
        entrada = entrada.strip()
        if not entrada:
            continue
        nombre, sep, valor = entrada.partition(":")
        nombre = nombre.strip()
        try:
            if not sep or not nombre:
                raise ValueError("falta el nombre o los dos puntos")
            por_minuto = int(valor.strip())
            # Un negativo daria tasa <= 0, que en la cubeta significa "sin
            # limite": justo lo contrario de lo que quiso escribir quien lo puso.
            if por_minuto < 0:
                raise ValueError("no puede ser negativo (0 es sin limite)")
            limites[nombre] = por_minuto
        except ValueError as e:
            log.error(
                "Entrada mal formada en RATE_LIMIT_POR_CONSUMIDOR (%s), se "
                "ignora: %r", e, entrada,
            )
    desconocidos = set(limites) - set(claves.values())
    if desconocidos:
        log.error(
            "RATE_LIMIT_POR_CONSUMIDOR nombra consumidores que no estan en "
            "API_KEYS, no le aplican a nadie: %s", ", ".join(sorted(desconocidos)),
        )
    return limites


_CLAVES = _cargar_claves()
_SOLO_CORTES = _cargar_solo_cortes(_CLAVES)
_LIMITES_PROPIOS = _cargar_limites(_CLAVES)

if _SOLO_CORTES:
    log.info("Limitados a /cortes: %s", ", ".join(sorted(_SOLO_CORTES)))

if not _CLAVES:
    # Falla cerrado: sin claves, todo request da 403. Mejor verlo al arrancar
    # que descubrirlo cuando el primer consumidor reporte que no puede entrar.
    log.error("No hay ninguna API key configurada: la API va a rechazar todo")
else:
    log.info("API keys cargadas para: %s", ", ".join(sorted(set(_CLAVES.values()))))


NO_AUTORIZADO = HTTPException(
    status_code=403,
    detail="No autorizado. API Key inválida o ausente en la cabecera X-API-Key.",
)

FUERA_DE_ALCANCE = HTTPException(
    status_code=403,
    detail="Esta clave solo tiene acceso al endpoint de cortes.",
)


def _resolver(api_key) -> str | None:
    """Nombre del consumidor dueño de la clave, o None si no la reconoce."""
    if not api_key:
        return None
    # compare_digest contra cada clave: comparar con == filtraría la clave
    # por diferencias de tiempo.
    for clave, nombre in _CLAVES.items():
        try:
            if hmac.compare_digest(api_key, clave):
                return nombre
        except TypeError:
            # compare_digest solo acepta str ASCII. Una clave con acentos o
            # emojis reventaría con 500 en vez de 403; llega más fácil por
            # ?key= que por el header, pero el guard va acá porque es el único
            # lugar por el que pasan las dos vías.
            return None
    return None


def verificar_api_key(api_key: str = Depends(api_key_header)) -> str:
    """Devuelve el nombre del consumidor. 403 si la clave no es válida."""
    nombre = _resolver(api_key)
    if nombre:
        return nombre
    raise NO_AUTORIZADO


def verificar_api_key_interna(consumidor: str = Depends(verificar_api_key)) -> str:
    """Igual que `verificar_api_key`, pero le cierra la puerta a los externos.

    Precinto y analiticas devuelven el parque de todas las empresas: es la vista
    del NOC. La centralita y el chatbot solo necesitan preguntar por un cliente,
    y para eso les alcanza /cortes.

    Va como dependencia de esos dos routers y no como un chequeo dentro de cada
    handler para que agregar un endpoint interno nuevo lo herede sin que nadie
    se tenga que acordar.
    """
    if consumidor in _SOLO_CORTES:
        log.warning(
            "'%s' pidio un endpoint interno; su alcance es solo /cortes", consumidor
        )
        raise FUERA_DE_ALCANCE
    return consumidor


def verificar_api_key_docs(
    api_key: str = Depends(api_key_header),
    key: str = Query(default=None, description="Alternativa a la cabecera X-API-Key"),
) -> str:
    """Igual que `verificar_api_key`, pero acepta la clave también por `?key=`.

    Existe solo para /docs, /redoc y /openapi.json: el navegador no manda
    cabeceras al tipear una URL, ni el `fetch` con el que Swagger UI se trae el
    esquema, así que por header la UI es inusable. Los endpoints de negocio
    siguen exigiendo el header y nada más.

    El precio de aceptarla por query string es que la clave queda escrita en el
    log de accesos del servidor y en el historial del navegador. Es una clave
    por consumidor y revocable, así que se acota a rotarla; aun así conviene que
    la URL con `?key=` no circule por chat ni por tickets.
    """
    nombre = _resolver(api_key) or _resolver(key)
    if nombre:
        return nombre
    raise NO_AUTORIZADO


class CubetaDeTokens:
    """Rate limit por consumidor.

    En memoria y por proceso: con N workers de uvicorn el límite efectivo es N
    veces el configurado. Compartirlo requeriría Redis, que es infraestructura
    nueva; para frenar un loop accidental o un consumidor desbocado alcanza.
    """

    def __init__(self, por_minuto: int, burst: int, propios: dict | None = None):
        self._defecto = (por_minuto / 60.0, max(1, burst))
        # El burst propio sale del mismo numero: darle 120/min a un consumidor y
        # dejarle la rafaga del default no es lo que se pidio.
        self._propios = {
            nombre: (pm / 60.0, max(1, pm)) for nombre, pm in (propios or {}).items()
        }
        self._estado = {}          # consumidor -> (tokens, ultima_recarga)
        self._lock = threading.Lock()

    def _cuota(self, consumidor: str):
        """(tasa, capacidad) del consumidor, o la general si no tiene propia."""
        return self._propios.get(consumidor, self._defecto)

    def por_minuto_de(self, consumidor: str) -> int:
        """Limite efectivo, para poder nombrarlo en el 429 sin mentir."""
        return round(self._cuota(consumidor)[0] * 60)

    def consumir(self, consumidor: str):
        """(permitido, segundos_de_espera)."""
        tasa, capacidad = self._cuota(consumidor)
        if tasa <= 0:
            return True, 0.0
        ahora = time.monotonic()
        with self._lock:
            tokens, ultimo = self._estado.get(consumidor, (capacidad, ahora))
            tokens = min(capacidad, tokens + (ahora - ultimo) * tasa)
            if tokens < 1:
                self._estado[consumidor] = (tokens, ahora)
                return False, (1 - tokens) / tasa
            self._estado[consumidor] = (tokens - 1, ahora)
            return True, 0.0


# Dos cubetas separadas y no una compartida: un request de cortes y uno de
# analiticas no cuestan lo mismo ni presionan lo mismo, asi que tampoco tienen
# por que descontar de la misma cuota.
_limite = CubetaDeTokens(
    config.RATE_LIMIT_POR_MINUTO, config.RATE_LIMIT_BURST, _LIMITES_PROPIOS
)
# La cuota interna no se ajusta por consumidor: ahi solo llegan claves internas.
_limite_interno = CubetaDeTokens(
    config.RATE_LIMIT_INTERNO_POR_MINUTO, config.RATE_LIMIT_INTERNO_BURST
)


def _descontar(cubeta: CubetaDeTokens, consumidor: str) -> str:
    permitido, espera = cubeta.consumir(consumidor)
    if not permitido:
        limite = cubeta.por_minuto_de(consumidor)
        log.warning("Rate limit alcanzado por '%s' (%s/min)", consumidor, limite)
        raise HTTPException(
            status_code=429,
            detail=f"Límite de {limite} consultas por minuto alcanzado",
            headers={"Retry-After": str(max(1, round(espera)))},
        )
    return consumidor


def limitar_tasa(consumidor: str = Depends(verificar_api_key)) -> str:
    """Valida la clave y descuenta un token. Devuelve el nombre del consumidor."""
    return _descontar(_limite, consumidor)


def limitar_tasa_interna(consumidor: str = Depends(verificar_api_key_interna)) -> str:
    """Alcance interno + cuota propia, para precinto y analiticas.

    Es lo que evita que un uso interno pesado —no hace falta que sea malicioso,
    alcanza con un tablero refrescando— le coma a /cortes las conexiones de
    zabbix que comparten.
    """
    return _descontar(_limite_interno, consumidor)
