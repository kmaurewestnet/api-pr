"""Consultas a Zabbix para el endpoint de precinto.

El precinto que manda el usuario se compara con `strpos(upper(...), upper(%s))`,
o sea como **texto literal**. Antes iba con `~*`, que en PostgreSQL no es "parecido
a" sino una expresion regular POSIX: el que llamaba no elegia el valor a buscar,
elegia el patron. Con eso, un `^` matcheaba todos los items del parque (decenas de
MB y todas las empresas en un solo request), `[` o `(` reventaban la consulta
entera con un 500, y un patron con backtracking podia colgar al motor.

La coincidencia parcial e insensible a mayusculas se mantiene tal cual estaba
documentada: `strpos(...) > 0` es exactamente "contiene". Lo unico que se pierde
es la capacidad de mandar metacaracteres, que nunca fue una funcion del endpoint.

El LIMIT es el segundo cinturon: una ONU sola en la ventana maxima da ~10.000
puntos por serie, asi que el tope no la toca, pero le pone techo a lo que puede
arrastrar una busqueda corta que matchee muchas ONUs.
"""

# Mantienen el filtro de tiempo
QUERY_ONU_RX = """
SELECT replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ') as cliente, h.value as onu_rx, floor((h.clock)/60)*60 AS time
FROM hosts ho, items i, history h
WHERE h.clock >= %s AND h.clock <= %s AND i.key_ ~* 'rx.ont' AND
      strpos(upper(replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ')),
             upper(%s)) > 0 AND
      ho.hostid = i.hostid AND i.itemid = h.itemid
ORDER BY time DESC
LIMIT %s;
"""

QUERY_OLT_RX = """
SELECT replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ') as cliente, h.value as onu_olt_rx, floor((h.clock)/60)*60 AS time
FROM hosts ho, items i, history h
WHERE h.clock >= %s AND h.clock <= %s AND i.key_ ~* 'rx.olt.ont' AND
      strpos(upper(replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ')),
             upper(%s)) > 0 AND
      ho.hostid = i.hostid AND i.itemid = h.itemid
ORDER BY time DESC
LIMIT %s;
"""

# Se eliminó el filtro h.clock de LOGS y ESTADO
#
# El filtro de formato de la fecha no es cosmético: algunos equipos reportan el
# centinela '0-00-00 00:00:00', que no es cadena vacía, pasaba el filtro anterior
# y hacía que el casteo a timestamp abortara TODA la consulta. Una sola fila
# corrupta dejaba al precinto sin ninguna de sus cuatro métricas (error 500).
# El regex subsume el chequeo de vacío que había antes.
QUERY_LOGS = """
SELECT distinct on (h.value) split_part(h.value, '-(', 1) as log, replace(split_part(h.value, '-(', 2), ')', '')::timestamp AS time
FROM hosts ho, items i, history_text h
WHERE i.key_ ~* 'hwGponDeviceOntControlLastDownCause' AND
      strpos(upper(replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ')),
             upper(%s)) > 0 AND
      ho.hostid = i.hostid AND i.itemid = h.itemid
      and replace(split_part(h.value, '-(', 2), ')', '') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$'
      and split_part(h.value, '-(', 1) !='Query-fails'
ORDER BY h.value, time DESC
LIMIT %s;
"""

QUERY_ESTADO = """
SELECT h.value as status, floor((h.clock)/60)*60 AS time
FROM hosts ho, items i, history_str h
WHERE i.key_ ~* 'hwGponDeviceOntEthernetOnlineState' AND
      strpos(upper(replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ')),
             upper(%s)) > 0 AND
      ho.hostid = i.hostid AND i.itemid = h.itemid
ORDER BY time DESC
LIMIT %s;
"""
