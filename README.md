# Zabbix Precintos API

API HTTP de consulta sobre ONUs y clientes:

| Endpoint | Qué responde | Bases |
|---|---|---|
| `GET /api/v1/precinto/{codigo_precinto}` | Series históricas de una ONU: RX, OLT RX, logs y estados | zabbix |
| `GET /api/v1/empresa/{empresa_id}/analytics` | Estado del parque completo de una empresa | napear + soldef + zabbix |
| `GET /api/v1/cortes/{numero_cliente}` | Si un cliente está caído y si el corte es de zona | gestion + zabbix + zabbix_wireless + soldef |
| `GET /health` | Conectividad de cada base y de las utilidades del sistema | las 5 |

Todos requieren la cabecera `X-API-Key`.

## Puesta en marcha

```bash
pip install -r requirements.txt
```

Copiar `.env.example` a `.env` y completar. Las variables de zabbix (`DB_HOST`,
`DB_NAME`, `DB_USER`, `DB_PASS`) conservan los nombres originales.

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

El endpoint de cortes además ejecuta `ping` y `snmpget`, que son binarios del
sistema y no dependencias de Python:

```bash
apt-get install -y iputils-ping snmp
```

Los pools de conexión se crean al primer uso de cada base, no al arrancar: si
faltan las credenciales de soldef, napear, gestion o zabbix_wireless, la API
igual levanta y los endpoints que no las usan siguen funcionando. `/health`
reporta cuál falla, e incluye si `ping` y `snmpget` están disponibles.

### Las cinco bases

| Nombre | Motor | Qué aporta | Endpoints |
|---|---|---|---|
| `zabbix` | PostgreSQL | Zabbix de fibra: items, hosts, historia, `nap_ocupacion` | precinto, analytics, cortes |
| `zabbix_wireless` | PostgreSQL | Zabbix de wireless, instancia separada | cortes |
| `soldef` | PostgreSQL | Inventario: bocas, precintos y aparatos de nodo | analytics, cortes |
| `napear` | MySQL | Reservas y empresas | analytics |
| `gestion` | MySQL | Clientes, contratos y conexiones | cortes |

## Endpoint de analíticas

```
GET /api/v1/empresa/{empresa_id}/analytics
```

| Parámetro | Default | Rango | Uso |
|---|---|---|---|
| `horas` | sin límite | 1–8760 | descarta lecturas más viejas que N horas |
| `page` | 1 | ≥1 | página del listado |
| `limit` | 500 | 1–5000 | tamaño de página |
| `full` | false | | devuelve todo por streaming, ignorando `page`/`limit` |
| `estado` | — | `online`\|`offline`\|`los`\|`powerfail`\|`sin_datos` | filtra el listado por categoría; el resumen siempre se calcula sobre el total |

Por defecto **no hay ventana**: se trae el último valor real de cada ONU y se
informa su antigüedad en `status_timestamp` / `los_timestamp`, para que el
consumidor decida si le sirve. Acotar con `horas` solo descarta datos viejos, no
acelera nada: la consulta ya entra por el índice `(itemid, clock)`.

### Cómo se determina el estado

**La mayoría de las ONUs no tiene el item `hwGponDeviceOntEthernetOnlineState`** —
solo algunas plantillas de Zabbix lo incluyen. Por eso el estado se resuelve por
precedencia, y cada dispositivo informa de dónde salió el suyo en
`origen_estado`:

| Precedencia | `origen_estado` | Criterio |
|---|---|---|
| 1 | `onlinestate` | Valor del item `OnlineState`. Autoritativo. |
| 2 | `los` | Derivado de la alarma óptica: `LOS` → offline, `No Alarm` → online |
| 3 | `null` | Sin ninguno de los dos items con lecturas → `sin_datos` |

El resumen trae el desglose en `resumen.origen_estado`, que es la métrica para
saber qué tan confiable es el resto de los números: si casi todo viene de `los`,
estás mirando un proxy óptico, no el estado operativo real de la ONU.

### Categorías del resumen

