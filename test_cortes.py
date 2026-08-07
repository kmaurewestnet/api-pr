"""Chequeo del endpoint de cortes. Sin framework: `python test_cortes.py`.

Cubre lo que puede romperse en silencio: la matriz de decisión del punto 4 del
documento, el umbral de corte de NAP, la interpretación de los valores de LOS y
que ninguna consulta haya vuelto a la interpolación de strings.
"""
import re
import sys

import config

from queries import cortes as q
from services import cortes as svc
from services import red

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
    # Texto que guarda Zabbix.
    assert svc.hay_los("LOS") is True
    assert svc.hay_los("los-i") is True
    assert svc.hay_los("No Alarm") is False
    assert svc.hay_los("Normal") is False
    # Entero que devuelve un snmpget directo a la OLT.
    assert svc.hay_los("1") is True
    assert svc.hay_los("0") is False
    # Sin dato no es lo mismo que sin alarma.
    assert svc.hay_los(None) is None
    assert svc.hay_los("   ") is None


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


def test_oid_invalido_no_llega_a_snmpget():
    """Los items de Zabbix pueden traer macros sin resolver en snmp_oid.

    Se apunta SNMPGET_PATH a /bin/echo: si un OID inválido llegara a ejecutarse,
    echo devolvería 0 con salida y snmpget() no daría None. Es decir, la prueba
    distingue "se rechazó antes de ejecutar" de "no se pudo ejecutar".
    """
    community, ruta = config.SNMP_COMMUNITY, config.SNMPGET_PATH
    config.SNMP_COMMUNITY, config.SNMPGET_PATH = "publico-de-prueba", "/bin/echo"
    try:
        for basura in ("{#SNMPINDEX}", "1.3.6.1; id", "", None, "1.3.6.1.x", "..1"):
            assert red.snmpget("10.0.0.1", basura) is None, basura
        # Control: con un OID bien formado sí se ejecuta el binario.
        assert red.snmpget("10.0.0.1", "1.3.6.1.4.1.2011.1") is not None
    finally:
        config.SNMP_COMMUNITY, config.SNMPGET_PATH = community, ruta


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
