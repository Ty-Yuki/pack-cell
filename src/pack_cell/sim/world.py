"""ワールドの管理: 初期化(吸着ジョイントの解除)・在庫の知覚・箱/仮置き台の状態・補充・真値による判定。"""
import math, os, pathlib, time

import numpy as np
import yaml

from pack_cell import layout as L, world_spec as WS
from pack_cell.core.scheduler import Source
from pack_cell.perception import bbox
from pack_cell.sim.gzif import GzIf, yaw_of, tilt_of, LAZY_JOINTS

_yaw = yaw_of

SHELF_CAMS = {"L": "cam_shelf_l", "R": "cam_shelf_r"}


class World:
    def __init__(self, cat, build_dir=None, log=print):
        self.cat, self.log = cat, log
        build_dir = pathlib.Path(build_dir or os.environ.get("SIM_BUILD", "/ws/sim/build"))
        self.spec = yaml.safe_load((build_dir / "spec.yaml").read_text())
        self.items = {i["name"]: i for i in self.spec["items"]}
        self.boxes = [b["name"] for b in self.spec["boxes"]]
        self.box_size = {b["name"]: b.get("size", "L") for b in self.spec["boxes"]}   # 箱 -> サイズ
        self.gz = GzIf()
        if not self.gz.wait_ready(list(self.items) + self.boxes):
            raise RuntimeError("Gazebo のモデルが揃わない")
        self.pairs = [(arm, n) for arm in ("arm_a", "arm_b") for n in self.items]
        if not LAZY_JOINTS:                 # URDF に吸着ジョイントを入れる方式では、ロード直後の溶接を解除して確かめる
            self.gz.release_all(self.pairs)
            self.verify_released()
        for c in SHELF_CAMS.values():
            self.gz.subscribe_boxes(c)
        self.gz.subscribe_camera("cam_box")
        self.by_label = {i["label"]: i for i in self.spec["items"]}
        self.line = None          # 出荷レーン(Line)。梱包位置の箱は line.at_pack
        self.buffer = {}          # unit -> (model, (x, y, bottom), yaw)
        self.hidden = set()       # 欠品にした品
        self.shipped_models = set()   # 出荷した品(箱ごと片付けた)
        self.unit_model = {}      # 計画の unit -> 実際に箱へ入れたモデル
        self.gz.sim_sleep(1.0)

    def verify_released(self, tries=4):
        """解除が効いたかを物理で確かめる: 全モデルを 3mm 持ち上げる指令を出し、動かないもの(アームに溶接されたまま)には
        新しい通信ノードから解除を送り直す。解除命令の取りこぼしで品がアームに(時には両方のアームに)溶接されたまま残ると、
        アームが起動時に動けなくなる。"""
        from pack_cell.sim.gzif import GzIf
        names = list(self.items)
        stuck = []
        for k in range(tries):
            before = {n: self.gz.pose(n) for n in names}
            # 水平に 2mm ずらす(高さ方向は重力ですぐ戻るので判定に使えない)。自由な品はその位置に留まる
            self.gz.set_poses([(n, (p[0][0] + 0.002, p[0][1], p[0][2] + 0.0005), _yaw(p[1])) for n, p in before.items() if p])
            self.gz.sim_sleep(0.3)
            stuck = [n for n in names if before[n] and abs(self.gz.pose(n)[0][0] - before[n][0][0]) < 0.001]
            self.gz.set_poses([(n, p[0], _yaw(p[1])) for n, p in before.items() if p])     # 元に戻す
            self.gz.sim_sleep(0.3)
            if not stuck:
                break
            self.log(f"[warn] 溶接が残っている: {stuck} → 解除を送り直す({k + 1})")
            fresh = GzIf(self.gz.world)
            fresh.release_all([(h, c) for h, c in self.pairs if c in stuck], rounds=2)
        self.stuck_at_start = stuck
        return not stuck

    def recover_start(self):
        """起動時にアームが動けない(解除命令の取りこぼしで品・箱が溶接されたまま)ときの立て直し:
        解除を送り直し、品物・箱・蓋を初期位置に戻す。"""
        self.gz.release_all(self.pairs)
        moves = [(m["name"], (m["x"], m["y"], m["z"]), m["yaw"]) for m in self.spec["items"] + self.spec["boxes"]]
        self.gz.set_poses(moves)
        self.gz.sim_sleep(1.0)

    # ---- 箱 ----
    @property
    def carton(self):
        """梱包位置の箱(まだ来ていなければ待機位置の箱)。"""
        return (self.line.at_pack or self.line.waiting) if self.line else self.boxes[0]

    def park_box(self, box, models=()):
        """出荷した箱を中身ごと画角の外へ片付ける(中身は棚に戻さない)。"""
        size = self.box_size[box]
        k = int(box.rsplit("_", 1)[1][1:])
        x, y, z, _ = WS.box_park_pose(k, size)
        self.gz.set_poses([(box, (x, y, z), 0.0)] + [(m, (x, y - 0.3 - 0.15 * i, 0.1), 0.0) for i, m in enumerate(models)])
        self.shipped_models.update(models)

    # ---- 在庫(知覚) ----
    def detect_shelf(self, shelf, fresh=True):
        """棚カメラの 3D バウンディングボックス → {model: (Source, Target相当の dict)}。
        xy と向きは知覚から、吸着点の高さは棚の段とカタログの寸法から(置き姿勢は棚で決まっている)。"""
        cam = SHELF_CAMS[shelf]
        t = self.gz.sim_time() if fresh else None
        v = self.gz.boxes(cam, newer_than=t)
        if v is None:
            raise RuntimeError(f"{cam} の出力が来ない")
        found = []
        for d in bbox.to_world(v[1], cam):
            it = self.by_label.get(d.label)
            if it is None or it["slot"][0] != shelf:
                continue
            slot = tuple(it["slot"])
            if (d.xyz[0] - it["x"]) ** 2 + (d.xyz[1] - it["y"]) ** 2 > 0.07 ** 2:
                continue                                       # スロットから外れている(落ちた・持ち去られた)
            h = self.cat[it["sku"]].height
            # 重ねた品: 外形の中心の高さから何段目かを求め、吸着点(上面)の高さは段の高さ + 品の高さ x 段数
            lv = max(0, round((d.xyz[2] - L.slot_top(slot)) / h - 0.5))
            found.append((it, slot, d, lv, L.slot_top(slot) + h * (lv + 1)))
        out = {}
        for it, slot, d, lv, top in found:
            if any(o is not it and math.hypot(od.xyz[0] - d.xyz[0], od.xyz[1] - d.xyz[1]) < 0.04 and olv > lv
                   for o, _, od, olv, _ in found):
                continue                                       # 上に品が載っている(上の品から取る)
            out[it["name"]] = (Source(it["name"], shelf, bool(it["shared"]), L.slot_side(slot)),
                               {"x": d.xyz[0], "y": d.xyz[1], "top": top, "yaw": d.yaw})
        return out

    def stacked_below(self, model):
        """model の下に重ねてある品(棚の管理情報から。上の品で隠れてカメラに映らない)。"""
        it = self.items[model]
        return [o["name"] for o in self.spec["items"]
                if o["slot"][:4] == it["slot"][:4] and L.slot_level(o["slot"]) < L.slot_level(it["slot"])]

    def inventory(self):
        """両方の棚の検出結果 + 仮置き台。{model: (Source, target)}"""
        inv = {}
        for sh in SHELF_CAMS:
            inv.update(self.detect_shelf(sh))
        return inv

    # ---- 失敗注入・補充・判定 ----
    def hide(self, model):
        """欠品の演出: 品を画角の外へ移す。"""
        self.hidden.add(model)
        self.gz.set_pose(model, (WS.PARK[0] + 0.5, WS.PARK[1] + 2.0, 0.1))
        self.gz.sim_sleep(0.6)                     # 棚カメラ(5Hz)の画像が移した後のものになるまで待つ

    def restock(self, models=None):
        """出荷済み・欠品にした品を棚の元のスロットへ戻す(補充)。"""
        moves = []
        for name, it in self.items.items():
            if models is not None and name not in models:
                continue
            sku = self.cat[it["sku"]]
            x, y, z, yaw = WS.slot_pose(sku, tuple(it["slot"]))
            moves.append((name, (x, y, z), yaw))
        self.gz.set_poses(moves)

    def judge(self, plan, carton=None):
        """真値による完了判定: 全品が箱内か・傾き・上下関係。"""
        carton = carton or self.carton
        (bx, by, bz), bq = self.gz.pose(carton)
        res = {"items": {}, "all_in_box": True, "max_tilt_deg": 0.0, "support_ok": True,
               "carton": {"xy": [round(bx, 3), round(by, 3)], "yaw_deg": round(math.degrees(yaw_of(bq)), 1),
                          "tilt_deg": round(math.degrees(tilt_of(bq)), 1)}}
        byu = {}
        for u, p in plan.placements.items():
            model = self.unit_model.get(u)
            if model is None:
                res["all_in_box"] = False
                continue
            (x, y, z), q = self.gz.pose(model)
            c, s_ = math.cos(-yaw_of(bq)), math.sin(-yaw_of(bq))       # 箱の座標系へ
            lx, ly = c * (x - bx) - s_ * (y - by), s_ * (x - bx) + c * (y - by)
            inner, outer = L.box_inner(plan.box), L.box_outer(plan.box)
            ix = abs(lx) < inner[0] / 2 and abs(ly) < inner[1] / 2 and z < bz + outer[2] / 2 + 0.02
            tilt = math.degrees(tilt_of(q))
            cx, cy = p.center_xy()
            res["items"][u] = {"model": model, "in_box": bool(ix), "tilt_deg": round(tilt, 1),
                               "err_mm": round(1000 * math.hypot(lx - cx, ly - cy), 1)}
            res["all_in_box"] &= bool(ix)
            res["max_tilt_deg"] = max(res["max_tilt_deg"], tilt)
            byu[u] = z
        for u, ss in plan.supports.items():
            for s in ss:
                if u in byu and s in byu and byu[u] <= byu[s]:
                    res["support_ok"] = False
        res["ok"] = res["all_in_box"] and res["max_tilt_deg"] < 5.0 and res["support_ok"]
        return res

    def judge_box(self, box, models):
        """出荷した箱の中身の判定(真値): 全品が箱の中にあり、傾いていないか。"""
        (bx, by, bz), bq = self.gz.pose(box)
        c, s_ = math.cos(-yaw_of(bq)), math.sin(-yaw_of(bq))
        res = {"box_x": round(bx, 3), "all_in_box": True, "max_tilt_deg": 0.0}
        for m in models:
            (x, y, z), q = self.gz.pose(m)
            lx, ly = c * (x - bx) - s_ * (y - by), s_ * (x - bx) + c * (y - by)
            inner, outer = L.box_inner(self.box_size[box]), L.box_outer(self.box_size[box])
            res["all_in_box"] &= bool(abs(lx) < inner[0] / 2 and abs(ly) < inner[1] / 2 and z < bz + outer[2] / 2 + 0.02)
            res["max_tilt_deg"] = round(max(res["max_tilt_deg"], math.degrees(tilt_of(q))), 1)
        res["ok"] = res["all_in_box"] and res["max_tilt_deg"] < 5.0
        return res
