"""2腕の協調スケジューラ(方針)。実機(Gazebo)の実行器とオフラインの離散イベントシミュレーションで共用する。

アームが手空きになるたびに next_action(arm) を呼び、行動を1つ受け取る。行動の終了は done()/failed() で知らせる。
干渉を避ける資源:
  zone       梱包位置の上空(同時に1台)。品を置く・自分の上流側と下流側の棚を行き来する(ベースの前を通る)ときに占有する
  shelf_L/R  棚(同時に1台)。取り出しの間だけ占有する
箱はコンベヤが運ぶ(line=True)。空箱が梱包位置に着いたら実行器が BOX を済みにし、全品を置いたら封函・出荷はラインが行う。
(line=False は、アームAが箱・蓋・出荷を扱っていた旧レイアウトの動作)
先取り(S2): 品 u を取りに行ってよいのは、u の先行品がすべて「置き終わった」か「相手が持っている」とき(デッドロック防止)。

戦略:
  S0 逐次       どちらか1台しか動かない(1台相当の基準)
  S1 固定分担   各アームは自分の棚の品だけ。先取りなし(先行品が置かれてから取りに行く)
  S2 提案手法   先取り + 共有スロットの動的割り当て + トポロジカル順のゾーン付与
"""
from dataclasses import dataclass, field

from pack_cell.core.dag import BOX, LID

SHIP = "SHIP"
A, B = "arm_a", "arm_b"
OTHER = {A: B, B: A}
OWNER_SHELF = {A: "L", B: "R"}


@dataclass
class Action:
    kind: str                 # box | fetch | place | lid | ship | remove | cross | park | wait
    arm: str
    unit: str = None
    source: object = None     # fetch: Source
    locks: tuple = ()
    side: int = 0             # fetch/remove の後に待つ側(-1: 上流側の棚, +1: 下流側の棚)

    def __str__(self):
        return f"{self.arm}:{self.kind}" + (f"({self.unit})" if self.unit else "")


@dataclass(frozen=True)
class Source:
    """品の取り出し元。棚のスロットか、仮置き台(buffer)。shared: 両アームが届く共有スロット。
    side: アームから見た側(-1: 上流側の棚, +1: 下流側の棚。仮置き台は ArmB の下流側)。"""
    model: str
    shelf: str               # "L" | "R" | "buffer"
    shared: bool = False
    side: int = 1

    def reachable_by(self, arm):
        if self.shelf == "buffer":
            return arm == B
        return OWNER_SHELF[arm] == self.shelf or self.shared


def is_removal(n):
    return n.startswith("RM:")


