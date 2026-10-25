"""計画のテキスト表示(CLI・ログ用)。"""
from pack_cell.core.planner import unit_sku


def grid_text(plan, cat):
    rows = []
    for p in sorted(plan.placements.values(), key=lambda p: (p.bottom, p.x, p.y)):
        on = ",".join(cat[unit_sku(s)].name for s in plan.supports.get(p.unit, [])) or "底"
        rows.append(f"  {cat[p.sku].name:　<8} x={100 * p.x:+5.1f} y={100 * p.y:+5.1f} 底={100 * p.bottom:4.1f}cm "
                    f"{'90°' if p.yaw else ' 0°'}  上に載る先: {on}")
    return "\n".join(rows)
