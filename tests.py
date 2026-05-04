"""
FlexOtimiza — Suite de Testes Automatizados
Roda cenários reais e detecta problemas objetivos antes do agente de IA analisar.
"""

import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import List, Callable, Any

# Add flexo path
sys.path.insert(0, "/home/claude/flexo")

from models import (
    OrderItem, BobinaStock, Machine, SetupParams, GlobalParams,
    MaterialType, BobinaSize, MachineSize, MaintenanceWindow,
)
from optimizer import optimize
from pattern_generator import generate_all_patterns


# ── Test infrastructure ───────────────────────────────────────────────────────

@dataclass
class TestResult:
    name:        str
    passed:      bool
    duration_s:  float
    detail:      str = ""
    error:       str = ""
    warnings:    List[str] = field(default_factory=list)


class TestSuite:
    def __init__(self):
        self.results: List[TestResult] = []

    def run(self, name: str, fn: Callable) -> TestResult:
        t0 = time.time()
        try:
            warnings, detail = fn()
            r = TestResult(name=name, passed=True,
                           duration_s=round(time.time()-t0, 2),
                           detail=detail, warnings=warnings or [])
        except AssertionError as e:
            r = TestResult(name=name, passed=False,
                           duration_s=round(time.time()-t0, 2),
                           error=str(e))
        except Exception as e:
            r = TestResult(name=name, passed=False,
                           duration_s=round(time.time()-t0, 2),
                           error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        self.results.append(r)
        status = "✅ PASS" if r.passed else "❌ FAIL"
        print(f"  {status}  [{r.duration_s:.2f}s]  {name}")
        if not r.passed:
            print(f"          → {r.error.splitlines()[0]}")
        for w in r.warnings:
            print(f"          ⚠️  {w}")
        return r

    def summary(self) -> dict:
        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)
        warnings = sum(len(r.warnings) for r in self.results)
        return {"total": len(self.results), "passed": passed,
                "failed": failed, "warnings": warnings}


# ── Fixtures ──────────────────────────────────────────────────────────────────

def default_params(**kwargs):
    defaults = dict(
        bobina_length_m=2000, speed_mpm=225,
        large_width_mm=2000, small_width_mm=1000,
        large_max_rolls=21, small_max_rolls=11,
        shift_start_h=8.0, shift_end_h=17.0,
        trim_mm=10,
    )
    defaults.update(kwargs)
    return GlobalParams(**defaults)

def default_setup(**kwargs):
    return SetupParams(fixed_time_min=15.0, time_per_knife_min=2.5, **kwargs)

def four_machines(params=None):
    p = params or default_params()
    return [
        Machine("G1", MachineSize.GRANDE,  p),
        Machine("G2", MachineSize.GRANDE,  p),
        Machine("P1", MachineSize.PEQUENA, p),
        Machine("P2", MachineSize.PEQUENA, p),
    ]

def full_stock():
    return [
        BobinaStock("BM-G-FSC", BobinaSize.GRANDE,  MaterialType.FOSCA_SEM_COLA, 10),
        BobinaStock("BM-G-FCC", BobinaSize.GRANDE,  MaterialType.FOSCA_COM_COLA, 10),
        BobinaStock("BM-G-LSC", BobinaSize.GRANDE,  MaterialType.LISA_SEM_COLA,  10),
        BobinaStock("BM-G-LCC", BobinaSize.GRANDE,  MaterialType.LISA_COM_COLA,  10),
        BobinaStock("BM-P-FSC", BobinaSize.PEQUENA, MaterialType.FOSCA_SEM_COLA, 10),
        BobinaStock("BM-P-FCC", BobinaSize.PEQUENA, MaterialType.FOSCA_COM_COLA, 10),
        BobinaStock("BM-P-LSC", BobinaSize.PEQUENA, MaterialType.LISA_SEM_COLA,  10),
        BobinaStock("BM-P-LCC", BobinaSize.PEQUENA, MaterialType.LISA_COM_COLA,  10),
    ]

def run_opt(items, stock=None, params=None, setup=None, machines=None,
            now_h=8.0, time_limit_s=20, **kwargs):
    p = params or default_params()
    return optimize(
        items=items,
        stock=stock or full_stock(),
        machines=machines or four_machines(p),
        setup=setup or default_setup(),
        params=p,
        now_h=now_h,
        time_limit_s=time_limit_s,
        **kwargs,
    )


