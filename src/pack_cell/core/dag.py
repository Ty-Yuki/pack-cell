"""積み順の DAG。計画(支持関係)と注文の順序制約から、各品を置く前に置き終わっているべき品を求める。

箱の供給(BOX)は全品より先、蓋(LID)は全品より後。再計画で取り出す品は REMOVE(u) → (置き直し) とする。
"""
from dataclasses import dataclass, field

from pack_cell.core.planner import order_edges, find_cycle, above, overlap_xy

BOX, LID = "BOX", "LID"


@dataclass
class Dag:
    nodes: list = field(default_factory=list)
    preds: dict = field(default_factory=dict)        # node -> set(先に終わっているべき node)

    def ready(self, done):
        """done(完了済み集合)を前提に、今すぐ始められる node。"""
        return [n for n in self.nodes if n not in done and self.preds[n] <= done]

    def topo(self):
        out, done = [], set()
        while len(out) < len(self.nodes):
            r = [n for n in self.ready(done) if n not in out]
            if not r:
                raise ValueError("DAG に閉路がある")
            out.append(r[0]); done.add(r[0])
        return out

    def depth(self):
        """各 node の最長先行チェーン長(スケジューラの優先度に使う)。"""
        d = {}
        for n in self.topo():
            d[n] = 1 + max((d[p] for p in self.preds[n]), default=-1)
        return d

    def to_dict(self):
        return {"nodes": self.nodes, "edges": [[p, n] for n in self.nodes for p in sorted(self.preds[n])]}


def build(spec, plan, fixed=None):
    """fixed: 既に箱に入っている {unit: Placement}(再計画時)。取り出さない品は DAG から外す(済み扱い)。
    plan.removed の品は RM:<unit>(箱 → 仮置き台)ノードを作り、置き直しの前に取り出す。"""
    fixed = fixed or {}
    removed = set(plan.removed)
    keep = [u for u in plan.placements if u not in fixed or u in removed]
    nodes = [BOX] + [f"RM:{u}" for u in sorted(removed, key=lambda u: -fixed[u].layer)] \
        + sorted(keep, key=lambda u: (plan.placements[u].layer, u)) + [LID]
    preds = {n: set() for n in nodes}
    for a, b in order_edges(spec, list(plan.placements), plan):
        if a in preds and b in preds:
            preds[b].add(a)
    for u in removed:
        old = fixed[u]
        preds[u].add(f"RM:{u}")
        for v in removed:                    # 上に載っている品を先に取り出す
            if v != u and above(fixed[v], old):
                preds[f"RM:{u}"].add(f"RM:{v}")
        for x in keep:                       # 空いた場所に置く品は、取り出しの後
            p = plan.placements[x]
            if x != u and overlap_xy(p, old, 0.002) and p.bottom < old.top - 0.003:
                preds[x].add(f"RM:{u}")
    for n in nodes:
        if n not in (BOX, LID):
            preds[n].add(BOX)
            preds[LID].add(n)
    if find_cycle(nodes, [(p, n) for n in nodes for p in preds[n]]):
        raise ValueError("積み順に閉路がある")
    return Dag(nodes, preds)
