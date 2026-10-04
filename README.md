# 控制茶文创试产批次协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

当前已内置 `batch_control` 试产批次控制模块，面向“茶·道”获奖设计的小规模试产：登记原料批次与供应证明、检验项目、领退料、工序产出、组件序列、包装组合与双人质量放行，并在每次事务后保持 投入 = 退料 + 合格 + 报废 + 返工 + 在制 的数量守恒。只有满足配方版本、检验与双人放行条件的组件才能进入下一工序；替代料、拆包重组、追加抽检与返工都保留原始谱系；发现污染或标签错误时沿真实用料关系定向冻结受影响的库存、订单和渠道批次，审计人员可从任一成品反查全部来源，也可从问题原料正向计算召回范围与数量差异。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/batch_control/：试产批次控制的表结构、领域服务、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由、批次控制规则和端到端验收测试。

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
    PYTHONPATH=src python3 -m batch_control.acceptance

基础服务验收会登记项目机构、操作者、业务节点和参考资料，核对幂等回执与审计链；批次控制验收会走通原料登记、检验、配方版本、幂等领料、替代料、工序守恒、双人放行、追加抽检、包装组合、销售渠道、污染定向冻结、正反向追溯与重启恢复。两条命令成功时都输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m batch_control.api --database batch_control.sqlite3 --host 127.0.0.1 --port 8081

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 批次控制接口

所有写接口都需要 request_id 保证幂等：同一扫码或重试只会执行一次，重复请求返回首次结果，不会重复扣料。角色分工：warehouse（仓库）登记批次与领退料，qc（质检）登记检验、双人放行与冻结，factory（工厂）工单与报工返工，brand（品牌方）订单与渠道发货，admin 管理配方版本，auditor 只读追溯。

- POST /batch/material-lots：登记原料批次与供应证明；
- POST /batch/inspections：登记检验项目（kind=additional 为追加抽检）；
- POST /batch/releases：双人放行（两次调用必须为不同质检人员）；
- POST /batch/recipes、POST /batch/recipes/activate：配方版本与启用（启用新版本自动停用旧版本）；
- POST /batch/work-orders：创建工单并锁定当前启用的配方版本；
- POST /batch/issues、POST /batch/returns：领料（支持 substituted_for 替代料）与退料；
- POST /batch/outputs：工序报工，产出组件序列或包装组合成品；
- POST /batch/reworks/resolve、POST /batch/components/rework、POST /batch/components/rework/resolve：返工处理；
- POST /batch/unpack：拆开包装组合，组件回到已放行库存，谱系保留；
- POST /batch/sales-orders、POST /batch/shipments：销售订单与渠道批次发货；
- POST /batch/freezes、POST /batch/freezes/lift：定向冻结与解除；
- GET /batch/trace?target_type=finished_unit&target_id=…：从成品反查全部来源；
- GET /batch/recall?target_type=material_lot&target_id=…：正向召回范围与数量差异；
- GET /batch/conservation?order_id=…：工序数量守恒核对；
- GET /batch/work-order?order_id=…：工单与工序台账。
