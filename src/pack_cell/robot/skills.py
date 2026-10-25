"""吸着のピック&プレース・スキル。Arm(動作)と GzIf(吸着・観測)をつなぐ。

吸着の成否は「吸着面が対象上面の中心から一定範囲内にあるか」で模擬する(真空シールの成立条件)。
失敗注入(fail_next)が立っている場合は吸着しない。持ち上げ後に対象の高さで成否を確認する。
"""
import math
from dataclasses import dataclass

import numpy as np

from pack_cell.robot.arm import TRANSPORT_Z, CLEAR_Z, SLOW, ArmError, wrap

SEAL_XY = 0.015            # 吸着が成立する水平ずれ
SEAL_Z = 0.012             # 吸着が成立する上下ずれ
APPROACH = 0.08            # 接近開始の高さ(上面から)
GAP = 0.002
RELEASE_GAP = 0.004        # 置くときに離す高さ(底面と置き面の隙間)


class GraspFailed(ArmError):
    pass


@dataclass
class Target:
    """対象の上面中心(吸着点)と yaw。"""
    x: float
    y: float
    top: float
    yaw: float = 0.0


class Skills:
    def __init__(self, arm, gz, log=print):
        self.arm, self.gz, self.log = arm, gz, log
        self.held = None           # (モデル名, 吸着点から底面までの高さ, 把持時の(品yaw - ツールyaw))
        self.fail_next = 0         # 失敗注入: 次の n 回の吸着を失敗させる

    def _seal_ok(self, name, top_offset):
        p = self.gz.pose(name)
        if p is None:
            return False, "pose不明"
        tip = self.arm.tip_world()
        c = np.array(p[0])
        dxy = float(np.linalg.norm(tip[:2] - c[:2]))
        dz = float(tip[2] - (c[2] + top_offset))
        ok = dxy < SEAL_XY and abs(dz) < SEAL_Z
        return ok, f"dxy={dxy * 100:.1f}cm dz={dz * 100:.1f}cm"

    def pick(self, name, tgt, height, center_to_top=None, period=math.pi, carry_z=None):
        """name を上面 tgt から吸着して搬送高さまで持ち上げる。height: 吸着点から底面までの高さ。
        center_to_top: モデル原点から吸着点までの高さ(省略時 height/2)。失敗時は GraspFailed。"""
        center_to_top = height / 2 if center_to_top is None else center_to_top
        a = self.arm
        tool_yaw = a.nearest_tool_yaw(tgt.yaw, period)
        a.transfer((tgt.x, tgt.y, tgt.top + APPROACH), tool_yaw)      # 近づくときは通常の搬送高さ(carry_z は持ち上げた後だけ)
        a.line((tgt.x, tgt.y, tgt.top + GAP), n=4, speed=0.6)
        ok, info = self._seal_ok(name, center_to_top)
        if self.fail_next > 0:
            self.fail_next -= 1
            ok, info = False, info + " (失敗注入)"
        if ok:
            self.gz.attach(a.name, name)
            a.sim_sleep(0.3)
        z0 = self.gz.pos(name)[2]
        a.line((tgt.x, tgt.y, tgt.top + APPROACH), n=3)
        a.carry_z = max(CLEAR_Z + height, TRANSPORT_Z) if carry_z is None else max(carry_z, tgt.top + APPROACH)
        a.line((tgt.x, tgt.y, a.carry_z), n=4)
        a.sim_sleep(0.2)
        z1 = self.gz.pos(name)[2]
        if not ok or z1 - z0 < 0.05:
            a.carry_z = None
            if ok:
                self.gz.detach(a.name, name)
            raise GraspFailed(f"{a.name}: {name} の吸着に失敗 ({info}, 上昇 {100 * (z1 - z0):.1f}cm)")
        item_yaw = self._yaw(name)
        self.held = (name, height, wrap(item_yaw - a.tool_yaw))
        self.log(f"{a.name}: pick {name} ({info})")
        return True

    def _yaw(self, name):
        from pack_cell.sim.gzif import yaw_of
        return yaw_of(self.gz.pose(name)[1])

    def place(self, x, y, bottom, yaw, slow=True, retreat=True, carry_z=None, speed=None):
        """持っている品を、底面が bottom・yaw になるよう置く。"""
        if self.held is None:
            raise ArmError(f"{self.arm.name}: 何も持っていない")
        name, height, rel = self.held
        a = self.arm
        tool_yaw = a.tool_yaw if yaw is None else a.tool_yaw + wrap(yaw - rel - a.tool_yaw)   # None: 向きを変えない(円柱)
        top = bottom + height
        a.transfer((x, y, top + APPROACH), tool_yaw, z=carry_z, **({"speed": speed} if speed else {}))
        a.line((x, y, top + RELEASE_GAP), n=4, speed=0.6 * (SLOW if slow else 1.0))
        self.gz.detach(a.name, name)
        a.sim_sleep(0.4)
        self.held = None
        a.carry_z = None
        if retreat:
            a.line((x, y, top + APPROACH), n=3)
        self.log(f"{a.name}: place {name}")

    def retreat_up(self):
        p = self.arm.tip_world()
        self.arm.line((p[0], p[1], self.arm.hold_z()), n=4)
