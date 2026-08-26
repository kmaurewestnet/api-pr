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
import db

from queries import cortes as q
from services import cortes as svc
from services import cortes_io as io
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
    from services.cortes_reglas import TablaSnmp

    # Sin traduccion configurada: no se afirma nada.
    vacia = TablaSnmp(los=(), sin_los=(), offline=(), online=())
    assert vacia.hay_los("1") is None
    assert vacia.hay_los("2") is None
    assert vacia.hay_los("0") is None

    # Con la traduccion cargada, cada codigo cae donde corresponde.
    parque = TablaSnmp(los={2}, sin_los={1}, offline={2}, online={1})
    assert parque.hay_los("2") is True
    assert parque.hay_los("1") is False
    assert parque.hay_los("7") is None      # codigo fuera de las dos listas
    assert parque.esta_offline("2") is True
    assert parque.esta_offline("1") is False
    assert parque.esta_offline("9") is None


def test_una_olt_de_otro_vendor_se_lee_con_su_tabla():
    """Para esto existen los cuatro SNMP_COD_*: una OLT donde 1 es la alarma y 2
    la lectura normal. El mismo valor tiene que dar lo contrario segun la tabla,
    y el texto ya mapeado por Zabbix no depende de ninguna de las dos."""
    from services.cortes_reglas import TablaSnmp

    parque = TablaSnmp(los={2}, sin_los={1}, offline={2}, online={1})
    invertida = TablaSnmp(los={1}, sin_los={2}, offline={1}, online={2})

    assert parque.hay_los("1") is False and invertida.hay_los("1") is True
    assert parque.hay_los("2") is True and invertida.hay_los("2") is False
    assert parque.esta_offline("1") is False and invertida.esta_offline("1") is True

    # Y la ONT combinada las sigue: una sola señal en alarma alcanza.
    assert parque.ont_caida("1", "1") is False
    assert invertida.ont_caida("1", "1") is True
    assert invertida.ont_caida("2", "1") is True    # solo OnlineState en alarma

    # El texto que ya mapeo Zabbix se lee igual con cualquier tabla.
    for tabla in (parque, invertida):
        assert tabla.hay_los("LOS") is True
        assert tabla.hay_los("No Alarm") is False
        assert tabla.esta_offline("Offline") is True


def test_ont_solar_combina_los_y_onlinestate():
    """El documento actualizado agrega la consulta de OIDs de OnlineState: una
    sola señal en alarma alcanza para dar la ONT por caida."""
    lecturas = {}
    fuente = io.FuenteReal()
    orig_valores, orig_interpretar = io._valores_snmp, io._interpretar
    io._valores_snmp = lambda ip, oids: {}
    io._interpretar = lambda oids, val, interprete, metrica, ip: lecturas.get(metrica, [])
    try:
        lecturas = {"LOS": [False], "OnlineState": [False]}
        assert fuente.ont_por_snmp("10.0.0.1", ["1.1"], ["1.2"]) is False
        lecturas = {"LOS": [False], "OnlineState": [True]}
        assert fuente.ont_por_snmp("10.0.0.1", ["1.1"], ["1.2"]) is True
        lecturas = {"LOS": [True], "OnlineState": []}
        assert fuente.ont_por_snmp("10.0.0.1", ["1.1"], []) is True
        # Ninguna de las dos se pudo interpretar: no evaluable, no "caida".
        lecturas = {}
        assert fuente.ont_por_snmp("10.0.0.1", ["1.1"], ["1.2"]) is None
    finally:
        io._valores_snmp, io._interpretar = orig_valores, orig_interpretar


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

    # TTL vencido: se recalcula. (ttl <= 0 es el interruptor de "desactivado",
    # no "siempre vencido": para simular vencimiento hace falta un TTL real.)
    corto = CacheTTL(ttl_seg=0.01, nombre="test")
    assert corto.obtener("k", lambda: "a") == "a"
    time.sleep(0.05)
    assert corto.obtener("k", lambda: "b") == "b"

    # Y en 0 se desactiva de verdad.
    apagado = CacheTTL(ttl_seg=0, nombre="test")
    assert apagado.obtener("k", lambda: "a") == "a"
    assert apagado.obtener("k", lambda: "b") == "b"


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


