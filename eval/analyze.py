"""E2/E3 の集計: Gazebo の実行ログから所要時間・成功率・稼働率を表にし、DES の予測と比べる。

usage: ./scripts/docker_run.sh python3 eval/analyze.py
出力: results/e2_runs.csv, results/summary.md, results/e2_makespan.png, results/e2_des_vs_gazebo.png
"""
import collections, csv, glob, json, math, pathlib, statistics as st, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from pack_cell.core import des
from pack_cell.core.dag import Dag
from pack_cell.core.scheduler import Source
from pack_cell import layout as L

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "results"
OUT.mkdir(exist_ok=True)
ORDER_JA = {"order1": "注文1(5点・卵は最後)", "order2b": "注文2(5点・冷たいものはまとめて)", "order3": "注文3(8点)"}
STRAT_JA = {"S0": "S0 逐次", "S1": "S1 固定分担", "S2": "S2 提案", "S2J": "S2+同時最適化"}


def load(prefix):
    runs = []
    for f in sorted(glob.glob(str(ROOT / f"logs/runs/{prefix}_*.json"))):
        name = pathlib.Path(f).stem
        parts = name.split("_")
        d = json.load(open(f))
        ev = d["events"]
        end = next((e for e in ev if e["kind"] == "order_end"), None)
        judge = next((e for e in ev if e["kind"] == "judge"), {})
        start = next((e for e in ev if e["kind"] == "order_start"), None)
        acts = collections.defaultdict(list)
        busy = collections.Counter()
        opened = {}
        for e in ev:
            if e["kind"] == "start":
                opened[e["arm"]] = e
            elif e["kind"] == "end" and e["arm"] in opened:
                s = opened.pop(e["arm"])
                dt = e["t"] - s["t"]
                key = s["action"]
                if key == "fetch" and s.get("source"):
                    key = "fetch"
                acts[key].append(dt)
                busy[e["arm"]] += dt
        ready = next((e for e in ev if e["kind"] == "line" and e.get("step") == "ready"), None)
        box_at = max(0.0, ready["t"] - start["t"]) if ready and start else None
        runs.append({"name": name, "order": parts[1], "strategy": parts[2], "seed": int(parts[3][1:]), "box_at": box_at,
                     "makespan": end["makespan"] if end else None, "error": end.get("error") if end else "no end",
                     "ok": bool(judge.get("ok")) and not (end or {}).get("error"),
                     "max_tilt": judge.get("max_tilt_deg"), "busy_a": busy["arm_a"], "busy_b": busy["arm_b"],
                     "grasp_failed": sum(1 for e in ev if e["kind"] == "grasp_failed"),
                     "acts": dict(acts), "dag": start["dag"] if start else None, "events": ev})
    return runs


def calibrate(runs):
    pool = collections.defaultdict(list)
    for r in runs:
        for k, v in r["acts"].items():
            pool[k] += v
    dur = dict(des.DUR)
    for k, v in pool.items():
        if v:
            dur[k] = st.median(v)
    if "fetch" in pool:
        dur["fetch_cross"] = dur["fetch"] * des.DUR["fetch_cross"] / des.DUR["fetch"]
    return dur, {k: (round(st.median(v), 1), len(v)) for k, v in pool.items()}


def stock_sources():
    from pack_cell.world_spec import stock_sources as ss
    return ss()


def des_predict(r, dur):
    d = r["dag"]
    preds = {n: set() for n in d["nodes"]}
    for a, b in d["edges"]:
        preds[b].add(a)
    strategy = "S2" if r["strategy"] == "S2J" else r["strategy"]
    return des.simulate(Dag(d["nodes"], preds), stock_sources(), strategy, dur=dur, box_at=r.get("box_at")).makespan


def fmt(v):
    return f"{st.mean(v):.1f} ± {st.stdev(v):.1f}" if len(v) > 1 else (f"{v[0]:.1f}" if v else "-")


