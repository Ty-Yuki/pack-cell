"""スケジューラのオフライン評価(離散イベントシミュレーション)。行動ごとの所要時間はモデルで与える。

所要時間の既定値は Gazebo での実測(logs の events)から校正する。
"""
import heapq
from dataclasses import dataclass, field

from pack_cell.core.dag import BOX
from pack_cell.core.scheduler import Scheduler, Source, A, B

DUR = {"box": 14.0, "lid": 12.0, "ship": 14.0, "park": 3.0, "fetch": 9.0, "fetch_cross": 12.0,
       "fetch_buffer": 8.0, "place": 8.0, "remove": 10.0,
       "cross": 4.0,         # 自分の上流側と下流側の棚の間を移る
       "feed": 4.5}          # コンベヤが空箱を待機位置から梱包位置へ運ぶ時間(この間もアームは取り出しを進める)
WAIT_POLL = 0.5


@dataclass
class DesResult:
    makespan: float
    busy: dict = field(default_factory=dict)        # arm -> 稼働時間
    timeline: list = field(default_factory=list)    # (arm, kind, unit, t0, t1)
    deadlock: bool = False


def duration(act, dur=DUR):
    if act.kind == "fetch":
        if act.source.shelf == "buffer":
            return dur["fetch_buffer"]
        own = {"arm_a": "L", "arm_b": "R"}[act.arm] == act.source.shelf
        return dur["fetch"] if own else dur["fetch_cross"]
    return dur[act.kind]


def simulate(dag, sources, strategy="S2", dur=DUR, t_max=3600.0, box_at=None, **kw):
    """sources: SKU -> [Source]。取り出した在庫は消費し、仮置き台への取り出しは buffer の在庫になる。
    box_at: 空箱が梱包位置に着く時刻(コンベヤ方式。省略時は dur["feed"]、箱が既にあるなら 0)。"""
    sch = Scheduler(dag, strategy=strategy, **kw)
    if sch.line and BOX not in sch.done:
        box_at = dur["feed"] if box_at is None else box_at
    stock = {k: list(v) for k, v in sources.items()}
    t, ev, res = 0.0, [], DesResult(0.0, {A: 0.0, B: 0.0})
    idle = {A: 0.0, B: 0.0}
    while not sch.finished():
        if sch.line and BOX not in sch.done and t >= box_at:
            sch.done.add(BOX)
        progressed = False
        for arm in (A, B):
            if sch.busy[arm] or idle[arm] > t:
                continue
            act = sch.next_action(arm, stock)
            if act is None or act.kind == "wait":
                idle[arm] = t + WAIT_POLL
                continue
            d = duration(act, dur)
            if act.kind == "fetch":
                stock[act.unit.split("#")[0]] = [s for s in stock.get(act.unit.split("#")[0], []) if s != act.source]
                stock[act.unit] = [s for s in stock.get(act.unit, []) if s != act.source]
            heapq.heappush(ev, (t + d, arm, act))
            res.timeline.append((arm, act.kind, act.unit, t, t + d))
            res.busy[arm] += d
            progressed = True
        if ev and (not progressed or ev[0][0] <= t):
            t1, arm, act = heapq.heappop(ev)
            t = max(t, t1)
            sch.finish(arm, True)
            if act.kind == "remove":
                u = act.unit[3:]
                stock.setdefault(u, []).append(Source(f"{u}@buffer", "buffer"))
        elif not progressed:
            if sch.line and BOX not in sch.done and not ev:
                t = max(t, min(min(idle.values()), box_at))
                continue
            t = min(idle.values()) if not ev else t
            if t > t_max:
                res.deadlock = True
                break
            t = max(t, min(idle.values()))
    res.makespan = t
    return res
