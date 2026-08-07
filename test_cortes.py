"""Chequeo del endpoint de cortes. Sin framework: `python test_cortes.py`.

Cubre lo que puede romperse en silencio: la matriz de decisión del punto 4 del
documento, el umbral de corte de NAP, la interpretación de los valores de LOS y
que ninguna consulta haya vuelto a la interpolación de strings.
"""
import os
import re
import shutil
import sys
import tempfile
import time
import threading

import config

from queries import cortes as q
from services import cortes as svc
from services import red
from services.cache import CacheTTL

FALLA, RESPONDE, SIN_DATO = False, True, None


def test_es_ftth():
    # 16 es fibra, 17 es wireless. Son los dos únicos IDs que trae la consulta.
    assert svc.es_ftth(16) is True
    assert svc.es_ftth(17) is False
    # El driver podría devolverlo como string o Decimal según el tipo de columna.
    assert svc.es_ftth("16") is True
    assert svc.es_ftth(None) is False
    assert svc.es_ftth("categoria-sin-id") is False


def test_cliente_con_dos_tecnologias_resuelve_como_fibra():
    """Sin orden explícito, la tecnología devuelta dependería del orden de filas
    que entregue MySQL, que no está garantizado."""
    filas = [
        {"nro_cliente": 1, "categoria_id": 17, "categoria": "Wireless", "ip": "10.55.3.44"},
        {"nro_cliente": 1, "categoria_id": 16, "categoria": "FTTH", "ip": "192.168.5.20"},
    ]
    original = svc._obligatoria
    svc._obligatoria = lambda base, fn, *a: filas
    try:
        assert svc.buscar_cliente("1")["is_ftth"] is True
        filas.reverse()
        assert svc.buscar_cliente("1")["is_ftth"] is True
    finally:
        svc._obligatoria = original


def test_hay_los():
    # Texto ya mapeado, tal como lo guarda Zabbix en su historial.
    assert svc.hay_los("LOS") is True
    assert svc.hay_los("los-i") is True
    assert svc.hay_los("No Alarm") is False
    assert svc.hay_los("Normal") is False
    # Sin dato no es lo mismo que sin alarma.
    assert svc.hay_los(None) is None
    assert svc.hay_los("   ") is None


def test_codigos_snmp_por_defecto():
    """La regresion del cliente 88704: con `int(valor) != 0`, el codigo 1
    ("No Alarm" / "Online") daba alarma, y por arrastre la NAP entera caida.

    Estos son los valores verificados contra las OLT del parque, con los
    defaults de config puestos: si alguien los invierte, esto falla."""
    assert svc.hay_los("2") is True         # LOS / LOSi
    assert svc.hay_los("1") is False        # No Alarm
    assert svc.esta_offline("2") is True    # Offline
    assert svc.esta_offline("1") is False   # Online


def test_codigo_snmp_desconocido_no_es_alarma():
    """La regresión que reporto produccion: con `int(valor) != 0`, un enum donde
    el codigo normal es 1 o 2 daba alarma para toda ONT sana, y de ahi la NAP
    entera se reportaba caida."""
    los, sin_los = config.SNMP_COD_LOS, config.SNMP_COD_SIN_LOS
    off, on = config.SNMP_COD_OFFLINE, config.SNMP_COD_ONLINE
    try:
        # Sin traduccion configurada: no se afirma nada.
        config.SNMP_COD_LOS = config.SNMP_COD_SIN_LOS = frozenset()
        assert svc.hay_los("1") is None
        assert svc.hay_los("2") is None
        assert svc.hay_los("0") is None

        # Con la traduccion cargada, cada codigo cae donde corresponde.
        config.SNMP_COD_LOS, config.SNMP_COD_SIN_LOS = frozenset({2}), frozenset({1})
        assert svc.hay_los("2") is True
        assert svc.hay_los("1") is False
        assert svc.hay_los("7") is None      # codigo fuera de las dos listas

        config.SNMP_COD_OFFLINE, config.SNMP_COD_ONLINE = frozenset({2}), frozenset({1})
        assert svc.esta_offline("2") is True
        assert svc.esta_offline("1") is False
        assert svc.esta_offline("9") is None
    finally:
        config.SNMP_COD_LOS, config.SNMP_COD_SIN_LOS = los, sin_los
        config.SNMP_COD_OFFLINE, config.SNMP_COD_ONLINE = off, on


