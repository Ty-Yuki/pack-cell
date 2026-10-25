"""注文を処理する(LLM の解釈 → 欠品確認 → 計画 → 2腕で実行 → 判定 → 封函・出荷)。

シナリオに orders(複数の注文)があれば、出荷レーンで連続して処理する: 次の注文の解釈は前の注文を詰めている間に進め、
詰め終えたら封函・出荷と次の空箱の搬入を同時に行い、すぐ次の注文の取り出しを始める。
追加注文で「別の箱」が選ばれたら、追加分を次の箱の注文として続ける。

usage: ./scripts/run_sim.sh -- python3 -u scripts/run_order.py --text "米と缶詰を2つ…" [--strategy S2] [--name run1]
       ./scripts/run_sim.sh -- python3 -u scripts/run_order.py --scenario order2
       ./scripts/run_sim.sh -- python3 -u scripts/run_order.py --scenario lane
"""
import argparse, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/ws/src")
import yaml

from pack_cell.app import Cell
from pack_cell.core import order as O

ap = argparse.ArgumentParser()
ap.add_argument("--text")
ap.add_argument("--scenario")
ap.add_argument("--strategy", default="S2")
ap.add_argument("--no-joint", action="store_true", help="配置とスケジュールの同時最適化を使わない")
ap.add_argument("--no-ship", action="store_true", help="詰めた箱を封函・出荷しない(1注文のみ)")
ap.add_argument("--name", default=None)
ap.add_argument("--fail-prob", type=float, default=0.0, help="品の吸着を確率で失敗させる(E3)")
ap.add_argument("--seed", type=int, default=0)
ap.add_argument("--strict-startup", action="store_true", help="起動時に立て直しが必要なら終了コード3で止める(評価用)")
ap.add_argument("--record", nargs="*", default=None, help="録画するカメラ(既定: cam_overview cam_box)")
a = ap.parse_args()

sc = {}
if a.scenario:
    sc = yaml.safe_load(open(f"/ws/config/scenarios/{a.scenario}.yaml"))
orders = sc.get("orders") or [{**sc, "text": a.text or sc.get("text")}]
import faulthandler
if os.environ.get("HANG_DUMP_SEC"):
    faulthandler.dump_traceback_later(int(os.environ["HANG_DUMP_SEC"]), exit=False)
try:
    cell = Cell()
except Exception as e:
    print("STARTUP_FAILED", e, flush=True)
    os._exit(3)
if a.strict_startup:
    inv0 = cell.world.inventory()
    n_seen = len(inv0) + sum(len(cell.world.stacked_below(m)) for m in inv0)   # 重ねた下の品は棚の管理情報から数える
    if cell.startup_recoveries or n_seen < len(cell.world.items):
        # 解除命令の取りこぼしで品がアームに溶接されたまま持ち去られると、棚の在庫が欠ける
        print(f"STARTUP_FAILED 立て直し {cell.startup_recoveries} 回 / 棚の在庫 {n_seen}/{len(cell.world.items)}", flush=True)
        os._exit(3)
for o in orders:                                       # 欠品は最初から(録画の前に棚から外す。途中で消えると不自然)
    for m in o.get("hide", []):
        cell.world.hide(m)
cell.ex.fail_prob = a.fail_prob
cell.ex.rng.seed(a.seed)
recs = []
name = a.name or a.scenario or "order"
if a.record is not None:
    import subprocess
    os.makedirs("/ws/logs/video", exist_ok=True)
    for cam in (a.record or ["cam_overview", "cam_box"]):
        recs.append(subprocess.Popen([sys.executable, "-m", "pack_cell.sim.record_cli", cam, f"/ws/logs/video/{name}_{cam}.mp4"],
                                     env={**os.environ, "PYTHONPATH": "/ws/src:" + os.environ.get("PYTHONPATH", "")}))
    time.sleep(2.0)

if a.record is not None:
    os.environ["LLM_EMULATE_LATENCY"] = "1"     # 録画では、記録した LLM の応答も実際の所要時間だけ待つ
TYPE_CPS, READ_S = 9.0, 2.0                     # オペレータが次の注文を打つ速さ [文字/s] と、返信を読む時間 [s]
pool = ThreadPoolExecutor(max_workers=1)        # 次の注文は、前の注文の返信を読んで打ち終えてから送る(詰めるのと並行)


def confirm_substitutes(spec, o):
    """欠品の確認(今詰めている注文が使う分を差し引く)。欠品があれば LLM が代替を提案し、オペレータの承認を得てから注文に反映する。
    シナリオの approve: 承認の返信(既定は「はい、<代替品>でお願いします。」)/ false なら代替せず、その品を除く。"""
    missing, have = cell.stock_check(spec)
    for sku in list(missing):
        sub, reason = cell.substitute(sku, have)
        ans = o.get("approve", True)
        if sub is None or ans is False:
            cell.emit("substitute_decided", missing=sku, sub=sub, approved=False)
            spec.items.pop(sku, None)
            continue
        text = ans if isinstance(ans, str) else f"はい、{cell.cat[sub].name}でお願いします。"
        cell.world.gz.sim_sleep(READ_S + len(text) / TYPE_CPS)   # 確認の返信を読んで打つ時間
        cell.emit("operator_send", text=text, reply=True)
        cell.emit("substitute_decided", missing=sku, sub=sub, approved=True)
        print(f"欠品: {sku} → 代替 {sub}(承認)", flush=True)
        cell.apply_substitution(spec, sku, sub)