def test_cache_sirve_el_valor_viejo_si_el_recalculo_falla():
    """El escenario que esto existe para evitar: durante un corte real, la
    consulta de NAP se pasa del statement_timeout, nap_caida queda en None y
    -con la OLT respondiendo- isZoneIncident pasa a false con 200 OK."""
    c = CacheTTL(ttl_seg=0.01, nombre="test", stale_max_seg=300)

    def vencido():
        time.sleep(0.05)

    assert c.obtener("nap", lambda: True) is True     # medicion real
    vencido()
    assert c.obtener("nap", lambda: None) is True     # sin dato -> se sirve el viejo
    vencido()
    assert c.obtener("nap", lambda: 1 / 0) is True    # error    -> se sirve el viejo
    vencido()
    assert c.obtener("nap", lambda: False) is False   # dato nuevo -> manda el nuevo
    vencido()
    assert c.obtener("nap", lambda: None) is False

    # Sin valor previo utilizable no hay nada que servir: el error se propaga.
    try:
        c.obtener("otra", lambda: 1 / 0)
        raise AssertionError("deberia haber propagado")
    except ZeroDivisionError:
        pass
    # ...y un None sin previo sigue siendo None.
    assert c.obtener("otra", lambda: None) is None


def test_cache_no_sirve_valores_viejos_para_siempre():
    """Pasado stale_max se acepta el 'sin dato': una NAP que dejo de reportar no
    puede quedar marcada como caida indefinidamente."""
    c = CacheTTL(ttl_seg=0.01, nombre="test", stale_max_seg=0)
    assert c.obtener("nap", lambda: True) is True
    time.sleep(0.05)
    assert c.obtener("nap", lambda: None) is None


def test_cache_no_guarda_excepciones():
    """PoolAgotado tiene que seguir cortando el request en la proxima consulta,
    no quedar cacheado 45 segundos."""
    import db

    c = CacheTTL(ttl_seg=60, nombre="test", stale_max_seg=300)
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


def test_precinto_no_compila_el_parametro_como_regex():
    """El precinto lo elige quien llama. Con `~*` no elegia el valor a buscar
    sino el patron: `^` devolvia el parque entero de las 17 empresas en un solo
    request y `(` reventaba la consulta con un 500."""
    from queries import precinto as qp

    consultas = {n: v for n, v in vars(qp).items()
                 if n.startswith("QUERY_") and isinstance(v, str)}
    esperado = {"QUERY_ONU_RX": 4, "QUERY_OLT_RX": 4,
                "QUERY_LOGS": 2, "QUERY_ESTADO": 2}
    assert set(consultas) == set(esperado), sorted(consultas)

    for nombre, sql in consultas.items():
        # Ningun operador de regex puede recibir el parametro del usuario.
        assert not re.search(r"~\*?\s*%s", sql), f"{nombre} matchea %s como regex"
        assert "strpos(upper(" in sql, f"{nombre} no compara el precinto literal"
        assert "LIMIT %s" in sql, f"{nombre} quedo sin tope de filas"
        assert "'%s'" not in sql, f"{nombre} tiene un %s entre comillas"
        real = sql.count("%s")
        assert real == esperado[nombre], f"{nombre}: {real} placeholders"


def test_limite_propio_por_consumidor():
    """Los consumidores de /cortes no se parecen entre si: una centralita hace un
    request por llamada atendida y un chatbot con entrada de usuario puede entrar
    en loop, y ese loop lo paga la OLT."""
    import security

    crudo = config.RATE_LIMIT_POR_CONSUMIDOR_CRUDO
    try:
        config.RATE_LIMIT_POR_CONSUMIDOR_CRUDO = (
            " chatbot:2 , centralita:0 ,  , sin-dos-puntos , malo:x , negativo:-1 "
        )
        limites = security._cargar_limites({"k1": "chatbot", "k2": "centralita"})
        # El negativo se descarta y no se toma literal: una tasa <= 0 en la
        # cubeta significa "sin limite", o sea lo contrario de lo que se quiso.
        assert limites == {"chatbot": 2, "centralita": 0}, limites

        cubeta = security.CubetaDeTokens(por_minuto=60, burst=60, propios=limites)
        # El 429 tiene que poder nombrar el limite real, no el general.
        assert cubeta.por_minuto_de("chatbot") == 2
        assert cubeta.por_minuto_de("soporte") == 60

        # El chatbot corta a las 2...
        assert [cubeta.consumir("chatbot")[0] for _ in range(3)] == [True, True, False]
        # ...sin tocarle la cuota a quien no tiene una propia.
        assert cubeta.consumir("soporte")[0] is True
        # 0 es "sin limite", no "bloqueado".
        assert all(cubeta.consumir("centralita")[0] for _ in range(100))
    finally:
        config.RATE_LIMIT_POR_CONSUMIDOR_CRUDO = crudo