def test_ont_solar_combina_los_y_onlinestate():
    """El documento actualizado agrega la consulta de OIDs de OnlineState: una
    sola señal en alarma alcanza para dar la ONT por caida."""
    lecturas = {}
    original = svc._interpretar
    svc._valores_snmp = lambda ip, oids: {}
    svc._interpretar = lambda oids, val, interprete, metrica, ip: lecturas.get(metrica, [])
    try:
        lecturas = {"LOS": [False], "OnlineState": [False]}
        assert svc._ont_por_snmp("10.0.0.1", ["1.1"], ["1.2"]) is False
        lecturas = {"LOS": [False], "OnlineState": [True]}
        assert svc._ont_por_snmp("10.0.0.1", ["1.1"], ["1.2"]) is True
        lecturas = {"LOS": [True], "OnlineState": []}
        assert svc._ont_por_snmp("10.0.0.1", ["1.1"], []) is True
        # Ninguna de las dos se pudo interpretar: no evaluable, no "caida".
        lecturas = {}
        assert svc._ont_por_snmp("10.0.0.1", ["1.1"], ["1.2"]) is None
    finally:
        svc._interpretar = original


def test_esta_offline():
    assert svc.esta_offline("Offline") is True
    assert svc.esta_offline("online") is False
    assert svc.esta_offline(None) is None


def test_ont_caida():
    assert svc.ont_caida("LOS", None) is True          # solo LOS
    assert svc.ont_caida(None, "Offline") is True      # solo Online State
    assert svc.ont_caida("No Alarm", "Online") is False
    # Las dos señales no siempre llegan juntas: alcanza con una en alarma.
    assert svc.ont_caida("No Alarm", "Offline") is True
    assert svc.ont_caida(None, None) is None


def test_nap_caida_umbral():
    # NAP chica (<= NAP_TOLERANCIA_DESDE): tienen que estar todos caídos.
    assert svc.nap_caida(3, 3) is True
    assert svc.nap_caida(2, 3) is False
    # NAP grande: se tolera un cliente sin reportar.
    assert svc.nap_caida(7, 8) is True
    assert svc.nap_caida(6, 8) is False
    assert svc.nap_caida(8, 8) is True
    # Sin ocupación declarada no se puede afirmar nada.
    assert svc.nap_caida(5, None) is None
    assert svc.nap_caida(None, 8) is None
    assert svc.nap_caida(0, 0) is None


def test_matriz_zone_incident_ftth():
    """Tabla del punto 4, fila por fila. (nap, ping_olt, ping_sw) -> esperado."""
    casos = [
        # NAP en corte: TRUE sin importar el resto.
        ((True, FALLA, FALLA), True),
        ((True, RESPONDE, RESPONDE), True),
        # NAP arriba + OLT no responde: TRUE (estado de NAP no confiable).
        ((False, FALLA, RESPONDE), True),
        # NAP arriba + OLT y switch caídos: TRUE (caída masiva).
        ((False, FALLA, FALLA), True),
        # NAP arriba + OLT responde: FALSE, responda o no el switch.
        ((False, RESPONDE, FALLA), False),
        ((False, RESPONDE, RESPONDE), False),
        # NAP no evaluable + OLT responde: FALSE, no se inventa un corte.
        ((SIN_DATO, RESPONDE, RESPONDE), False),
        # NAP no evaluable + OLT sin responder: TRUE por la OLT.
        ((SIN_DATO, FALLA, RESPONDE), True),
        # Sin IP de OLT (ping no evaluable) no se declara corte de zona.
        ((False, SIN_DATO, SIN_DATO), False),
    ]
    for (nap, ping_olt, _ping_sw), esperado in casos:
        _, zona = svc.decidir_ftth(RESPONDE, False, nap, ping_olt)
        assert zona is esperado, f"nap={nap} olt={ping_olt} dio {zona}"


