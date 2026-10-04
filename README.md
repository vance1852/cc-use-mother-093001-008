# 控制茶文创试产批次协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

`creative_program_foundation.pilot` 子包在此之上实现“茶·道”获奖设计的**试产批次控制系统**：原料批次与供应证明、检验项目、领退料与补报损耗、工序产出、组件序列、包装组合、双人质量放行，以及污染/标签错误时沿真实用料关系的精准冻结与正反向追溯。

## 试产批次控制能力

- **四方角色 API**：仓库（warehouse）、质检（qinspector）、工厂（factory）、品牌方（brand）通过 `/pilot/*` 接口执行各自步骤，admin 可跨角色操作。
- **原料谱系**：供应商、物料（茶叶/釉料/包装）、交付单、供应证明与产地逐批登记，不同产地/供应商不得混批；到货批次默认隔离待检，合格后方可发料；拆包/重组守恒并保留父子谱系，标签更正保留历史。
- **数量守恒**：每次事务后库存流水等于账面余量，工单保持 `投入 = 合格品 + 报废 + 返工 + 在制`，物料与组件均逐工序核对；在制报废不动仓库库存，退料不得超过真实在制量。
- **扫码与并发**：`scan_ref` 全局唯一，重复扫码不重复扣料；写事务串行化 + `BEGIN IMMEDIATE`，并发领料不会负库存或超计划需求；所有写接口支持 `request_id` 幂等重放。
- **放行闸门**：组件必须通过与工单配方版本一致的检验，并经质检与品牌方两名不同操作者双人放行后，才能进入下一工序；未放行/已冻结组件一律拒收。
- **替代料与逐件归属**：配方行可声明允许替代料；同一配方行由多个批次供料时必须逐件（`material_usage`）声明用料归属，作为精准召回的依据。
- **返工谱系**：返工支持原位修复与重建，重建产生新组件并保留 `reworked_from`、原投入组件与用料归属，谱系不中断。
- **精准冻结/召回**：从问题批次或组件出发，沿批次谱系、组件级用料、装配与返工关系只冻结真实受影响的库存、工单、包装、渠道批次（订单仅在全部渠道批次受影响时冻结）；支持只读召回范围与数量差异计算，以及按冻结前状态解冻。
- **持久化与审计**：未完成工单、隔离/冻结状态全部落盘，重启继续有效；所有动作进入哈希串联审计链，可从任一成品反查原料批次与供应证明，也可从问题原料正向计算召回范围。

### 主要接口

`POST /pilot/suppliers|materials|material-batches`、`/pilot/lots/receive|repack|relabel`、
`/pilot/inspection-specs`、`/pilot/inspections/lot|component`、
`/pilot/recipes`、`/pilot/work-orders`（及 `/close`）、
`/pilot/issues|returns|wip-scraps`、`/pilot/outputs`、
`/pilot/reworks/open|finish`、`/pilot/releases/request|approve|reject`、
`/pilot/orders|channel-batches|packages`、`/pilot/freezes`（及 `/lift`）、
`GET /pilot/recall`、`/pilot/components/{id}/trace`、`/pilot/work-orders/{id}/balance`。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/creative_program_foundation/pilot/：试产批次控制的表结构、领域服务、HTTP 路由与离线验收；
- tests/：基础规则、试产控制（服务 26 例 + HTTP 4 例 + 离线验收）、事务边界和端到端测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance
    PYTHONPATH=src python3 -m creative_program_foundation.pilot.acceptance

第一条命令验收基础登记链与审计链；第二条由四方角色走完试产全流程，核对扫码防重、数量守恒、双人放行、替代料精准召回（问题批次只牵连 3 个组件，订单与包装保持可售）以及重启后工单/冻结状态。成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