def test_cortes_y_endpoints_internos_no_comparten_cuota():
    """Dos cubetas y no una: agotar la de analiticas no puede dejar sin atender
    a la centralita, que es la que tiene gente esperando del otro lado."""
    import security
    from fastapi import HTTPException

    previo = security._limite_interno
    try:
        security._limite_interno = security.CubetaDeTokens(por_minuto=60, burst=1)
        assert security.limitar_tasa_interna("noc") == "noc"
        try:
            security.limitar_tasa_interna("noc")
            assert False, "el segundo request interno tendria que dar 429"
        except HTTPException as e:
            assert e.status_code == 429, e.status_code
            assert e.headers.get("Retry-After"), "el 429 va con Retry-After"

        # La cuota interna quedo agotada, pero /cortes descuenta de la suya.
        assert security.limitar_tasa("noc") == "noc"
    finally:
        security._limite_interno = previo


def test_consumidores_externos_solo_llegan_a_cortes():
    """La centralita y el chatbot son externos: tienen clave propia para /cortes,
    pero precinto y analiticas devuelven el parque de todas las empresas, que es
    la vista interna del NOC."""
    import security
    from fastapi import HTTPException

    crudo, legacy = config.API_KEYS_CRUDO, config.API_KEY_SECRETA
    externos = config.CONSUMIDORES_SOLO_CORTES
    claves_previas, alcance_previo = security._CLAVES, security._SOLO_CORTES
    try:
        config.API_KEYS_CRUDO = "noc:clave-noc,chatbot:clave-bot"
        config.API_KEY_SECRETA = ""
        config.CONSUMIDORES_SOLO_CORTES = " chatbot , "
        security._CLAVES = security._cargar_claves()
        security._SOLO_CORTES = security._cargar_solo_cortes(security._CLAVES)
        assert security._SOLO_CORTES == {"chatbot"}, security._SOLO_CORTES

        # Los dos entran por la puerta comun: las dos claves son validas, y por
        # esa puerta pasa /cortes.
        assert security.verificar_api_key("clave-noc") == "noc"
        assert security.verificar_api_key("clave-bot") == "chatbot"

        # Pero solo el interno atraviesa la de precinto y analiticas.
        assert security.verificar_api_key_interna("noc") == "noc"
        try:
            security.verificar_api_key_interna("chatbot")
            assert False, "el consumidor externo tendria que recibir 403"
        except HTTPException as e:
            assert e.status_code == 403, e.status_code

        # Un nombre mal escrito no restringe a nadie: por eso se valida contra
        # las claves cargadas y se loguea. Si esto deja de avisar, el proximo
        # consumidor externo queda abierto sin que nadie se entere.
        config.CONSUMIDORES_SOLO_CORTES = "chatbott"
        assert security._cargar_solo_cortes(security._CLAVES) == {"chatbott"}

        # Sin la variable no cambia nada de lo que ya andaba.
        config.CONSUMIDORES_SOLO_CORTES = ""
        assert security._cargar_solo_cortes(security._CLAVES) == frozenset()
    finally:
        config.API_KEYS_CRUDO, config.API_KEY_SECRETA = crudo, legacy
        config.CONSUMIDORES_SOLO_CORTES = externos
        security._CLAVES, security._SOLO_CORTES = claves_previas, alcance_previo


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