def test_matriz_is_online_ftth():
    # El ping responde: online, sin importar la ONT.
    assert svc.decidir_ftth(RESPONDE, True, False, RESPONDE)[0] is True
    # Ping falla + ONT caída: offline. Es el único camino a False del documento.
    assert svc.decidir_ftth(FALLA, True, False, RESPONDE)[0] is False
    # Ping falla pero la ONT dice que está bien: online.
    assert svc.decidir_ftth(FALLA, False, False, RESPONDE)[0] is True
    # Ping falla y no hay dato de ONT: se cae al ping, que es la única señal.
    assert svc.decidir_ftth(FALLA, SIN_DATO, False, RESPONDE)[0] is False
    # Sin IP para pingear y sin dato de ONT: no hay evidencia de que esté arriba.
    assert svc.decidir_ftth(SIN_DATO, SIN_DATO, False, RESPONDE)[0] is False


def test_matriz_wireless():
    assert svc.decidir_wireless(RESPONDE, RESPONDE, RESPONDE) == (True, False)
    assert svc.decidir_wireless(FALLA, RESPONDE, RESPONDE) == (False, False)
    # Cae el AP o el RB: el corte no es solo de este cliente.
    assert svc.decidir_wireless(FALLA, FALLA, RESPONDE) == (False, True)
    assert svc.decidir_wireless(FALLA, RESPONDE, FALLA) == (False, True)
    # Equipos que Zabbix no tiene: no evaluable, no se declara corte de zona.
    assert svc.decidir_wireless(RESPONDE, SIN_DATO, SIN_DATO) == (True, False)


def test_ip_routerboard():
    assert svc.ip_routerboard("10.55.3.44") == "10.55.0.1"
    assert svc.ip_routerboard("192.168.20.7") == "10.168.0.1"
    assert svc.ip_routerboard("") == ""
    assert svc.ip_routerboard("no-es-una-ip") == ""


def test_ip_valida_es_frontera_de_confianza():
    assert red.ip_valida(" 10.0.0.1 ") == "10.0.0.1"
    # Nada que no sea una IP llega nunca a un subproceso.
    for basura in ("10.0.0.1; rm -rf /", "$(whoami)", "`id`", "--flag", None, ""):
        assert red.ip_valida(basura) == ""


def test_regex_ip_escapa_los_puntos():
    # Sin escapar, 10.1.1.1 como patrón matchearía 10X1Y1Z1.
    assert svc._regex_ip("10.1.1.1") == r"10\.1\.1\.1"


def _consultas():
    return {n: v for n, v in vars(q).items()
            if n.startswith("Q_") and isinstance(v, str)}


def test_ninguna_consulta_interpola_variables():
    """Las queries del documento traían $code, $ip, $oltip y $nap pegados al
    string. Si alguna vuelve, esto falla."""
    for nombre, sql in _consultas().items():
        for marcador in ("$code", "$ip", "$oltip", "$nap", "$__unixEpoch"):
            assert marcador not in sql, f"{nombre} volvió a interpolar {marcador}"
        assert "'%s'" not in sql, f"{nombre} tiene un %s entre comillas"
        assert not re.search(r"\{.*\}", sql), f"{nombre} tiene un placeholder sin resolver"