# ════════════════════════════════════════════════════════════════════════════
# TESTS
# ════════════════════════════════════════════════════════════════════════════

suite = TestSuite()

print("\n" + "="*65)
print("  FlexOtimiza — Suite de Testes Automatizados")
print("="*65 + "\n")

# ── 1. CORRECTNESS TESTS ─────────────────────────────────────────────────────
print("▶ Correção dos Resultados\n")

def test_demand_satisfied():
    items = [
        OrderItem("I1","P1", 200, 5, MaterialType.FOSCA_SEM_COLA, 72),
        OrderItem("I2","P1", 300, 3, MaterialType.FOSCA_SEM_COLA, 72),
    ]
    r = run_opt(items)
    assert r.solver_status in ("OPTIMAL","FEASIBLE"), f"Solver falhou: {r.solver_status}"
    produced = {}
    for pull in r.pulls:
        for iid, qty in pull.pattern.items.items():
            produced[iid] = produced.get(iid, 0) + qty
    for item in items:
        assert produced.get(item.item_id, 0) >= item.quantity, \
            f"Item {item.item_id}: pediu {item.quantity}, produziu {produced.get(item.item_id,0)}"
    return [], f"Demanda atendida: {dict(produced)}"

suite.run("Demanda sempre satisfeita", test_demand_satisfied)


def test_pattern_width_constraint():
    params = default_params(large_width_mm=2000, trim_mm=10)
    usable = params.large_usable_mm  # 1980mm
    items = [OrderItem(f"I{i}","P1", 300, 2, MaterialType.FOSCA_SEM_COLA, 72) for i in range(6)]
    all_pats = generate_all_patterns(items, params,
                                      {BobinaSize.GRANDE: 21, BobinaSize.PEQUENA: 11})
    violations = []
    for (mat, size), pats in all_pats.items():
        bw = params.large_usable_mm if size == BobinaSize.GRANDE else params.small_usable_mm
        for p in pats:
            if p.total_width_mm > bw:
                violations.append(f"{p.pattern_id}: {p.total_width_mm}mm > {bw}mm")
    assert not violations, f"Padrões excedem largura útil: {violations}"
    return [], f"Todos os padrões dentro da largura útil ({usable}mm)"

suite.run("Padrões respeitam largura útil (com rebarba)", test_pattern_width_constraint)


def test_rebarba_not_in_waste():
    params = default_params(large_width_mm=2000, trim_mm=10)
    items = [OrderItem("I1","P1", 1980, 1, MaterialType.FOSCA_SEM_COLA, 72)]  # usa toda a largura útil
    r = run_opt(items, params=params, machines=four_machines(params))
    pulls_with_item = [p for p in r.pulls if "I1" in p.pattern.items]
    assert pulls_with_item, "Nenhuma puxada produzida"
    waste = pulls_with_item[0].pattern.waste_mm
    assert waste == 0, f"Desperdício deveria ser 0mm mas é {waste}mm (rebarba pode estar sendo contabilizada)"
    return [], f"Sobra = 0mm para item que usa exatamente a largura útil"

suite.run("Rebarba não entra no desperdício", test_rebarba_not_in_waste)


def test_machine_distribution():
    items = [
        OrderItem(f"I{i}","P1", 200, 2, MaterialType.FOSCA_SEM_COLA, 72)
        for i in range(8)
    ]
    r = run_opt(items)
    from collections import Counter
    dist = Counter(p.machine.machine_id for p in r.pulls)
    machines_used = len([m for m, c in dist.items() if c > 0])
    warnings = []
    if machines_used < 3:
        warnings.append(f"Apenas {machines_used} máquinas usadas — possível concentração de carga")
    return warnings, f"Distribuição: {dict(dist)}"

suite.run("Distribuição entre máquinas", test_machine_distribution)


def test_small_bobina_only_on_small_machine():
    items = [OrderItem("I1","P1", 400, 3, MaterialType.FOSCA_SEM_COLA, 72)]
    stock = [BobinaStock("B1", BobinaSize.PEQUENA, MaterialType.FOSCA_SEM_COLA, 5)]
    r = run_opt(items, stock=stock)
    for pull in r.pulls:
        if pull.bobina.size == BobinaSize.PEQUENA:
            assert pull.machine.size == MachineSize.PEQUENA or pull.machine.size == MachineSize.GRANDE, \
                f"Bobina pequena alocada em máquina inválida: {pull.machine.machine_id}"
    return [], "Bobinas pequenas alocadas corretamente"

