# 规则语义与配置项参考

本文件描述线索打分/分发规则引擎的配置格式、表达式语言与打分语义。
规则以**版本化规则集（RuleSet）**为最小发布单元：任何时刻对外生效的只有
一个完整发布的版本，评估过程不会读到跨版本/字段级混合的规则。

## 1. 规则集配置

```json
{
  "version": "v2026.10.1",
  "description": "10 月打分策略",
  "default_queue": "general",
  "default_degrade": "drop_rule",
  "rules": [
    {
      "id": "hot_cn",
      "name": "中国高热线索",
      "condition": "country == 'CN' and score >= 80",
      "score_weight": 40,
      "priority": 100,
      "routing_queue": "cn-hot",
      "channel": "web",
      "enabled": true
    }
  ],
  "score_bands": [
    {"name": "hot",  "min_score": 50, "max_score": 100},
    {"name": "cold", "min_score": 0,  "max_score": 49.999}
  ]
}
```

### 顶层配置项

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `version` | string | 是 | 版本号，全局唯一、**不可变**；重复创建会被拒绝 |
| `rules` | rule[] | 是 | 至少一条；`id` 在集内必须唯一 |
| `score_bands` | band[] | 否 | 分层（hot/warm/cold…），区间**不得重叠** |
| `default_queue` | string | 否 | 无命中或命中规则无队列时的兜底队列（默认 `default`） |
| `default_degrade` | string | 否 | 规则级异常的默认降级策略，见第 4 节 |
| `description` | string | 否 | 版本说明 |

### 规则（rule）字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 规则唯一标识，审计与错误定位都使用它 |
| `name` | string | 可读名称，缺省取 `id` |
| `condition` | string | 表达式，见第 2 节。**创建/发布时即被解析**，语法错误不会带到线上打分 |
| `score_weight` | number | 命中后对总分的贡献权重，可为负；总分最终被截断到 `[0,100]`（可配置） |
| `priority` | int | 优先级，数值越大越优先；相同则按 `id` 字典序，保证结果确定 |
| `routing_queue` | string \| null | 命中后的分发队列 |
| `channel` | string \| null | 规则来源渠道（如 web/partner），用于多渠道优先级对齐与命中来源展示 |
| `enabled` | bool | 是否参与评估，默认 true |

### 分层（band）字段

`name`、`min_score`、`max_score`；闭区间 `[min_score, max_score]`。
分层互不重叠，总分落入的第一个分层作为 `tier`，未命中任何分层则为 `null`。

## 2. 表达式语言

安全、确定性的小型 DSL（**不使用 `eval`，不能访问 Python 对象**）：

```
expr       := or_expr
or_expr    := and_expr ("or" and_expr)*
and_expr   := not_expr ("and" not_expr)*
not_expr   := "not" not_expr | comparison
comparison := operand (OP operand)?
OP         := "==" | "!=" | ">=" | "<=" | ">" | "<"
              | "in" | "not in" | "contains"
operand    := field | literal | list | "(" expr ")"
field      := IDENT ("." IDENT | "[" INT "]")*
list       := "[" [literal ("," literal)*] "]"
literal    := NUMBER | STRING | "true" | "false" | "null"
```

示例：

* `score >= 80 and country == 'CN'`
* `tier in ['hot', 'warm']`
* `tags contains 'vip'`
* `profile.age > 18 or source == 'ad'`
* `not unsubscribed`（裸标志字段缺失视为 false）
* `items[0] == 'a'`、`delta > -5`

### 缺失字段与三值逻辑

这是规则可解释性的关键：

* **裸字段真值测试**（如 `vip`、`not unsubscribed`）字段缺失时按 `false` 处理。
* **比较/阈值运算**（`missing.x > 1`）字段缺失时抛出类型化的
  `RuleExecutionError(missing_field=True)`，交给规则集的降级策略处理，
  绝不会把错误静默算成 0 分而无人知晓。
* `and` / `or` 采用三值逻辑：
  - `missing or true → true`，`missing or false → false`；
  - `missing and false → false`，`missing and true → false`
    （当前实现里未决定的缺失在布尔组合中不阻断已有确定结果）；
  - 只有在另一侧也无法决定时，缺失错误才向上传播。
* `in` / `contains` 右侧/左侧必须是列表、元组、字典或字符串，
  否则为类型错误；数字与字符串比较等类型不匹配同样抛错。

## 3. 打分与冲突消解

对每条线索：

1. 取规则集中所有 `enabled` 规则，按 **`(-priority, id)`** 规范排序。
2. 依次求值；命中规则的 `score_weight` 累加。
3. 总分 `score = clamp(0, 100, Σweight)`，并映射出 `tier`。
4. **分发队列**：命中列表中排序第一（优先级最高、同优先级 id 最小）的
   规则的 `routing_queue`；若该规则无队列则继续向下找，仍没有则用
   `default_queue`。这样多个渠道的规则命中同一条线索时，队列选择确定、
   可复现，且每一条命中都带 `source`（渠道）与 `weight`（贡献）。
5. 输出包含 `hits`（规则 id/name/version/weight/priority/queue/source）与
   `errors`（定位到具体规则），保证结果可解释。

评估是纯函数：输入乱序、重复提交、多线程并发，对同一线索的输出一致。

## 4. 规则异常隔离与降级策略

单条规则出错（表达式类型错误、阈值引用缺失字段等）**不会让整批失败**，
按规则集 `default_degrade` 处理，并在结果 `errors` 中给出规则 id、
版本、错误类型与原因：

| 策略 | 行为 |
| --- | --- |
| `drop_rule`（默认） | 丢弃该规则本次贡献，其它规则正常打分，`degraded=true` |
| `neutral` | 该规则贡献按 0 处理并记录错误 |
| `fail_batch` | 立即中止本批评估并抛出，适合强一致场景 |

语法错误在创建/发布阶段就被拒绝，因此线上只可能遇到**数据相关**的运行时错误。

## 5. 版本生命周期

```
DRAFT ──publish──▶ ACTIVE ──新版本发布──▶ SUPERSEDED ──rollback──▶ ACTIVE
```

* **DRAFT**：可编辑语义上的唯一阶段（实际上版本不可变，改规则请新建版本），
  永远不会用于打分。
* **ACTIVE**：唯一对外生效版本。
* **SUPERSEDED**：已发布但当前不生效，**保留且可回滚**。
* 发布前可对历史线索做**预演（rehearse）**，只有通过全部门禁才能激活。
* 激活是一次原子的指针交换（磁盘上是 tmp + `os.replace`）：
  发布失败或进程中途中断，旧版本继续生效，不会出现半写版本。

### 预演门禁（gates）

| 门禁 | 默认 | 含义 |
| --- | --- | --- |
| `max_abs_delta` | 1e6 | 任一线索新旧版本分差的最大绝对值 |
| `max_queue_change_rate` | 1.0 | 队列发生变化的线索占比上限 |
| `max_error_rate` | 1.0 | 出现规则错误的线索占比上限 |

任一门禁不通过，`passed=false` 且给出 `failure_reasons`，
带预演数据的发布会被拒绝（HTTP 422），线上版本保持不变。