# --- El cableado de las dos tandas -------------------------------------------
#
# Las reglas puras de arriba se prueban con argumentos posicionales, asi que un
# error en el armado de `tareas`/`tareas2` —una clave renombrada, una señal que
# nunca se pide— las deja pasar igual. Estas pruebas manejan `_evaluar_ftth` y
# `_evaluar_wireless` enteros contra una fuente en memoria: lo que se verifica es
# el camino de cada señal desde la fuente hasta su lugar en la matriz.

TOPO_FTTH = {"nap": "GLL-2763", "olt_nombre": "OLT-CENTRO", "olt_ip": "10.20.0.5"}
TOPO_SOLAR = {"nap": "GLL-90", "olt_nombre": "OLT-SOLAR-3", "olt_ip": "10.20.0.9"}
TOPO_SIN_ONT = {"nap": None, "olt_nombre": None, "olt_ip": ""}
CLIENTE_FTTH = {"ip": "192.168.5.20", "is_ftth": True,
                "nro_cliente": "302381", "categoria": "FTTH", "categoria_id": 16}
CLIENTE_WIFI = {"ip": "10.55.3.44", "is_ftth": False,
                "nro_cliente": "5001", "categoria": "Wireless", "categoria_id": 17}


class FuenteFalsa:
    """Fuente en memoria. Devuelve la señal pedida y anota que se la pidieron.

    Un valor que sea una excepcion se levanta en vez de devolverse: con eso se
    prueban tanto la degradacion a "no evaluable" como el corte por PoolAgotado.
    Una señal que no se declara vale None, que es justo el caso peligroso.
    """

    def __init__(self, **señales):
        self.señales = señales
        self.pedidos = []

    def _dar(self, nombre):
        self.pedidos.append(nombre)
        valor = self.señales.get(nombre)
        if isinstance(valor, Exception):
            raise valor
        return valor

    def ping_cliente(self, ip):
        return self._dar("ping_cliente")

    def ping_zona(self, ip, rol):
        return self._dar(f"ping_{rol}")

    def topologia_ftth(self, nro_cliente):
        return self._dar("topologia_ftth")

    def topologia_wireless(self, ip):
        return self._dar("topologia_wireless")

    def switch_del_nodo(self, olt_ip):
        return self._dar("switch")

    def estado_nap(self, olt_ip, nap, es_solar):
        return self._dar("nap")

    def estado_ont(self, nro_cliente):
        return self._dar("ont")

    def oids_solar(self, nro_cliente):
        return self._dar("oids")

    def ont_por_snmp(self, olt_ip, oids_los, oids_online):
        return self._dar("ont_snmp")


def _ftth(**señales):
    señales.setdefault("topologia_ftth", TOPO_FTTH)
    fuente = FuenteFalsa(**señales)
    return svc._evaluar_ftth("302381", CLIENTE_FTTH, fuente), fuente


def test_evaluar_ftth_recorre_la_matriz_completa():
    """Misma matriz que test_matriz_*, pero entrando por donde entra el request:
    si una clave de `tareas` deja de coincidir con la que lee `decidir_ftth`,
    esto rompe y aquellas no."""
    base = dict(ping_cliente=RESPONDE, ont=False, nap=False, ping_olt=RESPONDE,
                switch={"nombre": "sw", "ip": "10.20.0.2"}, ping_switch=RESPONDE)

    r, _ = _ftth(**base)
    assert r == {"isFtth": True, "isOnline": True, "isZoneIncident": False}

    # Ping caido pero la ONT no reporta alarma: sigue online.
    r, _ = _ftth(**{**base, "ping_cliente": FALLA, "ont": SIN_DATO})
    assert r["isOnline"] is False
    r, _ = _ftth(**{**base, "ping_cliente": FALLA, "ont": False})
    assert r["isOnline"] is True

    # Zona: la NAP caida o la OLT sin responder, cada una por su lado.
    r, _ = _ftth(**{**base, "nap": True})
    assert r["isZoneIncident"] is True
    r, _ = _ftth(**{**base, "ping_olt": FALLA})
    assert r["isZoneIncident"] is True
    # El ping al switch se ejecuta pero no cambia el resultado.
    r, fuente = _ftth(**{**base, "ping_switch": FALLA})
    assert r["isZoneIncident"] is False
    assert "ping_switch" in fuente.pedidos


