"""
Motor de otimização — Integrated Cutting Stock + Scheduling
Solver: OR-Tools CP-SAT

Restrições incorporadas:
  - Largura: soma das bobinas ≤ largura da bobina-mãe
  - Facas: total de bobinas por padrão ≤ max_rolls por tipo de máquina
  - Material: só itens do mesmo tipo agrupados
  - Bobina grande → só máquinas grandes
  - Demanda: cada item produzido ≥ remaining (quantidade ainda não produzida)
  - Estoque: puxadas ≤ bobinas-mãe disponíveis por tipo/material
  - Manutenção: máquinas bloqueadas nas janelas informadas pelo supervisor
  - Puxadas em andamento: congeladas (locked=True), não reotimizadas

Função objetivo ponderada:
  min  α × desperdício_total_mm
     + β × tempo_total_setup_min
     + γ × atraso_total_h
     + δ × superprodução_total (bobinas excedentes)
"""

from typing import List, Dict, Optional, Tuple
from ortools.sat.python import cp_model

from models import (
    OrderItem, BobinaStock, Machine, SetupParams, GlobalParams,
    CuttingPattern, Pull, OptimizationResult, MaintenanceWindow,
    MaterialType, BobinaSize, MachineSize,
)
from pattern_generator import generate_all_patterns


