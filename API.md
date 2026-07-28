# Guía de uso de la API

Referencia rápida para consumir la API. Los detalles de arquitectura, despliegue
y rendimiento están en [README.md](README.md).

## Endpoints

| | Qué hace |
|---|---|
| `GET /api/v1/precinto/{codigo}` | Series históricas de **una** ONU: RX, OLT RX, logs, estados |
| `GET /api/v1/empresa/{id}/analytics` | Estado del **parque completo** de una empresa |
| `GET /health` | Conectividad de las 3 bases por separado |

Todos requieren el header `X-API-Key`. Documentación interactiva en `/docs`.

## Parámetros de analytics

| Parámetro | Default | Rango | Para qué |
|---|---|---|---|
| `page` | 1 | ≥1 | Página del listado |
| `limit` | 500 | 1–5000 | Tamaño de página |
| `estado` | — | `online` · `offline` · `los` · `powerfail` · `sin_datos` | Filtra el listado por categoría |
| `full` | false | | Todo el listado por streaming, ignora `page` y `limit` |
| `horas` | sin límite | 1–8760 | Descarta lecturas más viejas que N horas |

## Respuesta

```json
{
  "status": "success",
  "metadata": {
    "empresa_id": 34,
    "empresa": "CMC Network",
    "total_seriales_napear": 252,
    "total_onus": 251,
    "rango_tiempo": {
      "desde_timestamp": null,
      "hasta_timestamp": 1785180027,
      "horas_consultadas": null
    },
    "paginacion": { "page": 1, "limit": 20, "total_items": 251, "total_paginas": 13 }
  },
  "resumen": {
    "total": 491,
    "online": 446,
    "offline": 34,
    "los": 6,
    "powerfail": 4,
    "sin_datos": 1,
    "porcentaje_online": 90.84,
    "origen_estado": { "onlinestate": 490, "los": 0, "sin_datos": 1 }
  },
  "dispositivos": [
    {
      "serial": 2464596,
      "nombre": "JN-018",
      "mac": "48575443ED3C75AA",
      "precinto": "JES0037",
      "status": "Online",
      "status_timestamp": 1784886153,
      "los": "No Alarm",
      "los_timestamp": 1784885561,
      "ldc": "Dying-gasp",
      "ldc_timestamp": 1784326271,
      "categoria": "online",
      "estado": "online",
      "origen_estado": "onlinestate",
      "con_los": false
    }
  ]
}
```

### Categorías

El resumen es un **reparto excluyente**: cada equipo cae en una sola categoría y
los cinco contadores suman `total`. Se evalúan en este orden, y gana la primera
que aplica:

| # | Condición | Categoría |
|---|---|---|
| 1 | La ONT reporta `Online` | `online` |
| 2 | Caída, con `Dying-gasp` a menos de **15 min** del corte | `powerfail` |
| 3 | Caída, con alarma LOS de menos de **7 días** | `los` |
| 4 | Caída (incluye LOS de 7 días o más) | `offline` |
| 5 | Sin estado ni LOS en Zabbix | `sin_datos` |

`powerfail` se evalúa antes que `los` a propósito: un corte de energía apaga la
ONT y eso genera LOS en la OLT, así que ambas señales aparecen juntas. El
dying-gasp es la más específica de las dos.

Operativamente la diferencia importa: `powerfail` es corte de luz en el domicilio
y se resuelve solo; `los` es corte de fibra y necesita cuadrilla.

Los umbrales se ajustan por entorno con `VENTANA_POWERFAIL_SEG` (default 900) y
`LOS_VIGENTE_DIAS` (default 7).

### Campos por dispositivo

| Campo | Origen |
|---|---|
| `serial` | `external_connector_id` de napear = boca de soldef |
| `nombre` | `nap_tag` de napear |
| `mac` | soldef, derivada del número de serie. Informativa, no se usa para cruzar |
| `precinto` | soldef. Es la clave de cruce contra Zabbix |
| `status` / `status_timestamp` | Item `OnlineState`, si la ONU lo tiene |
| `los` / `los_timestamp` | Alarma óptica `hwGponDeviceOntAlarmLOSi` |
| `ldc` / `ldc_timestamp` | Última causa de caída, ej. `Dying-gasp` + epoch del evento |
| `categoria` | Reparto excluyente: `online` · `offline` · `los` · `powerfail` · `sin_datos` |
| `estado` | `online` · `offline` · `sin_datos`. Se mantiene por compatibilidad |
| `origen_estado` | De dónde salió `estado`: `onlinestate`, `los`, o `null` |
| `con_los` | Si la alarma óptica está activa, sin importar su antigüedad |

## Combinaciones útiles

Solo el panorama general, sin listado:

```bash
curl -H "X-API-Key: $K" "localhost:8000/api/v1/empresa/34/analytics?limit=1"
```

Las caídas:

```bash
curl -H "X-API-Key: $K" "localhost:8000/api/v1/empresa/34/analytics?estado=offline"
```

Corte de fibra — necesita cuadrilla:

```bash
curl -H "X-API-Key: $K" "localhost:8000/api/v1/empresa/34/analytics?estado=los"
```

Corte de luz en el domicilio — se resuelve solo:

```bash
curl -H "X-API-Key: $K" "localhost:8000/api/v1/empresa/34/analytics?estado=powerfail"
```

Las que Zabbix no está monitoreando bien — útil para auditar cobertura:

```bash
curl -H "X-API-Key: $K" "localhost:8000/api/v1/empresa/34/analytics?estado=sin_datos"
```

Recorrer el listado por páginas:

```bash
curl -H "X-API-Key: $K" "localhost:8000/api/v1/empresa/34/analytics?limit=20&page=2"
```

Todo el parque de una sola vez:

```bash
curl -H "X-API-Key: $K" "localhost:8000/api/v1/empresa/34/analytics?full=true"
```

Solo datos frescos (últimas 24 h):

```bash
curl -H "X-API-Key: $K" "localhost:8000/api/v1/empresa/34/analytics?horas=24"
```

## Cuatro cosas que conviene tener presentes

**El `resumen` siempre se calcula sobre el total**, nunca sobre la página ni
sobre el filtro `estado`. Así no cambia según cómo lo consultes.

**Paginar no acelera nada.** Las tres bases se consultan enteras igual; el
recorte se hace en memoria al final. `page=13` tarda lo mismo que `page=1`. Sirve
para no mover 40.000 registros por HTTP, no para que la respuesta llegue antes.

**`estado` puede ser un proxy.** La mayoría de las ONUs no tiene el item
`OnlineState`; cuando falta, el estado se deriva de la alarma óptica. Eso detecta
fibra caída, no servicio caído: una ONU con fibra sana pero servicio muerto
figura `online`. `origen_estado` te dice cuál de los dos estás mirando por
dispositivo, y `resumen.origen_estado` qué tan mezclada viene la muestra. Si ves
mayoría `los`, tomá los números con esa reserva.

**Por defecto no hay corte de antigüedad.** Se trae el último valor real de cada
ONU, sea de hoy o de hace tres meses. Los campos `*_timestamp` te dejan juzgarlo;
`?horas=N` fuerza frescura a costa de más `sin_datos`.

## Códigos de respuesta

| Código | Cuándo |
|---|---|
| `200` | OK |
| `403` | API Key inválida o ausente |
| `404` | La empresa no tiene dispositivos en napear |
| `422` | Parámetro fuera de rango o `estado` inválido |
| `500` | Error al procesar |
| `503` | Alguna base de datos no responde — `/health` dice cuál |
