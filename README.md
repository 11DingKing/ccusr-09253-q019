# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 解释图（申诉复核）

重放内核在相同输入上为单个学生重建最小因果子图，解释总学时由哪些事件、合并区间、确认和修正步骤得出。节点带来源版本（培养方案版本 + 规则版本）与输入指纹；相同输入多次计算结果一致；隐私角色（`staff`/`mentor`/`student`）只裁剪可见字段，节点标识与图摘要保持不变。

- `GET /api/plans/{pv}/students/{sid}/explanation?viewer_role=` — 实时最小因果子图
- `GET /api/plans/{pv}/freezes/{fid}/explanation/{sid}?viewer_role=` — 按冻结截止点重放的子图
- `GET /api/plans/{pv}/freezes/{fid}/explanation/{sid}/nodes/{node_id}/trace` — 节点追溯（因果祖先与受影响后代）
- `GET /api/plans/{pv}/freezes/{fid}/explanation/{sid}/verify` — 完整性核验（指纹、边一致性、确定性重放、与冻结快照交叉核对）
- `POST /api/plans/{pv}/freezes/{fid}/explanation/{sid}/export` — 受控导出（必填用途，按角色裁剪，附自描述清单与内容摘要）

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

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及解释图的重叠签到合并、负向修正钳零、规则版本溯源、字段裁剪、节点追溯、完整性核验与受控导出；运行过程中不需要单独的数据库或网络服务。
