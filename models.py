from dataclasses import dataclass, field
from typing import List, Optional, Dict
from enum import Enum
from datetime import datetime


class MaterialType(str, Enum):
    FOSCA_SEM_COLA = "Fosca sem cola"
    FOSCA_COM_COLA = "Fosca com cola"
    LISA_SEM_COLA  = "Lisa sem cola"
    LISA_COM_COLA  = "Lisa com cola"


class BobinaSize(str, Enum):
    GRANDE  = "Grande"
    PEQUENA = "Pequena"


class MachineSize(str, Enum):
    GRANDE  = "Grande"
    PEQUENA = "Pequena"


@dataclass
class GlobalParams:
    bobina_length_m: float = 2000.0
    speed_mpm:       float = 225.0
    large_width_mm:  int   = 2000
    small_width_mm:  int   = 1000
    large_max_rolls: int   = 21
    small_max_rolls: int   = 11
    shift_start_h:   float = 8.0    # 08:00
    shift_end_h:     float = 17.0   # 17:00

    @property
    def cycle_time_min(self) -> float:
        return self.bobina_length_m / self.speed_mpm

    @property
    def shift_duration_min(self) -> float:
        return (self.shift_end_h - self.shift_start_h) * 60.0

    def minutes_remaining(self, now_h: float) -> float:
        """Minutos restantes no turno a partir de now_h (hora decimal)."""
        return max(0.0, (self.shift_end_h - now_h) * 60.0)


@dataclass
class OrderItem:
    item_id:    str
    order_id:   str
    width_mm:   int
    quantity:   int
    material:   MaterialType
    deadline_h: float
    produced:   int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.quantity - self.produced)


@dataclass
class BobinaStock:
    bobina_id: str
    size:      BobinaSize
    material:  MaterialType
    quantity:  int = 1


@dataclass
class Machine:
    machine_id:     str
    size:           MachineSize
    params:         GlobalParams = field(default_factory=GlobalParams)
    busy_until_h:   float = 0.0   # hora decimal em que a máquina fica livre

    @property
    def max_rolls(self) -> int:
        return self.params.large_max_rolls if self.size == MachineSize.GRANDE else self.params.small_max_rolls

    @property
    def bobina_width_mm(self) -> int:
        return self.params.large_width_mm if self.size == MachineSize.GRANDE else self.params.small_width_mm

    def accepts_bobina(self, b: "BobinaStock") -> bool:
        if self.size == MachineSize.PEQUENA:
            return b.size == BobinaSize.PEQUENA
        return True

    def is_free_at(self, now_h: float) -> bool:
        return self.busy_until_h <= now_h


@dataclass
class MaintenanceWindow:
    machine_id: str
    start_h:    float
    duration_h: float


@dataclass
class SetupParams:
    fixed_time_min:     float
    time_per_knife_min: float


@dataclass
class CuttingPattern:
    pattern_id:     str
    material:       MaterialType
    bobina_size:    BobinaSize
    items:          Dict[str, int]
    widths:         Dict[str, int]
    total_width_mm: int
    waste_mm:       int

    @property
    def total_rolls(self) -> int:
        return sum(self.items.values())

    @property
    def knife_positions(self) -> List[int]:
        positions, acc = [], 0
        for item_id, qty in self.items.items():
            for _ in range(qty):
                acc += self.widths[item_id]
                positions.append(acc)
        return positions[:-1]

    def knife_delta(self, previous: Optional["CuttingPattern"]) -> int:
        if previous is None:
            return len(self.knife_positions)
        return len(set(previous.knife_positions).symmetric_difference(set(self.knife_positions)))


@dataclass
class Pull:
    pull_id:        str
    pattern:        CuttingPattern
    bobina:         BobinaStock
    machine:        Machine
    position:       int
    locked:         bool  = False
    start_time_h:   float = 0.0   # hora real de início (decimal)
    end_time_h:     float = 0.0   # hora real de fim (decimal)

    @property
    def cycle_time_min(self) -> float:
        return self.machine.params.cycle_time_min

    def setup_time_min(self, p: SetupParams, prev: Optional[CuttingPattern]) -> float:
        return p.fixed_time_min + self.pattern.knife_delta(prev) * p.time_per_knife_min

    def total_time_min(self, p: SetupParams, prev: Optional[CuttingPattern]) -> float:
        return self.setup_time_min(p, prev) + self.cycle_time_min

    def exceeds_shift(self, params: GlobalParams) -> bool:
        return self.end_time_h > params.shift_end_h


@dataclass
class OPRecord:
    """Registro histórico de uma OP confirmada."""
    op_id:          str
    machine_id:     str
    confirmed_at:   str        # ISO datetime string
    pulls:          List[str]  # pull_ids
    items_produced: Dict[str, int]   # {item_id: qty}
    bobinas_used:   List[str]  # bobina_ids consumidas
    total_waste_mm: int
    start_time_h:   float
    end_time_h:     float


@dataclass
class OptimizationResult:
    pulls:                List[Pull]
    total_waste_mm:       float
    total_setup_min:      float
    total_delay_h:        float
    total_overproduction: int
    order_completion:     Dict[str, float]
    objective_value:      float
    solver_status:        str