def test_cantidad_de_parametros():
    """Cada consulta tiene que recibir exactamente los %s que declara."""
    esperado = {
        "Q_GESTION_CLIENTE": 3,   # nro_cliente + los dos category_id
        "Q_ZBX_TOPOLOGIA_FTTH": 1,
        "Q_SOLDEF_SWITCH": 1,
        "Q_ZBX_ESTADO_CLIENTE": 1,
        "Q_ZBX_ESTADO_NAP": 1,
        "Q_ZBX_OID_LOS_CLIENTE": 1,
        "Q_ZBX_OID_ONLINE_CLIENTE": 1,
        "Q_ZBX_OIDS_LOS_NAP": 1,
        "Q_ZBX_OCUPACION_NAP": 1,
        "Q_ZBX_WIFI_AP": 1,
        "Q_ZBX_WIFI_RB": 1,
    }
    consultas = _consultas()
    assert set(consultas) == set(esperado), "hay consultas sin contemplar acá"
    for nombre, cantidad in esperado.items():
        real = consultas[nombre].count("%s")
        assert real == cantidad, f"{nombre}: {real} placeholders, se esperaban {cantidad}"


def _snmpget_falso(tmp):
    """snmpget de mentira: imprime '<oid> 2' por cada OID que recibe, en el mismo
    formato que `snmpget -Oqn`."""
    ruta = os.path.join(tmp, "snmpget")
    with open(ruta, "w") as f:
        f.write(
            "#!/bin/sh\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in [0-9]*.[0-9]*) echo \".$a 2\";; esac\n"
            "done\n"
        )
    os.chmod(ruta, 0o755)
    return ruta


def test_snmpget_lee_varios_oids_en_una_invocacion():
    """Un solo subproceso para todos los OIDs, y el resultado mapeado por OID y
    no por posición: si la OLT omite un varbind, no se corren los valores."""
    community, ruta = config.SNMP_COMMUNITY, config.SNMPGET_PATH
    tmp = tempfile.mkdtemp()
    config.SNMP_COMMUNITY, config.SNMPGET_PATH = "prueba", _snmpget_falso(tmp)
    try:
        pedidos = ["1.3.6.1.4.1.2011.1", "1.3.6.1.4.1.2011.2", "1.3.6.1.4.1.2011.3"]
        valores = red.snmpget("10.0.0.1", pedidos)
        assert valores == {o: "2" for o in pedidos}, valores

        # Los OIDs invalidos se filtran antes del subproceso y no rompen al resto.
        mezcla = ["{#SNMPINDEX}", "1.3.6.1.4.1.2011.1", "1.3.6.1; id", None, "..1"]
        assert red.snmpget("10.0.0.1", mezcla) == {"1.3.6.1.4.1.2011.1": "2"}

        # Ningun OID valido: no se ejecuta nada.
        assert red.snmpget("10.0.0.1", ["{#SNMPINDEX}", ""]) == {}
        # Host que no es una IP: no se ejecuta nada.
        assert red.snmpget("10.0.0.1; rm -rf /", pedidos) == {}
    finally:
        config.SNMP_COMMUNITY, config.SNMPGET_PATH = community, ruta
        shutil.rmtree(tmp, ignore_errors=True)


def test_cache_respeta_el_ttl():
    llamadas = []
    c = CacheTTL(ttl_seg=60, nombre="test")
    assert c.obtener("k", lambda: llamadas.append(1) or "v") == "v"
    assert c.obtener("k", lambda: llamadas.append(1) or "otro") == "v"
    assert len(llamadas) == 1

    # None es un resultado valido: repetir un SNMP que ya dio timeout no lo
    # vuelve mas evaluable, y reintentar seria justo lo que hay que evitar.
    c.obtener("n", lambda: None)
    assert c.obtener("n", lambda: "recalculado") is None

    # TTL vencido: se recalcula.
    vencido = CacheTTL(ttl_seg=-1, nombre="test")
    assert vencido.obtener("k", lambda: "a") == "a"
    assert vencido.obtener("k", lambda: "b") == "b"


