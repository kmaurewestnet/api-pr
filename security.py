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


_CLAVES = _cargar_claves()

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

    def __init__(self, por_minuto: int, burst: int):
        self._tasa = por_minuto / 60.0
        self._capacidad = max(1, burst)
        self._estado = {}          # consumidor -> (tokens, ultima_recarga)
        self._lock = threading.Lock()

    def consumir(self, consumidor: str):
        """(permitido, segundos_de_espera)."""
        if self._tasa <= 0:
            return True, 0.0
        ahora = time.monotonic()
        with self._lock:
            tokens, ultimo = self._estado.get(consumidor, (self._capacidad, ahora))
            tokens = min(self._capacidad, tokens + (ahora - ultimo) * self._tasa)
            if tokens < 1:
                self._estado[consumidor] = (tokens, ahora)
                return False, (1 - tokens) / self._tasa
            self._estado[consumidor] = (tokens - 1, ahora)
            return True, 0.0


_limite = CubetaDeTokens(config.RATE_LIMIT_POR_MINUTO, config.RATE_LIMIT_BURST)


def limitar_tasa(consumidor: str = Depends(verificar_api_key)) -> str:
    """Valida la clave y descuenta un token. Devuelve el nombre del consumidor."""
    permitido, espera = _limite.consumir(consumidor)
    if not permitido:
        log.warning("Rate limit alcanzado por '%s'", consumidor)
        raise HTTPException(
            status_code=429,
            detail=(
                f"Límite de {config.RATE_LIMIT_POR_MINUTO} consultas por minuto "
                "alcanzado"
            ),
            headers={"Retry-After": str(max(1, round(espera)))},
        )
    return consumidor