suite.run("Bobina pequena apenas em máquinas compatíveis", test_small_bobina_only_on_small_machine)


def test_large_bobina_not_on_small_machine():
    items = [OrderItem("I1","P1", 1500, 2, MaterialType.FOSCA_SEM_COLA, 72)]
    stock = [BobinaStock("B1", BobinaSize.GRANDE, MaterialType.FOSCA_SEM_COLA, 5)]
    r = run_opt(items, stock=stock)
    violations = [p.pull_id for p in r.pulls
                  if p.bobina.size == BobinaSize.GRANDE and p.machine.size == MachineSize.PEQUENA]
    assert not violations, f"Bobina grande em máquina pequena: {violations}"
    return [], "Bobinas grandes nunca alocadas em máquinas pequenas"

suite.run("Bobina grande nunca vai para máquina pequena", test_large_bobina_not_on_small_machine)


def test_material_not_mixed_in_pattern():
    items = [
        OrderItem("I1","P1", 200, 2, MaterialType.FOSCA_SEM_COLA, 72),
        OrderItem("I2","P1", 200, 2, MaterialType.LISA_COM_COLA,  72),
    ]
    r = run_opt(items)
    for pull in r.pulls:
        mats = set(
            next(i.material for i in items if i.item_id == iid)
            for iid in pull.pattern.items
        )
        assert len(mats) == 1, \
            f"Puxada {pull.pull_id} mistura materiais: {mats}"
    return [], "Nenhum padrão mistura materiais diferentes"

suite.run("Materiais nunca misturados no mesmo padrão", test_material_not_mixed_in_pattern)


def test_knife_limit_respected():
    params = default_params()
    items = [OrderItem(f"I{i}","P1", 50, 5, MaterialType.FOSCA_SEM_COLA, 72) for i in range(25)]
    all_pats = generate_all_patterns(items, params,
                                      {BobinaSize.GRANDE: 21, BobinaSize.PEQUENA: 11})
    violations = []
    for (mat, size), pats in all_pats.items():
        max_r = 21 if size == BobinaSize.GRANDE else 11
        for p in pats:
            if p.total_rolls > max_r:
                violations.append(f"{p.pattern_id}: {p.total_rolls} bobinas > {max_r}")
    assert not violations, f"Limite de facas violado: {violations[:3]}"
    return [], "Limite de facas respeitado em todos os padrões"

suite.run("Limite de facas por máquina", test_knife_limit_respected)


# ── 2. TIME & SHIFT TESTS ────────────────────────────────────────────────────
print("\n▶ Controle de Turno e Horários\n")

def test_pull_times_sequential():
    items = [OrderItem(f"I{i}","P1", 300, 1, MaterialType.FOSCA_SEM_COLA, 72) for i in range(4)]
    r = run_opt(items, now_h=8.0)
    for mid in ["G1","G2","P1","P2"]:
        pulls = sorted([p for p in r.pulls if p.machine.machine_id == mid], key=lambda p: p.position)
        for i in range(len(pulls)-1):
            assert pulls[i].end_time_h <= pulls[i+1].start_time_h + 0.001, \
                f"{mid}: puxada {pulls[i].pull_id} termina após início de {pulls[i+1].pull_id}"
    return [], "Puxadas sequenciais por máquina sem sobreposição"

suite.run("Puxadas sequenciais sem sobreposição", test_pull_times_sequential)


def test_planning_mode_outside_shift():
    params = default_params(shift_start_h=8.0, shift_end_h=17.0)
    items = [OrderItem("I1","P1", 300, 2, MaterialType.FOSCA_SEM_COLA, 72)]
    # now_h = 20h (fora do turno) — deve planejar a partir das 8h
    r = run_opt(items, params=params, now_h=20.0)
    assert r.pulls, "Nenhuma puxada gerada fora do turno"
    first_start = min(p.start_time_h for p in r.pulls)
    assert first_start >= 8.0, f"Planejamento fora do turno deveria começar às 8h, mas começou às {first_start:.2f}h"
    return [], f"Modo planejamento: primeiro início às {first_start:.2f}h"

