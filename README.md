# 修复生态分析任务的越权领取基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- fixtures/：离线验收使用的调查协议与结构化观察记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
~~~

三条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析与风险处置，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

## 分析任务租约安全

分析任务（封存批次后生成）的领取与接管遵循以下规则，全部在 `taxonomy_lab` 中实现：

- **身份与资格**：领取、续作、完成、失败和接管都要求账号处于启用状态、具备 `analysis.run` 岗位权限（统计人员），并且账号被显式授予任务的 `task_family`（任务适用范围，来源于证据协议）。账号由审批人通过 `PATCH /users/{id}` 停用/启用、`POST /users/{id}/task_families` 授权；外协账号停用或未授权后无法再领取或提交结果。
- **可追溯租约**：每次占用都写入 `lease_owner`（操作者账号）和唯一的 `lease_fence_token`（栅栏令牌）；完成、失败、续作、接管都校验持有者与栅栏令牌。
- **到期接管**：租约到期后只有合格人员可通过再次领取或 `POST /jobs/{id}/takeover` 接管；旧持有者的迟到完成/失败一律以 409 拒绝，其结论在同一事务内回滚，不会覆盖新结论。续作使用 `POST /jobs/{id}/renew`。
- **稳定重放**：领取请求携带 `Idempotency-Key` 头时，同一账号、同一租约时长的重放返回首次结果；同键不同请求返回 409。
- **占用原因可查**：领取（`job.claimed`）、续作（`job.renewed`）、完成释放（`job.completed`）、失败释放（`job.failed`）、接管（`job.taken_over`）以及每次拒绝（`job.lease_rejected`，原因码如 `account_inactive`、`role_not_allowed`、`task_family_not_granted`、`lease_expired`、`stale_lease_holder`、`lease_active`）都按全局自增顺序写入 `lease_events`，可通过 `GET /jobs/lease_events`（或批次报告中的 `lease_events`）查询，服务重启后顺序完整还原。
