# Lead Scoring Rule Engine（线索打分与分发规则引擎）

把散落、改一次就要发版的打分逻辑，收敛成一套**可配置、可版本化、可回滚、
可灰度、可审计**的规则能力。纯 Python 标准库 + FastAPI，无表达式注入面
（自研安全 DSL，不用 `eval`）。

## 能力与需求对照

| # | 需求 | 实现位置 |
| --- | --- | --- |
| 1 | 版本化规则集；任意时刻只有一个完整发布版本生效，无字段级混合 | `store.py`（不可变版本文件 + 单个 active 原子指针），`engine._evaluate_active`（加锁取版本后整集评估） |
| 2 | 优先级与冲突消解；结果确定、可解释、带来源与权重 | `engine._canonical_rules` 按 `(-priority, id)` 排序；`RuleHit` 带 channel/source/weight |
| 3 | 热更新与回滚；发布失败/中断不影响生效版本；回滚与发布并发结果确定 | `RuleSetStore.activate`（tmp+`os.replace`）、`engine.publish/rollback`（同一把 RLock 串行化） |
| 4 | 批量并发打分；乱序/重复提交输出一致；重复提交无重复分发/副作用 | `_score_with` 线程池纯函数评估；`idempotency.py` 幂等键 + 单次 `score:dispatch` |
| 5 | 上线前预演/灰度；历史回放对比；未通过不得生效 | `engine.rehearse`、gates、`publish(..., rehearsal_leads=...)` |
| 6 | 单规则异常隔离；表达式/阈值错误不拖垮整批；可定位降级原因 | `DegradePolicy`、`RuleEvaluationError`（含 rule_id/version/type/message） |
| 7 | 全流程审计（版本、命中规则、输入）；关键操作权限校验；越权拒绝且留痕 | `audit.py`（append-only JSONL）、`auth.py`（RBAC），拒绝事件 `denied=true` |
| 8 | 自动化测试与文档 | `tests/`（76 个用例）、`docs/rules.md`、本文件 |

## 架构

```
lead_engine/
├── models.py        # 不可变领域类型：Rule/RuleSet/ScoreResult/AuditEvent...
├── expression.py    # 安全表达式 DSL：tokenizer/parser/AST/evaluator
├── config.py        # 规则集 JSON 解析、启动期全量校验（含表达式预解析）
├── store.py         # 版本持久化 + active 指针原子交换（崩溃安全）
├── engine.py        # 门面：打分/冲突消解/发布/回滚/预演/幂等/审计
├── idempotency.py   # 幂等键：同键只产生一次分发副作用
├── audit.py         # append-only JSONL 审计日志
├── auth.py          # 角色权限（rule_admin/rule_editor/reviewer/auditor/operator）
└── api.py           # FastAPI HTTP 接口
```

关键不变量：

* **整集一致**：一次打分只读取一个 `RuleSet` 对象，它在发布时就被完整校验
  （每条表达式都被解析），运行时不可能读到半成品版本。
* **原子切换**：激活 = 一次 `os.replace`；读者要么看到旧指针要么看到新指针。
* **纯函数评估**：打分过程无副作用；分发副作用由幂等键精确控制为一次。
* **确定排序**：所有规则遍历都按 `(-priority, id)`，并发/乱序不改变结果。

## 快速开始

```bash
uv sync
uv run uvicorn main:app --reload
# 或
uv run python main.py
```

持久化目录可在构造 `RuleEngine(store=RuleSetStore("./data"), ...)` 时指定；
默认是纯内存（适合测试）。

### HTTP 示例

鉴权通过请求头：`X-Actor`（操作者）、`X-Roles`（逗号分隔角色）。

