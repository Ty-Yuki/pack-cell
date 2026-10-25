"""注文(OrderSpec)と制約。LLM の出力(JSON)を受け取り、カタログに照らして検証する。

制約の語彙(type と args):
  before(a, b)      a を b より先に箱へ入れる
  first(a)/last(a)  最初/最後に入れる
  layer(a, bottom)  箱の底に置く   layer(a, top)  上に何も載せない位置に置く
  on(a, b)          a を b の真上に載せる
  not_on(a, b)      a を b の真上に載せない
  nothing_on(a)     a の上に何も載せない
  group([a, ...])   互いに隣接させる(同じ段の辺の共有か、真上・真下)
  separate(a, b)    隣接させない
引数は SKU id。数量が複数なら、その SKU の全個体に適用する。
source: explicit(ユーザーが言った) | implicit(LLM が常識で補った)。hard の既定は explicit=True / implicit=False。
"""
from dataclasses import dataclass, field

TYPES = {"before": 2, "first": 1, "last": 1, "layer": 2, "on": 2, "not_on": 2, "nothing_on": 1, "group": -1, "separate": 2}

# llama.cpp の response_format(json_schema) にそのまま渡す
ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"sku": {"type": "string"}, "qty": {"type": "integer", "minimum": 1, "maximum": 9}},
            "required": ["sku", "qty"]}},
        "constraints": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(TYPES)},
                "args": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "enum": ["explicit", "implicit"]},
                "evidence": {"type": "string"}},
            "required": ["type", "args", "source", "evidence"]}},
        "unknown_items": {"type": "array", "items": {"type": "string"}},
        "clarification": {"type": ["string", "null"]},
    },
    "required": ["items", "constraints", "unknown_items", "clarification"],
}


@dataclass(frozen=True)
class Constraint:
    type: str
    args: tuple
    source: str = "explicit"
    hard: bool = True
    evidence: str = ""

    def __str__(self):
        return f"{self.type}({', '.join(self.args)})"


@dataclass
class OrderSpec:
    items: dict = field(default_factory=dict)          # sku -> qty
    constraints: list = field(default_factory=list)
    unknown_items: list = field(default_factory=list)
    clarification: str = None

    def units(self):
        """個体のリスト: [(sku, 個体番号)]。"""
        return [(s, k) for s, q in self.items.items() for k in range(q)]

    def to_dict(self):
        return {"items": [{"sku": s, "qty": q} for s, q in self.items.items()],
                "constraints": [{"type": c.type, "args": list(c.args), "source": c.source, "hard": c.hard,
                                 "evidence": c.evidence} for c in self.constraints],
                "unknown_items": list(self.unknown_items), "clarification": self.clarification}


@dataclass
class Issue:
    """検証で見つかった問題。LLM の修正(repair)と UI 表示にそのまま使う。"""
    kind: str        # unknown_sku | bad_constraint | out_of_stock | capacity | infeasible | conflict
    message: str
    ref: str = ""

    def __str__(self):
        return f"[{self.kind}] {self.message}"


def from_json(d, cat, known=()):
    """LLM の出力を OrderSpec に。カタログ外の SKU や形式の誤りは Issue として返す(例外にしない)。
    known: 制約が参照してよい既存の SKU(追加注文で、元の注文の品を参照する場合)。"""
    spec, issues = OrderSpec(), []
    for it in d.get("items", []):
        sku, qty = str(it.get("sku", "")), int(it.get("qty", 1))
        if sku not in cat:
            issues.append(Issue("unknown_sku", f"カタログにない品目 '{sku}'", sku))
            continue
        spec.items[sku] = spec.items.get(sku, 0) + qty
    for c in d.get("constraints", []):
        t, args = c.get("type"), tuple(str(a) for a in c.get("args", []))
        if t not in TYPES:
            issues.append(Issue("bad_constraint", f"未知の制約 '{t}'"))
            continue
        n = TYPES[t]
        if t == "layer":
            if len(args) != 2 or args[1] not in ("bottom", "top"):
                issues.append(Issue("bad_constraint", f"layer の引数は (sku, bottom|top): {args}"))
                continue
            skus = args[:1]
        else:
            if (n > 0 and len(args) != n) or (n < 0 and len(args) < 2):
                issues.append(Issue("bad_constraint", f"{t} の引数の数が不正: {args}"))
                continue
            skus = args
        missing = [s for s in skus if s not in spec.items and s not in known]
        if missing:
            issues.append(Issue("bad_constraint", f"{t}{args} が注文にない品目 {missing} を参照している", t))
            continue
        src = c.get("source", "explicit")
        hard = bool(c.get("hard", src == "explicit"))
        spec.constraints.append(Constraint(t, args, src, hard, c.get("evidence", "")))
    spec.unknown_items = list(d.get("unknown_items", []))
    spec.clarification = d.get("clarification")
    return spec, issues
