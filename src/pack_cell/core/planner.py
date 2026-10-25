"""積み付け計画: 実寸の品を、箱の内側のハイトマップ(1cm 刻み)の上にビームサーチで置いていく。

- 物理ルール(常に hard): 下の品で底面の 80% 以上を支える(支持面の段差 1cm 以内)・耐荷重(上に載る重さは下の品へ
  接触面積で配分し、さらにその下へも伝える)・箱の高さ・非食品を食品の上に置かない
- 注文の制約: hard は枝刈り、soft はコストに加算
- コスト: soft 違反 + 重心の高さ(重い物を下に) + 角に寄せる(後の品の場所を残す)
- 置き場所の候補: 壁と、置いた品の縁に沿う位置(extreme points) x 水平の向き(0°/90°)
解がないときは、制約・物理ルールを順に緩めた探索で「満たせない理由」を返す(LLM の修正・UI 用)。

追加注文の再計画では、箱に入っている品を固定条件として渡す(fixed)。上に何も載っていない品は
「一時的に取り出してよい」候補(removable)として、取り出す個数が少ない順に試す。
"""
import itertools, math
from dataclasses import dataclass, field

import numpy as np

from pack_cell import layout as L
from pack_cell.core.order import Issue

RES = L.HM_RES
WALL = 0.008                   # 箱の壁からの余白[m](置くときのずれで壁に当たらないように)
STEP_TOL = 0.0101              # 支持面の段差の許容
SUPPORT_MIN = 0.80             # 底面のうち支えられている割合の下限
COG_MARGIN = 0.03              # 重心(中心)の真下 ±この範囲は必ず支えられていること[m](端に寄った支えだと置くずれや揺れで傾く)
W_SOFT = 10.0                  # soft 違反 1 件のコスト
W_COG = 20.0                   # Σ m·z_center [kg·m] の重み
W_CORNER = 6.0                 # 角に寄せる(x を優先した位置 [m])。散らすと後から大きい品の入る場所が残らない
W_HARD_RELAX = 100.0           # 緩和探索での hard 制約違反
W_PHYS_RELAX = 1000.0          # 緩和探索での物理ルール違反
BEAM = 32
ADJ = 0.02                     # 「隣接」とみなす距離


def unit_sku(u):
    return u.split("#")[0]


@dataclass(frozen=True)
class Placement:
    unit: str                 # "<sku>#<k>"
    sku: str
    x: float                  # 箱の内側の中心を原点とした品の中心 [m]
    y: float
    yaw: float                # 0: 品の W が箱の x 方向 / π/2: y 方向
    bottom: float             # 箱の内底面からの底面高さ [m]
    size: tuple               # 置いた姿勢での (x 方向, y 方向, 高さ) [m]

    @property
    def height(self):
        return self.size[2]

    @property
    def top(self):
        return self.bottom + self.size[2]

    @property
    def layer(self):
        """表示・並び替え用: 箱の底に置いた品は 0、ほかの品の上なら 1。"""
        return 0 if self.bottom < 1e-3 else 1

    def center_xy(self):
        return (self.x, self.y)

    def world_bottom(self):
        return L.BOX_FLOOR_Z + self.bottom

    def rect(self):
        return (self.x - self.size[0] / 2, self.x + self.size[0] / 2, self.y - self.size[1] / 2, self.y + self.size[1] / 2)


def overlap_xy(p, q, margin=0.0):
    a, b = p.rect(), q.rect()
    return a[0] < b[1] - margin and b[0] < a[1] - margin and a[2] < b[3] - margin and b[2] < a[3] - margin


def above(q, p):
    """q が p の上(真上の範囲で、底が p の上面以上)にある。"""
    return overlap_xy(p, q, 0.002) and q.bottom >= p.top - 0.003


def _adjacent(a, b):
    """横に接している(すき間 ADJ 以内で、側面が向かい合う)か、上下に重なっている。"""
    if overlap_xy(a, b, 0.002):
        return True
    ra, rb = a.rect(), b.rect()
    gx = max(rb[0] - ra[1], ra[0] - rb[1])
    gy = max(rb[2] - ra[3], ra[2] - rb[3])
    zoverlap = a.bottom < b.top - 0.005 and b.bottom < a.top - 0.005
    return zoverlap and max(gx, gy) <= ADJ and min(gx, gy) < 0


