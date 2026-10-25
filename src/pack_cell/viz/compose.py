"""実行ログ(イベント)と録画から、説明付きの動画フレームを描く。

画面(1920x1080)。左の 1200 幅が「実際の動き」、右の 720 幅が「会話と計画」、最下段のガントは全幅:
  左上  俯瞰カメラ(1200x540。2.2:1 でレーン全体)と字幕の帯
  右上  チャット(オペレータと LLM の吹き出し・入力欄、720x540)
  中段  注文キュー(560x360) / 箱の真上のカメラ「実際」(640x360) ║ 箱の配置図「計画」(720x360)。実際と計画は同じ向き・縮尺
  下    ガントチャート(ArmA / ArmB / 共有ゾーン / コンベヤ、1920x180)
時刻はすべて sim 時刻。動画の再生速度(speed)は呼び出し側で決める。
"""
import math

from PIL import Image, ImageDraw, ImageFont

from pack_cell import layout as L

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
W, H = 1920, 1080
VID_W, VID_H = 1280, 720
BG = (24, 27, 33)
PANEL = (34, 38, 46)
FG = (230, 232, 236)
SUB = (150, 156, 168)
ACCENT = (255, 196, 61)
KIND_COLOR = {"box": (122, 162, 247), "lid": (122, 162, 247), "ship": (122, 162, 247), "fetch": (158, 206, 106),
              "place": (247, 118, 142), "remove": (224, 175, 104), "park": (90, 96, 110), "cross": (130, 136, 150),
              "feed": (122, 162, 247), "seal": (187, 154, 247), "out": (115, 218, 202)}
KIND_JA = {"box": "空箱の搬入", "lid": "蓋", "ship": "出荷", "fetch": "取り出し", "place": "箱詰め", "remove": "仮置きへ出す",
           "park": "待機", "cross": "反対側の棚へ", "feed": "空箱の搬入", "seal": "封函", "out": "出荷"}
TEMP_COLOR = {"ambient": (196, 160, 110), "chilled": (110, 170, 220), "frozen": (170, 140, 230)}
ARM_JA = {"arm_a": "ArmA", "arm_b": "ArmB"}


_rr_orig = ImageDraw.ImageDraw.rounded_rectangle


def _rr_safe(self, xy, radius=0, *args, **kw):
    """角丸の半径を図形の大きさに収める(細い・小さい図形で PIL が描けずに落ちるのを防ぐ)。"""
    x0, y0, x1, y1 = xy
    x1, y1 = max(x0, x1), max(y0, y1)
    r = max(0, min(radius, int((x1 - x0) / 2) - 1, int((y1 - y0) / 2) - 1))
    if r <= 0:
        return self.rectangle([x0, y0, x1, y1], *args, **{k: v for k, v in kw.items() if k in ("fill", "outline", "width")})
    return _rr_orig(self, [x0, y0, x1, y1], r, *args, **kw)


ImageDraw.ImageDraw.rounded_rectangle = _rr_safe


def _font(size, bold=False):
    try:
        return ImageFont.truetype(FONT_B if bold else FONT, size)
    except OSError:
        return ImageFont.truetype(FONT, size)


F = {k: _font(v) for k, v in {"t": 11, "xxs": 13, "xs": 15, "s": 18, "m": 22, "l": 28}.items()}
FB = {k: _font(v, True) for k, v in {"xs": 15, "s": 18, "m": 22, "l": 30, "xl": 40}.items()}