@dataclass
class Scheduler:
    """dag のノード: BOX / 各 unit / RM:<unit>(再計画で箱から仮置き台へ取り出す) / LID。"""
    dag: object
    strategy: str = "S2"
    with_box: bool = True
    with_lid: bool = True
    with_ship: bool = True
    line: bool = True                                    # 箱はコンベヤが運ぶ(BOX は実行器が済みにする)
    lookahead: int = 3
    done: set = field(default_factory=set)
    holding: dict = field(default_factory=lambda: {A: None, B: None})
    claimed: dict = field(default_factory=dict)          # unit -> arm(取りに行っている)
    locks: dict = field(default_factory=dict)            # 資源 -> arm
    busy: dict = field(default_factory=lambda: {A: None, B: None})
    held_side: dict = field(default_factory=lambda: {A: 0, B: 0})
    at_side: dict = field(default_factory=lambda: {A: -1, B: -1})   # アームが今いる側(待機点は上流側)
    log: list = field(default_factory=list)

    def __post_init__(self):
        self.rank = {n: i for i, n in enumerate(self.dag.topo())}
        if not self.with_box:
            self.done.add(BOX)
        if not self.with_lid or self.line:
            self.done.add(LID)
        self.fail_count = {}

    # ---- 問い合わせ ----
    @property
    def units(self):
        return [n for n in self.dag.nodes if n not in (BOX, LID) and not is_removal(n)]

    def finished(self):
        return set(self.dag.nodes) <= self.done and (self.line or not self.with_ship or SHIP in self.done)

    def _preds_ok(self, n, held_by=None):
        """n の先行がすべて済んでいる(held_by のアームが持っている品は済んだとみなす)。"""
        return all(p in self.done or (held_by and self.holding.get(held_by) == p) for p in self.dag.preds[n])

    def _free(self, res, arm):
        return self.locks.get(res) in (None, arm)

    def _wide_pending(self):
        """A が箱・蓋・出荷の操作をしたい(B を下げる必要がある)。"""
        if self.line or self.holding[A] is not None:
            return False
        if BOX not in self.done:
            return True
        if LID not in self.done and all(n in self.done for n in self.dag.nodes if n != LID):
            return True
        return self.with_ship and LID in self.done and SHIP not in self.done

    # ---- 行動の選択 ----
    def next_action(self, arm, sources):
        """sources: SKU -> [Source](今取り出せる在庫。実行器が知覚から渡す)。"""
        if self.busy[arm]:
            return None
        if self.strategy == "S0" and self.busy[OTHER[arm]]:
            return Action("wait", arm)
        act = self._choose(arm, sources)
        if act.kind != "wait":
            for r in act.locks:
                self.locks[r] = arm
            self.busy[arm] = act
            if act.kind in ("fetch", "remove"):
                self.claimed[act.unit] = arm
            self.log.append(str(act))
        return act

    def _choose(self, arm, sources):
        h = self.holding[arm]
        wide = self._wide_pending()
        if arm == A and h is None and wide:
            kind = "box" if BOX not in self.done else ("lid" if LID not in self.done else "ship")
            side = -1 if kind == "lid" else 1
            shelf = "shelf_L" if side > 0 else "shelf_R"
            if not (self._free("zone", A) and self._free(shelf, A)) or \
                    (self.holding[B] is not None and self.held_side[B] == side):
                return Action("wait", A)
            return Action(kind, A, locks=("zone", shelf), side=side)
        if h is not None:
            if self._preds_ok(h) and self._free("zone", arm) and self._zone_turn(arm, h):
                return Action("place", arm, unit=h, locks=("zone",))
            return Action("wait", arm)
        if arm == B:                                         # 取り出し(仮置き台は B 側)
            for n in self.dag.nodes:
                if is_removal(n) and n not in self.done and n not in self.claimed and self._preds_ok(n) \
                        and self._free("zone", B):
                    return Action("remove", B, unit=n, locks=("zone",), side=1 if self.line else -1)
        cand = self._fetch_candidates(arm, sources)
        if cand:
            u, src = cand[0]
            if self.line and src.shelf == OWNER_SHELF[arm] and not src.shared and src.side != self.at_side[arm]:
                # 自分の反対側の棚へ: 先にベースの前(梱包位置の上空)を通って移る。ゾーンはその間だけ
                if not self._free("zone", arm):
                    return Action("wait", arm)
                return Action("cross", arm, locks=("zone",), side=src.side)
            locks = ()
            if src.shelf in ("L", "R"):
                locks = (f"shelf_{src.shelf}",)
                if src.shelf != OWNER_SHELF[arm] or src.shared:
                    locks += ("zone",)                        # 相手側の棚へは中央を横切る
            return Action("fetch", arm, unit=u, source=src, locks=locks, side=src.side or -1)
        return Action("wait", arm)

    def _zone_turn(self, arm, u):
        """S2: 両方が置ける品を持って待っているなら、トポロジカル順で先の品を持つ方に譲る。"""
        o = self.holding[OTHER[arm]]
        if self.strategy != "S2" or o is None or self.busy[OTHER[arm]] or not self._preds_ok(o):
            return True
        return self.rank[u] <= self.rank[o]

    def _can_fetch(self, arm, u):
        if self.strategy in ("S0", "S1"):
            return self._preds_ok(u)
        if arm == A and BOX not in self.done and not self.line:
            return False                                   # A は先に箱を用意する
        # 先行品が「済み」か「相手が持っている」か(箱はまだでもよい: B は箱が来るまで持って待つ)
        return all(p in self.done or p == BOX or self.holding[OTHER[arm]] == p for p in self.dag.preds[u])

    def _fetch_candidates(self, arm, sources):
        remaining = [u for u in self.units if u not in self.done and u not in self.claimed
                     and u not in self.holding.values()]
        if not remaining:
            return []
        min_rank = min(self.rank[u] for u in remaining)
        out = []
        for u in remaining:
            if not self._can_fetch(arm, u) or self.rank[u] > min_rank + (self.lookahead if self.strategy == "S2" else 99):
                continue
            pool = sources.get(u, []) if f"RM:{u}" in self.dag.preds else sources.get(u, []) + sources.get(u.split("#")[0], [])
            srcs = [s for s in pool if s.reachable_by(arm) and not self._source_taken(s)]   # 取り出した品は仮置き台から戻す
            if self.strategy == "S1":
                srcs = [s for s in srcs if s.shelf in (OWNER_SHELF[arm], "buffer")]
            srcs = [s for s in srcs if s.shelf not in ("L", "R") or (self._free(f"shelf_{s.shelf}", arm) and
                    ((s.shelf == OWNER_SHELF[arm] and not s.shared) or self._free("zone", arm)) and
                    (s.shelf == OWNER_SHELF[arm] or not self._other_staged_on(arm, s.side)))]
            if not srcs:
                continue
            srcs.sort(key=lambda s: (s.shelf not in (OWNER_SHELF[arm], "buffer"), s.shared))
            s = srcs[0]
            cross = s.shelf not in (OWNER_SHELF[arm], "buffer")
            if cross and not self._other_busy(arm):
                continue                                   # 相手の棚の品は、相手の手がふさがっているときだけ
            switch = self.line and not cross and s.shelf != "buffer" and s.side != self.at_side[arm]
            out.append((self.rank[u] + (0.5 if cross else 0.0) + (0.6 if switch else 0.0), u, s))   # 反対側へ移る品は少し後回し
        out.sort(key=lambda t: t[0])
        return [(u, s) for _, u, s in out]

    def debug_state(self, sources):
        """止まったときの調べ用: 状態と、残りの品を取りに行けない理由。"""
        rem = [u for u in self.units if u not in self.done]
        why = {}
        for u in rem:
            r = []
            if u in self.claimed: r.append(f"claimed:{self.claimed[u]}")
            if u in self.holding.values(): r.append("held")
            for arm in (A, B):
                if not self._can_fetch(arm, u): r.append(f"{arm[-1]}:preds")
            pool = sources.get(u, []) + sources.get(u.split("#")[0], [])
            r.append("src=" + ",".join(f"{x.model}/{x.shelf}{x.side:+d}" for x in pool))
            why[u] = r
        return {"done": sorted(self.done), "locks": dict(self.locks), "at_side": dict(self.at_side),
                "holding": dict(self.holding), "busy": {k: str(v) for k, v in self.busy.items()}, "why": why,
                "preds": {u: sorted(self.dag.preds[u]) for u in rem}}

    def _other_staged_on(self, arm, side):
        """相手が side 側の待機位置で品を持って待っている(その上空に腕を伸ばすと当たる)。"""
        o = OTHER[arm]
        return self.holding[o] is not None and self.held_side[o] == side

    def _other_busy(self, arm):
        o = OTHER[arm]
        return self.holding[o] is not None or (self.busy[o] is not None and self.busy[o].kind in ("fetch", "remove", "box", "lid", "ship"))

    def _source_taken(self, s):
        return any(a is not None and a.kind == "fetch" and a.source == s for a in self.busy.values())

    # ---- 結果の通知 ----
    def finish(self, arm, ok=True):
        act = self.busy[arm]
        self.busy[arm] = None
        for r in act.locks:
            if self.locks.get(r) == arm:
                del self.locks[r]
        self.claimed.pop(act.unit, None)
        if not ok:
            self.fail_count[act.unit] = self.fail_count.get(act.unit, 0) + 1
            return act
        if act.kind == "fetch":
            self.holding[arm] = act.unit
            self.held_side[arm] = act.side
            self.at_side[arm] = act.side
        elif act.kind == "place":
            self.holding[arm] = None
            self.done.add(act.unit)
        elif act.kind in ("remove", "lid"):
            self.done.add(act.unit if act.kind == "remove" else LID)
            if act.kind == "remove":
                self.at_side[arm] = act.side
        elif act.kind == "box":
            self.done.add(BOX)
        elif act.kind == "cross":
            self.at_side[arm] = act.side
        elif act.kind == "ship":
            self.done.add(SHIP)
        return act
