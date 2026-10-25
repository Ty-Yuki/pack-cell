"""セル全体の組み立て: ワールド・アーム・LLM・計画・実行器を束ね、注文を最後まで処理する。

CLI(scripts/run_order.py)と Web UI の両方から使う。動的なイベント(欠品・追加注文・失敗注入)もここで扱う。
"""
import json, math, os, pathlib, threading, time

import rclpy

from pack_cell import layout as L
from pack_cell.core import catalog, dag as D, joint, order as O, planner as PL
from pack_cell.core.scheduler import A, B
from pack_cell.coord.executor import Executor
from pack_cell.llm import prompts as PR
from pack_cell.llm.client import LlmClient
from pack_cell.llm.interpret import interpret
from pack_cell.robot.arm import Arm
from pack_cell.robot.skills import Skills
from pack_cell.sim.line import Line
from pack_cell.sim.world import World

LOG_DIR = pathlib.Path(os.environ.get("RUN_LOG_DIR", "/ws/logs/runs"))


class Cell:
    def __init__(self, on_event=None, llm=None):
        self.cat = catalog.load()
        self.planner = PL.Planner(self.cat)            # 最大の箱(解釈の検証で「入るか」を確かめる)
        self.planners = {b: PL.Planner(self.cat, b) for b in L.BOX_ORDER}
        self.llm = llm or LlmClient()
        self.world = World(self.cat)
        self.llm.sleep = self.world.gz.sim_sleep          # 記録した LLM の応答を再現するときは sim 時刻で待つ
        if not rclpy.ok():
            rclpy.init()
        self.arms = {A: Arm(A), B: Arm(B)}
        self.skills = {k: Skills(v, self.world.gz, log=lambda m: None) for k, v in self.arms.items()}
        # 起動時の待機点への移動は1台ずつ(真上向きから同時に動くと、途中で腕どうしが当たることがある)。失敗は起動エラーにする
        from pack_cell.robot.arm import ArmError
        self.startup_recoveries = 0
        for a in self.arms.values():
            for attempt in range(3):
                try:
                    a.home()
                    break
                except ArmError as e:
                    if attempt == 2:
                        raise
                    print(f"[warn] {a.name}: 起動時の待機点への移動に失敗 → 解除の再送と初期配置の復元 ({e})", flush=True)
                    self.startup_recoveries += 1
                    self.world.recover_start()
        self.ex = Executor(self.world, self.arms, self.skills, self.cat, on_event=on_event)
        self.emit = self.ex.emit
        self.line = Line(self.world, emit=self.emit)
        self.world.line = self.line
        self.box_items = {}           # 箱 -> 詰めた品のモデル(出荷後に棚へ補充する)
        self.shipped = []             # 出荷した箱の判定(封函機の位置で取る)

    # ---- 解釈と計画 ----
    def understand(self, text):
        self.emit("operator_send", text=text)          # 動画のチャット欄: 送信の時刻(LLM の応答待ちを表示する)
        r = interpret(text, self.cat, self.planner, self.llm)
        self.emit("interpreted", text=text, spec=r.spec.to_dict() if r.spec else None, rounds=r.rounds,
                  clarification=r.clarification, latency=round(r.latency, 2), ok=r.ok)
        return r

    def reserved(self):
        """今詰めている注文がこれから取る品の数(SKU -> 数)。次の注文の在庫確認ではこの分を差し引く。"""
        plan = getattr(self.ex, "plan", None)
        out = {}
        if plan is not None and getattr(self.ex, "sch", None) is not None and not self.ex.sch.finished():
            for u, p in plan.placements.items():
                if u not in self.world.unit_model:
                    out[p.sku] = out.get(p.sku, 0) + 1
        return out

    def stock_check(self, spec):
        """注文の品が棚にあるか(知覚)。足りない SKU -> 不足数。
        待機中の腕がカメラと品の間に入って見えないことがあるので、前の注文で最後に見えた在庫(まだ使っていないもの)も数える。"""
        inv = dict(self.world.inventory())
        used = set(self.world.unit_model.values()) | {m for ms in self.box_items.values() for m in ms} | self.world.shipped_models
        hidden = self.world.hidden
        if getattr(self.ex, "inventory", None):
            inv.update({m: v for m, v in self.ex.inventory.items() if m not in inv and m not in used and m not in hidden})
        for m in list(inv):                                # 重ねた品の下(上の品に隠れて映らない)も棚の管理情報から数える
            for below in self.world.stacked_below(m):
                if below not in used and below not in hidden:
                    inv.setdefault(below, None)
        have = {}
        for model in inv:
            sku = model.rsplit("_", 1)[0]
            have[sku] = have.get(sku, 0) + 1
        for sku, n in self.reserved().items():             # 今の注文で使う予定の分は、次の注文には回せない
            have[sku] = have.get(sku, 0) - n
        return {s: q - have.get(s, 0) for s, q in spec.items.items() if have.get(s, 0) < q}, have

    def substitute(self, missing_sku, have):
        in_stock = [s for s, n in have.items() if n > 0]
        out = self.llm.json(PR.substitute_messages(self.cat[missing_sku], self.cat, in_stock), PR.SUBSTITUTE_SCHEMA, "sub")
        sku = out.get("sku")
        ok = sku in self.cat and sku in in_stock
        self.emit("substitute", missing=missing_sku, proposal=sku if ok else None, reason=out.get("reason", ""))
        return (sku if ok else None), out.get("reason", "")

    def apply_substitution(self, spec, missing, sub):
        q = spec.items.pop(missing)
        spec.items[sub] = spec.items.get(sub, 0) + q
        cons = []
        for c in spec.constraints:
            args = tuple(sub if a == missing else a for a in c.args)
            cons.append(O.Constraint(c.type, args, c.source, c.hard, c.evidence))
        spec.constraints = cons

    def sources_now(self):
        inv = self.world.inventory()
        out = {}
        for model, (src, _) in inv.items():
            out.setdefault(model.rsplit("_", 1)[0], []).append(src)
        return out

    def choose_box(self, spec):
        """注文の品が入る最小の箱(小 → 中 → 大の順に配置を試す)。製函機に頼むサイズ。"""
        for b in L.BOX_ORDER:
            plan, _ = self.planners[b].plan(spec)
            if plan is not None:
                o = L.box_outer(b)
                self.emit("box_chosen", box=b, name=L.BOXES[b]["name"], outer=[round(v * 100) for v in o])
                return b
        return L.BOX_ORDER[-1]

    def make_plan(self, spec, strategy="S2", fixed=None, max_remove=0, joint_opt=True, box=None):
        # 実行中(再計画)は、実行器の在庫(最後に見えた位置 + 仮置き台)を使う。撮り直すと腕で隠れた品が抜ける
        sources = self.ex.sources() if getattr(self.ex, "inventory", None) is not None and fixed is not None else self.sources_now()
        if box is None:                                    # 再計画は今の箱のまま
            box = self.ex.plan.box if fixed is not None and getattr(self.ex, "plan", None) else L.BOX_ORDER[-1]
        planner = self.planners[box]
        if joint_opt:
            plan, issues, scored = joint.choose(planner, spec, sources, strategy, fixed=fixed,
                                                max_remove=max_remove, with_box=fixed is None)
        else:
            plan, issues = planner.plan(spec, fixed=fixed, max_remove=max_remove)
        if plan is None:
            return None, None, issues
        dag = D.build(spec, plan, fixed)
        return plan, dag, issues

    # ---- 実行 ----
    def execute(self, spec, plan, dag, strategy="S2", then="ship"):
        """1注文を詰める。空箱がまだ梱包位置になければコンベヤで運び込む(その間もアームは取り出しを進める)。
        then: "ship" = 詰め終えたら封函・出荷まで待つ / "next" = 封函・出荷と次の空箱の搬入を裏で始めて戻る / None = 箱を残す"""
        t0 = time.time()
        def ready(box, plan=plan):
            # 梱包位置に来た箱のサイズが計画と違えば、詰め始める前に止める(製函の依頼の順番の食い違い)
            if self.world.box_size.get(box) != plan.box:
                self.ex.error = f"梱包位置の箱({box})のサイズが計画({plan.box})と違う"
                self.emit("error", arm=None, message=self.ex.error)
                self.ex.stop.set()
                return
            self.ex.box_ready(box)
        self._ready = ready
        if self.line.at_pack is None and not self.ex.box_present:
            self.line.start_cycle(on_ready=ready, on_shipped=self._shipped)
        makespan = self.ex.run(spec, plan, dag, strategy=strategy)
        box = self.line.at_pack
        judge = self.world.judge(self.ex.plan, carton=box)
        self.emit("judge", **judge)
        self.box_items[box] = [self.world.unit_model[u] for u in self.ex.plan.placements if u in self.world.unit_model]
        res = {"makespan": makespan, "wall": round(time.time() - t0, 1), "error": self.ex.error, "judge": judge, "box": box}
        if self.ex.error:
            return res
        self.ex.box_present = False
        if then == "ship":
            t1 = self.world.gz.sim_time()
            self.line.finish(on_shipped=self._shipped)
            res["ship_s"] = round(self.world.gz.sim_time() - t1, 1)
            res["judge_shipped"] = self.shipped[-1][1] if self.shipped else None
        elif then == "next":                               # 次の注文の箱の確認は、次の execute() で差し替える
            self.line.start_cycle(on_ready=lambda b: self._ready(b), on_shipped=self._shipped)
        return res

    def _shipped(self, box):
        """出荷口に着いた箱: 中身の判定を記録し、箱を中身ごと片付ける。"""
        plan = self.ex.plan
        try:
            j = self.world.judge_box(box, self.box_items.get(box, []))
        except Exception as e:
            j = {"error": str(e)}
        self.shipped.append((box, j))
        self.emit("shipped", box=box, judge=j)
        models = self.box_items.pop(box, [])
        self.world.park_box(box, models)               # 中身ごと片付ける(棚には戻さない。在庫は棚に2個ずつ置いてある)

    # ---- 追加注文(実行中の再計画) ----
    def placed_fixed(self):
        """箱に入っている品の配置(計画どおりに置けている前提。実測の誤差はイベントに記録済み)。"""
        sch, plan = self.ex.sch, self.ex.plan
        return {u: p for u, p in plan.placements.items() if u in sch.done}

    def add_items(self, spec, text, choose="auto", chooser=None, reply=None):
        """実行を一時停止した状態で呼ぶ。追加分を解釈して元の注文に合流させ、再計画して差し替える。
        戻り値: 選んだ案の id(None なら差し替えなし)。"""
        self.emit("operator_send", text=text, add=True)
        msgs = PR.add_messages(text, self.cat, spec)
        out = self.llm.json(msgs, O.ORDER_SCHEMA, "order")
        delta, issues = O.from_json(out, self.cat, known=set(spec.items))
        if issues:                                    # 形式・カタログの問題は1回だけ直させる
            out = self.llm.json(PR.repair_messages(msgs, out, issues), O.ORDER_SCHEMA, "order")
            delta, issues = O.from_json(out, self.cat, known=set(spec.items))
        self.emit("interpreted_add", text=text, delta=delta.to_dict(), issues=[str(i) for i in issues])
        if not delta.items:
            self.emit("add_failed", text=text, reason=delta.clarification or "追加の品が特定できない")
            return None
        merged = O.OrderSpec(dict(spec.items), list(spec.constraints))
        r = type("R", (), {"spec": delta})
        for sku, q in r.spec.items.items():
            merged.items[sku] = merged.items.get(sku, 0) + q
        merged.constraints += [c for c in r.spec.constraints if c not in merged.constraints]
        fixed = self.placed_fixed()
        self.emit("add_request", text=text, added=r.spec.to_dict()["items"], fixed=sorted(fixed))
        options = []
        plan0, dag0, iss0 = self.make_plan(merged, self.ex.sch.strategy, fixed=fixed, max_remove=0)
        if plan0 is not None:
            options.append({"id": "as_is", "summary": "今の箱の空きにそのまま追加する", "extra_picks": 0, "boxes": 1,
                            "plan": plan0, "dag": dag0})
        else:
            for k in (1, 2):
                p, d, iss = self.make_plan(merged, self.ex.sch.strategy, fixed=fixed, max_remove=k)
                if p is not None:
                    names = "・".join(self.cat[u.split('#')[0]].name for u in p.removed)
                    options.append({"id": "repack", "summary": f"{names}を一度仮置き台に出し、追加の品を入れてから戻す",
                                    "extra_picks": 2 * len(p.removed), "boxes": 1, "extra_time_s": 23.0 * len(p.removed),
                                    "plan": p, "dag": d})
                    break
            options.append({"id": "new_box", "summary": "追加の品だけ別の箱にする", "extra_picks": 0, "boxes": 2,
                            "extra_time_s": 46.0, "plan": None, "dag": None})
        situation = f"箱詰めの終盤に追加注文「{text}」が来た。"
        if plan0 is None:
            situation += "今の箱の空きには入らない(" + " / ".join(str(i) for i in iss0) + ")。"
        ids = [o["id"] for o in options]
        schema = {**PR.EXPLAIN_SCHEMA, "properties": {**PR.EXPLAIN_SCHEMA["properties"],
                                                      "recommended": {"type": "string", "enum": ids}}}
        msgs = PR.explain_messages(situation, options)
        out = self.llm.json(msgs, schema, "explain")
        # 推奨の欄と説明の文が食い違う(文では別の案を勧めている)ことがある → 1回だけ聞き直す
        said = [i for i in ids if i in out.get("message", "")]
        if out.get("recommended") not in ids or (said and out.get("recommended") not in said):
            fix = msgs + [{"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
                          {"role": "user", "content": "recommended と message の勧める案が食い違っています。"
                                                      "どちらか1つに決め、両方をそろえて返してください。"}]
            out = self.llm.json(fix, schema, "explain")
        rec = out.get("recommended")
        self.emit("replan_options", situation=situation, recommended=rec, message=out.get("message", ""),
                  options=[{k: v for k, v in o.items() if k not in ("plan", "dag")} | (
                      {"plan": o["plan"].to_dict()} if o["plan"] else {}) for o in options])
        if chooser is not None:                       # UI: オペレータに選ばせる(時間切れなら推奨)
            pick = chooser([o["id"] for o in options], rec) or rec
        else:
            pick = rec if choose == "auto" else choose
            if reply and pick != rec:                 # オペレータが推奨と違う案を選ぶ(返信を読んで打つ時間を待ってから)
                self.world.gz.sim_sleep(2.0 + len(reply) / 9.0)
                self.emit("operator_send", text=reply, reply=True)
                self.emit("replan_override", recommended=rec, chosen=pick)
        opt = next((o for o in options if o["id"] == pick), options[0])
        self.last_delta = delta
        if opt["plan"] is None:
            # 別の箱: 追加分は次の箱(コンベヤが次に運んでくる空箱)に詰める。呼び出し側が次の注文として続ける
            self.emit("replan_chosen", id=opt["id"], applied=False, next_box=delta.to_dict())
            return opt["id"]
        self.ex.replace_plan(opt["plan"], opt["dag"], fixed)
        self.ex.spec = merged
        self.emit("replan_chosen", id=opt["id"], applied=True, plan=opt["plan"].to_dict(), dag=opt["dag"].to_dict())
        return opt["id"]

    def save(self, name, result):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{name}.json"
        path.write_text(json.dumps({"result": result, "events": self.ex.events}, ensure_ascii=False, indent=1, default=str))
        return path
