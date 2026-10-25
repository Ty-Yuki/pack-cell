"""スケジューラの行動を、2台のアームのスレッドで Gazebo 上に実行する。

各アームは手空きになるたびに(ロックを取って)スケジューラに次の行動を聞き、実行し、結果を返す。
イベント(行動の開始・終了、失敗、再計画)は sim 時刻付きで記録し、UI・評価・DES の校正に使う。
"""
import json, math, threading, time, traceback

import numpy as np

from pack_cell import layout as L
from pack_cell.core.dag import BOX, LID
from pack_cell.core.scheduler import Scheduler, Source, A, B
from pack_cell.robot.arm import ArmError
from pack_cell.robot.skills import GraspFailed, Target

MAX_GRASP_RETRY = 2


class Executor:
    def __init__(self, world, arms, skills, cat, on_event=None):
        self.world, self.arms, self.skills, self.cat = world, arms, skills, cat
        self.gz = world.gz
        self.lock = threading.RLock()
        self.events = []
        self.on_event = on_event
        self.sch = None
        self.plan = None
        self.inventory = None
        self.stop = threading.Event()
        self.error = None
        self.inject = {}               # model -> 失敗させる回数(失敗注入)
        self.fail_prob = 0.0           # 品の吸着を確率で失敗させる(E3)
        import random
        self.rng = random.Random(0)
        self.pause = threading.Event()  # 再計画中は新しい行動を始めない
        self.after_finish = None        # (act, ok) -> None。行動の終了直後(ロック内)に呼ぶ
        self.box_present = False        # 空箱が梱包位置にある(コンベヤが運んできた)

    # ---- イベント ----
    def emit(self, kind, **kw):
        ev = {"t": round(self.gz.sim_time(), 2), "kind": kind, **kw}
        with self.lock:
            self.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                traceback.print_exc()
        print("[event]", json.dumps(ev, ensure_ascii=False), flush=True)

    # ---- 在庫 ----
    def sources(self):
        """SKU -> [Source]。棚は最新の知覚、仮置き台は記録から。"""
        out = {}
        for model, (src, _) in self.inventory.items():
            sku = model.rsplit("_", 1)[0]
            out.setdefault(sku, []).append(src)
        for u, (model, *_rest) in self.world.buffer.items():
            out.setdefault(u, []).append(Source(model, "buffer", side=1))
        return out

    # ---- 実行 ----
    def box_ready(self, box=None):
        """コンベヤが空箱を梱包位置に置いた(ライン制御のスレッドから呼ばれる)。"""
        with self.lock:
            self.box_present = True
            if self.sch is not None:
                self.sch.done.add(BOX)

    def run(self, spec, plan, dag, strategy="S2", timeout=1800):
        self.plan, self.spec = plan, spec
        # 在庫: 今の棚の検出 + 前の注文で最後に見えた未使用の品(待機中の腕でカメラから隠れていることがある)
        fresh = self.world.inventory()
        old = {m: v for m, v in (self.inventory or {}).items() if m not in self.world.hidden}
        self.inventory = {**old, **fresh}
        self.world.unit_model.clear()
        self.error = None
        self.stop.clear()
        with self.lock:
            at_side = dict(self.sch.at_side) if self.sch is not None else None
            self.sch = Scheduler(dag, strategy=strategy, with_lid=False, with_ship=False, line=True)
            if at_side:
                self.sch.at_side = at_side
            if self.box_present:
                self.sch.done.add(BOX)
        self.emit("order_start", strategy=strategy, plan=plan.to_dict(), dag=dag.to_dict(), inventory=sorted(self.inventory))
        self.idle_since = None
        t0 = self.gz.sim_time()
        ths = [threading.Thread(target=self._arm_loop, args=(a,), daemon=True) for a in (A, B)]
        [t.start() for t in ths]
        t_wall = time.time()
        while any(t.is_alive() for t in ths):
            time.sleep(0.2)
            if time.time() - t_wall > timeout:
                self.error = "timeout"
                self.stop.set()
        makespan = self.gz.sim_time() - t0
        self.emit("order_end", makespan=round(makespan, 1), error=self.error)
        return makespan

    def wait_idle(self, timeout=300):
        """両アームの行動が終わるまで待つ(pause 中に使う)。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.lock:
                if all(v is None for v in self.sch.busy.values()):
                    return True
            time.sleep(0.1)
        return False

    def replace_plan(self, plan, dag, fixed):
        """再計画の結果に差し替える(呼び出し側は pause をかけ、両アームが手空き or 持って待機の状態で呼ぶ)。"""
        with self.lock:
            old = self.sch
            sch = Scheduler(dag, strategy=old.strategy, with_box=False, with_lid=False, with_ship=False, line=True)
            sch.holding = dict(old.holding)
            sch.held_side = dict(old.held_side)
            sch.at_side = dict(old.at_side)
            self.sch, self.plan = sch, plan

    def _arm_loop(self, arm):
        try:
            while not self.stop.is_set():
                with self.lock:
                    if self.sch.finished() and not self.pause.is_set():
                        return
                    act = None if self.pause.is_set() else self.sch.next_action(arm, self.sources())
                if act is None or act.kind == "wait":
                    self._check_stall()
                    self.arms[arm].sim_sleep(0.3)
                    continue
                self.idle_since, self._refreshed = None, False
                self.emit("start", arm=arm, action=act.kind, unit=act.unit,
                          source=act.source.model if act.source else None)
                ok, info = self._do(arm, act)
                with self.lock:
                    self.sch.finish(arm, ok)
                    if self.after_finish:
                        self.after_finish(act, ok)       # ロック内: 次の行動が選ばれる前に一時停止をかけられる
                self.emit("end", arm=arm, action=act.kind, unit=act.unit, ok=ok, info=info)
                if not ok and self.sch.fail_count.get(act.unit, 0) > MAX_GRASP_RETRY + 1:
                    raise RuntimeError(f"{act.unit} の取り出しに繰り返し失敗")
        except Exception as e:
            self.error = f"{arm}: {e}"
            traceback.print_exc()
            self._snapshot(f"error_{arm}")
            self.emit("error", arm=arm, message=str(e))
            self.stop.set()

    STALL_REFRESH = 15.0     # 両アームが手持ち無沙汰のまま続いたら、棚を撮り直す [sim s]
    STALL_ABORT = 60.0       # それでも進まなければ止める

    def _check_stall(self):
        """両アームとも何もできない状態が続く → 在庫の見落とし(腕で隠れていた等)を疑い、棚を撮り直して足す。"""
        with self.lock:
            if self.pause.is_set() or any(v is not None for v in self.sch.busy.values()):
                self.idle_since, self._refreshed = None, False
                return
            now = self.gz.sim_time()
            if self.idle_since is None:
                self.idle_since = now
                return
            waited = now - self.idle_since
        if waited > self.STALL_ABORT:
            raise RuntimeError(f"{waited:.0f}s 進まない(在庫: {sorted(self.inventory)})")
        if waited > self.STALL_REFRESH and not getattr(self, "_refreshed", False):
            self._refreshed = True
            with self.lock:
                self.emit("stall", state=self.sch.debug_state(self.sources()))
            fresh = self.world.inventory()
            used = set(self.world.unit_model.values())
            add = {m: v for m, v in fresh.items() if m not in self.inventory and m not in used}
            with self.lock:
                self.inventory.update(add)
            self.emit("inventory_refresh", added=sorted(add))

    def _snapshot(self, tag):
        """エラー時の状況を画像で残す(logs/<tag>_<cam>.png)。初期位置から動いた品の位置も記録する。"""
        try:
            moved = {}
            for it in self.world.spec["items"]:
                p = self.gz.pos(it["name"])
                if p is not None and math.dist(p, (it["x"], it["y"], it["z"])) > 0.03:
                    moved[it["name"]] = [round(float(v), 3) for v in p]
            self.emit("snapshot", tag=tag, moved=moved,
                      tips={k: [round(float(v), 3) for v in a.tip_world()] for k, a in self.arms.items()})
        except Exception:
            traceback.print_exc()
        try:
            import cv2
            for cam in ("cam_overview", "cam_side"):
                self.gz.subscribe_camera(cam)
                v = self.gz.image(cam, timeout=3.0)
                if v is not None:
                    cv2.imwrite(f"/ws/logs/{tag}_{cam}.png", cv2.cvtColor(v[1], cv2.COLOR_RGB2BGR))
        except Exception:
            traceback.print_exc()

    def _do(self, arm, act):
        a, s = self.arms[arm], self.skills[arm]
        k = act.kind
        if k == "cross":
            a.transfer(a.staging_point(act.side))
            return True, ""
        if k == "fetch":
            return self._fetch(arm, a, s, act)
        if k == "place":
            return self._place(arm, a, s, act.unit)
        if k == "remove":
            return self._remove(a, s, act.unit[3:])
        if k == "park":
            a.transfer(a.park_point(a.park_side()))
            return True, ""
        raise ValueError(k)

    # ---- 各行動 ----
    def _fetch(self, arm, a, s, act):
        src = act.source
        for attempt in range(MAX_GRASP_RETRY + 1):
            if src.shelf == "buffer":
                model, (bx, by, bottom), yaw = self.world.buffer[act.unit]
                h = self.cat[model.rsplit("_", 1)[0]].height
                tgt = Target(bx, by, bottom + h, yaw)
            else:
                det = self.world.detect_shelf(src.shelf)
                if src.model not in det and attempt > 0:
                    # 自分(や相手)の腕がカメラを遮っている → 自陣の待機点(画角の外)へ退いて撮り直す
                    a.transfer(a.park_point(a.park_side()))
                    det = self.world.detect_shelf(src.shelf)
                if src.model in det:
                    tgt = Target(**det[src.model][1])
                    self.inventory[src.model] = det[src.model]
                elif src.model in self.inventory:
                    # アームの腕がカメラと品の間に入って見えないときは、最後に見えた位置を使う
                    tgt = Target(**self.inventory[src.model][1])
                    self.emit("occluded", arm=arm, model=src.model)
                else:
                    return False, f"{src.model} が棚に見つからない"
                model, h = src.model, self.cat[src.model.rsplit("_", 1)[0]].height
            if self.cat[model.rsplit("_", 1)[0]].round:
                tgt.yaw = a.tool_yaw                         # 円柱は向きを合わせない(手首を回さない)
            key = model if self.inject.get(model, 0) > 0 else model.rsplit("_", 1)[0]
            if self.inject.get(key, 0) > 0:                   # 失敗注入(個体名か品目名で指定)
                self.inject[key] -= 1
                s.fail_next = 1
            elif self.fail_prob > 0 and self.rng.random() < self.fail_prob:
                s.fail_next = 1
            try:
                s.pick(model, tgt, height=h)
                break
            except GraspFailed as e:
                self.emit("grasp_failed", arm=arm, unit=act.unit, model=model, attempt=attempt + 1, info=str(e))
                if attempt == MAX_GRASP_RETRY:
                    a.transfer(a.staging_point(act.side))
                    return False, str(e)
        if src.shelf == "buffer":
            self.world.buffer.pop(act.unit, None)
        else:
            self.inventory.pop(src.model, None)
        self.world.unit_model[act.unit] = model
        a.transfer(a.staging_point(act.side))
        if src.shelf != "buffer" and self.world.stacked_below(model):
            # 重ねた品の上を取った: 下の品が見えるようになったので、棚を撮り直して在庫に足す(腕が退いてから)
            used = set(self.world.unit_model.values()) | self.world.shipped_models
            fresh = self.world.detect_shelf(src.shelf)
            with self.lock:
                for m, v in fresh.items():
                    if m not in self.inventory and m not in used and m not in self.world.hidden:
                        self.inventory[m] = v
        return True, f"{model}"

    def _place(self, arm, a, s, unit):
        p = self.plan.placements[unit]
        cx, cy = p.center_xy()
        s.place(cx, cy, p.world_bottom(), None if self.cat[p.sku].round else p.yaw)
        a.transfer(a.staging_point(self.sch.at_side[arm]))
        model = self.world.unit_model[unit]
        (x, y, z), _ = self.gz.pose(model)
        err = math.hypot(x - cx, y - cy)
        return True, f"誤差 {err * 1000:.0f}mm"

    def _remove(self, a, s, unit):
        model = self.world.unit_model[unit]
        (x, y, z), q = self.gz.pose(model)
        from pack_cell.sim.gzif import yaw_of
        h = self.cat[model.rsplit("_", 1)[0]].height
        s.pick(model, Target(x, y, z + h / 2, yaw_of(q)), height=h)
        # 仮置き台(ArmB の下流側の棚の奥の段)に、置いてある品の右隣へ品の幅の分だけ詰めて並べる
        w = self.cat[model.rsplit("_", 1)[0]].size[0]
        right = max([v[1][0] + self.cat[v[0].rsplit("_", 1)[0]].size[0] / 2 for v in self.world.buffer.values()],
                    default=L.SHELF_X[0])
        bx = right + 0.02 + w / 2
        if bx + w / 2 > L.SHELF_X[1]:
            raise ArmError(f"仮置き台に {model} を置く場所がない")
        by = L.BUFFER_SLOTS[0][1]
        top = L.SHELF_STEPS["back"][2]
        s.place(bx, by, top, 0.0)
        self.world.buffer[unit] = (model, (bx, by, top), 0.0)
        a.transfer(a.staging_point(1))
        return True, model