def test_cache_single_flight():
    """La mitad que importa. Las consultas de un corte llegan JUNTAS: con un TTL
    a secas fallan las N a la vez y salen las N a preguntar."""
    calculos = []
    c = CacheTTL(ttl_seg=60, nombre="test")
    barrera = threading.Barrier(8)

    def calcular():
        calculos.append(1)
        time.sleep(0.05)          # ensancha la ventana de colision
        return "valor"

    def hilo():
        barrera.wait()
        assert c.obtener("misma-nap", calcular) == "valor"

    hilos = [threading.Thread(target=hilo) for _ in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert len(calculos) == 1, f"se calculo {len(calculos)} veces, deberia ser 1"


def test_cache_no_guarda_excepciones():
    """PoolAgotado tiene que seguir cortando el request en la proxima consulta,
    no quedar cacheado 45 segundos."""
    import db

    c = CacheTTL(ttl_seg=60, nombre="test")
    for _ in range(2):
        try:
            c.obtener("k", lambda: (_ for _ in ()).throw(db.PoolAgotado("x")))
            raise AssertionError("deberia haber propagado")
        except db.PoolAgotado:
            pass
    assert c.obtener("k", lambda: "ok") == "ok"


def test_pool_agotado_corta_el_request():
    """Quedarse sin conexiones no es "no se pudo verificar": es no haber
    preguntado. Si se tragara como None, la API devolveria 200 con una respuesta
    calculada sobre datos que nunca se consultaron."""
    import db

    def falla():
        raise db.PoolAgotado("sin conexiones libres a 'zabbix'")

    # Una verificacion cualquiera que falla NO corta: eso es degradacion sana.
    r = svc._en_paralelo({"a": lambda: 1, "b": lambda: 1 / 0})
    assert r == {"a": 1, "b": None}

    # El pool agotado si corta, y llega al router como 503.
    try:
        svc._en_paralelo({"a": lambda: 1, "b": falla})
        raise AssertionError("deberia haber propagado PoolAgotado")
    except db.PoolAgotado:
        pass
    assert issubclass(db.PoolAgotado, db.DatabaseUnavailable)


def test_api_keys_por_consumidor():
    """La clave deja de ser un secreto compartido anonimo: cada consumidor tiene
    la suya, y el nombre es lo que identifica quien llama en el log y lo que el
    rate limit usa como unidad."""
    import security

    crudo, legacy = config.API_KEYS_CRUDO, config.API_KEY_SECRETA
    try:
        config.API_KEYS_CRUDO = "facturacion:clave-a, soporte:clave-b ,  , mal-formada"
        config.API_KEY_SECRETA = "clave-vieja"
        claves = security._cargar_claves()
        assert claves == {
            "clave-a": "facturacion",
            "clave-b": "soporte",
            # La clave unica anterior sigue andando: migrar no puede ser un corte.
            "clave-vieja": "legacy",
        }, claves

        # Una clave puede contener ':'; el nombre no.
        config.API_KEYS_CRUDO = "app:tok:en:largo"
        config.API_KEY_SECRETA = ""
        assert security._cargar_claves() == {"tok:en:largo": "app"}

        # Sin nada configurado, falla cerrado.
        config.API_KEYS_CRUDO = ""
        assert security._cargar_claves() == {}
    finally:
        config.API_KEYS_CRUDO, config.API_KEY_SECRETA = crudo, legacy


def test_rate_limit_por_consumidor():
    from security import CubetaDeTokens

    cubeta = CubetaDeTokens(por_minuto=60, burst=3)
    # La rafaga entra completa...
    assert [cubeta.consumir("a")[0] for _ in range(3)] == [True, True, True]
    # ...y despues corta, con un tiempo de espera util para el Retry-After.
    permitido, espera = cubeta.consumir("a")
    assert permitido is False and 0 < espera <= 1

    # Un consumidor desbocado no le consume la cuota a los demas.
    assert cubeta.consumir("b")[0] is True

    # En 0 se desactiva.
    assert all(CubetaDeTokens(0, 1).consumir("x")[0] for _ in range(50))


if __name__ == "__main__":
    pruebas = [v for n, v in sorted(vars().items())
               if n.startswith("test_") and callable(v)]
    fallos = 0
    for prueba in pruebas:
        try:
            prueba()
            print(f"  ok   {prueba.__name__}")
        except AssertionError as e:
            fallos += 1
            print(f"  FALLA {prueba.__name__}: {e}")
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasaron")
    sys.exit(1 if fallos else 0)
