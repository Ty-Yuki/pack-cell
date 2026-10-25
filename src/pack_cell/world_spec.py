"""ワールドに置くモデル(品物・箱)の初期配置。gen_world.py と WorldManager で共有する。

品物は在庫スロットごとに1モデル(名前 = <sku>_<個体番号>)。初期位置には乱数でずれを入れ、知覚で拾わせる。
知覚(バウンディングボックスカメラ)が品を見分けるためのラベル番号を各モデルに振る(label = 品の通し番号 + 1)。
"""
import math
from dataclasses import asdict, dataclass, field

from pack_cell import layout as L
from pack_cell.core import catalog as C

JITTER_XY = 0.01
JITTER_YAW = math.radians(8)
PARK = (-3.0, -3.0)          # 使わないモデルの置き場(カメラの画角外)


@dataclass
class Model:
    name: str
    x: float
    y: float
    z: float
    yaw: float = 0.0


@dataclass
class BoxModel(Model):
    size: str = "L"          # 箱のサイズ(layout.BOXES)


@dataclass
class ItemModel(Model):
    sku: str = ""
    unit: int = 0
    slot: tuple = ()
    size: tuple = ()
    mass: float = 0.0
    color: tuple = ()
    label: int = 0
    shared: bool = False


@dataclass
class Spec:
    items: list = field(default_factory=list)
    boxes: list = field(default_factory=list)

    def graspable(self, arm):
        """吸着ジョイントを持つ組。箱はコンベヤが運ぶので、アームが吸うのは品物だけ。"""
        return [it.name for it in self.items]

    def to_dict(self):
        return {"items": [asdict(i) for i in self.items], "boxes": [asdict(b) for b in self.boxes]}


def slot_pose(sku, slot, rng=None):
    x, y = L.slot_xy(slot)
    lv = L.slot_level(slot)
    z = L.slot_top(slot) + sku.height * (lv + 0.5)
    yaw = 0.0
    if rng is not None:
        k = 0.4 if lv else 1.0                    # 重ねた品のずれは小さめ(下の品からはみ出さない)
        x += k * rng.uniform(-JITTER_XY, JITTER_XY)
        y += k * rng.uniform(-JITTER_XY, JITTER_XY)
        yaw = k * rng.uniform(-JITTER_YAW, JITTER_YAW)
    return x, y, z, yaw


def box_pose(x, size="L"):
    """コンベヤ上の位置 x に置いた箱の姿勢。"""
    return (x, 0.0, L.TABLE_TOP + L.box_outer(size)[2] / 2, 0.0)


def box_park_pose(k, size="L"):
    """使っていない箱・出荷した箱の置き場(カメラの画角外)。"""
    return (PARK[0] - 0.6 * L.BOX_ORDER.index(size), PARK[1] - 0.6 * k, L.box_outer(size)[2] / 2, 0.0)


def build(rng=None, cat=None):
    cat = cat or C.load()
    spec = Spec()
    units = {}
    for i, slot in enumerate(L.STOCK):
        sku_id = slot[4]  # (棚, 段, x, |y|, SKU[, 何段目])
        sku = cat[sku_id]
        u = units.get(sku_id, 0)
        units[sku_id] = u + 1
        x, y, z, yaw = slot_pose(sku, slot, rng)
        spec.items.append(ItemModel(name=f"{sku_id}_{u}", x=x, y=y, z=z, yaw=yaw, sku=sku_id, unit=u, slot=tuple(slot),
                                    size=sku.size, mass=sku.mass, color=sku.color, label=i + 1, shared=L.slot_shared(slot)))
    for size in L.BOX_ORDER:                   # 箱はすべて置き場に。製函機が注文に合わせたサイズを出す
        for k in range(L.BOXES[size]["count"]):
            spec.boxes.append(BoxModel(f"carton_{size}{k}", *box_park_pose(k, size), size=size))
    return spec


def stock_sources(cat=None):
    """初期在庫の取り出し元(SKU -> [Source])。オフラインの DES・テスト・集計で使う。"""
    from pack_cell.core.scheduler import Source
    out = {}
    for it in build(None, cat).items:
        out.setdefault(it.sku, []).append(Source(it.name, it.slot[0], it.shared, L.slot_side(it.slot)))
    return out
