"""セルの配置定義。ワールド生成(sim/gen_world.py)・制御・計画で共有する。

座標はワールド系[m]。原点は梱包位置(コンベヤ上で箱が止まる位置)の中心の床面。箱は +x 方向へ流れる(上流 = -x)。
出荷レーン: 製函機 → 入口コンベヤ(待機位置)→ 梱包コンベヤ(梱包位置)→ 出口コンベヤ(封函機)→ 出荷。
ArmA は +y 側からコンベヤを向き(-y 向き)、ArmB は -y 側から +y を向く。
棚は各アームの左右(上流側・下流側)に1台ずつ。ArmA の2台が棚L(加工食品・日用品)、ArmB の2台が棚R(日配品)。
"""
import math

WORLD = "pack_cell"
TABLE_TOP = 0.70                      # コンベヤのベルト上面の高さ
BASE_Z = 0.85                         # アーム台座の上面(= base_link の高さ)。実寸の品を持って箱の縁を越える高さまで届くよう、作業面より高くする

ARMS = {
    "arm_a": {"xy": (0.0, 0.40), "yaw": -math.pi / 2},
    "arm_b": {"xy": (0.0, -0.40), "yaw": math.pi / 2},
}

# ---- 箱(宅配 100 サイズ相当) ----
BOX_WALL = 0.01
# 箱は3サイズ。計画器が注文の品が入る最小の箱を選び、製函機がそのサイズを作る(長辺が x 方向 = 流れの向き)
BOXES = {
    "S": {"name": "小", "outer": (0.32, 0.30, 0.16), "count": 2},     # 卵・食パン・缶詰・豆腐・アイスなど
    "M": {"name": "中", "outer": (0.40, 0.30, 0.22), "count": 3},     # 米・水・牛乳を含む注文
    "L": {"name": "大", "outer": (0.44, 0.32, 0.26), "count": 2},     # 背の高い品(洗剤 21cm)を含む注文・品数の多い注文
}
BOX_ORDER = ("S", "M", "L")


def box_outer(size):
    return BOXES[size]["outer"]


def box_inner(size):
    ox, oy, oz = BOXES[size]["outer"]
    return (ox - 2 * BOX_WALL, oy - 2 * BOX_WALL, oz - BOX_WALL)


BOX_OUTER = box_outer("L")                # 最大の箱(既定)
BOX_INNER = box_inner("L")
BOX_MASS = 0.35
FLAP_T = 0.004                        # フラップの厚み
# フラップ(箱のリンク)。名前: 開いた角度の符号。閉 = 0、開 = 外へ折り下げた FLAP_OPEN [rad]。短い fx を先に、長い fy を上に重ねて閉じる
FLAPS = {"fx0": 1, "fx1": -1, "fy0": -1, "fy1": 1}
FLAP_OPEN = 4.6
HM_RES = 0.01                         # 配置計画のハイトマップの刻み[m]
GAP = 0.008                           # 品どうし・壁とのすき間[m](置く精度の余裕)

# ---- 出荷レーン(コンベヤ) ----
BELT_W = 0.40                         # ベルトの幅(|y| < 0.20)
BELTS = {                             # 名前: (x0, x1)。区間ごとに別々に動かす・止める
    "infeed": (-1.75, -0.40),
    "pack": (-0.40, 0.40),
    "outfeed": (0.40, 3.40),             # 俯瞰カメラの画面の外まで延ばす(出荷した箱はそのまま流れて見えなくなる)
}
ERECTOR_X = -1.40                     # 製函機の出口(組み立てた空箱が出てくる位置)
WAIT_X = -0.80                        # 待機位置(次の空箱がここで待つ)
PACK_X = 0.0                          # 梱包位置
SEAL_X = 1.10                         # 封函機(フラップを閉じてテープを貼る位置)
EXIT_X = 3.00                         # 出荷(ここまで流したら搬出済み。俯瞰カメラの画面の外)
BELT_SPEED = 0.25                     # [m/s]
BELT_ACC = 0.3                        # [m/s^2] 中身がずれない加減速

# 共有ゾーン: 梱包位置の上空(同時に1台)。品を置く・自分の上流側と下流側の棚を行き来する(ベースの前を通る)ときに占有する
ZONE_Y = 0.22

# 仮置き台(ArmB 専用): ArmB の下流側の棚の奥の段
BUFFER_SLOTS = [(0.34, -0.585), (0.47, -0.585), (0.60, -0.585)]

