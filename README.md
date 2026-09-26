# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 解释图

重放内核在计算学时的同时产出稳定的因果解释图：每个节点带有来源版本（事件节点为计划版本，派生节点为规则版本）和覆盖全部因果输入的指纹，相同输入多次计算结果一致。冻结快照会保存当时的解释图，旧冻结可按截止事件确定性重建同一张图。

- `GET /api/plans/{plan}/explanation/students/{student}`：按学生（可选 `freeze_id`）生成最小因果子图，`viewer_role`（auditor/student/staff）决定可见字段，裁剪不改变节点标识与指纹。
- `GET /api/plans/{plan}/explanation/nodes/{node}`：追溯单个节点及其全部因果祖先。
- `GET /api/plans/{plan}/freezes/{freeze}/explanation/verify`：核验冻结图文档完整性，并用事件日志重放比对摘要。
- `GET /api/plans/{plan}/explanation/students/{student}/export`：受控导出，清单记录查看角色、裁剪口径与图摘要，受限字段仅 staff 角色可见。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及解释图的重叠签到合并、负向修正钳制、规则版本指纹、最小因果子图、字段裁剪与完整性核验；运行过程中不需要单独的数据库或网络服务。
