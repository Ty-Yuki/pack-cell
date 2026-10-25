import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import pytest
from pack_cell import layout as L
from pack_cell.core import catalog, order, planner, dag as D, des
from pack_cell.core.scheduler import Source

CAT = catalog.load()
P = planner.Planner(CAT)


def stock_sources():
    from pack_cell.world_spec import stock_sources as ss
    return ss()


def mk(items, cons=()):
    spec, _ = order.from_json({"items": [{"sku": s, "qty": q} for s, q in items],
                               "constraints": [dict(type=t, args=list(a), source="explicit", evidence="") for t, a in cons]}, CAT)
    return spec


ORDERS = [
    mk([("rice", 1), ("canned", 2), ("egg", 1), ("bread", 1)], [("last", ["egg"])]),
    mk([("milk", 1), ("tofu", 1), ("ice", 1), ("canned", 1), ("egg", 1)], [("group", ["milk", "tofu", "ice"])]),
    mk([("water", 1), ("rice", 1), ("canned", 2), ("egg", 1), ("bread", 1), ("tofu", 1), ("ice", 1)]),
]


@pytest.mark.parametrize("spec", ORDERS)
@pytest.mark.parametrize("strategy", ["S0", "S1", "S2"])
def test_all_strategies_finish(spec, strategy):
    plan, _ = P.plan(spec)
    r = des.simulate(D.build(spec, plan), stock_sources(), strategy)
    assert not r.deadlock
    placed = [u for arm, k, u, *_ in r.timeline if k == "place"]
    assert sorted(placed) == sorted(plan.placements)


@pytest.mark.parametrize("spec", ORDERS)
def test_s2_not_slower(spec):
    plan, _ = P.plan(spec)
    d = D.build(spec, plan)
    m = {s: des.simulate(d, stock_sources(), s).makespan for s in ("S0", "S1", "S2")}
    assert m["S2"] <= m["S1"] + 1e-6 and m["S2"] < m["S0"], m


def test_order_respected():
    spec = ORDERS[0]
    plan, _ = P.plan(spec)
    d = D.build(spec, plan)
    r = des.simulate(d, stock_sources(), "S2")
    t_place = {u: t1 for arm, k, u, t0, t1 in r.timeline if k == "place"}
    for n in d.nodes:
        for p in d.preds[n]:
            if p in t_place and n in t_place:
                assert t_place[p] <= t_place[n] - 1e-9 or t_place[p] < t_place[n] + 1e-9


def test_replan_with_removal_runs():
    spec = mk([("lowfat_milk", 1), ("tofu", 1), ("ice", 1), ("canned", 1), ("egg", 1)], [("group", ["lowfat_milk", "tofu", "ice"])])
    plan, _ = P.plan(spec)
    fixed = dict(plan.placements)
    spec.items["water"] = 1
    p2, _ = P.plan(spec, fixed=fixed, max_remove=1)
    d = D.build(spec, p2, fixed)
    assert any(n.startswith("RM:") for n in d.nodes)
    r = des.simulate(d, stock_sources(), "S2", with_box=False)
    assert not r.deadlock
    kinds = [(k, u) for arm, k, u, *_ in r.timeline]
    rm = [n for n in d.nodes if n.startswith("RM:")][0]
    assert ("remove", rm) in kinds and ("place", rm[3:]) in kinds and ("place", "water#0") in kinds


def test_joint_choose_with_removal():
    from pack_cell.core import joint
    spec = mk([("lowfat_milk", 1), ("tofu", 1), ("ice", 1), ("canned", 1), ("egg", 1)], [("group", ["lowfat_milk", "tofu", "ice"])])
    plan, _ = P.plan(spec)
    fixed = dict(plan.placements)
    spec.items["water"] = 1
    p, issues, scored = joint.choose(P, spec, stock_sources(), fixed=fixed, max_remove=1, with_box=False)
    assert p is not None and p.removed, issues
    assert all(c.removed == p.removed for c in P.plan_candidates(spec, fixed=fixed, max_remove=1)[0])