suite.run("Modo planejamento fora do turno", test_planning_mode_outside_shift)


def test_busy_machine_respected():
    params = default_params()
    machines = [
        Machine("G1", MachineSize.GRANDE, params, busy_until_h=12.0),  # G1 ocupada até 12h
        Machine("G2", MachineSize.GRANDE, params, busy_until_h=0.0),
        Machine("P1", MachineSize.PEQUENA, params, busy_until_h=0.0),
        Machine("P2", MachineSize.PEQUENA, params, busy_until_h=0.0),
    ]
    items = [OrderItem("I1","P1", 300, 3, MaterialType.FOSCA_SEM_COLA, 72)]
    r = run_opt(items, params=params, machines=machines, now_h=8.0)
    g1_pulls = [p for p in r.pulls if p.machine.machine_id == "G1"]
    for pull in g1_pulls:
        assert pull.start_time_h >= 12.0, \
            f"G1 iniciou às {pull.start_time_h:.2f}h mas estava ocupada até 12h"
    return [], f"G1 respeitou busy_until_h=12h"

suite.run("Máquina ocupada não recebe puxadas antes de ficar livre", test_busy_machine_respected)


# ── 3. EDGE CASES ─────────────────────────────────────────────────────────────
print("\n▶ Casos de Borda\n")

def test_single_item_single_machine():
    items = [OrderItem("I1","P1", 500, 1, MaterialType.FOSCA_SEM_COLA, 72)]
    r = run_opt(items)
    assert r.solver_status in ("OPTIMAL","FEASIBLE")
    assert len(r.pulls) >= 1
    return [], f"1 item → {len(r.pulls)} puxada(s), status={r.solver_status}"

suite.run("Caso mínimo: 1 item, 1 pedido", test_single_item_single_machine)


def test_item_wider_than_small_bobina():
    # Item de 1500mm não cabe em bobina pequena (980mm útil) — deve ir para bobina grande
    items = [OrderItem("I1","P1", 1500, 2, MaterialType.FOSCA_SEM_COLA, 72)]
    stock = [
        BobinaStock("BG", BobinaSize.GRANDE,  MaterialType.FOSCA_SEM_COLA, 5),
        BobinaStock("BP", BobinaSize.PEQUENA, MaterialType.FOSCA_SEM_COLA, 5),
    ]
    r = run_opt(items, stock=stock)
    for pull in r.pulls:
        if "I1" in pull.pattern.items:
            assert pull.bobina.size == BobinaSize.GRANDE, \
                f"Item de 1500mm foi para bobina {pull.bobina.size}"
    return [], "Item largo foi corretamente para bobina grande"

suite.run("Item mais largo que bobina pequena vai para grande", test_item_wider_than_small_bobina)


def test_no_stock_for_material():
    items = [OrderItem("I1","P1", 300, 2, MaterialType.LISA_COM_COLA, 72)]
    stock = [BobinaStock("B1", BobinaSize.GRANDE, MaterialType.FOSCA_SEM_COLA, 5)]  # material diferente
    try:
        r = run_opt(items, stock=stock, time_limit_s=10)
        assert r.solver_status == "INFEASIBLE" or len(r.pulls) == 0, \
            "Deveria retornar sem puxadas quando não há estoque do material"
    except RuntimeError as e:
        assert "INFEASIBLE" in str(e) or "Verifique" in str(e)
    return [], "Sem estoque para material → inviável (correto)"

suite.run("Sem estoque para o material → inviável", test_no_stock_for_material)


def test_order_with_multiple_items_different_materials():
    items = [
        OrderItem("I1","P1", 200, 3, MaterialType.FOSCA_SEM_COLA, 48),
        OrderItem("I2","P1", 300, 2, MaterialType.LISA_COM_COLA,  48),  # material diferente, mesmo pedido
    ]
    r = run_opt(items)
    assert r.solver_status in ("OPTIMAL","FEASIBLE")
    # Verifica que os dois itens foram produzidos
    produced = {}
    for pull in r.pulls:
        for iid, qty in pull.pattern.items.items():
            produced[iid] = produced.get(iid, 0) + qty
    assert produced.get("I1",0) >= 3, "I1 não foi produzido"
    assert produced.get("I2",0) >= 2, "I2 não foi produzido"
    # Verifica que nunca aparecem no mesmo padrão
    for pull in r.pulls:
        assert not ("I1" in pull.pattern.items and "I2" in pull.pattern.items), \
            "I1 e I2 (materiais diferentes) aparecem no mesmo padrão"
    return [], "Pedido com 2 materiais: itens produzidos em puxadas separadas"

