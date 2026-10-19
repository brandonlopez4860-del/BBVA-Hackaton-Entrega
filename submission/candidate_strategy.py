"""Estrategia de despacho optimizada para LOG-8092.

Solución integral con Retención Inteligente (Batching),
Heurística Adaptativa SLA/Costo, y Planificación Mensual dinámica.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List

from interfaces import (
    BaseDispatcherStrategy,
    CarrierService,
    CarrierSnapshot,
    DeliveryAttemptResult,
    DispatchContext,
    FleetUpgradeDecision,
    MonthlyPlanningContext,
    Shipment,
)

class CandidateStrategy(BaseDispatcherStrategy):
    """Estrategia optimizada para BBVA Hackaton 2026."""

    def __init__(self, carrier_service: CarrierService) -> None:
        super().__init__(carrier_service)
        self.pending: List[Shipment] = []
        self.all_historical_records = []
        self.last_carrier_by_shipment: Dict[str, str] = {}
        self.defer_until: Dict[str, datetime] = {}
        self.carriers: Tuple[CarrierSnapshot, ...] = ()
        self.attempts: Dict[str, int] = {}

    def on_shipments_available(self, now: datetime, shipments: Tuple[Shipment, ...]) -> None:
        del now
        for shipment in shipments:
            self.pending.append(shipment)
            self.all_historical_records.append(shipment)
            if shipment.shipment_id not in self.attempts:
                self.attempts[shipment.shipment_id] = 1

    def on_dispatch_tick(self, context: DispatchContext) -> None:
        self.carriers = context.carriers

        eligible_shipments = []
        waiting_shipments = []

        # 1. Separar envíos listos de los penalizados o programados a futuro
        for shipment in self.pending:
            if shipment.ready_at > context.now or self.defer_until.get(shipment.shipment_id, context.now) > context.now:
                waiting_shipments.append(shipment)
            else:
                eligible_shipments.append(shipment)

        # 2. AGRUPACIÓN ESTRICTA (Zona, Destino, Intento)
        batches = defaultdict(list)
        for shipment in eligible_shipments:
            attempt = self.attempts.get(shipment.shipment_id, 1)
            key = (shipment.zone, shipment.delivery_cluster_id, attempt)
            batches[key].append(shipment)

        unprocessed_shipments = []
        
        # Inventario virtual para este tick de 15 minutos
        virtual_units = {c.carrier_id: c.available_units for c in self.carriers}

        # 3. EVALUACIÓN DE LOTES (SLA, Costo y Tiempo de retención)
        for key, shipment_group in batches.items():
            zone, cluster, attempt = key
            
            while shipment_group:
                tiene_vip = any(s.product in ("VIP_BLACK", "VIP") for s in shipment_group)
                urgent_deadline = min(s.sla_deadline for s in shipment_group)
                horas_para_vencer = (urgent_deadline - context.now).total_seconds() / 3600
                
                oldest_ready = min(max(s.ready_at, s.available_at) for s in shipment_group)
                oldest_wait_mins = (context.now - oldest_ready).total_seconds() / 60.0

                carrier = self._select_best_carrier(shipment_group, context, virtual_units)
                
                if not carrier:
                    unprocessed_shipments.extend(shipment_group)
                    break
                
                capacity = carrier.batch_capacity
                
                # RETENCIÓN INTELIGENTE: Agrupamos hasta 30 min si hay espacio y no hay urgencia
                if (len(shipment_group) < capacity and not tiene_vip 
                    and horas_para_vencer >= 24 and oldest_wait_mins < 30):
                    unprocessed_shipments.extend(shipment_group)
                    break
                
                batch_to_send = shipment_group[:capacity]
                shipment_group = shipment_group[capacity:]
                
                batch_ids = tuple(s.shipment_id for s in batch_to_send)
                receipt = self.carrier_service.dispatch_batch(carrier.carrier_id, batch_ids)
                
                if receipt.accepted:
                    virtual_units[carrier.carrier_id] -= 1
                    for s in batch_to_send:
                        self.last_carrier_by_shipment[s.shipment_id] = carrier.carrier_id
                else:
                    if receipt.rejection_reason in {"CAPABILITY_UNSUPPORTED", "OUTSIDE_OPERATING_WINDOW", "CAPACITY_EXCEEDED"}:
                        for s in batch_to_send:
                            self.defer_until[s.shipment_id] = context.now + timedelta(hours=24)
                    unprocessed_shipments.extend(batch_to_send)

        self.pending = waiting_shipments + unprocessed_shipments

    def on_delivery_succeeded(self, result: DeliveryAttemptResult) -> None:
        self.last_carrier_by_shipment.pop(result.shipment.shipment_id, None)
        self.defer_until.pop(result.shipment.shipment_id, None)
        self.attempts.pop(result.shipment.shipment_id, None)

    def on_delivery_failed(self, result: DeliveryAttemptResult) -> None:
        shipment_id = result.shipment.shipment_id
        self.last_carrier_by_shipment[shipment_id] = result.carrier_id
        self.defer_until.pop(shipment_id, None)
        self.attempts[shipment_id] = self.attempts.get(shipment_id, 1) + 1
        self.pending.append(result.shipment)

    def on_monthly_planning(self, context: MonthlyPlanningContext) -> Tuple[FleetUpgradeDecision, ...]:
        """Invierte presupuesto sobrante en mejorar la flota."""
        self.carriers = context.carriers
        decisions = []
        
        if not context.upgrades_enabled or not context.decision_policy:
            return ()

        budget = (context.reference_budget_cents - context.spent_cents) * 0.5
        if budget <= 0:
            return ()

        available_upgrades = {u.upgrade_type: u for u in context.decision_policy.upgrades}
        sorted_carriers = sorted(self.carriers, key=lambda c: c.base_fee_cents)
        
        for carrier in sorted_carriers:
            for upg_type in carrier.supported_upgrades:
                if upg_type in available_upgrades:
                    upg_def = available_upgrades[upg_type]
                    if budget >= upg_def.cost_cents and upg_def.max_per_carrier_period > 0:
                        decisions.append(FleetUpgradeDecision(
                            carrier_id=carrier.carrier_id,
                            upgrade_type=upg_type,
                            quantity=1
                        ))
                        budget -= upg_def.cost_cents

        return tuple(decisions)

    def _select_best_carrier(self, batch: List[Shipment], context: DispatchContext, virtual_units: Dict[str, int]) -> Optional[CarrierSnapshot]:
        """Selecciona el transportista balanceando Costo vs SLA."""
        if not self.carriers:
            return None

        zone = batch[0].zone
        urgent_deadline = min(s.sla_deadline for s in batch)
        horas_para_vencer = (urgent_deadline - context.now).total_seconds() / 3600
        
        required_caps = set()
        tiene_vip = False
        for s in batch:
            required_caps.update(s.required_capabilities)
            if s.product in ("VIP_BLACK", "VIP"):
                tiene_vip = True

        opciones_viables = []

        for c in self.carriers:
            if not c.operates_now or virtual_units.get(c.carrier_id, 0) <= 0:
                continue
            if zone not in c.allowed_zones:
                continue
            if not required_caps.issubset(set(c.capabilities)):
                continue
                
            costo_extra_zona = next((zp.surcharge_cents for zp in c.zone_prices if zp.zone == zone), 0)
            costo_total = c.base_fee_cents + costo_extra_zona
            
            opciones_viables.append({
                "carrier": c,
                "costo": costo_total,
                "velocidad": c.speed_kmh
            })

        if not opciones_viables:
            return None

        if horas_para_vencer < 24 or tiene_vip:
            opciones_viables.sort(key=lambda x: (-x["velocidad"], x["costo"]))
        else:
            opciones_viables.sort(key=lambda x: (x["costo"], -x["velocidad"]))

        return opciones_viables[0]["carrier"]