Sobre `estado` se construye un reparto **excluyente** en el campo `categoria`,
que es lo que cuenta el resumen. Los cinco contadores suman `total`:

| Orden | Condición | Categoría |
|---|---|---|
| 1 | Reporta `Online` | `online` |
| 2 | Caída con `Dying-gasp` a menos de `VENTANA_POWERFAIL_SEG` (900 s) del corte | `powerfail` |
| 3 | Caída con alarma LOS de menos de `LOS_VIGENTE_DIAS` (7) | `los` |
| 4 | Caída (incluye LOS vencido) | `offline` |
| 5 | Sin datos en Zabbix | `sin_datos` |

`powerfail` va antes que `los` porque un corte de energía apaga la ONT y eso
genera LOS en la OLT: las dos señales llegan juntas y el dying-gasp es la
específica. Si LOS ganara, no se detectaría ningún powerfail.

La cercanía del dying-gasp se mide contra el **momento de la caída**
(`status_timestamp`, o `los_timestamp` si no hay item de estado), no contra
ahora. Los 900 s tienen que absorber el desfase entre cuándo la ONT reportó el
evento y cuándo Zabbix lo registró, que depende del intervalo de sondeo: si
`powerfail` da 0 en todas las empresas, ese umbral quedó corto.

### Última causa de caída

`ldc` y `ldc_timestamp` salen del item `hwGponDeviceOntControlLastDownCause`, que
guarda causa y fecha en un solo string: `Dying-gasp-(2026-07-17 22:11:11)`. Se
parte en la primera aparición de `-(`, así que una causa con guiones (`LOS-i`) no
se corta mal.

Es una consulta aparte de la de estado y LOS porque ese item es de **texto**: sus
valores viven en `history_text`, no en `history_str`. Ambas corren en paralelo.

La fecha embebida no trae zona horaria; se interpreta con la de la base
(`current_setting('TimeZone')`) antes de convertirla a epoch. Si viniera
malformada, `ldc_timestamp` queda en `null` y `ldc` igual se devuelve — un valor
roto no tumba la consulta entera.

```bash
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/api/v1/empresa/2/analytics?limit=100"
```

### Cadena de identificadores

```
napear.registros.empresa_id
  -> registro_reservas.external_connector_id      ("serial")
  -> soldef.dispositivos_bocas.id
  -> soldef.dispositivos_precintos.etiqueta       ("precinto")
  -> zabbix: items.name, que trae el precinto entre 'descr_' y '_odb'
```

El precinto es la clave de cruce contra Zabbix porque viaja embebido en el
nombre del item:

```
(WESTNET) 302381 - LAURA MARCELA DIAZ_zone_CIUDAD_descr_WN12753_odb_GLL-2763_authd_20260727
                                                        ^^^^^^^ precinto
```

Se extrae con `replace(split_part(split_part(i.name,'descr_',2),'_odb',1),'_',' ')`,
la misma expresión que usa el endpoint de precinto. El `replace('_',' ')` final
es el que convierte `descr_PR_W3165_odb` en `PR W3165`, que es exactamente el
formato en que soldef devuelve el precinto.

La MAC (derivada de `dispositivos.nro_serie` traduciendo el prefijo de fabricante
a hexadecimal: `HWTC` → `48575443`, `ZTEG` → `5A544547`, …) se sigue devolviendo
en la respuesta, pero **no participa del cruce**.

Como son tres motores distintos, el merge final se hace en memoria indexando por
precinto normalizado. La normalización (`upper` + `trim` + colapsar espacios) se
aplica igual en SQL y en Python: si divergieran, la query devolvería filas que
después no se encontrarían en el índice.

`nombre` sale del `nap_tag` de napear — es el único campo con forma de nombre en
toda la cadena; soldef no devuelve uno.

## Endpoint de detección de cortes

```
GET /api/v1/cortes/{numero_cliente}
```

Responde exactamente tres booleanos, sin envoltorio:

```json
{ "isFtth": true, "isOnline": false, "isZoneIncident": true }
```