def main():
    runs = load("e2")
    if not runs:
        print("e2 の結果がない"); return
    dur, med = calibrate([r for r in runs if r["ok"]])
    for r in runs:
        r["des"] = des_predict(r, dur) if r["dag"] else None
    with open(OUT / "e2_runs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "order", "strategy", "seed", "makespan", "des_pred", "ok", "max_tilt", "busy_a", "busy_b", "grasp_failed", "error"])
        for r in runs:
            w.writerow([r["name"], r["order"], r["strategy"], r["seed"], r["makespan"], round(r["des"] or 0, 1), r["ok"],
                        r["max_tilt"], round(r["busy_a"], 1), round(r["busy_b"], 1), r["grasp_failed"], r["error"]])
    orders = [o for o in ORDER_JA if any(r["order"] == o for r in runs)]
    strats = [s for s in STRAT_JA if any(r["strategy"] == s for r in runs)]
    lines = ["# 評価結果のまとめ", "", "## E2 協調(Gazebo, sim 時刻, 平均 ± 標準偏差)", "",
             "| 注文 | " + " | ".join(STRAT_JA[s] for s in strats) + " | S0→最良の短縮 |", "|---" * (len(strats) + 2) + "|"]
    table = {}
    for o in orders:
        row = []
        for s in strats:
            v = [r["makespan"] for r in runs if r["order"] == o and r["strategy"] == s and r["ok"]]
            n_all = sum(1 for r in runs if r["order"] == o and r["strategy"] == s)
            table[(o, s)] = v
            row.append(f"{fmt(v)}s ({len(v)}/{n_all})")
        base = st.mean(table[(o, "S0")]) if table.get((o, "S0")) else None
        best = min((st.mean(table[(o, s)]) for s in strats if table.get((o, s))), default=None)
        red = f"{100 * (1 - best / base):.0f}%" if base and best else "-"
        lines.append(f"| {ORDER_JA[o]} | " + " | ".join(row) + f" | {red} |")
    boxes = {}
    for r in runs:
        b = next((e.get("name") for e in r["events"] if e["kind"] == "box_chosen"), None)
        if b:
            boxes.setdefault(r["order"], set()).add(b)
    lines += ["", "括弧内は成功数/実行数。成功 = エラーなく全品を詰め終え、真値の判定(全品が箱内・傾き5°未満・上下関係)を満たす。",
              "所要時間 = 注文の開始から全品を詰め終えるまで(空箱がコンベヤで梱包位置に届くまでの時間を含む。封函・出荷は含まない)。",
              "選ばれた箱: " + "、".join(f"{ORDER_JA[o]} → {'/'.join(sorted(b))}" for o, b in boxes.items() if o in ORDER_JA), ""]
    ok_all = [r for r in runs if r["ok"]]
    lines += [f"- 全体の成功率: {len(ok_all)}/{len(runs)}",
              f"- 詰め終わりの最大傾き: {max((r['max_tilt'] or 0) for r in ok_all):.1f}°" if ok_all else "",
              "- 行動ごとの所要時間の中央値(Gazebo 実測, 件数): " + ", ".join(f"{k} {v[0]}s({v[1]})" for k, v in sorted(med.items())), ""]
    pairs = [(r["makespan"], r["des"]) for r in ok_all if r["des"]]
    if pairs:
        err = [abs(a - b) / a for a, b in pairs]
        lines += [f"- DES(実測で校正)の予測誤差: 平均 {100 * st.mean(err):.1f}% / 最大 {100 * max(err):.1f}%(n={len(pairs)})", ""]
    # E3: 吸着失敗の確率注入(注文3, S2+同時最適化)。失敗率0%は E2 の結果を使う
    e3 = {"0%": [r for r in runs if r["order"] == "order3" and r["strategy"] == "S2J"]}
    for tag, pre in (("20%", "e3p20"), ("40%", "e3p40")):
        e3[tag] = load(pre)
    if any(e3[k] for k in ("20%", "40%")):
        lines += ["## E3 ロバスト性(注文3, S2+同時最適化, 吸着失敗を確率で注入)", "",
                  "| 失敗率 | 成功 | 所要時間 | 吸着失敗の回数(平均) |", "|---|---|---|---|"]
        for tag, rs in e3.items():
            if not rs:
                continue
            v = [r["makespan"] for r in rs if r["ok"]]
            lines.append(f"| {tag} | {len(v)}/{len(rs)} | {fmt(v)}s | {st.mean(r['grasp_failed'] for r in rs):.1f} |")
        lines += ["", "失敗した吸着は再検出して再試行する(同じアームで最大2回、その後は相手のアームも候補)。", ""]
    (OUT / "summary.md").write_text("\n".join(lines))
    print("\n".join(lines))
    plot(table, orders, strats, pairs)


def plot(table, orders, strats, pairs):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        print("matplotlib がないのでグラフは省略"); return
    font_manager.fontManager.addfont("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    plt.rcParams["font.family"] = "Noto Sans CJK JP"
    fig, ax = plt.subplots(figsize=(9, 4.5))
    wbar = 0.8 / len(strats)
    colors = ["#9aa0a6", "#7aa2f7", "#f7768e", "#e0af68"]
    for i, s in enumerate(strats):
        means = [st.mean(table[(o, s)]) if table.get((o, s)) else 0 for o in orders]
        sds = [st.stdev(table[(o, s)]) if len(table.get((o, s), [])) > 1 else 0 for o in orders]
        ax.bar([j + i * wbar for j in range(len(orders))], means, wbar, yerr=sds, label=STRAT_JA[s], color=colors[i % 4], capsize=3)
    ax.set_xticks([j + wbar * (len(strats) - 1) / 2 for j in range(len(orders))])
    ax.set_xticklabels([ORDER_JA[o] for o in orders])
    ax.set_ylabel("所要時間 [s](sim 時刻)")
    ax.set_title("2腕の協調戦略による所要時間(Gazebo, 3シード)")
    ax.legend()
    fig.tight_layout(); fig.savefig(OUT / "e2_makespan.png", dpi=150)
    if pairs:
        fig, ax = plt.subplots(figsize=(4.5, 4.5))
        a, b = zip(*pairs)
        ax.scatter(a, b, s=18)
        lo, hi = min(a + b) * 0.9, max(a + b) * 1.1
        ax.plot([lo, hi], [lo, hi], "k--", lw=1)
        ax.set_xlabel("Gazebo の実測 [s]"); ax.set_ylabel("DES の予測 [s]")
        ax.set_title("所要時間モデルの妥当性")
        fig.tight_layout(); fig.savefig(OUT / "e2_des_vs_gazebo.png", dpi=150)


if __name__ == "__main__":
    main()