@dataclass
class Plan:
    placements: dict = field(default_factory=dict)      # unit -> Placement
    supports: dict = field(default_factory=dict)        # unit -> [支持している unit]
    cost: float = 0.0
    violations: list = field(default_factory=list)      # 満たせなかった制約・ルール(文字列)
    removed: tuple = ()                                 # 再計画で一時的に取り出す unit
    box: str = "L"                                      # 箱のサイズ(layout.BOXES)

    def above(self, unit):
        return [u for u, ss in self.supports.items() if unit in ss]

    def to_dict(self):
        return {"placements": [{"unit": p.unit, "sku": p.sku, "layer": p.layer, "x": round(p.x, 4), "y": round(p.y, 4),
                                "yaw": round(p.yaw, 4), "bottom": round(p.bottom, 4), "size": [round(v, 4) for v in p.size],
                                "height": p.height, "center": [round(p.x, 4), round(p.y, 4)]}
                               for p in self.placements.values()],
                "box": self.box, "box_inner": [round(v, 4) for v in L.box_inner(self.box)],
                "supports": {u: list(s) for u, s in self.supports.items()},
                "cost": round(self.cost, 3), "violations": list(self.violations), "removed": list(self.removed)}


def placement_from_dict(d):
    return Placement(d["unit"], d["sku"], d["x"], d["y"], d["yaw"], d["bottom"], tuple(d["size"]))


class _Rules:
    def __init__(self, spec, cat, relax):
        self.cat, self.relax = cat, relax
        self.by_type = {}
        for c in spec.constraints:
            self.by_type.setdefault(c.type, []).append(c)

    def cost_of(self, c):
        if self.relax == "all":
            return W_HARD_RELAX if c.hard else W_SOFT
        return math.inf if c.hard else W_SOFT

    def phys_cost(self):
        return W_PHYS_RELAX if self.relax else math.inf

    def get(self, t):
        return self.by_type.get(t, [])


def _name(cat, sku):
    return cat[sku].name if sku in cat else sku


def order_edges(spec, units, plan=None):
    """積み順の先行関係(a を b より先に)。支持関係 + before / first / last。"""
    edges = set()
    if plan is not None:
        for u, ss in plan.supports.items():
            for s in ss:
                edges.add((s, u))
    by_sku = {}
    for u in units:
        by_sku.setdefault(unit_sku(u), []).append(u)
    for c in spec.constraints:
        if c.type == "before":
            for a in by_sku.get(c.args[0], []):
                for b in by_sku.get(c.args[1], []):
                    edges.add((a, b))
        elif c.type in ("first", "last"):
            for a in by_sku.get(c.args[0], []):
                for b in units:
                    if unit_sku(b) != c.args[0]:
                        edges.add((a, b) if c.type == "first" else (b, a))
    return edges


def find_cycle(nodes, edges):
    succ = {n: [] for n in nodes}
    for a, b in edges:
        succ.setdefault(a, []).append(b)
    color, stack = {}, []

    def dfs(n):
        color[n] = 1; stack.append(n)
        for m in succ.get(n, []):
            if color.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if color.get(m) is None:
                r = dfs(m)
                if r:
                    return r
        color[n] = 2; stack.pop()
        return None

    for n in nodes:
        if color.get(n) is None:
            r = dfs(n)
            if r:
                return r
    return None


class _State:
    __slots__ = ("hm", "owner", "placed", "supports", "share", "load", "cost", "viol", "sig")

    def copy(self):
        s = _State()
        s.hm, s.owner = self.hm.copy(), self.owner.copy()
        s.placed, s.supports, s.share = dict(self.placed), dict(self.supports), dict(self.share)
        s.load = dict(self.load)
        s.cost, s.viol, s.sig = self.cost, list(self.viol), self.sig
        return s