El detalle de cada verificación (qué respondió cada ping, qué NAP y qué OLT se
resolvieron) queda en el log del servidor, no en la respuesta:

```
cliente=302381 ftth solar=False nap=GLL-2763 olt=OLT-CENTRO(10.20.0.5) sw=10.20.0.2 | ping_cli=False ping_olt=True ping_sw=True ont_caida=True nap_caida=False -> online=False zona=False
```

`numero_cliente` se valida como **solo dígitos, hasta 12 caracteres**. Entra a un
`=` de MySQL, a un `~*` de PostgreSQL y al log: restringirlo a dígitos lo vuelve
inofensivo en los tres, sin depender solo del escapado del driver.

### Recorrido

```
Gestión (MySQL): categoría + IP del cliente          -> isFtth
  |
  +-- FTTH ---- Zabbix Fibra: NAP, OLT y su IP
  |               +-- OLT normal  : LOS y Online State por consulta al historial
  |               +-- OLT "Solar" : LOS por snmpget en vivo contra la OLT
  |             Soldef: switch del nodo de la OLT
  |
  +-- Wireless - Zabbix Wireless: Access Point y RouterBoard del nodo
```

La OLT se considera Solar cuando su `hosts.host` contiene "solar": esas no
guardan historial utilizable y hay que preguntarles por SNMP en el momento. El
nombre del item en esas OLT tiene otro formato, así que el cliente se extrae
después de `'ONU LOSi'` y no con `split_part(i.name,'_zone',1)`.

### Códigos SNMP: por qué hay que configurarlos

Zabbix guarda en su historial el valor **ya mapeado** (`LOS`, `No Alarm`,
`Offline`). Un `snmpget` directo a la OLT devuelve el **entero del MIB**. Las
cuatro variables `SNMP_COD_*` hacen esa traducción, con estos defaults
verificados contra las OLT del parque:

| Item | Código | Significa | Variable |
|---|---|---|---|
| `hwGponDeviceOntAlarmLOSi` | `1` | No Alarm | `SNMP_COD_SIN_LOS` |
| `hwGponDeviceOntAlarmLOSi` | `2` | LOS / LOSi | `SNMP_COD_LOS` |
| `hwGponDeviceOntEthernetOnlineState` | `1` | Online | `SNMP_COD_ONLINE` |
| `hwGponDeviceOntEthernetOnlineState` | `2` | Offline | `SNMP_COD_OFFLINE` |

Solo hay que tocarlas si aparece una OLT de otro vendor. Un código que no esté en
ninguna de las dos listas de su métrica queda en "no evaluable" y se loguea con
su valor crudo: **nunca se asume alarma sobre un valor que no se sabe leer**.

Eso último no es paranoia. La primera versión traducía con `int(valor) != 0`, o
sea cualquier entero no nulo era alarma. Como el código normal es `1`, **toda ONT
sana se reportaba caída**, y como los OIDs de la NAP entera daban lo mismo,
`clientes_caidos == total_clientes` y la caja se declaraba en corte. Una ONT
online con todos sus vecinos online devolvía `isZoneIncident: true`.

El log muestra el valor crudo junto a su interpretación, sin `LOG_LEVEL=DEBUG`:

```
INFO [services.cortes] snmp 172.30.0.98 LOS: 8/8 OIDs interpretados, 0 positivos | 1.3.6...1='1'->False; ...
```

Las verificaciones del último paso corren en un `ThreadPoolExecutor`
(`CORTES_MAX_WORKERS`, default 6). El endpoint se declara `def`, así que FastAPI
lo ejecuta en su propio threadpool: ni el subproceso de `ping` ni psycopg2 o
mysql-connector —que son drivers sincrónicos— bloquean el event loop.

### Consumo por request

Dos límites que definen cuántas consultas simultáneas aguanta:

