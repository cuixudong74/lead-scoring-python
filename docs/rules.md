# 线索打分与分发规则引擎

## 1. 设计目标

本模块把原先散落在代码中的打分逻辑收敛为**不可变、可校验、可预演、可发布、可回滚、可审计**的规则集：

- 任意时刻只有一个完整版本的规则集生效；读取方拿到的是不可变快照，不会读取到字段级混合状态。
- 多条规则命中时，分数贡献全部保留；分发去向按确定的优先级规则消解冲突。
- 发布必须经过成功预演；发布失败或中途中断不会改变当前生效版本。
- 回滚与新的发布通过同一把操作锁和原子指针切换线性化。
- 批量、乱序、重复线索按 `lead_id` 去重并稳定输出；并发分发通过幂等键保证副作用最多一次。
- 单条规则异常被隔离，不影响其他规则和整批评估；错误定位到 `rule_id`、版本与原因。
- 权限拒绝、发布、回滚、打分、分发、审计读取均写入审计事件。

## 2. 规则集模型

```json
{
  "version": "2026.10.08-hot-leads",
  "pass_score": 80,
  "default_route": "nurture",
  "rules": [
    {
      "rule_id": "enterprise-webinar",
      "name": "企业客户且参加过线上会",
      "priority": 100,
      "condition": {
        "all": [
          {"field": "company.size", "op": "gte", "value": 500},
          {"field": "events", "op": "contains", "value": "webinar"},
          {"field": "region", "op": "in", "value": ["CN", "SG"]}
        ]
      },
      "points": 80,
      "weight": 1.5,
      "route": "field-sales",
      "error_policy": "degrade"
    }
  ]
}
```

### 顶层字段

| 字段 | 含义 |
| --- | --- |
| `version` | 字符串版本号，必须非空且不能重复保存。 |
| `rules` | 至少一条规则；`rule_id` 在同一版本内唯一。 |
| `pass_score` | 合格阈值；输出 `score >= pass_score` 时 `qualified=true`。 |
| `default_route` | 没有任何命中规则声明路由时使用的默认路由。 |

### 规则字段

| 字段 | 含义 |
| --- | --- |
| `rule_id` | 稳定规则标识，用于命中明细、异常定位和审计。 |
| `name` | 可读名称。 |
| `priority` | 整数，数值越大优先级越高。 |
| `condition` | 条件表达式，见下文 DSL。 |
| `points` | 基础分值。 |
| `weight` | 权重，不能为负；贡献值为 `points * weight`。 |
| `route` | 可选分发目标。 |
| `error_policy` | 规则异常策略：`ignore`、`degrade`（默认）、`skip_rule`。`ignore` 静默跳过；`skip_rule` 记录具体原因但不标记整批降级；`degrade` 记录原因并标记 `degraded=true`。 |

## 3. 条件 DSL

条件使用纯数据 JSON，不执行任意 Python 代码。

### 比较操作

| 操作符 | 语义 |
| --- | --- |
| `eq` | 等于，默认操作符。 |
| `ne` | 不等于。 |
| `gt` / `gte` | 大于 / 大于等于。 |
| `lt` / `lte` | 小于 / 小于等于。 |
| `in` | 字段值在给定列表中。 |
| `contains` | 容器包含给定值，或字符串包含子串。 |
| `exists` | 字段存在；只需 `{"field":"a.b","op":"exists"}`。 |
| `matches` | 字符串与 Python 全匹配正则 `re.fullmatch`。 |

字段路径支持点号，例如 `company.size`；缺失字段参与普通比较时结果为不命中。非法表达式或类型错误不会炸掉整批评分，而会进入该规则的错误处理路径。

### 布尔组合

```json
{"all": [条件1, 条件2]}
{"any": [条件1, 条件2]}
{"not": 条件}
```

组合可以嵌套。空条件 `{}` 恒为真，适合作为兜底规则。

## 4. 打分与冲突消解

1. 规则按 `(-priority, rule_id)` 排序评估，保证相同优先级下也有确定性顺序。
2. 所有命中规则都出现在 `hits` 中，每条明细包含：
   - `rule_id`
   - `source_version`
   - `priority`
   - `points`
   - `weight`
   - `contribution = points * weight`
   - `route`
3. 总分是所有命中贡献的确定性求和。
4. 分发冲突只在声明了 `route` 的命中规则中消解：优先级最高者胜；同优先级按最小 `rule_id` 胜。没有路由命中时使用 `default_route`。
5. 每个结果包含 `input_hash`，哈希输入包含版本、线索 ID 和规范化后的线索数据；字段书写顺序不同不会改变结果。

## 5. 版本生命周期

```text
save_draft -> validate -> preview/replay -> publish -> active
                                      ^           |
                                      |           v
                                  replay/preview rollback
```