def test_evaluar_ftth_solar_toma_la_ont_de_la_segunda_tanda():
    """En Solar la ONT no sale de una consulta sino del snmpget de la segunda
    tanda: son dos caminos distintos hasta la misma clave."""
    r, fuente = _ftth(topologia_ftth=TOPO_SOLAR, ping_cliente=FALLA,
                      oids={"oids_los": ["1.1"], "oids_online": ["1.2"]},
                      ont_snmp=False, nap=False, ping_olt=RESPONDE)
    assert r["isOnline"] is True          # la ONT vino por SNMP y no reporta alarma
    assert "oids" in fuente.pedidos       # se pidieron los OIDs de este cliente...
    assert "ont_snmp" in fuente.pedidos   # ...y se preguntó a la OLT
    assert "ont" not in fuente.pedidos    # y NO se usó el camino por consulta

    # Y el no-Solar es el espejo exacto.
    _, fuente = _ftth(ont=False, nap=False, ping_olt=RESPONDE)
    assert "ont" in fuente.pedidos
    assert "oids" not in fuente.pedidos and "ont_snmp" not in fuente.pedidos


def test_pool_agotado_corta_el_request_entero():
    """Quedarse sin conexiones no es "la red no contestó": tiene que salir por
    503 y no calcularse una respuesta sobre datos que nunca se consultaron."""
    try:
        _ftth(ping_cliente=RESPONDE, nap=db.PoolAgotado("sin conexiones"))
        assert False, "PoolAgotado tenia que propagarse"
    except db.PoolAgotado:
        pass

    # Y sigue saliendo cuando se entra por detectar(), que es el camino real.
    original = svc.buscar_cliente
    svc.buscar_cliente = lambda nro: CLIENTE_FTTH
    try:
        fuente = FuenteFalsa(topologia_ftth=TOPO_FTTH,
                             ping_olt=db.PoolAgotado("sin conexiones"))
        try:
            svc.detectar("302381", fuente)
            assert False, "PoolAgotado tenia que propagarse desde detectar()"
        except db.PoolAgotado:
            pass
    finally:
        svc.buscar_cliente = original


def test_una_verificacion_que_falla_queda_no_evaluable():
    """Cualquier otra excepcion degrada esa señal a None y no arrastra al resto:
    None no es False, asi que no cuenta como caida en la matriz."""
    r, _ = _ftth(ping_cliente=RESPONDE, ont=RuntimeError("zabbix lento"),
                 nap=RuntimeError("timeout"), ping_olt=RESPONDE)
    assert r == {"isFtth": True, "isOnline": True, "isZoneIncident": False}

    # Soldef caida: se pierde el ping al switch y nada mas.
    r, _ = _ftth(ping_cliente=RESPONDE, ont=False, nap=False, ping_olt=RESPONDE,
                 switch=RuntimeError("soldef no responde"))
    assert r["isZoneIncident"] is False


def test_topologia_caida_no_degrada_a_doscientos():
    """Sin topologia, isZoneIncident seria inventado: es 503, no 200."""
    for error in (db.DatabaseUnavailable("zabbix"), RuntimeError("lo que sea")):
        try:
            _ftth(topologia_ftth=error)
            assert False, f"{type(error).__name__} tenia que propagarse"
        except db.DatabaseUnavailable:
            pass


