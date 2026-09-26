# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、入组后方案修订工作台（旧分层兼容性核对、审核期暂停入组）、隐藏分组、外部编号并发幂等、中心隔离、双人紧急揭盲和审计。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`site1`、`site2`（研究中心），`coord`（协调员），`monitor1`、`monitor2`（监查员）。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度和随机种子。
- `POST /api/trials/{id}/protocol`：入组前修改方案；一旦入组即锁定。
- `POST /api/trials/{id}/start`：开始入组。
- `GET /api/trials/{id}/config`：当前方案、分层因素及入组是否暂停。
- `POST /api/trials/{id}/enroll`：按当前用户中心入组；响应只返回分配编号，不返回分组；版本申请审核期间返回 `enrollment_paused`。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/trials/{id}/amendments`：入组开始后提交方案修订（新版本号、原因必填，自动快照已入组名单）。系统核对旧分层能否沿用：分层因素集合不变、旧分组全部保留、区组长度合法才进入 `pending_review`，否则直接 `returned`。
- `GET /api/trials/{id}/amendments`：版本申请记录（含已入组快照、兼容性核对结果、审核意见）。
- `POST /api/amendments/{id}/review`：审核通过（新版本生效，旧分层沿用）或退回；两种结论都会解除入组暂停并释放挂起的揭盲申请。
- `POST /api/participants/{id}/unblinding-requests`：监查员/中心/协调员遇严重不良事件可发起。版本申请未完成时申请停在 `materials_pending`，响应带回挂起的版本申请编号，版本审核结束后自动转 `pending`。
- `GET /api/trials/{id}/unblinding-requests`：查看揭盲状态；组别字段仅在双人确认通过后出现，监查员平时不可见。
- `POST /api/unblinding-requests/{id}/approve`：两名不同人员先后确认；发起人不能确认，同一人不能确认两次；通过后才返回组别。
- `GET /api/trials/{id}/summary`：中心级汇总、入组暂停标记和审计记录。

## 工作台页面

`/` 为方案修订与紧急揭盲同一工作台，脚本与样式分开维护：`web/index.html`、`web/styles.css`、`web/app.js`。可提交版本申请、查看兼容性核对与已入组快照、审核通过/退回，以及发起紧急揭盲并跟踪揭盲状态。

## 业务规则

- 入组开始后协调员不能直接改方案，只能提交版本修订；审核（通过或退回）期间暂停新入组。
- 兼容性核对：新版本的分层因素集合必须与旧方案一致，且不得删除已入组使用的分组，否则系统直接退回并给出原因。
- 紧急揭盲必须经两名不同人员确认，且任一确认人不得是发起人；两次确认完成前任何人都看不到组别。
- 版本申请未完成期间发起的揭盲停在「待补材料」，显示版本申请编号；版本审核一有结论即自动转「待确认」。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
