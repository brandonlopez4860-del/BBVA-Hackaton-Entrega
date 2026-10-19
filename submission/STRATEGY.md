# Estrategia de Resolución LOG-8092

## Qué se cambió y por qué

La estrategia inicial (`candidate_strategy.py` heredada) despachaba las tarjetas de una en una mediante un enfoque Round-Robin, lo cual desaprovechaba la capacidad de los vehículos, disparaba los costos de despacho e impactaba negativamente en el tiempo medio de entrega por la congestión del servicio.

Para superar estos problemas, implementé una arquitectura basada en tres pilares:

1. **Agrupación Estricta (Batching):** Se retienen las tarjetas elegibles agrupándolas rigurosamente por `zone`, `delivery_cluster_id` y número de intento.
2. **Retención Inteligente de Costos:** Para optimizar la tarifa, los lotes de tarjetas que no llenan un camión, que no contienen productos VIP, y cuyo SLA vence en más de 24 horas, son retenidos artificialmente hasta por 30 minutos (el límite reglamentario es 45 min) para esperar más tarjetas del mismo destino y consolidar viajes.
3. **Control de Inventario Virtual:** Para evitar excepciones por `CAPACITY_EXCEEDED` al solicitar varios camiones en un mismo _tick_ temporal, el algoritmo mantiene un caché en memoria de las unidades disponibles (`virtual_units`) y las descuenta en caliente.

## Heurística de Selección de Transportistas

El motor de selección fue rediseñado. Tras filtrar los transportistas por zona, operatividad, unidades disponibles y capacidades requeridas (ej. vehículos blindados), se aplica un árbol de decisión matemático:

- **Modo Urgencia / RCP (SLA en riesgo o presencia de VIPs):** Si la fecha límite del lote vence en menos de 24 horas, o el lote incluye tarjetas `VIP_BLACK` / `VIP`, la estrategia ordena a los transportistas priorizando la **Velocidad** (descendente) sobre el **Costo** (ascendente) para blindar el SLA y potenciar el indicador de satisfacción (RCP).
- **Modo Ahorro:** En condiciones de estabilidad, prioriza el **Costo Base + Recargo Zonal** (ascendente) sobre la velocidad, ahorrando presupuesto.

## Inversión Dinámica (Upgrades)

En el _hook_ `on_monthly_planning`, la estrategia invierte el presupuesto remanente. De forma conservadora, utiliza hasta el 50% del presupuesto disponible no gastado para comprar mejoras (`CAPACITY` o `FLEET`) enfocadas en los transportistas con la tarifa base más económica.

## Limitaciones actuales

El enfoque de mejoras mensuales (upgrades) es determinista o "greedy" basándose en el presupuesto pasado; no utiliza modelamiento estadístico de demanda futura para predecir si en el próximo mes habrá picos por festividades masivas. El algoritmo prioriza la estabilidad de RAM y CPU frente a inferencias complejas de previsión.