def test_evaluar_wireless_recorre_su_matriz():
    def wifi(**señales):
        señales.setdefault("topologia_wireless",
                           {"ap": "AP-1", "ap_ip": "10.55.0.1",
                            "rb": "RB-1", "rb_ip": "10.55.0.254"})
        fuente = FuenteFalsa(**señales)
        return svc._evaluar_wireless("5001", CLIENTE_WIFI, fuente), fuente

    r, _ = wifi(ping_cliente=RESPONDE, ping_ap=RESPONDE, ping_routerboard=RESPONDE)
    assert r == {"isFtth": False, "isOnline": True, "isZoneIncident": False}
    # En wireless isOnline es solo el ping al cliente.
    r, _ = wifi(ping_cliente=FALLA, ping_ap=RESPONDE, ping_routerboard=RESPONDE)
    assert r["isOnline"] is False
    # Zona: la infraestructura compartida del nodo, cualquiera de las dos.
    r, _ = wifi(ping_cliente=RESPONDE, ping_ap=FALLA, ping_routerboard=RESPONDE)
    assert r["isZoneIncident"] is True
    r, _ = wifi(ping_cliente=RESPONDE, ping_ap=RESPONDE, ping_routerboard=FALLA)
    assert r["isZoneIncident"] is True
    # Sin dato no es lo mismo que sin responder.
    r, _ = wifi(ping_cliente=RESPONDE)
    assert r["isZoneIncident"] is False


def test_cliente_sin_ont_en_zabbix_se_evalua_por_ping():
    """Degradacion documentada: sin NAP ni OLT la respuesta sale igual, con las
    señales de zona en no evaluable."""
    r, fuente = _ftth(topologia_ftth=TOPO_SIN_ONT, ping_cliente=FALLA, ont=True)
    assert r == {"isFtth": True, "isOnline": False, "isZoneIncident": False}
    # Se piden igual, y la fuente decide que no hay nada que consultar.
    assert "nap" in fuente.pedidos and "ping_olt" in fuente.pedidos


def test_solo_las_senales_de_zona_se_cachean():
    """El caché es lo que evita que 50 clientes de la misma caja disparen 50
    veces el mismo walk SNMP durante un corte. Pero cachear una señal por cliente
    sería servirle a uno la respuesta de otro: la diferencia está en la interfaz
    de la fuente, y esta prueba la fija."""
    cache = CacheTTL(60, 10, nombre="prueba")
    fuente = io.FuenteReal(cache=cache)
    pedidos = []
    original = io.red.ping
    io.red.ping = lambda ip, etiqueta="": pedidos.append(etiqueta) or True
    try:
        # Infraestructura de la caja: se pregunta una vez y se reparte.
        assert fuente.ping_zona("10.20.0.5", "olt") is True
        assert fuente.ping_zona("10.20.0.5", "olt") is True
        assert pedidos.count("olt") == 1
        # Otra IP es otra clave: no se reusa la respuesta de la primera.
        assert fuente.ping_zona("10.20.0.2", "switch") is True
        assert pedidos.count("switch") == 1
        # La respuesta puntual del cliente se pregunta siempre.
        assert fuente.ping_cliente("192.168.5.20") is True
        assert fuente.ping_cliente("192.168.5.20") is True
        assert pedidos.count("cliente") == 2
    finally:
        io.red.ping = original

    # Y el caché de produccion quedo intacto: la prueba uso el suyo.
    assert io._zona._valores == {}


# --- Precinto ----------------------------------------------------------------

def test_nombre_cliente_sale_de_las_series_de_rx():
    """El nombre viaja en el nombre del item, asi que solo aparece si alguna de
    las dos series de RX trajo lecturas: logs y estado no lo seleccionan."""
    from services import precinto as prec

    assert prec.nombre_cliente([{"cliente": "JES0037"}], []) == "JES0037"
    # Sin ONU RX cae a la serie de la OLT.
    assert prec.nombre_cliente([], [{"cliente": "JES0099"}]) == "JES0099"
    # Ninguna de las dos trajo lecturas: el precinto puede no existir.
    assert prec.nombre_cliente([], []) == "No identificado"
    # La fila vino pero la columna es nula: `cliente` esta declarado str.
    assert prec.nombre_cliente([{"cliente": None}], []) == "No identificado"


