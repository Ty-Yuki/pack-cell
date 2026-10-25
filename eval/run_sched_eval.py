"""E2 協調の評価: 注文 x 戦略 x シードを Gazebo で実行する(ホストから docker を並列に起動)。

usage: python3 eval/run_sched_eval.py [--orders order1 order2b order3] [--seeds 0 1 2] [--par 3]
戦略: S0(逐次) / S1(固定分担) / S2(提案) は同じ配置計画(物理コスト最良)で比べ、
      S2J は配置とスケジュールの同時最適化を加えたもの。
結果: logs/runs/e2_<order>_<strategy>_s<seed>.json
"""
import argparse, itertools, os, pathlib, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--orders", nargs="+", default=["order1", "order2b", "order3"])
ap.add_argument("--strategies", nargs="+", default=["S0", "S1", "S2", "S2J"])
ap.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2])
ap.add_argument("--par", type=int, default=3)
ap.add_argument("--extra", default="", help="run_order.py への追加引数")
ap.add_argument("--prefix", default="e2")
ap.add_argument("--skip-done", action="store_true")
a = ap.parse_args()

jobs = list(itertools.product(a.orders, a.strategies, a.seeds))
slots = list(range(a.par))


def run(job, slot):
    order, strat, seed = job
    name = f"{a.prefix}_{order}_{strat}_s{seed}"
    if a.skip_done and (ROOT / f"logs/runs/{name}.json").exists():
        return name, "skip"
    strategy = "S2" if strat == "S2J" else strat
    joint = "" if strat == "S2J" else "--no-joint"
    cmd = ["./scripts/run_sim.sh", "--seed", str(seed), "--", "python3", "-u", "scripts/run_order.py", "--scenario", order,
           "--strategy", strategy, "--name", name, "--seed", str(seed)] + ([joint] if joint else []) + a.extra.split()
    cmd += ["--strict-startup"]
    env = {**os.environ, "RUN_ID": str(slot + 1), "LLM_MODE": os.environ.get("LLM_MODE", "record")}
    t0 = time.time()
    for attempt in range(3):          # 起動時の失敗(終了コード3)は起動し直す。測る作業の前の段階なので結果から除外しない
        with open(ROOT / f"logs/{name}.out", "w") as f:
            rc = subprocess.call(["timeout", "1200"] + cmd, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
        if rc != 3:
            break
        with open(ROOT / "logs/startup_retries.txt", "a") as f:
            f.write(f"{name} attempt{attempt + 1}\n")
    return name, f"rc={rc} {time.time() - t0:.0f}s restarts={attempt}"


free = list(slots)
import threading
lock = threading.Lock()


def worker(job):
    with lock:
        slot = free.pop()
    try:
        r = run(job, slot)
        print(*r, flush=True)
        return r
    finally:
        with lock:
            free.append(slot)


with ThreadPoolExecutor(a.par) as ex:
    list(ex.map(worker, jobs))
