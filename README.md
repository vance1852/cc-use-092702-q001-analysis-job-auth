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

## 分析任务租约与权限（taxonomy_lab）

秋季鸟类同步调查等场景下，夜间声纹、样线观察和红外相机记录分属不同任务适用范围
（协议中的 `task_family`）。分析任务的领取与接管遵循以下规则：

- **统一身份**：领取、续作、完成、失败、释放全部通过 `X-Actor-Id` 鉴权，不再接受
  游离的 `worker_id`。账号必须存在且处于启用状态，且角色具备 `analysis.run`（统计人员）。
- **岗位与任务适用范围**：统计人员还必须持有管理员（`admin`）授予的对应任务族资格
  （`POST /qualifications`），否则轮询看不到、指定领取会被拒绝。停用外协账号
  （`POST /users/deactivate`）或撤销资格后，该账号不能再领取、续作、完成或失败任何任务。
- **可追溯租约**：租约绑定到用户编号（`lease_owner`/`claimed_by`）。到期任务只能由
  合格人员接管；接管会记录原持有者。旧持有者的迟到完成/失败一律拒绝，不能覆盖新结论。
- **幂等领取**：领取请求可带 `Idempotency-Key`，同一请求重放返回首次的稳定响应；
  同键不同内容返回冲突。
- **管理可解释**：每次占用（acquired）、接管（taken_over）、续作（renewed）、释放
  （released）、完成（succeeded）、失败（failed）和拒绝（rejected）都写入带严格序号的
  `lease_events` 表，含原因代码与文字说明，可通过 `GET /jobs/{id}/timeline` 审计查询，
  服务重启后顺序完整可还原。

| 接口 | 说明 |
| --- | --- |
| `POST /jobs/claim` | 领取（可带 `job_id`、`Idempotency-Key`）；过期任务由此接管 |
| `POST /jobs/{id}/renew` | 持有者续作租约 |
| `POST /jobs/{id}/complete` | 持有者在租约有效期内提交结论 |
| `POST /jobs/{id}/fail` | 持有者报告失败，可设 `retry_seconds` |
| `POST /jobs/{id}/release` | 持有者说明原因并主动释放 |
| `GET  /jobs/{id}/timeline` | 审计角色查询租约占用/拒绝/释放/接管顺序 |
| `POST /users/activate` `/users/deactivate` | 管理员启用/停用账号（需原因） |
| `POST /qualifications` `/qualifications/revoke` | 管理员授予/撤销任务适用范围资格 |