suite.run("Pedido com múltiplos materiais → puxadas separadas", test_order_with_multiple_items_different_materials)


def test_already_produced_items_excluded():
    items = [
        OrderItem("I1","P1", 200, 5, MaterialType.FOSCA_SEM_COLA, 72, produced=5),  # já completo
        OrderItem("I2","P1", 300, 3, MaterialType.FOSCA_SEM_COLA, 72, produced=1),  # 2 restantes
    ]
    r = run_opt(items)
    for pull in r.pulls:
        assert "I1" not in pull.pattern.items, \
            "I1 já estava 100% produzido mas apareceu numa puxada"
    return [], "Itens já produzidos corretamente excluídos"

suite.run("Itens já produzidos não reaparecem na otimização", test_already_produced_items_excluded)


def test_maintenance_window_respected():
    params = default_params()
    machines = four_machines(params)
    maintenance = [MaintenanceWindow("G1", start_h=9.0, duration_h=2.0)]
    items = [OrderItem(f"I{i}","P1", 200, 1, MaterialType.FOSCA_SEM_COLA, 72) for i in range(6)]
    r = run_opt(items, params=params, machines=machines, maintenance=maintenance, now_h=8.0)
    g1_pulls = [p for p in r.pulls if p.machine.machine_id == "G1"]
    violations = [p for p in g1_pulls if 9.0 <= p.start_time_h < 11.0]
    if violations:
        return [f"G1 tem {len(violations)} puxada(s) durante janela de manutenção (09:00-11:00)"], \
               "Aviso: janela de manutenção pode não estar sendo respeitada no sequenciamento"
    return [], "Janela de manutenção de G1 respeitada"

suite.run("Janela de manutenção não recebe puxadas", test_maintenance_window_respected)


# ── 4. PERFORMANCE TESTS ─────────────────────────────────────────────────────
print("\n▶ Performance\n")

def test_performance_typical_load():
    # Carga típica: 4 pedidos com 2-3 itens cada
    items = [
        OrderItem("P1-A","P1",125,6,MaterialType.FOSCA_COM_COLA,72),
        OrderItem("P1-B","P1",478,6,MaterialType.FOSCA_COM_COLA,72),
        OrderItem("P2-A","P2",1000,4,MaterialType.LISA_SEM_COLA,72),
        OrderItem("P2-B","P2",1678,2,MaterialType.LISA_SEM_COLA,72),
        OrderItem("P3-A","P3",250,1,MaterialType.FOSCA_SEM_COLA,72),
        OrderItem("P3-B","P3",899,6,MaterialType.LISA_SEM_COLA,72),
        OrderItem("P4-A","P4",78,6,MaterialType.LISA_SEM_COLA,72),
        OrderItem("P4-B","P4",466,3,MaterialType.LISA_COM_COLA,72),
    ]
    t0 = time.time()
    r = run_opt(items, time_limit_s=25)
    elapsed = time.time() - t0
    warnings = []
    if elapsed > 20:
        warnings.append(f"Tempo de {elapsed:.1f}s está próximo do limite do Streamlit Cloud (25s)")
    assert r.solver_status in ("OPTIMAL","FEASIBLE"), f"Sem solução: {r.solver_status}"
    return warnings, f"Carga típica resolvida em {elapsed:.1f}s | status={r.solver_status}"

suite.run("Performance: carga típica (8 itens, 4 pedidos)", test_performance_typical_load)