# ---- 棚(2段: 手前・奥) ----
# 品は実寸なので、スロットは品ごとに中心座標で決める。棚の |x| の範囲は両側(上流 -x・下流 +x)とも同じ。
SHELF_STEPS = {"front": (0.24, 0.47, 0.72), "back": (0.47, 0.70, 0.78)}     # |y| の範囲と天面の高さ
SHELF_X = (0.26, 0.68)                 # |x| の範囲
SHELF_SIGN = {"L": 1.0, "R": -1.0}
SHELF_OWNER = {"L": "arm_a", "R": "arm_b"}

# 在庫: (棚, 段, x, |y|, SKU[, 何段目])。x は符号付き(負 = 上流側の棚)。何段目は重ねた品の下からの段(省略時 0)。
# 重ねる品: 耐荷重の範囲で店頭でも重ねるもの(米・缶詰・アイス)。割れ物・潰れやすい品・背が高い品は並べる。
STOCK = [
    ("L", "front", -0.47, 0.355, "rice"), ("L", "front", -0.47, 0.355, "rice", 1),
    ("L", "back", -0.47, 0.53, "water"), ("L", "back", -0.47, 0.645, "water"),
    ("L", "front", 0.34, 0.33, "canned"), ("L", "front", 0.34, 0.33, "canned", 1),
    ("L", "front", 0.45, 0.33, "canned"), ("L", "front", 0.45, 0.33, "canned", 1),
    ("L", "front", 0.59, 0.30, "detergent"), ("L", "front", 0.59, 0.40, "detergent"),   # 背が高い(21cm)ので低い手前の段に
    ("L", "back", 0.47, 0.585, "chips"),
    ("R", "front", -0.53, 0.30, "egg"), ("R", "front", -0.53, 0.415, "egg"),
    ("R", "front", -0.33, 0.30, "tofu"), ("R", "front", -0.33, 0.415, "tofu"),
    ("R", "back", -0.60, 0.585, "bread"), ("R", "back", -0.455, 0.585, "bread"),
    ("R", "back", -0.32, 0.585, "ice"), ("R", "back", -0.32, 0.585, "ice", 1),
    ("R", "front", 0.36, 0.35, "milk"),          # 牛乳は最後の1本(注文で使い切ると、次の注文では欠品になる)
    ("R", "front", 0.48, 0.30, "lowfat_milk"), ("R", "front", 0.48, 0.40, "lowfat_milk"),
]


def slot_level(slot):
    """重ねた品の何段目か(0 = 棚に直接置いた品)。"""
    return slot[5] if len(slot) > 5 else 0


def slot_xy(slot):
    sh, step, x, y = slot[:4]
    return (x, SHELF_SIGN[sh] * y)


def slot_side(slot):
    """スロットのある側: -1 = 上流側の棚、+1 = 下流側の棚。"""
    return 1 if slot[2] > 0 else -1


def slot_top(slot):
    return SHELF_STEPS[slot[1]][2]


SHARED_REACH = 0.72                   # 主担当でないアームからの水平距離がこれ以下なら共有スロット(両方が届く。今の配置では該当なし)


def slot_shared(slot):
    other = "arm_b" if SHELF_OWNER[slot[0]] == "arm_a" else "arm_a"
    x, y = slot_xy(slot)
    bx, by = ARMS[other]["xy"]
    return ((x - bx) ** 2 + (y - by) ** 2) ** 0.5 <= SHARED_REACH


# ---- カメラ ----
CAMERAS = {
    # 名前: (pose "x y z roll pitch yaw", 幅, 高さ, 水平FOV, Hz)
    "cam_shelf_l": ("0 0.47 1.95 0 1.5708 1.5708", 800, 400, 1.35, 5),       # 3D バウンディングボックスカメラ(ArmA の両側の棚)
    "cam_shelf_r": ("0 -0.47 1.95 0 1.5708 1.5708", 800, 400, 1.35, 5),
    "cam_box": ("0 0 2.80 0 1.5708 1.5708", 1280, 720, 0.62, 10),     # 箱の真上(長辺が横・上流が左。両アームの手先まで入る)
    "cam_overview": ("0.05 -1.95 3.30 0 0.887 1.639", 1440, 780, 1.04, 30),   # 正面の高い位置からの俯瞰: レーン全体と2台のアーム
    "cam_side": ("0 -1.6 1.15 0 0.22 1.5708", 1280, 720, 0.75, 30),
}


BOX_FLOOR_Z = TABLE_TOP + BOX_WALL    # 箱の内底面