def understand_and_request(o, wait=True):
    """注文を解釈し、欠品があれば代替の承認を得て、入る最小の箱を選んで製函機に頼む
    (次の注文は、今の注文を詰めている間にここまで進める)。"""
    text = o["text"]
    if wait:
        cell.world.gz.sim_sleep(READ_S + len(text) / TYPE_CPS)   # 前の返信を読み、次の注文を打つ時間(sim 時刻)
    r = cell.understand(text)
    r.box = None
    if r.ok:
        confirm_substitutes(r.spec, o)
        r.box = cell.choose_box(r.spec)
        cell.line.request(r.box)
    return r


pending = {}                                    # 注文(id) -> 解釈の Future。追加分(別の箱)の注文は LLM を通さない
for i, o in enumerate(orders):
    if o.get("text"):
        pending[id(o)] = pool.submit(understand_and_request, o, i > 0)


def add_hook(ev):
    """追加注文: 指定の品を置き終えた直後(全品なら出荷の直前)に一時停止し、別スレッドで再計画する。"""
    def hook(act, ok, ev=ev):
        if not ok or act.kind != "place" or ev.get("_fired"):
            return
        sch = cell.ex.sch
        n_placed = sum(1 for u in sch.units if u in sch.done)
        if n_placed >= ev.get("after_placed", len(sch.units)):
            ev["_fired"] = True
            cell.ex.pause.set()

            def work():
                try:
                    cell.ex.wait_idle()
                    pick = cell.add_items(cell.ex.spec, ev["add"], ev.get("choose", "auto"), reply=ev.get("reply"))
                    if pick == "new_box":                 # 追加分は別の箱: 受け付け済みの注文の後に並べる(製函の依頼の順と合わせる)
                        orders.append({"spec": cell.last_delta, "label": "追加分(別の箱)"})
                except Exception as e:
                    import traceback; traceback.print_exc()
                    cell.emit("error", arm=None, message=f"再計画に失敗: {e}")
                finally:
                    cell.ex.pause.clear()
            threading.Thread(target=work, daemon=True).start()
    return hook


results, code = [], 0
k = 0
while k < len(orders):
    o = orders[k]
    last = k == len(orders) - 1
    cell.emit("order_queue", index=k, total=len(orders), text=o.get("text") or o.get("label"))
    box = None
    if o.get("spec") is not None:                          # 追加分(別の箱): LLM を通さず、入る最小の箱を頼む
        spec = o["spec"]
        box = cell.choose_box(spec)
        cell.line.request(box)
        cell.emit("interpreted", text=o.get("label"), spec=spec.to_dict(), rounds=[], clarification=None, latency=0.0, ok=True)
    else:
        fut = pending.get(id(o))
        r = fut.result() if fut else understand_and_request(o, wait=False)
        box = r.box
        if not r.ok:
            print("確認が必要:", r.clarification, flush=True)
            os._exit(2)
        spec = r.spec
    for m, n in o.get("inject_fail", {}).items():
        cell.ex.inject[m] = n
    confirm_substitutes(spec, o)                           # 始める直前にも欠品を確かめる(解釈の時点で確認済みなら何もしない)
    plan, dag, issues = cell.make_plan(spec, a.strategy, joint_opt=not a.no_joint, box=box)
    if plan is None:
        print("計画できない:", [str(i) for i in issues], flush=True)
        os._exit(4)
    hooks = [add_hook(ev) for ev in o.get("events", []) if "add" in ev]
    cell.ex.after_finish = (lambda act, ok: [h(act, ok) for h in hooks]) if hooks else None
    last = k == len(orders) - 1                                 # 追加注文で次の箱が増えることがある
    res = cell.execute(spec, plan, dag, strategy=a.strategy, then=None if a.no_ship else ("ship" if k == len(orders) - 1 else "next"))
    cell.ex.after_finish = None
    res["order"] = k
    results.append(res)
    print(f"結果[{k}]:", {kk: v for kk, v in res.items() if kk != "judge"}, "ok=", res["judge"]["ok"], flush=True)
    if res["error"] or not res["judge"]["ok"]:
        code = 1
        break
    k += 1
cell.line.join()
cell.ex._snapshot(f"final_{name}")
for p in recs:
    p.terminate()
for p in recs:
    p.wait(timeout=180)
summary = {"orders": results, "shipped": [{"box": b, **j} for b, j in cell.shipped],
           "makespan": results[0]["makespan"] if len(results) == 1 else None}
if len(results) == 1:
    summary.update(results[0])
print("結果:", summary, flush=True)
path = cell.save(name if a.name or a.scenario else f"order_{a.strategy}_{int(time.time())}", summary)
print("保存:", path, flush=True)
ok = code == 0 and all(j.get("ok") for _, j in cell.shipped) and (a.no_ship or len(cell.shipped) == len(results))
os._exit(0 if ok else 1)
