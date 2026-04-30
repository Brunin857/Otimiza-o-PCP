"""
Gerador de padrões de corte.

Gera todas as combinações válidas de itens (de larguras diferentes,
de pedidos diferentes) que cabem numa bobina-mãe, respeitando:
  - Mesmo tipo de material
  - Soma de larguras ≤ largura da bobina-mãe
  - Total de bobinas ≤ max_rolls da máquina (limite de facas)
"""

from itertools import product as iproduct
from typing import List, Dict, Tuple
from models import OrderItem, CuttingPattern, MaterialType, BobinaSize, GlobalParams


def generate_patterns(
    items:          List[OrderItem],
    bobina_width_mm: int,
    bobina_size:    BobinaSize,
    material:       MaterialType,
    max_rolls:      int,
    max_patterns:   int = 2000,
) -> List[CuttingPattern]:
    """
    Gera padrões para um dado (material, tamanho de bobina).

    Itens de larguras e pedidos diferentes podem ser combinados
    no mesmo padrão, desde que material seja igual.
    """
    eligible = [i for i in items if i.material == material and i.remaining > 0]
    if not eligible:
        return []

    widths    = {i.item_id: i.width_mm for i in eligible}
    # limite por item: quanto cabe na largura E quanto ainda falta produzir
    max_qty = {
        i.item_id: min(i.remaining, bobina_width_mm // i.width_mm, max_rolls)
        for i in eligible
    }

    patterns: List[CuttingPattern] = []
    item_ids = [i.item_id for i in eligible]
    ranges   = [range(0, max_qty[iid] + 1) for iid in item_ids]

    pid = 0
    for combo in iproduct(*ranges):
        if pid >= max_patterns:
            break

        alloc = dict(zip(item_ids, combo))
        if all(v == 0 for v in alloc.values()):
            continue

        total_rolls = sum(alloc.values())
        if total_rolls > max_rolls:
            continue

        total_w = sum(widths[iid] * qty for iid, qty in alloc.items())
        if total_w > bobina_width_mm:
            continue

        used = {iid: qty for iid, qty in alloc.items() if qty > 0}
        waste = bobina_width_mm - total_w

        patterns.append(CuttingPattern(
            pattern_id=f"PAT{pid:04d}_{material.value[:3]}_{bobina_size.value[0]}",
            material=material,
            bobina_size=bobina_size,
            items=used,
            widths=widths,
            total_width_mm=total_w,
            waste_mm=waste,
        ))
        pid += 1

    return patterns


def generate_all_patterns(
    items:    List[OrderItem],
    params:   GlobalParams,
    max_rolls_by_size: Dict[BobinaSize, int],
) -> Dict[Tuple, List[CuttingPattern]]:
    """
    Gera padrões para todas as combinações de (material, tamanho de bobina).
    """
    bobina_widths = {
        BobinaSize.GRANDE:  params.large_width_mm,
        BobinaSize.PEQUENA: params.small_width_mm,
    }
    all_patterns = {}
    for material in MaterialType:
        for size in BobinaSize:
            key = (material, size)
            pats = generate_patterns(
                items=items,
                bobina_width_mm=bobina_widths[size],
                bobina_size=size,
                material=material,
                max_rolls=max_rolls_by_size[size],
            )
            if pats:
                all_patterns[key] = pats
    return all_patterns


# ── Teste ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from models import OrderItem, MaterialType, BobinaSize, GlobalParams

    params = GlobalParams()
    items = [
        OrderItem("I1", "P1", 100, 6, MaterialType.FOSCA_SEM_COLA, 48),
        OrderItem("I2", "P1", 200, 3, MaterialType.FOSCA_SEM_COLA, 48),
        OrderItem("I3", "P2", 300, 2, MaterialType.FOSCA_SEM_COLA, 24),
        OrderItem("I4", "P2", 150, 2, MaterialType.FOSCA_SEM_COLA, 24),
    ]

    all_pats = generate_all_patterns(
        items, params,
        max_rolls_by_size={BobinaSize.GRANDE: 21, BobinaSize.PEQUENA: 11}
    )

    for (mat, size), pats in all_pats.items():
        print(f"\n{mat.value} | {size.value} — {len(pats)} padrões")
        best = sorted(pats, key=lambda p: p.waste_mm)[:5]
        for p in best:
            parts = " + ".join(f"{q}×{p.widths[iid]}mm" for iid, q in p.items.items())
            print(f"  [{p.total_rolls} bobinas] {parts} | sobra={p.waste_mm}mm")
