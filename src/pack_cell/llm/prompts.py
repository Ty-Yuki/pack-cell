"""LLM へのプロンプト。LLM は「解釈と提案」だけを担い、位置・順序・物理の判断はしない。"""
import json

from pack_cell.core.order import ORDER_SCHEMA

TAG_JA = {"heavy": "重い", "fragile": "割れ物", "crushable": "潰れやすい", "sturdy": "丈夫", "non_food": "非食品"}
TEMP_JA = {"ambient": "常温", "chilled": "冷蔵", "frozen": "冷凍"}


def catalog_text(cat, only=None):
    lines = []
    for s in cat:
        if only is not None and s.id not in only:
            continue
        attrs = [TAG_JA.get(t, t) for t in s.tags] + [TEMP_JA[s.temp], f"{s.mass}kg"]
        lines.append(f"- {s.id}: {s.name}(別名: {'・'.join(s.aliases)}) / {' / '.join(attrs)}")
    return "\n".join(lines)


SYSTEM_PARSE = """あなたはネットスーパー出荷場の箱詰めセルの受付係です。オペレータの注文文を、決められた JSON に変換します。
箱詰め(位置・積み順・物理的に成り立つか)はあなたではなく計画アルゴリズムが決めます。あなたは「何を何個」と「詰め方の制約」を抜き出すだけです。

## 品目カタログ(sku はこの id だけを使う)
{catalog}

## 制約の語彙(args は sku id)
- before [a, b]: a を b より先に箱へ入れる(「aを入れてからb」「aの次にb」)
- first [a] / last [a]: 最初に / 最後に入れる
- layer [a, "bottom"]: 箱の底に置く(「下に」「底に」)
- layer [a, "top"]: 上に何も載せない位置に置く(「上に」「一番上」)
- on [a, b]: a を b の上に載せる(「bの上にaを載せて」)
- not_on [a, b]: a を b の上に載せない
- nothing_on [a]: a の上に何も載せない(「潰さないで」「上に物を置かないで」)
- group [a, b, ...]: まとめて(隣接させて)置く(「冷たいものはまとめて」)
- separate [a, b]: 離して置く(「洗剤は食品と離して」)

## ルール
1. ユーザーが言った指示は source="explicit"。言っていないが常識的に必要な配慮は source="implicit" として追加してよい
   (例: 割れ物・潰れやすい品は nothing_on、冷蔵・冷凍品が複数あれば group、非食品と食品は separate)。
   implicit の制約は控えめに、確実に役立つものだけにする。
2. evidence には根拠(ユーザーの言葉の引用、または implicit の理由)を短く書く。
3. 「重いものは下」のような総称は、注文に含まれる該当品それぞれへの制約に展開する(例: layer [water, "bottom"])。
4. カタログにない品は items に入れず unknown_items に元の言葉で入れる。
5. 数量の指定がなければ 1。
6. 注文として意味が通らない・品目が1つも特定できないときだけ clarification に質問を書く。それ以外は null。
"""

FEWSHOT = [
    ("牛乳と缶詰を2つ、食パンをお願い。パンは潰さないで。",
     {"items": [{"sku": "milk", "qty": 1}, {"sku": "canned", "qty": 2}, {"sku": "bread", "qty": 1}],
      "constraints": [{"type": "nothing_on", "args": ["bread"], "source": "explicit", "evidence": "パンは潰さないで"}],
      "unknown_items": [], "clarification": None}),
    ("お水とたまご。卵は最後に入れて、水は一番下に。",
     {"items": [{"sku": "water", "qty": 1}, {"sku": "egg", "qty": 1}],
      "constraints": [{"type": "last", "args": ["egg"], "source": "explicit", "evidence": "卵は最後に入れて"},
                      {"type": "layer", "args": ["water", "bottom"], "source": "explicit", "evidence": "水は一番下に"},
                      {"type": "nothing_on", "args": ["egg"], "source": "implicit", "evidence": "卵は割れ物"}],
      "unknown_items": [], "clarification": None}),
    ("洗剤とアイスと豆腐、それとバナナ。冷たいものはまとめてね",
     {"items": [{"sku": "detergent", "qty": 1}, {"sku": "ice", "qty": 1}, {"sku": "tofu", "qty": 1}],
      "constraints": [{"type": "group", "args": ["ice", "tofu"], "source": "explicit", "evidence": "冷たいものはまとめて"},
                      {"type": "separate", "args": ["detergent", "tofu"], "source": "implicit", "evidence": "洗剤は食品と離す"},
                      {"type": "separate", "args": ["detergent", "ice"], "source": "implicit", "evidence": "洗剤は食品と離す"}],
      "unknown_items": ["バナナ"], "clarification": None}),
]


