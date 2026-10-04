"""试产批次控制系统的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PilotReceipt:
    """一次幂等写操作的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool
    response: dict[str, Any] | None = None


@dataclass(frozen=True)
class StockSnapshot:
    """库存/在制守恒核对结果。

    对原料批次：投入 = 已领料 - 退料；在制 = 已领料 - 退料 - 报废 - 消耗。
    对工单产出：投入量 = 合格品 + 报废 + 返工 + 在制组件。
    """

    scope: str
    scope_id: str
    input_qty: float
    good_qty: float
    scrap_qty: float
    rework_qty: float
    wip_qty: float
    balanced: bool


@dataclass(frozen=True)
class LineageNode:
    """谱系追溯返回的单条用料关系。"""

    depth: int
    entity_type: str
    entity_id: str
    relation: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class FreezeImpact:
    """一次冻结事件沿真实用料关系计算出的影响范围。"""

    event_id: str
    source_kind: str
    source_id: str
    reason: str
    lots: list[str]
    work_orders: list[str]
    components: list[str]
    packages: list[str]
    channel_batches: list[str]
    orders: list[str]
    quantities: dict[str, Any]