| | Cuánto | Por qué |
|---|---|---|
| Conexiones a `zabbix` en paralelo | **1** | Los 4 lookups del camino Solar (OIDs de LOS, de OnlineState, de la NAP y ocupación) van sobre una sola conexión en `_pg_multi`. Entran por índice sobre `items`: secuenciarlos cuesta milisegundos frente a los segundos de un ping |
| Subprocesos `snmpget` para una NAP de 64 | **4** | `snmpget` acepta varios OIDs en el mismo PDU (`SNMP_OIDS_POR_CONSULTA`, default 20) |

Con `POOL_MAX=10` eso da ~10 requests concurrentes antes de tocar el techo del
pool. Y cuando lo toca, **falla en vez de mentir**: `db.PoolAgotado` es la única
excepción que `_en_paralelo` no convierte en "no evaluable", porque quedarse sin
conexiones no es "la red no contestó" sino "no llegué a preguntar". Sale por
`503`. Todo lo demás sigue degradando a `200`.

El resultado se mapea por OID (`snmpget -Oqn` devuelve `<oid> <valor>` por línea)
y no por posición: si la OLT omite o reordena un varbind, no se corren todos los
valores.

### Caché del estado de zona

El estado de una NAP y el ping a una OLT son **la misma respuesta para todos los
clientes de esa caja**. Sin caché, un corte real —que es justo cuando más
consultas llegan— hace que 50 clientes de la misma NAP disparen 50 veces el mismo
walk SNMP contra la misma OLT: la API haría su máximo trabajo cuando la red está
peor.

`services/cache.py` cachea, con TTL de `CACHE_ZONA_TTL_SEG` (45 s):

| Se cachea | No se cachea |
|---|---|
| Estado de la NAP (incluye el walk SNMP entero) | Ping al cliente |
| Ping a la OLT y al switch | Estado de la ONT del cliente |
| Ping al AP y al RouterBoard | Topología del cliente |
| Switch del nodo (Soldef) | |

Lo per-cliente es la respuesta puntual de cada uno y tiene que ser fresca.

El **single-flight** (un lock por clave) es la mitad que importa. Un TTL a secas
no alcanza: las consultas de un corte llegan *juntas*, fallan todas en el caché a
la vez y salen todas a preguntar. Con el lock por clave, la primera calcula y el
resto espera ese resultado.

Medido con 30 requests simultáneas de la misma NAP de 64 clientes:

| | Sin caché | Con caché |
|---|---|---|
| Invocaciones `snmpget` | 150 | **34** |
| Consultas SQL | 180 | **93** |
| Pings a OLT + switch | 60 | **2** |
| Pings al cliente | 30 | 30 *(no se cachea)* |

El costo: un corte que acaba de empezar puede tardar hasta `CACHE_ZONA_TTL_SEG`
en reflejarse en `isZoneIncident`. `isOnline` no se ve afectado, porque el ping
al cliente y su ONT nunca se cachean. En 0 se desactiva.

#### El valor vencido no se tira

Si el recálculo **falla o devuelve "no evaluable"** y hay un valor anterior real,
se sirve ese, hasta `CACHE_STALE_MAX_SEG` (300 s) desde que se midió de verdad.

No es un lujo. La consulta de estado de NAP tarda ~5-7 s contra un
`CORTES_STATEMENT_TIMEOUT_MS` de 15 s. Si en un pico de carga se pasa, `nap_caida`
queda en `None`, y con la OLT respondiendo:

```python
is_zone_incident = nap is True or ping_olt is False   # None → False
```

o sea que **durante un corte de zona real, un Zabbix lento haría que la API
responda `isZoneIncident: false` con 200 OK**. Una medición real de hace dos
minutos vale incomparablemente más que eso.

Al servir un valor viejo se le renueva el TTL —para no reintentar la consulta
cara en cada request— pero **no** su antigüedad, así que igual caduca a los
`CACHE_STALE_MAX_SEG`: una NAP que dejó de reportar no queda marcada como caída
indefinidamente. Cada vez que pasa, queda un `WARNING` con la edad del dato.

Sin valor previo utilizable no hay nada que servir y la excepción se propaga:
`PoolAgotado` sigue saliendo por 503.

### Consumidores, rate limit y deadline