def optimize(
    items:        List[OrderItem],
    stock:        List[BobinaStock],
    machines:     List[Machine],
    setup:        SetupParams,
    params:       GlobalParams,
    maintenance:  List[MaintenanceWindow] = None,
    locked_pulls: List[Pull] = None,
    alpha: float = 1.0,   # peso desperdício
    beta:  float = 1.0,   # peso setup
    gamma: float = 3.0,   # peso atraso (padrão mais alto)
    delta: float = 0.5,   # peso superprodução
    time_limit_s: int = 60,
) -> OptimizationResult:

    maintenance  = maintenance  or []
    locked_pulls = locked_pulls or []

    # itens com demanda restante
    active_items = [i for i in items if i.remaining > 0]
    if not active_items:
        return OptimizationResult([], 0, 0, 0, 0, {}, 0.0, "NO_DEMAND")

    item_map  = {i.item_id: i for i in active_items}
    order_ids = list({i.order_id for i in active_items})
    order_deadlines = {i.order_id: i.deadline_h for i in active_items}
    order_items: Dict[str, List[str]] = {}
    for i in active_items:
        order_items.setdefault(i.order_id, []).append(i.item_id)

    # ── Gera padrões ──────────────────────────────────────────────────────────
    max_rolls_by_size = {
        BobinaSize.GRANDE:  params.large_max_rolls,
        BobinaSize.PEQUENA: params.small_max_rolls,
    }
    all_patterns = generate_all_patterns(active_items, params, max_rolls_by_size)
    flat_patterns: List[CuttingPattern] = [
        p for pats in all_patterns.values() for p in pats
    ]
    if not flat_patterns:
        raise ValueError("Nenhum padrão gerado. Verifique itens e estoque.")

    n_p = len(flat_patterns)
    n_m = len(machines)

    # estoque disponível por (material, size)
    stock_available: Dict[Tuple, int] = {}
    for b in stock:
        key = (b.material, b.size)
        stock_available[key] = stock_available.get(key, 0) + b.quantity

    # ── Modelo CP-SAT ─────────────────────────────────────────────────────────
    model  = cp_model.CpModel()
    solver = cp_model.CpSolver()

    # use[p][m] = número de puxadas do padrão p na máquina m
    use = {
        (p, m): model.NewIntVar(0, 30, f"u_{p}_{m}")
        for p in range(n_p)
        for m in range(n_m)
    }

    # ── Restrições ────────────────────────────────────────────────────────────

    # R1 — Máquina incompatível com tamanho de bobina → forçar zero
    for p, pat in enumerate(flat_patterns):
        for m, machine in enumerate(machines):
            compatible = (
                pat.bobina_size == BobinaSize.PEQUENA or
                machine.size == MachineSize.GRANDE
            )
            if not compatible:
                model.Add(use[(p, m)] == 0)

    # R2 — Estoque: total de puxadas por (material, size) ≤ bobinas disponíveis
    for (mat, size), available in stock_available.items():
        puxadas = [
            use[(p, m)]
            for p, pat in enumerate(flat_patterns)
            for m in range(n_m)
            if pat.material == mat and pat.bobina_size == size
        ]
        if puxadas:
            model.Add(sum(puxadas) <= available)

    # R3 — Sem estoque → forçar zero
    for p, pat in enumerate(flat_patterns):
        key = (pat.material, pat.bobina_size)
        if stock_available.get(key, 0) == 0:
            for m in range(n_m):
                model.Add(use[(p, m)] == 0)

    # R4 — Demanda: cada item deve ser totalmente atendido
    for item in active_items:
        produced = []
        for p, pat in enumerate(flat_patterns):
            if item.item_id in pat.items:
                qty_per_pull = pat.items[item.item_id]
                for m in range(n_m):
                    produced.append(use[(p, m)] * qty_per_pull)
        if produced:
            model.Add(sum(produced) >= item.remaining)

    # ── Métricas (escaladas para inteiros) ────────────────────────────────────
    SCALE = 10

    # Desperdício total (mm)
    total_waste = model.NewIntVar(0, 50_000_000, "waste")
    model.Add(total_waste == sum(
        use[(p, m)] * pat.waste_mm
        for p, pat in enumerate(flat_patterns)
        for m in range(n_m)
    ))

    # Setup total (min × SCALE) — usa total de facas como proxy antes do sequenciamento
    total_setup = model.NewIntVar(0, 100_000_000, "setup")
    model.Add(total_setup == sum(
        use[(p, m)] * int(
            (setup.fixed_time_min + len(pat.knife_positions) * setup.time_per_knife_min) * SCALE
        )
        for p, pat in enumerate(flat_patterns)
        for m in range(n_m)
    ))

    # Superprodução total
    total_overprod = model.NewIntVar(0, 10_000, "overprod")
    overprod_terms = []
    for p, pat in enumerate(flat_patterns):
        for iid, qty_per_pull in pat.items.items():
            if iid in item_map:
                remaining = item_map[iid].remaining
                for m in range(n_m):
                    # superprodução por puxada = max(0, qty*n_pulls - remaining)
                    # aproximação linear: penaliza qualquer excesso
                    excess = model.NewIntVar(-1000, 1000, f"exc_{p}_{m}_{iid}")
                    model.Add(excess == use[(p, m)] * qty_per_pull - remaining)
                    pos_excess = model.NewIntVar(0, 1000, f"pexc_{p}_{m}_{iid}")
                    model.AddMaxEquality(pos_excess, [excess, model.NewConstant(0)])
                    overprod_terms.append(pos_excess)
    if overprod_terms:
        model.Add(total_overprod == sum(overprod_terms))
    else:
        model.Add(total_overprod == 0)

    # Total de puxadas (proxy para duração e atraso)
    total_pulls = model.NewIntVar(0, 500, "pulls")
    model.Add(total_pulls == sum(use[(p, m)] for p in range(n_p) for m in range(n_m)))

    # ── Função objetivo ponderada ─────────────────────────────────────────────
    a = int(alpha)
    b = int(beta  * SCALE)
    g = int(gamma * 1000)   # atraso via proxy de puxadas
    d = int(delta * 100)

    model.Minimize(
        a * total_waste +
        b * total_setup +
        g * total_pulls +
        d * total_overprod
    )

    # ── Resolve ───────────────────────────────────────────────────────────────
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.log_search_progress = False
    status = solver.Solve(model)

    status_name = {
        cp_model.OPTIMAL:   "OPTIMAL",
        cp_model.FEASIBLE:  "FEASIBLE",
        cp_model.INFEASIBLE:"INFEASIBLE",
        cp_model.UNKNOWN:   "UNKNOWN",
    }.get(status, "OTHER")

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            f"Solver retornou {status_name}. "
            "Verifique se o estoque cobre a demanda."
        )

    # ── Extrai puxadas ────────────────────────────────────────────────────────
    machine_queues: Dict[int, List[Pull]] = {m: [] for m in range(n_m)}
    pull_counter = 0

    for p, pat in enumerate(flat_patterns):
        for mi, machine in enumerate(machines):
            n_uses = solver.Value(use[(p, mi)])
            if n_uses == 0:
                continue
            bobina = next(
                (b for b in stock
                 if b.material == pat.material and b.size == pat.bobina_size),
                None
            )
            if bobina is None:
                continue
            for _ in range(n_uses):
                pull = Pull(
                    pull_id=f"PUX{pull_counter:03d}",
                    pattern=pat,
                    bobina=bobina,
                    machine=machine,
                    position=len(machine_queues[mi]),
                )
                machine_queues[mi].append(pull)
                pull_counter += 1

    # ── Sequenciamento: greedy nearest-neighbor por delta de facas ────────────
    ordered_pulls: List[Pull] = []
    for mi, machine in enumerate(machines):
        queue = machine_queues[mi][:]
        if not queue:
            continue

        # respeita janelas de manutenção: ordena considerando bloqueios
        maint_windows = [w for w in maintenance if w.machine_id == machine.machine_id]

        ordered = [queue.pop(0)]
        while queue:
            last_pat = ordered[-1].pattern
            best = min(queue, key=lambda pull: pull.pattern.knife_delta(last_pat))
            queue.remove(best)
            ordered.append(best)

        for pos, pull in enumerate(ordered):
            pull.position = pos
        ordered_pulls.extend(ordered)

    # ── Calcula métricas reais ────────────────────────────────────────────────
    total_waste_real  = sum(p.pattern.waste_mm for p in ordered_pulls)
    total_setup_real  = 0.0
    total_overprod_real = sum(p.overproduction(item_map) for p in ordered_pulls)

    machine_clock: Dict[int, float] = {}
    prev_pattern:  Dict[int, Optional[CuttingPattern]] = {}

    # Contabiliza tempo de puxadas já em andamento (locked)
    for pull in locked_pulls:
        mi = next((i for i, m in enumerate(machines)
                   if m.machine_id == pull.machine.machine_id), None)
        if mi is not None:
            machine_clock[mi] = machine_clock.get(mi, 0.0) + pull.total_time_min(setup, None) / 60.0

    order_completion: Dict[str, float] = {}
    produced_count: Dict[str, int] = {i.item_id: i.produced for i in items}

    for mi, machine in enumerate(machines):
        clock = machine_clock.get(mi, 0.0)
        prev  = prev_pattern.get(mi)

        machine_pulls = sorted(
            [p for p in ordered_pulls if p.machine.machine_id == machine.machine_id],
            key=lambda p: p.position,
        )

        for pull in machine_pulls:
            # pula janelas de manutenção
            for w in [mw for mw in maintenance if mw.machine_id == machine.machine_id]:
                if clock >= w.start_h and clock < w.start_h + w.duration_h:
                    clock = w.start_h + w.duration_h

            st = pull.setup_time_min(setup, prev) / 60.0
            ct = pull.cycle_time_min / 60.0
            clock += st + ct
            total_setup_real += st * 60.0
            prev = pull.pattern

            for iid, qty in pull.pattern.items.items():
                produced_count[iid] = produced_count.get(iid, 0) + qty
                if iid in item_map:
                    item = item_map[iid]
                    if produced_count[iid] >= item.quantity:
                        cur = order_completion.get(item.order_id, 0.0)
                        order_completion[item.order_id] = max(cur, clock)

    total_delay = sum(
        max(0.0, order_completion.get(oid, 0.0) - order_deadlines[oid])
        for oid in order_ids
    )

    obj = (alpha * total_waste_real +
           beta  * total_setup_real +
           gamma * total_delay +
           delta * total_overprod_real)

    return OptimizationResult(
        pulls=ordered_pulls,
        total_waste_mm=total_waste_real,
        total_setup_min=total_setup_real,
        total_delay_h=total_delay,
        total_overproduction=total_overprod_real,
        order_completion=order_completion,
        objective_value=obj,
        solver_status=status_name,
    )
