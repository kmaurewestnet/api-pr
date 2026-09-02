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

El valor vencido no se tira: si el recálculo falla o devuelve "no evaluable", se
sirve el anterior. Sin eso, un pico de lentitud en Zabbix durante un corte real
hace que la consulta de NAP se pase del statement_timeout, `nap_caida` quede en
None y —con la OLT respondiendo— `isZoneIncident` pase a false con un 200 OK: la
API diría "no hay corte" en medio del corte. Un estado de NAP de hace dos
minutos es incomparablemente mejor que eso.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)


class CacheTTL:
    def __init__(self, ttl_seg: int, limite: int = 5000, nombre: str = "",
                 stale_max_seg: int = 0):
        self._ttl = ttl_seg
        self._stale_max = stale_max_seg
        self._limite = limite
        self._nombre = nombre
        self._valores = {}          # clave -> (valor, vence_en, calculado_en)
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

    def _vencido_utilizable(self, clave):
        """(hay_valor, valor, antigüedad) del valor vencido, si todavía sirve.

        Solo sirve un valor real: un None viejo no aporta nada sobre un None
        nuevo. Y solo dentro de `stale_max` desde que se calculó de verdad, para
        que una NAP que dejó de tener datos no quede reportada como caída para
        siempre.
        """
        entrada = self._valores.get(clave)
        if entrada is None:
            return False, None, 0
        valor, _, calculado_en = entrada
        edad = time.monotonic() - calculado_en
        if valor is None or edad > self._ttl + self._stale_max:
            return False, None, edad
        return True, valor, edad

    def _guardar(self, clave, valor, calculado_en):
        with self._maestro:
            self._valores[clave] = (valor, time.monotonic() + self._ttl, calculado_en)

    def _purgar(self):
        """Descarta lo que ya no sirve ni como valor vencido. Las claves son NAPs
        y OLTs, un conjunto acotado; esto cubre el caso de una `nap` mal extraída
        que genere claves basura."""
        limite = time.monotonic() - (self._ttl + self._stale_max)
        for k in [k for k, (_, _, cal) in self._valores.items() if cal <= limite]:
            self._valores.pop(k, None)
            self._locks.pop(k, None)

    def obtener(self, clave, calcular):
        """Valor cacheado, o el de `calcular()` si venció.

        Si el recálculo falla o devuelve None y hay un valor anterior utilizable,
        se sirve ese: una medición real de hace un rato vale más que un "no
        evaluable", que aguas abajo se traduce en "no hay corte". Se le renueva
        el TTL para no reintentar en cada request, pero no la antigüedad, así que
        igual caduca a los `stale_max`.
        """
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

            ahora = time.monotonic()
            try:
                valor = calcular()
            except Exception as e:
                hay_previo, previo, edad = self._vencido_utilizable(clave)
                if not hay_previo:
                    raise
                # %r y no %s: DatabaseUnavailable tiene el mensaje genérico
                # que va al 503, y su repr es el que nombra la base.
                log.warning(
                    "cache %s: %s falló (%r), se sirve el valor de hace %.0fs",
                    self._nombre, clave, e, edad,
                )
                self._guardar(clave, previo, ahora - edad)
                return previo

            if valor is None:
                hay_previo, previo, edad = self._vencido_utilizable(clave)
                if hay_previo:
                    log.warning(
                        "cache %s: %s quedó sin dato, se sirve el de hace %.0fs",
                        self._nombre, clave, edad,
                    )
                    self._guardar(clave, previo, ahora - edad)
                    return previo

            self._guardar(clave, valor, ahora)
            return valor

    def limpiar(self):
        with self._maestro:
            self._valores.clear()
            self._locks.clear()