`API_KEYS` toma pares `nombre:clave` separados por coma. El nombre no es
decorativo: aparece en cada línea de log del endpoint y es la unidad del rate
limit, así que se le puede cortar a un consumidor sin cortarles a todos. La clave
única anterior (`API_KEY_SECRETA`) sigue funcionando como consumidor `legacy`.

```
API_KEYS=facturacion:xxx,soporte:yyy,app-movil:zzz
```

`API_KEYS_SOLO_CORTES` lista, por nombre, los consumidores que **solo** pueden
usar `/cortes` — los externos: hoy la centralita y el chatbot. `/precinto` y
`/analytics` devuelven el parque de las 17 empresas (cruzar empresas es la
función de esos endpoints, no un descuido), así que son la vista interna del NOC
y con una clave externa responden `403`. Un nombre que no exista en `API_KEYS` se
loguea como ERROR al arrancar: sería un consumidor que quedó sin restringir sin
que nadie se entere.

```
API_KEYS_SOLO_CORTES=centralita,chatbot
```

El rate limit es una cubeta de tokens por consumidor, con dos cuotas separadas:
`RATE_LIMIT_POR_MINUTO` (default 60) para `/cortes`, y
`RATE_LIMIT_INTERNO_POR_MINUTO` (default 10) para `/precinto` y `/analytics`. En
`0` se desactiva. Van separadas porque no cuestan lo mismo: una analítica recorre
el parque entero de la empresa aunque se pida `limit=1` y comparte con `/cortes`
el pool de Zabbix, así que con una sola cuota un tablero refrescando puede dejar
sin atender a la centralita.

`RATE_LIMIT_POR_CONSUMIDOR` (`chatbot:20,centralita:120`) le da una cuota propia
a un consumidor puntual, sobre la cuota general. Los endpoints internos no se
ajustan por consumidor: ahí solo llegan claves internas.

Devuelve `429` con `Retry-After`. Importa más que en una API común por la
amplificación: **un request puede disparar decenas de consultas SNMP contra una
OLT de producción**, así que un loop sobre números de cliente le hace DoS a la
red, no a la API.

Es en memoria y **por proceso**: con N workers de uvicorn el límite efectivo es N
veces el configurado. El comando de arriba levanta **un solo proceso**, así que
el número es exacto; si en producción se agregan workers o se pasa a gunicorn,
hay que dividir el límite configurado por esa cantidad. Compartirlo entre
procesos requeriría Redis; para frenar un loop accidental o un consumidor
desbocado, alcanza.

El endpoint se declara `async def` y todo el trabajo bloqueante va a un
`ThreadPoolExecutor` propio de `CORTES_MAX_CONCURRENTES` (default 5). Eso da dos
cosas:

- **Control de admisión explícito.** Por encima del tope los requests encolan en
  el event loop sin ocupar un hilo, en vez de apilarse compitiendo por las
  conexiones del pool. Conviene mantenerlo en `POOL_MAX/2` o menos, porque cada
  request usa como mucho 2 conexiones de `zabbix`.
- **Deadline.** `CORTES_TIMEOUT_SEG` (25 s) corta la espera con un `504`. El
  trabajo en curso no se puede matar —son subprocesos y drivers sincrónicos—
  pero termina solo, porque cada paso tiene su propio timeout. Lo que se corta es
  que el cliente quede colgado sin tope.

El rate limit **no** está aplicado a `/precinto` ni a `/analytics`: no conozco el
patrón de llamadas de sus consumidores actuales y un límite mal calibrado los
rompe. Agregarlo es cambiar `Depends(verificar_api_key)` por
`Depends(limitar_tasa)` en cada router.

### Cómo se decide cada campo

| Campo | Regla |
|---|---|
| `isFtth` | El plan activo tiene `category_id` = `CATEGORIA_FTTH_ID` (16). 17 es wireless |
| `isOnline` | `false` solo si el ping al cliente falla **y** la ONT reporta LOS/Offline. En wireless es el ping al cliente |
| `isZoneIncident` | Fibra: la NAP está en corte **o** la OLT no responde. Wireless: el AP o el RouterBoard no responden |

