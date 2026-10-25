"""注文文 → OrderSpec → 検証・計画 のループ(LLM の解釈 + アルゴリズムの検証)。

1. LLM が注文文を OrderSpec(JSON)に変換する
2. カタログ照合と計画器で検証する
3. 問題があれば理由を LLM に返して直させる(最大 max_repair 回)
4. それでも駄目、または LLM が質問を返したら、オペレータへの確認(clarification)で止める
"""
from dataclasses import dataclass, field

from pack_cell.core import order as O
from pack_cell.llm import prompts as PR


@dataclass
class Interpretation:
    text: str
    spec: O.OrderSpec = None
    plan: object = None
    issues: list = field(default_factory=list)
    clarification: str = None
    rounds: list = field(default_factory=list)     # [{"out": LLM出力, "issues": [...]}]
    latency: float = 0.0

    @property
    def ok(self):
        return self.plan is not None and self.clarification is None


def interpret(text, cat, planner, llm, max_repair=2, use_repair=True):
    res = Interpretation(text)
    msgs = PR.parse_messages(text, cat)
    out = llm.json(msgs, O.ORDER_SCHEMA, "order")
    res.latency += llm.last_latency
    for r in range(max_repair + 1):
        spec, issues = O.from_json(out, cat)
        plan = None
        if spec.items:
            plan, plan_issues = planner.plan(spec)
            issues += plan_issues
        else:
            issues.append(O.Issue("empty", "品目が1つも特定できない"))
        res.rounds.append({"out": out, "issues": [str(i) for i in issues]})
        res.spec, res.plan, res.issues = spec, plan, issues
        blocking = [i for i in issues if i.kind != "unknown_sku"] or ([] if plan else issues)
        if spec.clarification:
            res.clarification = spec.clarification
            break
        if plan is not None and not blocking:
            break
        if not use_repair or r == max_repair:
            if plan is None:
                res.clarification = "注文を計画にできませんでした: " + " / ".join(str(i) for i in issues)
            break
        msgs = PR.repair_messages(msgs, out, issues)
        out = llm.json(msgs, O.ORDER_SCHEMA, "order")
        res.latency += llm.last_latency
    return res
