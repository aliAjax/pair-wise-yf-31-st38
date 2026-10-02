# 航班中断恢复系统

独立的 Python 标准库项目，用 SQLite 保存机场、飞机、机组、航线许可、航班、中断事件和恢复方案。系统会校验维护间隔、执勤时限、机场宵禁、航线许可、资源重叠，并计算取消、延误、受影响旅客和错失衔接成本。

## 运行

```bash
python3 app.py --db airline_recovery.db
```

默认监听 `127.0.0.1:8202`，首页为 `/`，健康检查为 `/health`。

身份头：`X-User-Id`、`X-Role`。角色包括 `viewer`、`scheduler`、`ops_manager`、`auditor`、`atc`（空管）。

## 主要接口

- `POST /api/airports`、`/api/aircraft`、`/api/crew`、`/api/permits`：基础资源与约束。
- `POST /api/flights`、`POST /api/disruptions`：创建航班和中断。
- `POST /api/recovery-plans`：一次提交方案及航班调整；带 `"submit_for_approval": true` 时按方案版本登记待批事项（也可先存草稿再 `POST /api/plans/{id}/submit`）。
- `POST /api/plans/{id}/assignments`：用 `expected_revision` 临时改派；方案版本一变，旧版本未执行的批复立即失效（状态 `void`）并释放其额度，已执行航班保留，新版本重新登记待批事项。
- `POST /api/plans/{id}/validate`、`/lock`：草稿方案校验并原子锁定（已提交空管的方案不能直接锁定，放行由批复对账完成）。
- `POST /api/slot-pools`：夜间换场机场的临时起降额度池（机场、时间窗口、容量）。
- `GET /api/approvals`（可按 `status`、`plan_id` 过滤）、`GET /api/approvals/{item_no}`：待批/暂缓/已放行/撤回/失效清单、步骤轨迹、额度占用与撤回原因。
- `POST /api/approvals/{item_no}`：空管（`atc` 或 `ops_manager`）登记 `{"decision":"approved"|"withdrawn","atc_ref":...,"reason":...}`。
  - 同一事项重复批复只采第一次（响应带 `idempotent: true`），重复撤回不会覆盖首次原因；对旧版本事项的批复返回 `item_void`。
  - 批准后在占额前和执行前两次重新核对宵禁、维护、执勤、额度池/容量；未通过的航班置 `held` 且不占额度，修复后重试即可放行。
  - 通过的事项依次完成 `reserve_slot`（占用临时额度）与 `execute_flight`（按新时刻与飞机/机组执行航班），置 `released`。
  - 撤回（含晚到撤回）退回未执行部分并释放额度；航班已执行的不回滚，保留已占额度并记录说明。
- `POST /api/approvals/retry`：写入失败后按 `"item_no"` 续做未完成步骤（步骤带唯一记录，额度不会重复占用）；不带事项号则对账全部 `pending_atc/held` 项。步骤提交失败时接口返回 HTTP 202 和 `write_failed`。
- `GET /api/disruptions/{id}/compare`：比较恢复方案成本。
- `POST /api/flights/{id}/cancel`、`/recover`：取消和人工恢复。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响。首页控制台可查看待批、暂缓、已放行、撤回原因和额度占用，并可批准、撤回、重试。

### 批复对账状态

`pending_atc`（已按版本登记待批）→ 批复后：`released`（占额并执行）/ `held`（宵禁、维护、执勤或额度复核未过）/ `withdrawn`（撤回，未执行、额度已释放）；版本变更后旧事项为 `void`。每个事项保留 `steps` 轨迹（`reserve_slot`、`execute_flight`、`withdraw`、`void`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

时区和机场本地时刻没有引入完整时区数据库；模型使用简化航线许可与宵禁规则。身份头、SQLite 和单进程 HTTP 服务适合原型演示，正式运行需要外部身份系统、共享数据库和更强的跨实例锁。