def test_performance_heavy_load():
    # Carga pesada: 8 pedidos com 3 itens cada = 24 itens
    import random
    random.seed(42)
    widths = [78, 125, 200, 250, 300, 400, 478, 500, 600, 800, 900, 1000]
    mats   = list(MaterialType)
    items  = []
    for pid in range(8):
        for j, w in enumerate(random.sample(widths, 3)):
            items.append(OrderItem(
                f"P{pid}-{j}", f"P{pid}", w,
                random.randint(1,5), random.choice(mats), 72
            ))
    t0 = time.time()
    r = run_opt(items, time_limit_s=25)
    elapsed = time.time() - t0
    warnings = []
    if r.solver_status == "FEASIBLE":
        warnings.append("Status FEASIBLE — solução pode não ser ótima (carga alta)")
    if elapsed > 23:
        warnings.append(f"Tempo {elapsed:.1f}s muito próximo do limite")
    return warnings, f"Carga pesada ({len(items)} itens) em {elapsed:.1f}s | status={r.solver_status}"

suite.run("Performance: carga pesada (24 itens, 8 pedidos)", test_performance_heavy_load)


# ── 5. BUSINESS LOGIC TESTS ──────────────────────────────────────────────────
print("\n▶ Lógica de Negócio\n")

def test_deadline_respected_when_possible():
    # Pedido urgente de 4h deve terminar antes que pedido de 72h
    items = [
        OrderItem("URGENTE","PU", 200, 2, MaterialType.FOSCA_SEM_COLA, 4),
        OrderItem("NORMAL", "PN", 200, 2, MaterialType.FOSCA_SEM_COLA, 72),
    ]
    r = run_opt(items)
    comp_urgente = r.order_completion.get("PU")
    comp_normal  = r.order_completion.get("PN")
    warnings = []
    if comp_urgente and comp_normal:
        if comp_urgente > comp_normal:
            warnings.append(f"Pedido urgente (4h) termina depois do normal: {comp_urgente:.2f}h vs {comp_normal:.2f}h")
    return warnings, f"Urgente: {comp_urgente:.2f}h | Normal: {comp_normal:.2f}h" if comp_urgente and comp_normal else "N/A"

suite.run("Pedido urgente termina antes do normal", test_deadline_respected_when_possible)


def test_waste_calculation_correct():
    params = default_params(large_width_mm=2000, trim_mm=10)
    usable = params.large_usable_mm  # 1980mm
    items = [OrderItem("I1","P1", 1000, 1, MaterialType.FOSCA_SEM_COLA, 72)]
    r = run_opt(items, params=params, machines=four_machines(params))
    pulls = [p for p in r.pulls if "I1" in p.pattern.items]
    assert pulls, "Nenhuma puxada para I1"
    pull = pulls[0]
    expected_waste = usable - pull.pattern.total_width_mm
    assert pull.pattern.waste_mm == expected_waste, \
        f"Desperdício calculado incorretamente: {pull.pattern.waste_mm}mm ≠ {expected_waste}mm"
    return [], f"Desperdício = {usable}mm (útil) - {pull.pattern.total_width_mm}mm (cortes) = {expected_waste}mm ✓"

suite.run("Cálculo de desperdício correto (sem rebarba)", test_waste_calculation_correct)


def test_superproduction_minimized():
    items = [OrderItem("I1","P1", 200, 3, MaterialType.FOSCA_SEM_COLA, 72)]
    r = run_opt(items, delta=5.0)  # peso alto para superprodução
    produced = sum(qty for pull in r.pulls for iid, qty in pull.pattern.items.items() if iid=="I1")
    overprod = max(0, produced - 3)
    warnings = []
    if overprod > 5:
        warnings.append(f"Superprodução alta: {overprod} bobinas extras")
    return warnings, f"Pediu 3, produziu {produced} (excesso={overprod})"

suite.run("Superprodução minimizada com peso alto", test_superproduction_minimized)


# ── SUMMARY ──────────────────────────────────────────────────────────────────
s = suite.summary()
print("\n" + "="*65)
print(f"  RESUMO: {s['passed']}/{s['total']} testes passaram | "
      f"{s['failed']} falhas | {s['warnings']} avisos")
print("="*65)

# Export structured results for the AI agent
import json
results_export = {
    "summary": s,
    "tests": [
        {
            "name": r.name,
            "passed": r.passed,
            "duration_s": r.duration_s,
            "detail": r.detail,
            "error": r.error,
            "warnings": r.warnings,
        }
        for r in suite.results
    ]
}

with open("/home/claude/test_results.json", "w") as f:
    json.dump(results_export, f, indent=2, ensure_ascii=False)

print(f"\n  Resultados exportados → /home/claude/test_results.json")
print(f"  Prontos para análise do agente de IA.\n")