class Story:
    """イベント列を、ある時刻の表示状態に変換する。"""

    def __init__(self, events, cat):
        self.ev = sorted(events, key=lambda e: e["t"])
        self.cat = cat
        self.t_start = next((e["t"] for e in self.ev if e["kind"] in ("line", "order_start")), self.ev[0]["t"])
        self.t_end = next((e["t"] for e in reversed(self.ev) if e["kind"] in ("shipped", "order_end")), self.ev[-1]["t"])
        self.n_orders = max([e.get("total", 1) for e in self.ev if e["kind"] == "order_queue"] or [1])
        self.box_no = {}                          # 箱 -> 何番目の注文の箱か(1始まり)
        for e in self.ev:
            if e["kind"] == "line" and e.get("step") == "ready" and e["box"] not in self.box_no:
                self.box_no[e["box"]] = len(self.box_no) + 1

    def name(self, unit_or_sku):
        sku = (unit_or_sku or "").split("#")[0].replace("RM:", "")
        return self.cat[sku].name if sku in self.cat else (unit_or_sku or "")

    def upto(self, t):
        return [e for e in self.ev if e["t"] <= t]

    def plan_at(self, t):
        plan = None
        for e in self.upto(t):
            if e["kind"] == "order_start":
                plan = e["plan"]
            elif e["kind"] == "replan_chosen" and e.get("applied"):
                plan = e["plan"]
        return plan

    def order_at(self, t):
        """今詰めている注文の番号(1始まり)と本文。"""
        k, text = 0, ""
        for e in self.upto(t):
            if e["kind"] == "order_queue":
                k, text = e["index"] + 1, e.get("text") or ""
        return k, text

    def done_units(self, t):
        done, removed = set(), set()
        for e in self.upto(t):
            if e["kind"] == "order_start":
                done, removed = set(), set()
            if e["kind"] == "end" and e.get("ok") and e["action"] == "place":
                done.add(e["unit"])
                removed.discard(e["unit"])
            if e["kind"] == "end" and e.get("ok") and e["action"] == "remove":
                u = e["unit"][3:]
                done.discard(u); removed.add(u)
        return done, removed

    def bars(self, t):
        """[(arm, kind, unit, t0, t1, ok)] t1 は進行中なら t。"""
        open_, out = {}, []
        for e in self.upto(t):
            if e["kind"] == "start":
                open_[e["arm"]] = e
            elif e["kind"] == "end" and e["arm"] in open_:
                s = open_.pop(e["arm"])
                out.append((e["arm"], s["action"], s["unit"], s["t"], e["t"], e.get("ok", True)))
        for arm, s in open_.items():
            out.append((arm, s["action"], s["unit"], s["t"], t, None))
        return out

    def line_bars(self, t):
        """コンベヤの工程 [(kind, 箱番号, t0, t1)]: 空箱の搬入(feed→ready)・封函(seal→sealed)・出荷(sealed→shipped)。"""
        open_, out = {}, []
        for e in self.upto(t):
            if e["kind"] != "line":
                continue
            st, box = e.get("step"), e.get("box")
            if st == "feed" and box:
                open_[("feed", box)] = e["t"]
            elif st == "ready":
                t0 = open_.pop(("feed", box), None)
                if t0 is not None:
                    out.append(("feed", box, t0, e["t"]))
            elif st == "seal":
                open_[("seal", box)] = e["t"]
            elif st == "sealed":
                out.append(("seal", box, open_.pop(("seal", box), e["t"]), e["t"]))
                open_[("out", box)] = e["t"]
            elif st == "shipped":
                out.append(("out", box, open_.pop(("out", box), e["t"]), e["t"]))
        for (kind, box), t0 in open_.items():
            out.append((kind, box, t0, t))
        return [(k, self.box_no.get(b, 0), a, b_) for k, b, a, b_ in out]

    # ---- チャット ----
    TYPE_CPS = 9.0              # オペレータの打鍵の速さ [文字/sim秒]
    ADD_LATENCY = 4.0           # 追加注文の解釈と再計画案の説明にかかる LLM の時間(記録に所要時間がないときの表示用)

    def _build_chat(self):
        """チャットの項目 [(出る時刻, 種類, 本文の行 [(文字, 色)])] と、入力欄の打鍵 [(開始, 送信, 本文)]・LLM の考え中 [(開始, 終了)]。
        種類: op(オペレータの吹き出し) / llm(LLM の吹き出し) / sys(中央の通知) / div(注文の区切り)"""
        items, typing, thinking = [], [], []
        self._order_sends = []                             # 注文ごとの(打ち始め, 送信)
        sends = {}                                         # 本文 -> 送信の時刻(operator_send)
        queued = {}                                        # 注文の本文 -> 箱詰めを始めた時刻と番号
        for e in self.ev:
            if e["kind"] == "order_queue" and e.get("text") not in queued:
                queued[e.get("text")] = (e["t"], e["index"] + 1)
        last_end = self.ev[0]["t"]
        pending_add = None
        for e in self.ev:
            k, t = e["kind"], e["t"]
            if k == "operator_send" and not e.get("reply"):      # 注文の送信(返信は下で吹き出しにする)
                sends.setdefault(e["text"], t)
            elif k == "end":
                last_end = t
            elif k == "order_start" and self.n_orders > 1:          # 区切りは計画ができて詰め始めた時刻に出す
                n_started = sum(1 for x in self.ev if x["kind"] == "order_start" and x["t"] <= t)
                items.append((t, "div", [(f"注文{n_started} の箱詰めを開始", SUB)]))
            elif k == "interpreted":
                if not e.get("text"):
                    continue
                if not e.get("rounds") and e.get("latency", 0) == 0:          # 追加分を次の箱に回した注文(LLM なし)
                    items.append((t, "sys", [(f"{e['text']}: " + self._items_ja(e.get("spec") or {}), SUB)]))
                    continue
                # 送信は「応答の時刻 - LLM の所要時間」より前(応答を記録から再生したときは sim 時刻の上で一瞬で返るため)
                send = min(sends.get(e["text"], t), t - e.get("latency", 0))
                typing.append((send - len(e["text"]) / self.TYPE_CPS - 0.6, send, e["text"]))
                self._order_sends.append((typing[-1][0], send))
                items.append((send, "op", [(e["text"], FG)]))
                thinking.append((send, t))
                spec = e.get("spec") or {}
                lines = [("品目: " + self._items_ja(spec), FG)]
                q = queued.get(e["text"])
                if q and q[0] > t + 1.0 and self.n_orders > 1:          # 前の注文を詰めている間に受け付けた
                    lines.insert(0, (f"注文{q[1]}として受け付けました(今の箱の次に詰めます)", SUB))
                for c in spec.get("constraints", []):
                    tag = "明示" if c["source"] == "explicit" else "暗黙"
                    lines.append((f"[{tag}] {self._con_ja(c)}", ACCENT if c["source"] == "implicit" else FG))
                if e.get("clarification"):
                    lines.append(("確認: " + e["clarification"], ACCENT))
                items.append((t, "llm", lines))
                items.append((t + 0.01, "sys", [(f"注文を解釈(LLM {e.get('latency', 0):.1f}s)→ 検証器 OK → 配置を計画", SUB)]))
            elif k == "substitute":
                items.append((t, "sys", [(f"欠品: {self.name(e['missing'])}(在庫切れ)", (247, 118, 142))]))
                if e.get("proposal"):
                    items.append((t + 0.01, "llm", [(f"{self.name(e['missing'])}が在庫切れです。代わりに{self.name(e['proposal'])}で"
                                                     "よろしいですか?", FG), (e.get("reason", ""), SUB)]))
            elif k == "operator_send" and e.get("reply"):     # 確認への返信(承認など)
                typing.append((t - len(e["text"]) / self.TYPE_CPS - 0.6, t, e["text"]))
                items.append((t, "op", [(e["text"], FG)]))
            elif k == "replan_override":
                items.append((t, "sys", [(f"オペレータの判断: 推奨({e['recommended']})ではなく {e['chosen']} を選択", ACCENT)]))
            elif k == "substitute_decided":
                msg = (f"代替を承認: {self.name(e['missing'])} → {self.name(e['sub'])}" if e.get("approved")
                       else f"代替せず {self.name(e['missing'])} を注文から除く")
                items.append((t, "sys", [(msg, (158, 206, 106) if e.get("approved") else (247, 118, 142))]))
            elif k == "box_chosen":
                o = e.get("outer", [])
                items.append((t, "sys", [(f"箱: {e.get('name', e['box'])}({'×'.join(str(v) for v in o)}cm)を選んで製函機へ", SUB)]))
            elif k == "grasp_failed":
                items.append((t, "sys", [(f"{ARM_JA[e['arm']]}: {self.name(e['unit'])}の吸着に失敗 → 再検出して再試行", (247, 118, 142))]))
            elif k == "interpreted_add":
                send = min(sends.get(e["text"], last_end), t - self.ADD_LATENCY)
                typing.append((send - len(e["text"]) / self.TYPE_CPS - 0.6, send, e["text"]))
                items.append((send, "op", [(e["text"], FG)]))
                pending_add = send
                items.append((t, "llm", [("追加: " + self._items_ja(e.get("delta") or {}), FG)]))
            elif k == "replan_options":
                if pending_add is not None:
                    thinking.append((pending_add, t))
                    pending_add = None
                fit = all(o["id"] == "as_is" for o in e["options"])
                items.append((t - 0.01, "sys", [("計画器: 今の箱にそのまま入る" if fit else
                                                 "計画器: 今の箱の空きには入らない → 案 " + " / ".join(o["id"] for o in e["options"]),
                                                 (247, 118, 142))]))
                rec = next((o for o in e["options"] if o["id"] == e["recommended"]), None)
                lines = [(f"案: " + " / ".join(f"{o['id']}({o['summary']})" for o in e["options"]), SUB)]
                lines.append((f"推奨は {e['recommended']} です。" + (rec["summary"] + "。" if rec else ""), ACCENT))
                lines.append((e.get("message", ""), FG))
                items.append((t, "llm", lines))
            elif k == "replan_chosen":
                if e.get("applied"):
                    items.append((t, "sys", [(f"再計画を適用({e['id']})", SUB)]))
                elif e.get("next_box"):
                    items.append((t, "sys", [("追加分は次の箱に詰める(コンベヤが次の空箱を運ぶ)", SUB)]))
            elif k == "judge":
                items.append((t, "sys", [(f"詰め終わり 判定(真値) {'OK' if e.get('ok') else 'NG'}: 全品が箱内・最大傾き "
                                          f"{e.get('max_tilt_deg', 0):.1f}°", (158, 206, 106))]))
            elif k == "shipped":
                j = e.get("judge") or {}
                items.append((t, "sys", [(f"出荷: 箱{self.box_no.get(e['box'], '')} 封函済み・中身 {'OK' if j.get('ok') else 'NG'}",
                                          (115, 218, 202))]))
        items.sort(key=lambda x: x[0])
        # 通知(sys)はライン状況の「最近の出来事」へ。チャットは吹き出しと注文の区切りだけ
        self._log = [(t0, lines) for t0, kind, lines in items if kind == "sys"]
        items = [it for it in items if it[1] != "sys"]
        self._chat, self._typing, self._thinking = items, typing, thinking

    def log_at(self, t, n=4):
        if not hasattr(self, "_chat"):
            self._build_chat()
        return [x for x in self._log if x[0] <= t][-n:]

    def arm_now(self, t):
        """各アームの今の行動 {arm: (kind, unit, 進み具合 0〜1)}(なければ None)。
        進み具合は、ログにある実際の終了時刻から求める(終わる瞬間にちょうど 1 になる)。"""
        if not hasattr(self, "_ends"):
            self._ends = {}                                # (arm, 開始時刻) -> 終了時刻
            open_ = {}
            for e in self.ev:
                if e["kind"] == "start":
                    open_[e["arm"]] = e["t"]
                elif e["kind"] == "end" and e["arm"] in open_:
                    self._ends[(e["arm"], open_.pop(e["arm"]))] = e["t"]
        out = {"arm_a": None, "arm_b": None}
        for arm, kind, unit, a0, a1, ok in self.bars(t):
            if ok is None:
                t1 = self._ends.get((arm, a0))
                frac = (t - a0) / max(0.1, t1 - a0) if t1 else 0.5
                out[arm] = (kind, unit, max(0.0, min(1.0, frac)))
        return out

    def boxes_now(self, t):
        """コンベヤ上の箱の状態 [(箱番号, 状態)]。"""
        st = {}
        names = {"erect": "製函 → 待機位置へ", "feed": "梱包位置へ搬入中", "ready": "梱包位置で箱詰め中", "seal": "封函中",
                 "sealed": "出荷中", "shipped": None}
        order = []
        for e in self.upto(t):
            if e["kind"] != "line":
                continue
            for b in (e.get("box"), e.get("ship")):
                if not b:
                    continue
                step = e["step"] if b == e.get("box") else ("feed_out" if e["step"] == "feed" else e["step"])
                lab = "封函機へ" if step == "feed_out" else names.get(step, st.get(b))
                if b not in order:
                    order.append(b)
                st[b] = lab
        return [(self.box_no.get(b, "次"), st[b]) for b in order if st.get(b)]

    def queue_at(self, t):
        """注文キューの表示 [{no, items, state}]。"""
        if not hasattr(self, "_chat"):
            self._build_chat()
        # 届いた注文(打ち始めた・始まった)だけ。まだ来ていない注文は出さない
        n = max([k + 1 for k, (t0, _) in enumerate(self._order_sends) if t0 <= t] +
                [e["index"] + 1 for e in self.ev if e["kind"] == "order_queue" and e["t"] <= t] + [0])
        interp = [e for e in self.ev if e["kind"] == "interpreted" and e.get("text") and (e.get("rounds") or e.get("latency"))]
        queues = [e for e in self.ev if e["kind"] == "order_start"]       # 詰め始めた時刻(計画ができた時刻)
        ends = [e for e in self.ev if e["kind"] == "order_end"]
        shipped = {self.box_no.get(e["box"]): e["t"] for e in self.ev if e["kind"] == "shipped"}
        rows = []
        for k in range(n):
            q = queues[k] if k < len(queues) and queues[k]["t"] <= t else None
            it = interp[k] if k < len(interp) and interp[k]["t"] <= t else None
            items = self._items_ja(it.get("spec") or {}) if it else ""
            if q:                                          # 始めてからは実際の計画(代替・追加を反映)
                plan = None
                for e in self.ev:
                    if e["t"] > t or (k + 1 < len(queues) and e["t"] >= queues[k + 1]["t"]):
                        break
                    if e["t"] >= q["t"] and (e["kind"] == "order_start" or (e["kind"] == "replan_chosen" and e.get("applied"))):
                        plan = e["plan"]
                if plan:
                    cnt = {}
                    for p in plan["placements"]:
                        cnt[p["sku"]] = cnt.get(p["sku"], 0) + 1
                    items = "、".join(f"{self.name(s)}" + (f"×{c}" if c > 1 else "") for s, c in cnt.items())
                end = ends[k] if k < len(ends) and ends[k]["t"] <= t else None
                if end is None:
                    state = "詰め中"
                elif shipped.get(k + 1, 1e18) <= t:
                    state = "出荷済み"
                else:
                    state = "封函・出荷中"
            else:
                if not hasattr(self, "_chat"):
                    self._build_chat()
                ts = self._order_sends[k] if k < len(self._order_sends) else None
                state = ("待ち(解釈済み)" if it else "LLM が解釈中" if ts and t >= ts[1] else
                         "入力中" if ts and t >= ts[0] else "受付待ち")
            boxes = [e for e in self.ev if e["kind"] == "box_chosen" and e["t"] <= t]
            bname = boxes[k]["name"] if k < len(boxes) else ""
            if q and plan and plan.get("box"):
                bname = {"S": "小", "M": "中", "L": "大"}.get(plan["box"], bname)
            rows.append({"no": k + 1, "items": items, "state": state, "box": bname})
        return rows

    def _items_ja(self, spec):
        return "、".join(f"{self.name(i['sku'])}×{i['qty']}" for i in spec.get("items", []))

    def chat(self, t):
        if not hasattr(self, "_chat"):
            self._build_chat()
        return [it for it in self._chat if it[0] <= t]

    def typing_at(self, t):
        """入力欄に今打っている文字列(なければ "")。"""
        if not hasattr(self, "_chat"):
            self._build_chat()
        for t0, t1, text in self._typing:
            if t0 <= t < t1:
                n = int(max(0.0, t - t0) * self.TYPE_CPS)
                return text[:n]
        return ""

    def thinking_at(self, t):
        if not hasattr(self, "_chat"):
            self._build_chat()
        return any(t0 <= t < t1 for t0, t1 in self._thinking)

    def t_first(self):
        """動画の始まり: 最初の注文を打ち始める時刻。"""
        if not hasattr(self, "_chat"):
            self._build_chat()
        return min([t0 for t0, _, _ in self._typing] + [self.t_start])

    def _con_ja(self, c):
        a = [self.name(x) if x not in ("top", "bottom") else {"top": "上", "bottom": "底"}[x] for x in c["args"]]
        t = c["type"]
        return {"before": lambda: f"{a[0]} → {a[1]} の順", "first": lambda: f"{a[0]} を最初に", "last": lambda: f"{a[0]} を最後に",
                "layer": lambda: f"{a[0]} を{a[1]}に", "on": lambda: f"{a[0]} を {a[1]} の上に",
                "not_on": lambda: f"{a[0]} を {a[1]} の上に載せない", "nothing_on": lambda: f"{a[0]} の上に載せない",
                "group": lambda: "・".join(a) + " をまとめる", "separate": lambda: f"{a[0]} と {a[1]} を離す"}.get(t, lambda: t)()

    def caption(self, t):
        cur = [b for b in self.bars(t) if b[5] is None]
        parts = [f"コンベヤ: 箱{n}の{KIND_JA[k]}" for k, n, a, b in self.line_bars(t) if b >= t - 1e-6 and n]
        for arm, kind, unit, *_ in sorted(cur):
            parts.append(f"{ARM_JA[arm]}: {self.name(unit) + ' の' if unit and kind in ('fetch', 'place', 'remove') else ''}{KIND_JA.get(kind, kind)}")
        return "   ".join(parts)


