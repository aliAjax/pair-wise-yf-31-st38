# 航班中断恢复系统

独立的 Python 标准库项目，用 SQLite 保存机场、飞机、机组、航线许可、航班、中断事件和恢复方案。系统会校验维护间隔、执勤时限、机场宵禁、航线许可、资源重叠，并计算取消、延误、受影响旅客和错失衔接成本。

## 运行

```bash
python3 app.py --db airline_recovery.db
```

默认监听 `127.0.0.1:8202`，首页为 `/`，健康检查为 `/health`。

身份头：`X-User-Id`、`X-Role`。角色包括 `viewer`、`scheduler`、`ops_manager`、`auditor`。

## 主要接口

- `POST /api/airports`、`/api/aircraft`、`/api/crew`、`/api/permits`：基础资源与约束。
- `POST /api/flights`、`POST /api/disruptions`：创建航班和中断。
- `POST /api/recovery-plans`：一次提交方案及航班调整。
- `POST /api/plans/{id}/assignments`：用 `expected_revision` 临时改派。
- `POST /api/plans/{id}/validate`、`/lock`：校验并原子锁定方案。
- `GET /api/disruptions/{id}/compare`：比较恢复方案成本。
- `POST /api/flights/{id}/cancel`、`/recover`：取消和人工恢复。
- `POST /api/quotas`：按机场与夜窗口维护临时起降额度。
- `POST /api/plans/{id}/approval-items`：按方案版本登记待批事项并预留额度。
- `POST /api/approval-items/{item_no}/approvals`：登记批复（文号幂等，重复批复只采一次），重新核对宵禁、维护、执勤与额度占用后只放行通过的航班。
- `POST /api/approval-items/{item_no}/retry`：写入失败后按事项号重试未完成项。
- `POST /api/approval-items/{item_no}/withdraw`、`POST /api/plans/{id}/withdraw`：撤回未执行部分并释放额度。
- `GET /api/approval-console`：查看待批、已放行、撤回原因与额度占用。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

时区和机场本地时刻没有引入完整时区数据库；模型使用简化航线许可与宵禁规则。身份头、SQLite 和单进程 HTTP 服务适合原型演示，正式运行需要外部身份系统、共享数据库和更强的跨实例锁。
