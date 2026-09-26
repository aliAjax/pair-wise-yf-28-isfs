# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲、入组后方案版本变更申请和审计。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`site1`、`site2`（研究中心），`coord`（协调员），`monitor1`、`monitor2`（独立监查员）。前端页面、样式（`web/static/styles.css`）与脚本（`web/static/app.js`）分开维护。

## 同一工作台的两条联动流程

### 方案版本变更申请（入组开始后）

协调员不能再直接改方案，改走版本申请：

- 申请必须填**变更原因**，**已入组名单由系统自动快照归档**（受试者编号、中心、分配编号、分层因素），不信任前端传入。
- 系统先核对**旧分层能否沿用到新版本**：分层因素集合必须完全一致；增删因素（旧受试者无取值/旧因素被删除）判为不兼容，申请**直接退回**并写明原因，不暂停入组。
- 兼容的申请进入“审核中”，试验**暂停新入组**（入组接口返回 `enrollment_paused` 与版本申请编号 `VA-xxxx`）；监查员填写审核意见后批准生效或退回，两种结论都会恢复入组；协调员可撤回自己未完结的申请。

### 紧急揭盲（SAE）

- 监查员平时在受试者列表/详情中看不到组别；监查员可作为揭盲发起人（按中心隔离规则除外）。
- 揭盲须经**两名不同人员**分别确认，且确认人不能是申请人；第二人确认通过后响应才返回 `arm`。
- 提交揭盲时若试验有“审核中”的版本申请，申请停在 **“待补材料”**（`state=awaiting_materials`），响应与界面都显示阻塞的版本申请编号；版本审核结束前补交材料会被挡回，结束后补交即转为待审批。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度和随机种子。
- `POST /api/trials/{id}/protocol`：仅入组前可修改方案；一旦入组即锁定（返回 `protocol_locked`，提示走版本申请）。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/enroll`：按当前用户中心入组；响应只返回分配编号，不返回分组；版本审核期间返回 `enrollment_paused`。
- `GET /api/trials/{id}/participants`：分中心返回数据，任何角色都看不到未揭盲者的组别。
- `POST /api/trials/{id}/version-applications`：协调员提交版本变更申请（原因+拟变更配置），系统归档名单并核对分层兼容性。
- `GET  /api/trials/{id}/version-applications`、`GET /api/version-applications/{id}`：申请列表/详情（详情含归档名单）。
- `POST /api/version-applications/{id}/review`：监查员批准（生效）或退回，需审核意见。
- `POST /api/version-applications/{id}/cancel`：协调员撤回自己的申请并恢复入组。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲；版本申请未完成时状态为待补材料。
- `POST /api/unblinding-requests/{id}/supplement`：版本审核结束后补交材料，转为待审批。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人或申请人不能审批。
- `GET  /api/trials/{id}/unblinding-requests`：揭盲状态列表（仅已批准记录带组别）。
- `GET /api/trials/{id}/summary`：中心级汇总、挂起状态、版本申请和审计记录。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