La tabla del documento para `isZoneIncident` se reduce a esas dos condiciones: el
ping al switch no cambia el resultado en ninguna de sus filas (con la NAP arriba
y la OLT respondiendo, da `false` responda o no el switch). Se ejecuta igual,
porque está en el procedimiento y sirve en el log para diagnosticar.

**Corte de NAP:** la caja se considera caída cuando todos sus clientes reportan
LOS. En NAPs de más de `NAP_TOLERANCIA_DESDE` (3) clientes se tolera uno sin
reportar. El umbral vive una sola vez, en `services/cortes.nap_caida()`, así que
el camino por consulta y el camino por SNMP no pueden divergir. Sin fila en
`nap_ocupacion` el resultado queda en "no evaluable", no en `false`.

### Qué pasa cuando algo falla

Cada verificación tiene tres estados: responde, no responde, y **no evaluable**.
"No evaluable" nunca cuenta como falla — un timeout de ping sí significa "caído",
pero un binario faltante o una base que no contesta, no.

| Falla | Respuesta |
|---|---|
| Gestión no responde | `503` — sin ella no se sabe ni la tecnología ni la IP |
| La consulta de topología no responde (zabbix / zabbix_wireless) | `503` — `isZoneIncident` sería inventado |
| El cliente no existe o no tiene contrato activo en las categorías 16/17 | `404` |
| `numero_cliente` no es numérico o pasa los 12 dígitos | `422` |
| Una verificación individual falla (ping, snmpget, consulta de estado) | `200`, esa señal queda en "no evaluable" y se loguea |
| Soldef no responde | `200`, solo se pierde el ping al switch |
| El cliente no tiene ONT en Zabbix | `200`, se evalúa solo por ping y se loguea un warning |

`CORTES_STATEMENT_TIMEOUT_MS` (15 s) es propio de este endpoint: tiene que
responder en segundos y no puede heredar los 120 s de las analíticas.

```bash
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/api/v1/cortes/302381"
```

### Diferencias con `documentacion_api_cortes_v1.md`

Tres cambios deliberados sobre las consultas del documento, además de pasarlas
todas a parámetros bind:

1. `$__unixEpochGroupAlias(h.clock,'1m')` es una macro de Grafana, no SQL. Se
   reemplazó por su expansión, `floor(h.clock/60)*60`, que es la misma expresión
   que ya usa `queries/precinto.py`.
2. Las consultas de LOS y de Online State eran idénticas salvo por `i.key_`: se
   unificaron en una con una columna `metrica`. Un viaje a la base en vez de dos.
3. **La NAP se extrae con una sola expresión.** El documento usaba una versión
   corta para la topología y una larga —la corta más las normalizaciones `-AP`,
   `au`, `1084M` y `288e`— para el estado de NAP, y después comparaba una contra
   la otra. Cualquier NAP cuyo nombre incluyera esos fragmentos se extraía
   distinto en cada lado y el estado no podía matchear nunca. Se usa la larga en
   ambos lados.

Dos cosas que el documento no define y hubo que resolver:

* **La matriz de wireless.** Solo lista los tres pings. Se tomó `isOnline` = ping
  al cliente, y `isZoneIncident` = el AP o el RouterBoard no responden, que es la
  infraestructura compartida del nodo.
* **Qué `category_id` es fibra.** El documento filtra por `IN (16, 17)` pero no
  dice cuál es cuál: **16 es fibra, 17 es wireless**. `isFtth` se decide sobre el
  ID y no sobre `category.name` —que es lo que el documento llamaba
  "categoria"— porque el nombre es texto libre y cambia con cada plan nuevo. Los
  dos IDs viven en `config.CATEGORIA_FTTH_ID` / `CATEGORIA_WIRELESS_ID` y entran
  como parámetros a la consulta, así que el `IN` y la decisión de `isFtth` no
  pueden desalinearse.

Un cliente con fibra **y** wireless activos a la vez se resuelve como fibra, y se
loguea un warning. Sin ese criterio explícito la respuesta dependería del orden
en que MySQL devolviera las filas, que no está garantizado.