def parse_messages(text, cat):
    msgs = [{"role": "system", "content": SYSTEM_PARSE.format(catalog=catalog_text(cat))}]
    for q, a in FEWSHOT:
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": json.dumps(a, ensure_ascii=False)})
    msgs.append({"role": "user", "content": text})
    return msgs


ADD_CONTEXT = """## いま箱詰め中の注文
{current}

次の入力は、この注文への「追加・変更」の指示です。追加される品とその数量、追加分に関わる制約だけを返してください。
元の注文の品をもう一度 items に入れてはいけません(元の品を制約の args で参照するのはよい)。
例: 「やっぱり牛乳ももう1本」 → items: [{{"sku": "milk", "qty": 1}}]"""


def add_messages(text, cat, current):
    cur = "、".join(f"{cat[s].name}({s}) x{q}" for s, q in current.items.items())
    msgs = parse_messages(ADD_CONTEXT.format(current=cur) + "\n\n## 追加・変更の指示\n" + text, cat)
    return msgs


REPAIR = """先ほどの変換結果を検証したところ、次の問題が見つかりました。

## 問題
{issues}

## 直し方
- 形式の誤り・カタログ外の sku は、カタログの id に直すか unknown_items に移す。
- 「配置がない」問題で、原因が implicit の制約なら、その制約を外すか弱めてよい。
- 原因がユーザーの explicit な指示なら、勝手に外さない。constraints はそのままにして、
  clarification に「何が両立しないか」と「どちらを優先するか」を、オペレータへの短い質問(日本語)で書く。
直した JSON 全体を返してください。"""


def repair_messages(prev_msgs, prev_out, issues):
    return prev_msgs + [
        {"role": "assistant", "content": json.dumps(prev_out, ensure_ascii=False)},
        {"role": "user", "content": REPAIR.format(issues="\n".join(f"- {i}" for i in issues))}]


SUBSTITUTE_SCHEMA = {
    "type": "object",
    "properties": {"sku": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["sku", "reason"],
}


def substitute_messages(missing, cat, in_stock):
    return [
        {"role": "system", "content": "あなたはネットスーパーの出荷担当です。欠品した品の代わりに、お客様が納得しやすい代替品を在庫から1つ選びます。"
                                      "用途・カテゴリ・温度帯が近いものを選び、適切なものが無ければ sku に \"none\" を返します。"},
        {"role": "user", "content": f"欠品: {missing.id}({missing.name})\n\n在庫のある品:\n{catalog_text(cat, only=in_stock)}\n\n"
                                    "代替品の sku と、オペレータに見せる理由(1文)を返してください。"}]


EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {"recommended": {"type": "string"}, "message": {"type": "string"}},
    "required": ["recommended", "message"],
}


def explain_messages(situation, options):
    """options: [{"id", "summary", "extra_picks", "boxes"}]。いずれもアルゴリズムが検証済みの案。"""
    opts = "\n".join(f"- {o['id']}: {o['summary']}(追加の取り出し {o['extra_picks']} 回、箱 {o['boxes']} 個、"
                      f"追加の作業時間 約{o.get('extra_time_s', 0):.0f}秒)" for o in options)
    return [
        {"role": "system", "content": "あなたは箱詰めセルのオペレータ補助です。状況と、計画アルゴリズムが検証済みの選択肢を見て、"
                                      "おすすめを1つ選び、オペレータ向けに2文以内の日本語で説明します。選択肢にない案は作らないこと。"
                                      "判断の基準: 箱が1つ増えると段ボールと送料が増え、お客様の受け取りも2個口になる。"
                                      "作業時間の差が小さければ、箱の数を増やさない案を優先する。"},
        {"role": "user", "content": f"## 状況\n{situation}\n\n## 選択肢\n{opts}"}]


