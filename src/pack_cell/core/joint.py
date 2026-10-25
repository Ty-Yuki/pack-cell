"""配置とスケジュールの同時最適化: 物理コストの低い配置候補を、2腕スケジュールの所要時間(DES)で選び直す。

score = makespan[s] + W_PLAN * 計画コスト。soft 違反は計画コストに 10 ずつ入っているので、
所要時間のために注文の配慮を捨てることは(数秒の短縮では)起きない。
"""
from pack_cell.core import dag as D, des

W_PLAN = 3.0


def choose(planner, spec, sources, strategy="S2", k=40, fixed=None, max_remove=0, **sim_kw):
    cands, issues = planner.plan_candidates(spec, k=k, fixed=fixed, max_remove=max_remove)
    if not cands:
        return None, issues, []
    scored = []
    for p in cands:
        d = D.build(spec, p, fixed)
        r = des.simulate(d, sources, strategy, **sim_kw)
        if r.deadlock:
            continue
        scored.append((r.makespan + W_PLAN * p.cost, r.makespan, p))
    if not scored:        # どの候補も DES で完了しない(在庫の見落としなど) → 物理コスト最良の候補を使う
        return cands[0], issues, []
    scored.sort(key=lambda t: t[0])
    return scored[0][2], issues, [(m, p.cost) for _, m, p in scored]