## Índices que espera esta API en Zabbix

Las tres consultas del camino FTTH filtran `items` por `key_`. Sin índice, cada
una recorre las ~156.000 filas de la tabla: medido, 616 ms de los 645 ms de la
consulta de estado de ONT.

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS items_key_pattern_idx
    ON items (key_ varchar_pattern_ops);
ANALYZE items;
```

`varchar_pattern_ops` es lo que hace que `LIKE 'patrón%'` pueda usar el índice
cuando la base no está en collation C. `CONCURRENTLY` para no bloquear `items`
en un Zabbix vivo; la tabla casi no se escribe (solo al provisionar o descubrir),
así que mantenerlo es despreciable.

**Las consultas usan `LIKE` anclado, que es sensible a mayúsculas**, a diferencia
del `~*` del documento original. Eso es lo que permite el índice. Si alguna vez
cambia la capitalización de una key, la consulta devuelve cero filas **en
silencio** y el estado de ONT queda en "no evaluable". El chequeo:

```sql
SELECT key_, count(*) FROM items
WHERE key_ ILIKE '%hwGponDeviceOntAlarmLOSi%' OR key_ ILIKE '%OnlineState%'
GROUP BY key_ ORDER BY 2 DESC LIMIT 10;
```

Las keys tienen que empezar exactamente con `hwGponDeviceOntAlarmLOSi` y
`hwGponDeviceOntEthernetOnlineState`.

## Rendimiento

La consulta a Zabbix separa dos cosas de naturaleza distinta:

| | Qué hace | Cuánto cambia | Costo |
|---|---|---|---|
| CTE `items_metrica` | Resolver identidad: precinto → itemid | Solo al provisionar o mover una ONT | Un escaneo de `items` |
| `JOIN history_str` | Leer el último valor | Constantemente | Índice `(itemid, clock)` |

La versión original resolvía la identidad cruzando la MAC contra
`history_text.value` — una columna **sin índice**, en una tabla de millones de
filas, escaneada entera en cada request y por cada métrica. Cruzar por precinto
mueve esa resolución a `items`, que es chica y está indexada por `key_` y
`hostid`, y deja el acceso a la historia por `itemid`, que entra por la PK.

Dos consecuencias de diseño:

1. **`QUERY_CHUNK_SIZE` está en 50000 a propósito.** El escaneo de `items` se
   repite por lote, así que conviene que una empresa grande entre en uno solo.
2. **Estado y LOS van en una sola consulta**, no en dos paralelas: ambas
   comparten la resolución precinto → itemid, así que hacerlas por separado
   duplicaba el trabajo sin ganar nada.

No hace falta ningún índice nuevo, ni tabla materializada, ni cache: la API tiene
solo lectura sobre Zabbix y una ONT recién provisionada aparece en la siguiente
consulta.

`STATEMENT_TIMEOUT_MS` (default 120000) corta una consulta colgada en vez de
dejar la conexión tomada. El log de cada request incluye el tiempo por paso:

```
empresa=2 seriales=12345 onus=12000 precintos=11987 | napear=0.31s soldef=1.20s zabbix=2.44s total=4.02s
```

## Estructura

```
config.py             DSNs de las 5 bases, API key, logging, límites, red/SNMP
db.py                 pools de conexión + context managers
security.py           identidad del consumidor + rate limit
models.py             modelos de respuesta (documentan /docs)
queries/precinto.py   las 4 consultas del endpoint de precinto
queries/analytics.py  las 4 consultas del cruce entre bases
queries/cortes.py     las 10 consultas de detección de cortes
services/analytics.py orquestación de los 3 pasos, cruce y agregados
services/cache.py     caché con TTL y single-flight del estado de zona
services/red.py       ping ICMP y snmpget: subprocesos acotados y validados
services/cortes.py    recorrido de topología y matriz de decisión de cortes
routers/              un módulo por endpoint
test_cortes.py        chequeo de la matriz de decisión, sin framework
```

```bash
python test_cortes.py
```