- `save_draft`：做静态校验并以不可变快照保存为 `draft`。
- `preview`：用历史线索直接评估候选规则集，同时与当前生效版本对比。候选版本无需已保存；预演通过时记录规则集 checksum。
- `publish`：必须提供通过的预演报告，且报告 checksum 必须等于已保存版本 checksum。
- `rollback`：只能激活曾经成功发布过的版本。原生效版本状态变为 `rolled_back`。
- 当前生效指针与目标版本状态在同一个临界区原子切换。

失败语义：

- 校验失败、预演失败、checksum 不匹配：拒绝发布，当前版本继续生效。
- 找不到版本或目标从未发布：操作失败，当前版本继续生效。
- 发布和回滚并发：两个操作串行化，最终只会有一个完整发布版本生效。

## 6. 预演与灰度判断

`preview` 返回：

- `passed`：是否达到发布门禁。
- `reasons`：未通过原因。
- `results`：候选版本对历史线索的评分。
- `comparisons`：候选版本与当前版本的分数、路由差异。
- `baseline_version`：当前基线版本。
- `checksum`：候选规则集规范化哈希。

默认门禁要求：

1. 规则集静态校验通过；
2. 至少回放一条历史线索；
3. 候选规则没有产生降级结果。

可以通过 `require_no_degradation=false` 得到包含降级信息的试算报告，但该报告不会通过默认发布门禁。

## 7. 批量、并发与幂等

- 批量接口以 `lead_id` 去重，重复对象只输出一次。
- 输出按首次出现顺序排列；输入乱序不影响每个线索的分数、命中明细和路由。
- 批量评估使用线程池；所有评估均基于同一个不可变规则集快照。
- 分发幂等键由 `lead_id`、版本、规范化输入哈希和路由决定。
- 仓库使用 `put-if-absent` 语义，只有赢得竞争的请求执行一次发送回调。

## 8. 异常隔离

单条规则可能因为配置错误、字段类型不匹配或非法正则失败。引擎捕获规则边界内异常并输出：

```json
{
  "rule_id": "enterprise-webinar",
  "source_version": "2026.10.08-hot-leads",
  "reason": "TypeError: '>=' not supported between ..."
}
```

- 其他规则继续评估。
- `degrade`（默认）会记录错误并设置 `degraded=true`。
- `skip_rule` 会记录可定位的错误原因，但不把整批评分标记为降级。
- `ignore` 不记录错误明细；适用于明确允许的可选探测规则。
- 默认预演门禁会阻止包含 `degrade` 降级结果的版本生效。

## 9. 权限与审计

HTTP 请求头：

- `X-User-Id`：必填。
- `X-Permissions`：逗号分隔权限。

权限包括：

| 权限 | 操作 |
| --- | --- |
| `score` | 单条/批量打分。 |
| `dispatch` | 分发；调用者仍需具备 `score`。 |
| `ruleset:publish` | 草稿保存、发布。 |
| `ruleset:rollback` | 回滚。 |
| `ruleset:preview` | 预演、历史回放。 |
| `audit:read` | 查看审计。 |

审计事件包含 `event_id`、UTC 时间、操作者、动作、资源、是否允许和详情。越权操作抛出 `403/AuthorizationError`，同时写入 `allowed=false` 事件。

## 10. HTTP API

| 方法与路径 | 权限 | 说明 |
| --- | --- | --- |
| `GET /health` | 无 | 查询服务和当前版本。 |
| `POST /rulesets/draft` | `ruleset:publish` | 保存并静态校验草稿。 |
| `POST /rulesets/preview` | `ruleset:preview` | 预演请求体内的候选规则集并对比当前版本。 |
| `POST /rulesets/replay` | `ruleset:preview` | 回放已保存候选版本并对比当前版本。 |
| `POST /rulesets/publish` | `ruleset:publish` | 凭通过预演发布。 |
| `POST /rulesets/{version}/rollback` | `ruleset:rollback` | 回滚到已发布版本。 |
| `POST /score` | `score` | 单条评分。 |
| `POST /score/batch` | `score` | 批量、并发、去重评分。 |
| `POST /dispatch` | `score,dispatch` | 幂等分发。 |
| `GET /audit` | `audit:read` | 查看审计事件。 |

## 11. 存储边界说明

当前仓库实现是进程内、线程安全内存实现，适合单进程应用和自动化测试。生产环境若多进程部署，应替换 `RuleRepository`：

- 版本内容写入支持 CAS/事务的表或对象存储；
- 生效版本使用单行原子指针；
- checksum 唯一且不可变；
- 幂等分发表建立唯一键；
- 审计事件只追加不更新；
- 发布与回滚用数据库事务或 compare-and-swap 保持同样的原子提交语义。
