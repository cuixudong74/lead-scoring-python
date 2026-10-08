# Lead Scoring Rule Engine

一个版本化、可预演、可回滚、可审计的线索打分与分发规则引擎。

## 快速开始

```bash
pytest -q
PYTHONPATH=src uvicorn main:app --reload
```

核心能力和规则语义见 [docs/rules.md](docs/rules.md)。

## 最小 Python 示例

```python
from lead_scoring.models import Lead, Permission, Rule, RuleSet, User
from lead_scoring.service import RuleEngineService

admin = User("admin", frozenset({
    Permission.SCORE,
    Permission.PUBLISH,
    Permission.PREVIEW,
}))

ruleset = RuleSet(
    version="v1",
    pass_score=80,
    default_route="nurture",
    rules=(
        Rule(
            rule_id="hot",
            name="Hot",
            priority=100,
            condition={"field": "score", "op": "gte", "value": 80},
            points=100,
            route="sales",
        ),
    ),
)

service = RuleEngineService()
service.save_draft(ruleset, admin)
report = service.preview(ruleset, (Lead("l1", {"score": 90}),), admin)
service.publish("v1", admin, report)
print(service.score(Lead("l1", {"score": 90}), admin))
```

## 测试覆盖

- 条件 DSL 与嵌套布尔表达式
- 多规则优先级、同优先级确定性和路由冲突消解
- 单条规则异常隔离与可定位原因
- 预演门禁、历史线索回放与新旧版本差异
- 发布失败不影响当前版本
- 回滚与发布并发的确定性结果
- 批量乱序、重复线索和并发评分一致性
- 重复/并发分发的最多一次副作用
- HTTP 权限拒绝和审计追踪
