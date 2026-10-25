"""E1 言語理解の評価: 正解付き30文で、LLM 単独と LLM+検証器(修正ループ)を比べる。

usage: ./scripts/docker_run.sh python3 eval/run_lang_eval.py [--out results/e1_lang.json]
LLM 単独: LLM の最初の出力だけを使う(確認するかどうかも LLM の判断)。
LLM+検証器: カタログ照合と計画器で検証し、問題を LLM に返して直させる(最大2回)。物理的に無理なら確認で止める。
"""
import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from pack_cell.core import catalog, order as O, planner as PL
from pack_cell.llm import prompts as PR
from pack_cell.llm.client import LlmClient
from pack_cell.llm.interpret import interpret

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="eval/instructions.jsonl")
ap.add_argument("--out", default="results/e1_lang.json")
a = ap.parse_args()

cat = catalog.load()
P = PL.Planner(cat)
llm = LlmClient()
data = [json.loads(l) for l in open(a.data) if l.strip()]


def norm(c):
    t, args = c[0], list(c[1])
    return (t, tuple(sorted(args)) if t in ("group", "separate") else tuple(args))


def score(d, spec, outcome, latency):
    got_items = dict(spec.items) if spec else {}
    exp = {norm(c) for c in d["constraints"]}
    got_exp = {norm((c.type, c.args)) for c in (spec.constraints if spec else []) if c.source == "explicit"}
    got_imp = {norm((c.type, c.args)) for c in (spec.constraints if spec else []) if c.source == "implicit"}
    imp_ok = {norm(c) for c in d.get("implicit_ok", [])}
    return {"id": d["id"], "cat": d["cat"], "items_ok": got_items == d["items"],
            "unknown_ok": set(d.get("unknown", [])) <= set(spec.unknown_items if spec else []),
            "exp_tp": len(exp & got_exp), "exp_fn": len(exp - got_exp), "exp_fp": len(got_exp - exp),
            "imp_n": len(got_imp), "imp_hit": len(got_imp & imp_ok), "outcome": outcome, "expect": d["expect"],
            "outcome_ok": outcome == d["expect"], "latency": round(latency, 2)}


rows = {"llm_only": [], "llm_validator": []}
for d in data:
    # LLM 単独
    t0 = time.time()
    out = llm.json(PR.parse_messages(d["text"], cat), O.ORDER_SCHEMA, "order")
    lat = llm.last_latency
    spec, issues = O.from_json(out, cat)
    rows["llm_only"].append(score(d, spec, "clarify" if out.get("clarification") else "ok", lat))
    # LLM + 検証器
    r = interpret(d["text"], cat, P, llm)
    rows["llm_validator"].append(score(d, r.spec, "ok" if r.ok else "clarify", r.latency) | {"rounds": len(r.rounds)})
    print(d["id"], rows["llm_only"][-1]["outcome"], "->", rows["llm_validator"][-1]["outcome"], "expect", d["expect"],
          "items", rows["llm_validator"][-1]["items_ok"], flush=True)


def summary(rs):
    n = len(rs)
    tp = sum(r["exp_tp"] for r in rs); fn = sum(r["exp_fn"] for r in rs); fp = sum(r["exp_fp"] for r in rs)
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    conf = [r for r in rs if r["expect"] == "clarify"]
    nonc = [r for r in rs if r["expect"] == "ok"]
    imp = [r for r in rs if r["cat"] == "implicit"]
    return {"n": n, "items_acc": sum(r["items_ok"] for r in nonc) / len(nonc),
            "explicit_precision": round(prec, 3), "explicit_recall": round(rec, 3),
            "explicit_f1": round(2 * prec * rec / (prec + rec), 3) if prec + rec else 0.0,
            "conflict_detect": sum(r["outcome"] == "clarify" for r in conf) / len(conf),
            "false_clarify": sum(r["outcome"] == "clarify" for r in nonc) / len(nonc),
            "implicit_per_order": round(sum(r["imp_n"] for r in imp) / len(imp), 2),
            "implicit_hit_rate": round(sum(r["imp_hit"] for r in imp) / max(1, sum(r["imp_n"] for r in imp)), 3),
            "latency_mean": round(sum(r["latency"] for r in rs) / n, 2)}


res = {k: {"summary": summary(v), "rows": v} for k, v in rows.items()}
pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
json.dump(res, open(a.out, "w"), ensure_ascii=False, indent=1)
for k, v in res.items():
    print(k, json.dumps(v["summary"], ensure_ascii=False))
