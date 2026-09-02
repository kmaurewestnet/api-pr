"""Control de admisión: cuántos requests de cada endpoint se procesan a la vez.

`/cortes` ya tenía el suyo —un ThreadPoolExecutor propio en su router— porque es
`async def` y todo su trabajo bloqueante va ahí. `/precinto` y `/analytics` son
`def`, así que los corría el threadpool de FastAPI, cuyo default son 40 hilos: un
número que nadie eligió para esta API y que a 1-2 conexiones de zabbix por
request pedía hasta 120 conexiones sobre un pool de 14.

El semáforo es `asyncio` y la dependencia es `async def` a propósito. FastAPI
resuelve las dependencias asincrónicas en el event loop, no en el threadpool: los
requests que esperan admisión no ocupan un hilo, que es justo lo que haría un
`threading.Semaphore` acá y solo movería la cola de lugar.

El slot se libera cuando el handler retorna, que es cuando las conexiones ya
volvieron al pool: `analytics` con `full=true` serializa desde memoria, sin base.
"""
import asyncio
import logging

from fastapi import HTTPException

import config
import db

log = logging.getLogger(__name__)


class Admision:
    """Un tope de requests simultáneos, con espera acotada.

    Vencida la espera responde 503 y no 504: el request todavía no empezó, así
    que no es "tardó demasiado" sino "no hay lugar". Para el consumidor, además,
    un 503 es reintentable y un 504 lo deja sin saber si algo llegó a correr.
    """

    def __init__(self, nombre: str, maximo: int, espera_seg: float):
        self.nombre = nombre
        self.maximo = maximo
        self.espera_seg = espera_seg
        self._semaforo = asyncio.Semaphore(maximo) if maximo > 0 else None

    async def __call__(self):
        if self._semaforo is None:      # en 0 se desactiva
            yield
            return
        try:
            await asyncio.wait_for(
                self._semaforo.acquire(), timeout=self.espera_seg
            )
        except asyncio.TimeoutError:
            log.warning(
                "Admisión de %s: %d en curso y %.0fs de espera agotados",
                self.nombre, self.maximo, self.espera_seg,
            )
            raise HTTPException(status_code=503, detail=db.SERVICIO_NO_DISPONIBLE)
        try:
            yield
        finally:
            self._semaforo.release()


# Uno por endpoint y no uno compartido: cada uno toma una cantidad distinta de
# conexiones por request (ver CONEXIONES_ZABBIX_POR_REQUEST en cada servicio), y
# main.py suma los tres contra POOL_MAX al arrancar.
admitir_analytics = Admision(
    "analytics", config.ANALYTICS_MAX_CONCURRENTES, config.INTERNO_ESPERA_SEG
)
admitir_precinto = Admision(
    "precinto", config.PRECINTO_MAX_CONCURRENTES, config.INTERNO_ESPERA_SEG
)
