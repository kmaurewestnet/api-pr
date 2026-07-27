"""Consultas a Zabbix para el endpoint de precinto. Movidas sin cambios."""

# Mantienen el filtro de tiempo
QUERY_ONU_RX = """
SELECT replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ') as cliente, h.value as onu_rx, floor((h.clock)/60)*60 AS time
FROM hosts ho, items i, history h
WHERE h.clock >= %s AND h.clock <= %s AND i.key_ ~* 'rx.ont' AND
      replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ') ~* %s AND
      ho.hostid = i.hostid AND i.itemid = h.itemid
ORDER BY time DESC;
"""

QUERY_OLT_RX = """
SELECT replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ') as cliente, h.value as onu_olt_rx, floor((h.clock)/60)*60 AS time
FROM hosts ho, items i, history h
WHERE h.clock >= %s AND h.clock <= %s AND i.key_ ~* 'rx.olt.ont' AND
      replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ') ~* %s AND
      ho.hostid = i.hostid AND i.itemid = h.itemid
ORDER BY time DESC;
"""

# Se eliminó el filtro h.clock de LOGS y ESTADO
QUERY_LOGS = """
SELECT distinct on (h.value) split_part(h.value, '-(', 1) as log, replace(split_part(h.value, '-(', 2), ')', '')::timestamp AS time
FROM hosts ho, items i, history_text h
WHERE i.key_ ~* 'hwGponDeviceOntControlLastDownCause' AND
      replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ') ~* %s AND
      ho.hostid = i.hostid AND i.itemid = h.itemid
      and replace(split_part(h.value, '-(', 2), ')', '') !=''
      and split_part(h.value, '-(', 1) !='Query-fails'
ORDER BY h.value, time DESC;
"""

QUERY_ESTADO = """
SELECT h.value as status, floor((h.clock)/60)*60 AS time
FROM hosts ho, items i, history_str h
WHERE i.key_ ~* 'hwGponDeviceOntEthernetOnlineState' AND
      replace(split_part(split_part(i.name, 'descr_', 2), '_odb', 1), '_', ' ') ~* %s AND
      ho.hostid = i.hostid AND i.itemid = h.itemid
ORDER BY time DESC;
"""