```bash
# 1) 编辑者创建草稿
curl -s -XPOST localhost:8000/api/rulesets \
  -H 'Content-Type: application/json' \
  -H 'X-Actor: ed' -H 'X-Roles: rule_editor' \
  -d @- <<'JSON'
{"version":"v1","rules":[
  {"id":"hot","condition":"score >= 80","score_weight":40,
   "priority":100,"routing_queue":"hot-q","channel":"web"}]}
JSON

# 2) 预演（对历史线索回放，检查门禁）
curl -s -XPOST localhost:8000/api/rulesets/v1/rehearse \
  -H 'Content-Type: application/json' -H 'X-Actor: alice' -H 'X-Roles: rule_admin' \
  -d '{"leads":[{"id":"h1","attributes":{"score":90}}],
       "gates":{"max_abs_delta":1000,"max_queue_change_rate":1.0}}'

# 3) 管理员发布（可携带预演数据，未过门禁忌发）
curl -s -XPOST localhost:8000/api/rulesets/v1/publish \
  -H 'Content-Type: application/json' -H 'X-Actor: alice' -H 'X-Roles: rule_admin' \
  -d '{}'

# 4) 打分（带幂等键，重复提交不会重复分发）
curl -s -XPOST localhost:8000/api/score \
  -H 'Content-Type: application/json' -H 'X-Actor: svc' -H 'X-Roles: service' \
  -d '{"idempotency_key":"batch-001",
       "leads":[{"id":"L1","attributes":{"score":90,"country":"CN"}}]}'

# 5) 回滚
curl -s -XPOST localhost:8000/api/rulesets/rollback \
  -H 'Content-Type: application/json' -H 'X-Actor: alice' -H 'X-Roles: rule_admin' \
  -d '{"version":"v1"}'

# 6) 审计（仅 rule_admin / auditor）
curl -s 'localhost:8000/api/audit?action=ruleset:publish' \
  -H 'X-Actor: alice' -H 'X-Roles: rule_admin'
```

打分响应（节选）包含完整解释链：

```json
{"results":[{"lead_id":"L1","version":"v1","score":40,"tier":"hot",
  "routed_queue":"hot-q","degraded":false,
  "hits":[{"rule_id":"hot","rule_name":"hot","version":"v1","weight":40,
           "priority":100,"routing_queue":"hot-q","source":"web"}],
  "errors":[]}]}
```

### 直接使用 Python API

```python
from lead_engine.auth import Authorizer
from lead_engine.engine import RuleEngine
from lead_engine.models import Lead

roles = {"alice": ["rule_admin"], "svc": ["service"]}
eng = RuleEngine(authorizer=Authorizer(lambda a: roles.get(a, [])))
eng.create_draft({"version": "v1", "rules": [
    {"id": "hot", "condition": "score >= 80", "score_weight": 40,
     "priority": 100, "routing_queue": "hot-q"}]}, "alice")
eng.publish("v1", "alice")
print(eng.score_one(Lead("L1", {"score": 90}), "svc"))
```

## 权限模型

| 动作 | 允许角色 |
| --- | --- |
| `ruleset:create` | rule_editor, rule_admin |
| `ruleset:rehearse` | rule_editor, rule_admin, reviewer |
| `ruleset:publish` | rule_admin |
| `ruleset:rollback` | rule_admin |
| `score:submit` | rule_admin, operator, service |
| `audit:read` | rule_admin, auditor |

所有越权操作抛出 403 的同时写一条 `denied=true` 的审计事件。

## 持久化与崩溃恢复

给出目录后：

```
<dir>/versions/<version>.json   # 不可变版本（tmp+fsync+rename 落盘）
<dir>/active.json               # 唯一 active 指针（同上原子写）
audit.log                       # append-only 审计 JSONL
idem.json                       # 幂等键 -> 结果
```

* 版本文件写完才可见，因此不存在"写了一半的版本"。
* 进程在激活前崩溃：active 指针仍是旧版本，草稿留在磁盘上不会生效。
* 重启后 `RuleSetStore(path)` 自动恢复版本、状态与 active 指针。

## 测试

```bash
uv run pytest -q
```

覆盖：表达式语法/三值逻辑、配置校验、优先级冲突、规则异常三种降级、
批量乱序/重复一致性、高并发打分与发布/回滚互换、幂等重复提交只分发一次、
预演门禁阻断发布、越权拒绝留痕、跨重启持久化与半写文件不出现。

详见 [`docs/rules.md`](docs/rules.md)。