def test_la_respuesta_de_precinto_cumple_su_esquema_publicado():
    """El ejemplo de /docs y el modelo tienen que decir lo mismo: hasta ahora la
    unica documentacion de la forma de salida era el ejemplo, sin nada que lo
    atara a lo que el endpoint devuelve."""
    import datetime

    from models import PrecintoResponse
    from routers.precinto import EJEMPLO_RESPUESTA
    from services import precinto as prec

    PrecintoResponse(**EJEMPLO_RESPUESTA)

    # Y lo que arma el modulo tambien, con un `time` de log tal como lo entrega
    # el driver: un datetime, no el string del ejemplo.
    payload = prec.armar_respuesta(
        "JES0037", 6, 1785158427, 1785180027,
        ([{"cliente": "JES0037", "onu_rx": "-21.35", "time": 1785179940}], [],
         [{"log": "Dying-gasp", "time": datetime.datetime(2026, 7, 15, 3, 11, 11)}], []),
    )
    assert payload["metadata"]["cliente"] == "JES0037"
    assert PrecintoResponse(**payload).model_dump_json()

    # Un precinto sin lecturas es 200 con las cuatro listas vacias.
    vacio = prec.armar_respuesta("NO-EXISTE", 6, 1, 2, ([], [], [], []))
    assert vacio["metadata"]["cliente"] == "No identificado"
    assert PrecintoResponse(**vacio).metricas.onu_rx == []


def test_toda_respuesta_de_error_documenta_su_cuerpo():
    """La forma `{"detail": ...}` se arma con models.error() y no a mano en cada
    decorador. Esto vigila la entrada SIGUIENTE: una que se escriba literal se
    olvida del `model`, del ejemplo, o de los dos, y /docs la publica vacia."""
    import main

    esquema = main.app.openapi()
    revisadas, sin_ejemplo = 0, []
    for ruta, operaciones in esquema["paths"].items():
        for metodo, operacion in operaciones.items():
            for codigo, respuesta in operacion.get("responses", {}).items():
                if not codigo.startswith(("4", "5")):
                    continue
                cuerpo = (respuesta.get("content") or {}).get("application/json") or {}
                ref = (cuerpo.get("schema") or {}).get("$ref", "")
                # El 422 generico lo genera FastAPI con su propio modelo.
                if ref.endswith("HTTPValidationError"):
                    continue
                revisadas += 1
                if "examples" in cuerpo:
                    casos = [c["value"] for c in cuerpo["examples"].values()]
                elif "example" in cuerpo:
                    casos = [cuerpo["example"]]
                else:
                    sin_ejemplo.append(f"{metodo.upper()} {ruta} -> {codigo}")
                    continue
                for caso in casos:
                    assert "detail" in caso, f"{ruta} {codigo}: el ejemplo no trae detail"

    assert not sin_ejemplo, f"respuestas de error sin ejemplo: {sin_ejemplo}"
    # Y que no pase en vacio si alguien rompe el recorrido del esquema.
    assert revisadas >= 15, f"solo se revisaron {revisadas} respuestas de error"


def test_el_presupuesto_del_pool_de_zabbix_avisa_cuando_no_cierra():
    """La relacion entre CORTES_MAX_CONCURRENTES y POOL_MAX vivia en un
    comentario de config.py que nombraba un solo consumidor y traia un numero
    desactualizado. Ahora cada modulo declara el suyo y esto verifica la cuenta."""
    import main

    # Los defaults del repo cierran justo en el borde: 5 x 2 = 10 = POOL_MAX.
    assert main.revisar_presupuesto_zabbix(10, 5, 2) is None
    assert main.revisar_presupuesto_zabbix(20, 5, 2) is None

    # Un escalon mas de admision ya no entra, y el aviso dice a cuanto bajar.
    aviso = main.revisar_presupuesto_zabbix(10, 6, 2)
    assert aviso and "CORTES_MAX_CONCURRENTES a 5" in aviso

    # Y si cortes pasara a tomar tres conexiones, el tope actual tampoco entra.
    assert main.revisar_presupuesto_zabbix(10, 5, 3) is not None

    # Los tres consumidores estan declarados por su propio modulo.
    assert set(main.CONSUMIDORES_ZABBIX) == {"cortes", "analytics", "precinto"}
    assert all(c >= 1 for c in main.CONSUMIDORES_ZABBIX.values())

    # Y la configuracion que se va a desplegar tiene que cerrar.
    assert main.revisar_presupuesto_zabbix(
        config.POOL_MAX,
        config.CORTES_MAX_CONCURRENTES,
        main.CONSUMIDORES_ZABBIX["cortes"],
    ) is None, "los defaults del repo no cierran"


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
