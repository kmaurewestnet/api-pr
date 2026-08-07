"""Caché con TTL y single-flight, para el estado que comparten varios clientes.

El estado de una NAP y el ping a una OLT son **la misma respuesta** para todos
los clientes de esa caja. Sin caché, un corte real —que es justo cuando más
consultas llegan— hace que 50 clientes de la misma NAP disparen 50 veces el
mismo walk SNMP contra la misma OLT: la API hace su máximo trabajo cuando la red
está peor.

El single-flight (un lock por clave) es la mitad que importa. Un TTL a secas no
alcanza: las 50 consultas de un corte llegan **juntas**, fallan las 50 en el
caché a la vez y salen las 50 a preguntar. Con el lock por clave, la primera
calcula y las otras 49 esperan ese resultado.

No se cachea nada por cliente —ni el ping al cliente ni el estado de su ONT—:
eso es la respuesta puntual de cada uno y tiene que ser fresca.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)


class CacheTTL:
    def __init__(self, ttl_seg: int, limite: int = 5000, nombre: str = ""):
        self._ttl = ttl_seg
        self._limite = limite
        self._nombre = nombre
        self._valores = {}          # clave -> (valor, vence_en)
        self._locks = {}            # clave -> Lock de cálculo
        self._maestro = threading.Lock()

    def _vigente(self, clave):
        """(hay_valor, valor). El valor puede ser None y seguir siendo válido:
        'no evaluable' es un resultado, y repetir un SNMP que ya dio timeout no
        lo vuelve más evaluable."""
        entrada = self._valores.get(clave)
        if entrada is not None and entrada[1] > time.monotonic():
            return True, entrada[0]
        return False, None

    def _purgar(self):
        """Descarta lo vencido. Las claves son NAPs y OLTs, un conjunto acotado;
        esto cubre el caso de una `nap` mal extraída que genere claves basura."""
        ahora = time.monotonic()
        vencidas = [k for k, (_, hasta) in self._valores.items() if hasta <= ahora]
        for k in vencidas:
            self._valores.pop(k, None)
            self._locks.pop(k, None)

    def obtener(self, clave, calcular):
        """Valor cacheado, o el de `calcular()` si venció. Una excepción no se
        cachea y se propaga: PoolAgotado tiene que seguir cortando el request."""
        if self._ttl <= 0:
            return calcular()

        hay, valor = self._vigente(clave)
        if hay:
            log.debug("cache %s hit: %s", self._nombre, clave)
            return valor

        with self._maestro:
            if len(self._valores) >= self._limite:
                self._purgar()
            lock = self._locks.setdefault(clave, threading.Lock())

        with lock:
            # Otro hilo pudo haberlo calculado mientras esperábamos el lock: esa
            # es exactamente la avalancha que este caché existe para evitar.
            hay, valor = self._vigente(clave)
            if hay:
                log.debug("cache %s hit tras espera: %s", self._nombre, clave)
                return valor
            valor = calcular()
            with self._maestro:
                self._valores[clave] = (valor, time.monotonic() + self._ttl)
            return valor

    def limpiar(self):
        with self._maestro:
            self._valores.clear()
            self._locks.clear()
