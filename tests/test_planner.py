import sys, pathlib, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import pytest
from pack_cell import layout as L
from pack_cell.core import catalog, order, planner, dag

CAT = catalog.load()
P = planner.Planner(CAT)


def mk(items, cons=()):
    spec, issues = order.from_json({"items": [{"sku": s, "qty": q} for s, q in items],
                                    "constraints": [dict(type=t, args=list(a), source=src, evidence="") for t, a, src in cons]}, CAT)
    assert not issues, issues
    return spec


def check_physics(plan):
    ps = list(plan.placements.values())
    inner = L.box_inner(plan.box)                      # 計画が選んだ箱の内寸に収まる
    for p in ps:
        x0, x1, y0, y1 = p.rect()
        assert -inner[0] / 2 - 1e-6 <= x0 and x1 <= inner[0] / 2 + 1e-6
        assert -inner[1] / 2 - 1e-6 <= y0 and y1 <= inner[1] / 2 + 1e-6
        assert p.top <= inner[2] + 1e-9
        for q in ps:                                   # 品どうしが重ならない
            if q is not p and planner.overlap_xy(p, q, 0.002):
                assert q.bottom >= p.top - 0.003 or p.bottom >= q.top - 0.003, (p.unit, q.unit)
    for u, ss in plan.supports.items():
        for s in ss:
            assert planner.above(plan.placements[u], plan.placements[s])
    load = {u: 0.0 for u in plan.placements}           # 上に載る重さ(下へ伝える)が耐荷重以下

    def add(u, w):
        ss = plan.supports.get(u, [])
        for s in ss:
            load[s] += w / len(ss)
            add(s, w / len(ss))
    for u, p in plan.placements.items():
        add(u, CAT[p.sku].mass)
    for u, p in plan.placements.items():
        assert load[u] <= CAT[p.sku].max_load + 0.05, (u, load[u])


def test_scenario1_basic():
    s = mk([("rice", 1), ("canned", 2), ("egg", 1), ("bread", 1)], [("last", ["egg"], "explicit")])
    t0 = time.time(); plan, issues = P.plan(s); dt = time.time() - t0
    assert plan is not None, issues
    check_physics(plan)
    assert not plan.above("egg#0") and not plan.above("bread#0")
    order_ = dag.build(s, plan).topo()
    assert order_[0] == dag.BOX and order_[-1] == dag.LID and order_[-2] == "egg#0"
    assert dt < 5, dt


def test_heavy_items_go_low():
    s = mk([("water", 1), ("rice", 1), ("canned", 2), ("egg", 1), ("bread", 1), ("tofu", 1), ("ice", 1)])
    plan, issues = P.plan(s)
    assert plan is not None, issues
    check_physics(plan)
    assert plan.placements["rice#0"].bottom == 0.0


def test_conflicting_order():
    s = mk([("rice", 1), ("egg", 1)], [("before", ["rice", "egg"], "explicit"), ("before", ["egg", "rice"], "explicit")])
    plan, issues = P.plan(s)
    assert plan is None and any(i.kind == "conflict" for i in issues)


def test_capacity():
    s = mk([("water", 5), ("rice", 4)])
    plan, issues = P.plan(s)
    assert plan is None and any(i.kind == "capacity" for i in issues)


def test_group_cold():
    s = mk([("milk", 1), ("tofu", 1), ("ice", 1), ("chips", 1), ("canned", 1)], [("group", ["milk", "tofu", "ice"], "explicit")])
    plan, issues = P.plan(s)
    assert plan is not None and not plan.violations
    assert planner.Planner._connected([plan.placements[u] for u in ("milk#0", "tofu#0", "ice#0")])


def test_detergent_not_on_food():
    s = mk([("detergent", 1), ("rice", 1)])
    plan, _ = P.plan(s)
    assert "detergent#0" not in plan.supports


def test_on_constraint_heavy_on_egg_is_infeasible_with_reason():
    s = mk([("egg", 1), ("water", 1)], [("on", ["water", "egg"], "explicit")])
    plan, issues = P.plan(s)
    assert plan is None
    assert "耐荷重" in " ".join(str(i) for i in issues)


def test_on_constraint_satisfiable():
    s = mk([("rice", 1), ("egg", 1)], [("on", ["egg", "rice"], "explicit")])
    plan, _ = P.plan(s)
    assert plan.supports["egg#0"] == ["rice#0"]


def test_replan_add_item_with_removal():
    s = mk([("lowfat_milk", 1), ("tofu", 1), ("ice", 1), ("canned", 1), ("egg", 1)], [("group", ["lowfat_milk", "tofu", "ice"], "explicit")])
    plan, _ = P.plan(s)
    fixed = dict(plan.placements)
    s.items["water"] = 1
    p0, _ = P.plan(s, fixed=fixed, max_remove=0)
    p1, issues = P.plan(s, fixed=fixed, max_remove=1)
    assert p0 is None and p1 is not None and p1.removed, issues
    check_physics(p1)


def test_unknown_sku_is_reported():
    spec, issues = order.from_json({"items": [{"sku": "banana", "qty": 1}, {"sku": "egg", "qty": 1}], "constraints": []}, CAT)
    assert [i.kind for i in issues] == ["unknown_sku"] and spec.items == {"egg": 1}