NO_HEAD = set("。、，．・）)」』！？ー…")    # 行頭に置かない文字(禁則)


def _wrap(draw, text, font, width):
    lines, line = [], ""
    for ch in text:
        if draw.textlength(line + ch, font=font) > width and line and ch not in NO_HEAD:
            lines.append(line); line = ch
        else:
            line += ch
    if line:
        lines.append(line)
    return lines


CHAT_BG = (28, 31, 38)
OP_BUB = (40, 96, 160)
LLM_BUB = (52, 57, 68)


GANTT_H = 180                           # 下段のガント
LAYOUT = {"mid_h": 250}                 # 中段(ライン状況・実際・計画)の高さ。上段は残り
TOP_H, MID_H = H - GANTT_H - 250, 250
LEFT_W = 1200                           # 実際の動き(左)の幅。右の 720 が会話と計画
PLAN_PPM = 700                          # 「実際」と「計画」の縮尺 [px/m](中段の高さに合わせて draw_frame で決める)
RIGHT_BG = (30, 34, 44)                 # 会話と計画の領域の背景(実際の動きと区別する)


def _fit(img, w, h, top=0.5):
    """縦横比を保って w x h に切り抜く(top: 縦方向に切り抜く位置 0=上 1=下)。"""
    iw, ih = img.size
    if iw / ih > w / h:
        cw = int(ih * w / h)
        img = img.crop(((iw - cw) // 2, 0, (iw - cw) // 2 + cw, ih))
    else:
        ch = int(iw * h / w)
        y0 = int((ih - ch) * top)
        img = img.crop((0, y0, iw, y0 + ch))
    return img.resize((w, h))


def _box_cam(img, w, h):
    """箱の真上のカメラを、計画図と同じ縮尺(PLAN_PPM)に切り抜く。"""
    _, _, cz, _, _, _ = (float(v) for v in L.CAMERAS["cam_box"][0].split())
    fov = L.CAMERAS["cam_box"][3]
    rim = L.TABLE_TOP + L.BOX_OUTER[2]
    src_ppm = img.size[0] / (2 * (cz - rim) * math.tan(fov / 2))
    cw = int(w * src_ppm / PLAN_PPM)
    ch = int(h * src_ppm / PLAN_PPM)
    iw, ih = img.size
    return img.crop(((iw - cw) // 2, (ih - ch) // 2, (iw + cw) // 2, (ih + ch) // 2)).resize((w, h))


def _label(d, x, y, text, bg=(0, 0, 0)):
    """領域の見出し(角の丸い札。領域の端から少し離して置く)。"""
    tw = d.textlength(text, font=FB["xs"])
    d.rounded_rectangle([x + 10, y + 8, x + 10 + tw + 20, y + 32], radius=12, fill=bg)
    d.text((x + 20, y + 20), text, font=FB["xs"], fill=FG, anchor="lm")


def draw_frame(story, t, video_img, title, speed_label, box_img=None):
    global TOP_H, MID_H
    MID_H = LAYOUT["mid_h"]
    TOP_H = H - GANTT_H - MID_H
    global PLAN_PPM
    PLAN_PPM = min(700, (MID_H - 76) / L.BOX_INNER[1])
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    # 実際の動き: 俯瞰
    if video_img is not None:
        vi = video_img
        if LAYOUT.get("over_crop"):                      # 見本用: 録画の一部を切り抜いて拡大(撮り直すときはカメラの画角で合わせる)
            cx0, cy0, cx1 = LAYOUT["over_crop"]
            iw, ih = vi.size
            cw = int(iw * (cx1 - cx0)); chh = int(cw * TOP_H / LEFT_W)
            vi = vi.crop((int(iw * cx0), int(ih * cy0), int(iw * cx0) + cw, int(ih * cy0) + chh))
        im.paste(_fit(vi, LEFT_W, TOP_H, top=0.45), (0, 0))
    ov = Image.new("RGBA", (LEFT_W, 40), (0, 0, 0, 170))
    im.paste(ov, (0, 0), ov)
    d.text((16, 20), title, font=FB["m"], fill=FG, anchor="lm")
    d.text((LEFT_W - 16, 20), speed_label, font=FB["m"], fill=ACCENT, anchor="rm")
    # 会話と計画: チャット
    chat = Image.new("RGB", (W - LEFT_W, TOP_H), RIGHT_BG)
    _draw_chat(ImageDraw.Draw(chat), story, t, 0, 0, W - LEFT_W, TOP_H, bg=RIGHT_BG)
    im.paste(chat, (LEFT_W, 0))
    # 中段: 注文キュー / 実際 ║ 計画
    _draw_status(d, story, t, 0, TOP_H, 560, MID_H)
    if box_img is not None:
        im.paste(_box_cam(box_img, 640, MID_H), (560, TOP_H))
    _label(d, 560, TOP_H, "実際")
    _draw_plan(d, story, t, LEFT_W, TOP_H, W - LEFT_W, MID_H)
    # 領域の間のすき間(背景色の溝)
    for r in ([0, TOP_H - 3, W, TOP_H + 3], [557, TOP_H, 563, TOP_H + MID_H], [LEFT_W - 3, 0, LEFT_W + 3, TOP_H + MID_H]):
        d.rectangle(r, fill=BG)
    _draw_gantt(d, story, t, 0, TOP_H + MID_H, W, H - TOP_H - MID_H)
    return im


ARM_COL = {"arm_a": (122, 162, 247), "arm_b": (247, 118, 142)}
STATE_COL = {"出荷済み": (115, 218, 202), "封函・出荷中": (187, 154, 247), "詰め中": ACCENT, "LLM が解釈中": (125, 207, 255),
             "入力中": SUB, "待ち(解釈済み)": (125, 207, 255), "受付待ち": SUB}


def _draw_status(d, story, t, x, y, w, h):
    """ライン状況: 注文 / 各アームの今の行動 / コンベヤ上の箱 / 最近の出来事(通知)。"""
    d.rectangle([x, y, x + w, y + h], fill=PANEL)
    d.text((x + 18, y + 20), "ライン状況", font=FB["xs"], fill=FG, anchor="lm")
    rows = story.queue_at(t)
    info = f"経過 {max(0.0, t - story.t_start):.0f}s ・ 出荷 {sum(r['state'] == '出荷済み' for r in rows)}箱"
    d.text((x + w - 18, y + 20), info, font=F["xs"], fill=SUB, anchor="rm")
    yy = y + 40
    col1 = x + 86

    def chip(cx, cy, text, col):
        tw = d.textlength(text, font=F["xxs"])
        d.rounded_rectangle([cx, cy + 1, cx + tw + 12, cy + 19], radius=9, fill=tuple(int(c * 0.25) for c in col), outline=col)
        d.text((cx + 6, cy + 10), text, font=F["xxs"], fill=col, anchor="lm")
        return tw + 12
    # 注文
    d.text((x + 18, yy + 10), "注文", font=FB["xs"], fill=SUB, anchor="lm")
    for r in rows:
        c = STATE_COL.get(r["state"], FG)
        d.text((col1, yy + 10), f"#{r['no']}", font=FB["xs"], fill=FG, anchor="lm")
        if r.get("box"):                                   # 箱のサイズ(小・中・大)
            d.rounded_rectangle([col1 + 152, yy + 1, col1 + 172, yy + 19], radius=4, outline=(150, 156, 168))
            d.text((col1 + 162, yy + 10), r["box"], font=F["xxs"], fill=FG, anchor="mm")
        cw = chip(col1 + 30, yy, r["state"], c)
        items = r["items"] or "—"
        maxw = w - (col1 + 30 + 150) + x - 6
        while d.textlength(items, font=F["xs"]) > maxw and len(items) > 2:
            items = items[:-2] + "…"
        d.text((col1 + 30 + 150, yy + 10), items, font=F["xs"], fill=FG if r["state"] != "出荷済み" else SUB, anchor="lm")
        yy += 24
    yy += 6
    d.line([x + 18, yy, x + w - 18, yy], fill=(60, 64, 76))
    yy += 6
    # アーム
    now = story.arm_now(t)
    for arm in ("arm_a", "arm_b"):
        d.text((x + 18, yy + 10), ARM_JA[arm], font=FB["xs"], fill=ARM_COL[arm], anchor="lm")
        cur = now[arm]
        if cur is None:
            d.text((col1, yy + 10), "待機", font=F["xs"], fill=SUB, anchor="lm")
        else:
            kind, unit, frac = cur
            txt = (story.name(unit) + " を" if unit and kind in ("fetch", "place", "remove") else "") + KIND_JA.get(kind, kind)
            d.rectangle([col1, yy + 4, col1 + 12, yy + 16], fill=KIND_COLOR.get(kind, FG))
            d.text((col1 + 18, yy + 10), txt, font=F["xs"], fill=FG, anchor="lm")
            bx = x + w - 130
            d.rounded_rectangle([bx, yy + 6, bx + 116, yy + 14], radius=4, fill=(52, 57, 68))
            d.rounded_rectangle([bx, yy + 6, bx + max(8, 116 * frac), yy + 14], radius=4, fill=KIND_COLOR.get(kind, FG))
        yy += 23
    # コンベヤ
    d.text((x + 18, yy + 10), "コンベヤ", font=FB["xs"], fill=SUB, anchor="lm")
    boxes = story.boxes_now(t)
    d.text((col1, yy + 10), "   ".join((f"箱{n}" if n != "次" else "次の空箱") + f": {s}" for n, s in boxes[-3:]) or "—",
           font=F["xs"], fill=FG, anchor="lm")
    yy += 26
    d.line([x + 18, yy, x + w - 18, yy], fill=(60, 64, 76))
    yy += 4
    # 最近の出来事
    d.text((x + 18, yy + 10), "出来事", font=FB["xs"], fill=SUB, anchor="lm")
    n_fit = max(1, int((y + h - yy - 4) // 20))
    for t0, lines in story.log_at(t, n_fit):
        text, c = lines[0]
        while d.textlength(text, font=F["xs"]) > w - (col1 - x) - 12 and len(text) > 2:
            text = text[:-2] + "…"
        a = min(1.0, (t - t0) / 0.8)
        d.text((col1, yy + 10), text, font=F["xs"], fill=_mix(PANEL, c, a), anchor="lm")
        yy += 20


def _mix(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def _fit_text(d, text, maxw, fonts=("xs", "xxs", "t")):
    """幅 maxw に収まる最大のフォントを選び、それでも入らなければ末尾を「…」で省略する。"""
    for f in fonts:
        if d.textlength(text, font=F[f]) <= maxw:
            return text, F[f]
    f = F[fonts[-1]]
    while len(text) > 1 and d.textlength(text + "…", font=f) > maxw:
        text = text[:-1]
    return (text + "…" if len(text) >= 1 else ""), f


def _draw_plan(d, story, t, x, y, w, h):
    """計画: 箱を真上から見た図。実際(箱の真上のカメラ)と同じ向き(上流が左・ArmA 側が上)・同じ縮尺。左: 底 / 右: 上に重ねる品。
    詰めた品は薄い塗り、まだの品は枠だけ。品名は枠の中央に、収まる大きさで(入らなければ省略)。"""
    d.rectangle([x, y, x + w, y + h], fill=RIGHT_BG)
    plan = story.plan_at(t)
    k, _ = story.order_at(t)
    _label(d, x, y, "計画", bg=(0, 0, 0))
    if plan is None:
        return
    lg = "塗り = 詰めた品"
    d.text((x + w - 18, y + 20), lg, font=F["xxs"], fill=SUB, anchor="rm")
    done, removed = story.done_units(t)
    bw, bh = plan.get("box_inner", L.BOX_INNER)[:2]
    sc = PLAN_PPM
    gap = (w - 2 * bw * sc) / 3
    for kk, title in enumerate(("底", "上に重ねる品")):
        lx = x + gap + kk * (bw * sc + gap)
        oy = y + 60
        d.text((lx, oy - 6), title, font=F["xxs"], fill=SUB, anchor="lb")
        d.rectangle([lx, oy, lx + bw * sc, oy + bh * sc], outline=(84, 90, 104), width=1)
        ps = [p for p in plan["placements"] if (p["bottom"] < 1e-3) == (kk == 0)]
        for p in sorted(ps, key=lambda p: p["bottom"]):
            sx, sy = p["size"][0], p["size"][1]
            rx0 = lx + (p["x"] - sx / 2 + bw / 2) * sc
            ry0 = oy + (bh / 2 - p["y"] - sy / 2) * sc      # +y(ArmA 側)を上に
            rect = [rx0 + 1, ry0 + 1, rx0 + sx * sc - 1, ry0 + sy * sc - 1]
            sku = story.cat[p["sku"]]
            col = TEMP_COLOR[sku.temp]
            if p["unit"] in done:
                d.rounded_rectangle(rect, radius=4, fill=_mix(RIGHT_BG, col, 0.32), outline=_mix(RIGHT_BG, col, 0.7), width=1)
                tc = _mix(RIGHT_BG, (235, 237, 242), 0.85)
            else:
                d.rounded_rectangle(rect, radius=4, outline=col, width=2)
                tc = col
            rw, rh_ = rect[2] - rect[0], rect[3] - rect[1]
            name, f = _fit_text(d, sku.name, rw - 6)
            sub = f"{100 * p['bottom']:.0f}cm上" if p["bottom"] > 1e-3 else ""
            two = sub and rh_ >= 34
            fh = f.size + 2
            mx, my = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
            if rh_ >= fh:
                d.text((mx, my - (fh / 2 if two else 0)), name, font=f, fill=tc, anchor="mm")
            if two:
                st, sf = _fit_text(d, sub, rw - 6, ("xxs", "t"))
                d.text((mx, my + fh / 2 + 1), st, font=sf, fill=_mix(RIGHT_BG, tc, 0.75), anchor="mm")
    if removed:
        d.text((x + gap, y + h - 22), "仮置き中: " + "、".join(story.name(u) for u in removed), font=F["xxs"], fill=ACCENT)


def _draw_chat(d, story, t, x, y, w, h, bg=CHAT_BG):
    """チャット欄: 吹き出し(オペレータは右・青、LLM は左・灰)、通知は中央の小さな文字。下に入力欄。"""
    d.rectangle([x, y, x + w, y + h], fill=bg)
    in_h = 50
    bottom = y + h - in_h - 6
    maxw = int(w * 0.78)
    blocks = []                                            # (高さ, 描画関数)
    for t0, kind, lines in story.chat(t):
        el = t - t0
        a = 1 - (1 - min(1.0, el / FADE_S)) ** 3                     # ふわっと出る(ease-out)
        n = int(el * STREAM_CPS) if kind == "llm" else None           # LLM の文は少しずつ出す
        blocks.append(_chat_block(d, kind, lines, x, w, maxw, a=a, nchars=n, bg=bg))
    if story.thinking_at(t):
        blocks.append(_chat_block(d, "think", [("", SUB)], x, w, maxw, bg=bg, t=t))
    yy = bottom
    for hgt, draw in reversed(blocks):              # 新しいものから下に積む。上にはみ出した分は見出しの帯で隠す
        yy -= hgt
        draw(yy)
        if yy < y + 34:
            break
    d.rectangle([x, y, x + w, y + 38], fill=bg)
    d.text((x + 22, y + 20), "会話(オペレータ ⇄ LLM)", font=FB["xs"], fill=FG, anchor="lm")
    # 入力欄(右に送信ボタン)。文字は欄の縦の中心
    iy = y + h - in_h
    box = [x + 18, iy + 4, x + w - 100, iy + in_h - 10]
    mid = (box[1] + box[3]) / 2
    d.rounded_rectangle(box, radius=10, fill=(44, 48, 58), outline=(70, 76, 90))
    typed = story.typing_at(t)
    if typed:
        caret = "▌" if int(t * 2) % 2 == 0 else ""
        txt = typed
        while d.textlength(txt + "▌", font=F["s"]) > box[2] - box[0] - 28:
            txt = txt[1:]
        d.text((box[0] + 14, mid), txt + caret, font=F["s"], fill=FG, anchor="lm")
    else:
        d.text((box[0] + 14, mid), "注文を入力…", font=F["s"], fill=(100, 106, 120), anchor="lm")
    btn = [x + w - 88, box[1], x + w - 20, box[3]]
    d.rounded_rectangle(btn, radius=10, fill=OP_BUB if typed else (60, 66, 80))
    d.text(((btn[0] + btn[2]) / 2, mid), "送信", font=FB["s"], fill=FG, anchor="mm")


FADE_S = 0.9          # 吹き出しが出きるまで [sim 秒]
STREAM_CPS = 90.0     # LLM の文を出す速さ [文字/sim 秒]


def _chat_block(d, kind, lines, x, w, maxw, a=1.0, nchars=None, bg=CHAT_BG, t=0.0):
    pad, lh = 9, 23
    if kind == "div":
        text, col = lines[0]
        hgt = 30

        def draw(yy):
            tw = d.textlength(text, font=F["xs"])
            c = _mix(bg, (70, 76, 90), a)
            d.line([x + 22, yy + 14, x + (w - tw) / 2 - 8, yy + 14], fill=c)
            d.line([x + (w + tw) / 2 + 8, yy + 14, x + w - 22, yy + 14], fill=c)
            d.text((x + w / 2, yy + 14), text, font=F["xs"], fill=_mix(bg, col, a), anchor="mm")
        return hgt, draw
    if kind == "think":                                   # 考え中: 3つの点が順に明るくなる
        hgt = 20 + 40

        def draw(yy):
            d.text((x + 24, yy), "LLM", font=F["xs"], fill=SUB)
            d.rounded_rectangle([x + 22, yy + 20, x + 84, yy + 54], radius=17, fill=LLM_BUB)
            for i in range(3):
                k = 0.5 + 0.5 * math.sin(t * 6 - i * 0.9)
                c = _mix((90, 96, 110), (220, 224, 232), k)
                d.ellipse([x + 32 + i * 16, yy + 32, x + 42 + i * 16, yy + 42], fill=c)
        return hgt, draw
    wrapped = []
    for text, col in lines:
        wrapped += [(ln, col) for ln in _wrap(d, text, F["s"], maxw - 2 * pad)]
    bw = max(d.textlength(ln, font=F["s"]) for ln, _ in wrapped) + 2 * pad
    hgt = len(wrapped) * lh + 2 * pad + 26
    rise = (1 - a) * 18                                   # 下から少し浮き上がる

    def draw(yy):
        yy = yy + rise
        who = "オペレータ" if kind == "op" else "LLM"
        fill = _mix(bg, OP_BUB if kind == "op" else LLM_BUB, a)
        if kind == "op":
            bx1 = x + w - 22
            bx0 = bx1 - bw
            d.text((bx1 - d.textlength(who, font=F["xs"]), yy), who, font=F["xs"], fill=_mix(bg, SUB, a))
        else:
            bx0 = x + 22
            bx1 = bx0 + bw
            d.text((bx0 + 2, yy), who, font=F["xs"], fill=_mix(bg, SUB, a))
        by0 = yy + 20
        d.rounded_rectangle([bx0, by0, bx1, by0 + len(wrapped) * lh + 2 * pad], radius=14, fill=fill)
        left = nchars if nchars is not None else 10 ** 9
        for i, (ln, col) in enumerate(wrapped):
            if left <= 0:
                break
            d.text((bx0 + pad, by0 + pad + i * lh + lh / 2), ln[:left], font=F["s"], fill=_mix(fill, col, a), anchor="lm")
            left -= len(ln)
    return hgt, draw


def _draw_gantt(d, story, t, x, y, w, h):
    """ガント(全幅・低め): ArmA / ArmB / 共有ゾーン / コンベヤ。全体の時間を横幅に収める。上の段との間を空ける。"""
    d.rectangle([x, y, x + w, y + h], fill=BG)
    t0 = story.t_start
    span = max(60.0, story.t_end - t0 + 5)
    left, right = x + 104, x + w - 24
    sx = (right - left) / span
    rows = ["ArmA", "ArmB", "共有ゾーン", "コンベヤ"]
    rh, gap = 22, 8
    top = y + 48
    lx = left
    for k in ("fetch", "place", "cross", "remove", "feed", "seal", "out"):
        d.rounded_rectangle([lx, y + 21, lx + 12, y + 31], radius=2, fill=KIND_COLOR[k])
        d.text((lx + 17, y + 26), KIND_JA[k], font=F["xxs"], fill=SUB, anchor="lm")
        lx += 30 + d.textlength(KIND_JA[k], font=F["xxs"])
    for i, label in enumerate(rows):
        yy = top + i * (rh + gap)
        d.text((x + 24, yy + rh / 2), label, font=F["xxs"], fill=FG, anchor="lm")
        d.rounded_rectangle([left, yy, right, yy + rh], radius=4, fill=(30, 33, 40))

    def bar(x0, x1, yy, col, text=None):
        x1 = max(x0 + 3, x1)
        if x1 - x0 >= 10:
            d.rounded_rectangle([x0, yy + 2, x1, yy + rh - 2], radius=3, fill=col)
        else:                                              # 細いバーは角を丸めない(丸めると描けない)
            d.rectangle([x0, yy + 2, x1, yy + rh - 2], fill=col)
        if text and x1 - x0 > 22:
            tx, f = _fit_text(d, text, x1 - x0 - 6, ("xxs", "t"))
            if tx and not tx.startswith("…"):
                d.text((x0 + 3, yy + rh / 2), tx, font=f, fill=(20, 20, 24), anchor="lm")
    for arm, kind, unit, a0, a1, ok in story.bars(t):
        i = 0 if arm == "arm_a" else 1
        bx0, bx1 = left + (a0 - t0) * sx, left + (a1 - t0) * sx
        col = KIND_COLOR.get(kind, (120, 120, 120))
        if ok is False:
            col = (120, 60, 70)
        lab = story.name(unit) if unit and kind in ("fetch", "place", "remove") else None
        bar(bx0, bx1, top + i * (rh + gap), col, lab)
        if kind in ("place", "box", "lid", "ship", "remove", "cross"):
            bar(bx0, bx1, top + 2 * (rh + gap), (200, 200, 210) if arm == "arm_a" else (140, 140, 160))
    for kind, n, a0, a1 in story.line_bars(t):
        bar(left + (a0 - t0) * sx, left + (a1 - t0) * sx, top + 3 * (rh + gap), KIND_COLOR[kind], f"箱{n}" if n else None)
    for e in story.upto(t):                                # 注文の区切り
        if e["kind"] == "order_start":
            ox = left + (e["t"] - t0) * sx
            d.line([ox, top - 4, ox, top + 4 * (rh + gap) - gap], fill=(90, 96, 110), width=1)
    cx = left + (max(t, t0) - t0) * sx                     # 現在時刻
    d.line([cx, top - 6, cx, top + 4 * (rh + gap) - gap + 2], fill=ACCENT, width=2)