class Planner:
    def __init__(self, cat, box="L"):
        """box: 箱のサイズ(layout.BOXES のキー)。サイズごとに計画器を作る。"""
        self.cat = cat
        self.box = box
        inner = L.box_inner(box)
        self.use_x, self.use_y = inner[0] - 2 * WALL, inner[1] - 2 * WALL
        self.nx, self.ny = int(round(self.use_x / RES)), int(round(self.use_y / RES))
        self.inner_h = inner[2] - 0.003
        self.keep_top = 1
        self.last_top = []

    # ---- 入口 ----
    def plan_candidates(self, spec, k=40, fixed=None, max_remove=0):
        """コストの低い順に最大 k 個の実行可能な配置。スケジュールを考慮した選択(joint.choose)に使う。"""
        self.keep_top = k
        try:
            best, issues = self.plan(spec, fixed=fixed, max_remove=max_remove)
            cands = list(self.last_top) if best is not None else []
            for c in cands:
                c.removed = best.removed
        finally:
            self.keep_top = 1
        return cands, issues

    def plan(self, spec, fixed=None, max_remove=0):
        """(Plan or None, [Issue])。fixed: 箱に入っている {unit: Placement}(固定条件)。"""
        issues = self.precheck(spec, fixed)
        if any(i.kind in ("conflict", "capacity") for i in issues):
            return None, issues
        fixed = dict(fixed or {})
        units = [f"{s}#{k}" for s, k in spec.units()]
        for k in range(max_remove + 1):
            best, best_top = None, []
            for removed in self._removable_sets(fixed, k):
                fx = {u: p for u, p in fixed.items() if u not in removed}
                todo = [u for u in units if u not in fx]
                tops = self._search(spec, fx, todo, relax=None)
                if tops and (best is None or tops[0].cost < best.cost):
                    for t in tops:
                        t.removed = tuple(sorted(removed))
                    best, best_top = tops[0], tops
            if best is not None:
                self.last_top = best_top[:max(1, self.keep_top)]
                return best, issues
        todo = [u for u in units if u not in fixed]
        relaxed = self._search(spec, dict(fixed), todo, relax="phys")
        if relaxed:
            msg = "指示どおりに置くと物理的に成り立たない: " + " / ".join(dict.fromkeys(relaxed[0].violations))
        else:
            relaxed = self._search(spec, dict(fixed), todo, relax="all")
            msg = "全ての指示を同時に満たす配置がない"
            if relaxed and relaxed[0].violations:
                msg += ": " + " / ".join(dict.fromkeys(relaxed[0].violations))
        issues.append(Issue("infeasible", msg))
        self.last_top = []
        return None, issues

    def precheck(self, spec, fixed=None):
        issues = []
        vol = sum(self.cat[s].size[0] * self.cat[s].size[1] * self.cat[s].size[2] * q for s, q in spec.items.items())
        inner = L.box_inner(self.box)
        box = inner[0] * inner[1] * inner[2]
        if vol > 0.85 * box:
            issues.append(Issue("capacity", f"品物が箱に入りきらない(体積 {vol * 1000:.1f}L / 箱 {box * 1000:.1f}L)"))
        for s in spec.items:
            W, D, H = self.cat[s].size
            if H > self.inner_h or not ((W <= self.use_x and D <= self.use_y) or (D <= self.use_x and W <= self.use_y)):
                issues.append(Issue("capacity", f"{self.cat[s].name} は箱に入らない大きさ"))
        units = [f"{s}#{k}" for s, k in spec.units()]
        cyc = find_cycle(units, order_edges(spec, units))
        if cyc:
            names = " → ".join(_name(self.cat, unit_sku(u)) for u in cyc)
            issues.append(Issue("conflict", f"入れる順番の指定が矛盾している({names})"))
        for c in spec.constraints:
            if c.type == "first" and any(c2.type == "last" and c2.args[0] == c.args[0] for c2 in spec.constraints):
                issues.append(Issue("conflict", f"{_name(self.cat, c.args[0])} が最初と最後の両方に指定されている"))
        return issues

    def _removable_sets(self, fixed, k):
        """取り出す k 個の組合せ。上に載っている品は一緒に取り出す(下だけ抜くことはできない)。"""
        if k == 0:
            yield ()
            return
        for combo in itertools.combinations(sorted(fixed), k):
            if all(v in combo for u in combo for v, p in fixed.items() if v != u and above(p, fixed[u])):
                yield combo

    # ---- 探索 ----
    def _orders(self, spec, todo):
        """探索で品を置く順(何通りか)。on(a, b) なら b を先に。"""
        cat = self.cat
        keys = [lambda u: (-cat[unit_sku(u)].max_load, -_area(cat, u), u),     # 上に物を載せられる品から(下に来るべき品)
                lambda u: (-cat[unit_sku(u)].mass, -_vol(cat, u), u),
                lambda u: (-_vol(cat, u), -cat[unit_sku(u)].mass, u),
                lambda u: (-cat[unit_sku(u)].size[0] * cat[unit_sku(u)].size[1], -cat[unit_sku(u)].mass, u)]
        outs = []
        for key in keys:
            o = sorted(todo, key=key)
            for c in spec.constraints:            # on(a, b): b を a より前へ
                if c.type == "on":
                    a = [u for u in o if unit_sku(u) == c.args[0]]
                    b = [u for u in o if unit_sku(u) == c.args[1]]
                    if a and b and o.index(b[-1]) > o.index(a[0]):
                        for u in b:
                            o.remove(u)
                        i = o.index(a[0])
                        o[i:i] = b
            if o not in outs:
                outs.append(o)
        return outs

    def _search(self, spec, fixed, todo, relax):
        R = _Rules(spec, self.cat, relax)
        base = _State()
        base.hm = np.zeros((self.nx, self.ny))
        base.owner = np.full((self.nx, self.ny), -1, dtype=np.int32)
        base.placed, base.supports, base.share, base.load = {}, {}, {}, {}
        base.cost, base.viol, base.sig = 0.0, [], ()
        names = sorted(set(fixed) | set(todo))
        self._idx = {u: i for i, u in enumerate(names)}
        self._names = names
        for u, p in sorted(fixed.items(), key=lambda kv: kv[1].bottom):
            self._stamp(base, p, self._supports_of(base, p))
            base.cost += W_COG * self.cat[p.sku].mass * (p.bottom + p.height / 2)
        finals = []
        for order in self._orders(spec, todo):
            beam = [base]
            for u in order:
                nxt, seen = [], set()
                for st in beam:
                    for cand in self._candidates(R, st, u):
                        if cand.sig in seen:
                            continue
                        seen.add(cand.sig)
                        nxt.append(cand)
                nxt.sort(key=lambda s: s.cost)
                beam = nxt[:BEAM]
                if not beam:
                    break
            for st in beam:
                if len(st.placed) == len(fixed) + len(todo):
                    c2, v2 = self._final(R, spec, st)
                    if c2 < math.inf:
                        finals.append(Plan(dict(st.placed), {k: list(v) for k, v in st.supports.items()},
                                           st.cost + c2, st.viol + v2, box=self.box))
        uniq, seen = [], set()
        for p in sorted(finals, key=lambda p: p.cost):
            sig = tuple(sorted((v.sku, round(v.x, 3), round(v.y, 3), round(v.bottom, 3)) for v in p.placements.values()))
            if sig not in seen:
                seen.add(sig)
                uniq.append(p)
        return uniq[:max(1, self.keep_top)]

    def _supports_of(self, st, p):
        i0, i1, j0, j1 = self._cells(p)
        if p.bottom < 1e-4:
            return {}
        reg_h, reg_o = st.hm[i0:i1, j0:j1], st.owner[i0:i1, j0:j1]
        mask = reg_h >= p.bottom - STEP_TOL
        ids, counts = np.unique(reg_o[mask], return_counts=True)
        tot = counts.sum()
        return {self._names[i]: c / tot for i, c in zip(ids, counts) if i >= 0}

    def _cells(self, p):
        w, d = p.size[0] + L.GAP, p.size[1] + L.GAP
        x0 = p.x - w / 2 + self.use_x / 2
        y0 = p.y - d / 2 + self.use_y / 2
        i0, j0 = int(round(x0 / RES)), int(round(y0 / RES))
        return (max(i0, 0), min(i0 + int(math.ceil(w / RES - 1e-6)), self.nx),
                max(j0, 0), min(j0 + int(math.ceil(d / RES - 1e-6)), self.ny))

    def _stamp(self, st, p, sup):
        i0, i1, j0, j1 = self._cells(p)
        st.hm[i0:i1, j0:j1] = p.top
        st.owner[i0:i1, j0:j1] = self._idx[p.unit]
        st.placed[p.unit] = p
        if sup:
            st.supports[p.unit] = sorted(sup)
            st.share[p.unit] = sup
            self._add_load(st, p.unit, self.cat[p.sku].mass)
        st.sig = tuple(sorted(st.sig + ((p.sku, round(p.x, 3), round(p.y, 3), round(p.yaw, 2)),)))

    def _propagated(self, st, sup, m):
        """支え sup({unit: 割合})に重さ m を載せたとき、各品に新たにかかる重さ(下へ伝える)。"""
        out = {}

        def go(u, w):
            out[u] = out.get(u, 0.0) + w
            for s, f in st.share.get(u, {}).items():
                go(s, w * f)
        for s, f in sup.items():
            go(s, m * f)
        return out

    def _add_load(self, st, u, m):
        for s, f in st.share.get(u, {}).items():
            st.load[s] = st.load.get(s, 0.0) + m * f
            self._add_load(st, s, m * f)

    def _candidates(self, R, st, u):
        sku = self.cat[unit_sku(u)]
        W, D, H = sku.size
        yaws = [0.0] if (sku.round or abs(W - D) < 1e-6) else [0.0, math.pi / 2]
        out = []
        for yaw in yaws:
            sx, sy = (W, D) if yaw == 0.0 else (D, W)
            w, d = int(math.ceil((sx + L.GAP) / RES - 1e-6)), int(math.ceil((sy + L.GAP) / RES - 1e-6))
            if w > self.nx or d > self.ny:
                continue
            xs, ys = {0, self.nx - w}, {0, self.ny - d}
            for p in st.placed.values():
                i0, i1, j0, j1 = self._cells(p)
                xs |= {i1, i0 - w}
                ys |= {j1, j0 - d}
            for i in xs:
                if i < 0 or i + w > self.nx:
                    continue
                for j in ys:
                    if j < 0 or j + d > self.ny:
                        continue
                    r = self._try(R, st, u, sku, i, j, w, d, sx, sy, yaw)
                    if r is not None:
                        out.append(r)
        return out

    def _try(self, R, st, u, sku, i, j, w, d, sx, sy, yaw):
        reg = st.hm[i:i + w, j:j + d]
        bottom = float(reg.max())
        cost, viol = 0.0, []
        name = sku.name

        def bad(c, msg):
            nonlocal cost
            if c == math.inf:
                return True
            cost += c
            viol.append(msg)
            return False

        x = (i + w / 2) * RES - self.use_x / 2
        y = (j + d / 2) * RES - self.use_y / 2
        p = Placement(u, sku.id, x, y, yaw, bottom, (sx, sy, sku.height))
        if bottom + sku.height > self.inner_h and bad(R.phys_cost(), f"{name} を積むと箱の高さを超える({100 * (bottom + sku.height):.0f}cm)"):
            return None
        sup = {}
        if bottom > 1e-4:
            mask = reg >= bottom - STEP_TOL
            frac = float(mask.mean())
            if frac < SUPPORT_MIN and bad(R.phys_cost(), f"{name} の下の支えが足りない(底面の {100 * frac:.0f}%)"):
                return None
            cm = int(round(COG_MARGIN / RES))
            ci, cj = w // 2, d // 2
            if not mask[max(ci - cm, 0):ci + cm + 1, max(cj - cm, 0):cj + cm + 1].all() and \
                    bad(R.phys_cost(), f"{name} の重心の真下が支えられていない"):
                return None
            ids, counts = np.unique(st.owner[i:i + w, j:j + d][mask], return_counts=True)
            tot = counts.sum()
            sup = {self._names[k]: c / tot for k, c in zip(ids, counts) if k >= 0}
            for s, extra in self._propagated(st, sup, sku.mass).items():     # 下の品のさらに下まで、重さを伝えて確かめる
                low = self.cat[st.placed[s].sku]
                if st.load.get(s, 0.0) + extra > low.max_load + 1e-6:
                    if bad(R.phys_cost(), f"{low.name} に {name}({sku.mass}kg) の重さがかかり耐荷重 {low.max_load}kg を超える"):
                        return None
            for s, f in sup.items():
                ps = st.placed[s]
                sup_sku = self.cat[ps.sku]
                if not sku.food and sup_sku.food:
                    if bad(R.phys_cost(), f"非食品の {name} を食品の {sup_sku.name} の上に置けない"):
                        return None
                for c in R.get("nothing_on"):
                    if c.args[0] == ps.sku and bad(R.cost_of(c), f"{sup_sku.name} の上に何も載せない({c.evidence or c.source})"):
                        return None
                for c in R.get("layer"):
                    if c.args == (ps.sku, "top") and bad(R.cost_of(c), f"{sup_sku.name} は一番上に置く({c.evidence or c.source})"):
                        return None
                for c in R.get("last"):
                    if c.args[0] == ps.sku and bad(R.cost_of(c), f"{sup_sku.name} は最後に入れるので上に載せられない"):
                        return None
                for c in R.get("not_on"):
                    if c.args == (sku.id, ps.sku) and bad(R.cost_of(c), f"{name} を {sup_sku.name} の上に載せない({c.evidence or c.source})"):
                        return None
                for c in R.get("before"):
                    if c.args == (sku.id, ps.sku) and bad(R.cost_of(c), f"{name} を {sup_sku.name} より先に入れると、{sup_sku.name} の上に載せられない"):
                        return None
            for c in R.get("layer"):
                if c.args == (sku.id, "bottom") and bad(R.cost_of(c), f"{name} は箱の底に置く({c.evidence or c.source})"):
                    return None
            for c in R.get("first"):
                if c.args[0] == sku.id and bad(R.cost_of(c), f"{name} は最初に入れるので他の品の上に置けない"):
                    return None
        for c in R.get("on"):
            if c.args[0] == sku.id and not any(st.placed[s].sku == c.args[1] for s in sup) and \
                    bad(R.cost_of(c), f"{name} を {_name(self.cat, c.args[1])} の上に載せる({c.evidence or c.source})"):
                return None
        ns = st.copy()
        self._stamp(ns, p, sup)
        ns.cost += cost + W_COG * sku.mass * (bottom + sku.height / 2) + W_CORNER * (i + 0.5 * j) * RES
        ns.viol += viol
        return ns

    def _final(self, R, spec, st):
        """全品を置いた後に分かる制約: group / separate / 順序の閉路。"""
        cost, viol = 0.0, []
        by_sku = {}
        for u, p in st.placed.items():
            by_sku.setdefault(p.sku, []).append(p)
        for c in R.get("group"):
            ps = [p for s in c.args for p in by_sku.get(s, [])]
            if len(ps) > 1 and not self._connected(ps):
                cost += R.cost_of(c)
                viol.append("、".join(_name(self.cat, s) for s in c.args) + f" をまとめて置けない({c.evidence or c.source})")
        for c in R.get("separate"):
            if any(_adjacent(a, b) for a in by_sku.get(c.args[0], []) for b in by_sku.get(c.args[1], [])):
                cost += R.cost_of(c)
                viol.append(f"{_name(self.cat, c.args[0])} と {_name(self.cat, c.args[1])} を離して置けない")
        units = list(st.placed)
        cyc = find_cycle(units, order_edges(spec, units, Plan(st.placed, st.supports)))
        if cyc:
            cost += R.phys_cost()
            viol.append("入れる順番と積み重ねが矛盾する(" + " → ".join(_name(self.cat, unit_sku(u)) for u in cyc) + ")")
        return cost, viol

    @staticmethod
    def _connected(ps):
        seen, stack = {0}, [0]
        while stack:
            i = stack.pop()
            for j in range(len(ps)):
                if j not in seen and _adjacent(ps[i], ps[j]):
                    seen.add(j); stack.append(j)
        return len(seen) == len(ps)


def _area(cat, u):
    s = cat[unit_sku(u)].size
    return s[0] * s[1]


def _vol(cat, u):
    s = cat[unit_sku(u)].size
    return s[0] * s[1] * s[2]
