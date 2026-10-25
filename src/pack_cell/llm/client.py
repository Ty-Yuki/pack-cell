"""ローカル LLM(OpenAI 互換 API)のクライアント。JSON スキーマで出力を縛り、応答をキャッシュする。

LLM_MODE:
  record(既定)  キャッシュにあれば使い、なければ問い合わせて保存する
  live           常に問い合わせる(保存はする)
  replay         キャッシュのみ(動画の撮り直し・テスト用。無ければ例外)
"""
import hashlib, json, os, pathlib, threading, time

from openai import OpenAI

CACHE = pathlib.Path(os.environ.get("LLM_CACHE", pathlib.Path(__file__).resolve().parents[3] / "data" / "llm_cache.jsonl"))
_lock = threading.Lock()


class LlmError(RuntimeError):
    pass


class LlmClient:
    def __init__(self, base_url=None, model=None, mode=None, timeout=120):
        self.base_url = base_url or os.environ.get("LLM_BASE_URL", "http://127.0.0.1:30000/v1")
        self.mode = mode or os.environ.get("LLM_MODE", "record")
        self.timeout = timeout
        self._cli = None
        self.model = model or os.environ.get("LLM_MODEL")
        self.thinking = os.environ.get("LLM_THINKING", "0") == "1"
        self._cache = self._load()
        self.last_latency = 0.0
        self.sleep = time.sleep                         # 応答の待ち(セルは sim 時刻で待つ関数に差し替える)
        self.last_cached = False

    def _client(self):
        if self._cli is None:
            self._cli = OpenAI(base_url=self.base_url, api_key="local", timeout=self.timeout)
            if not self.model:
                self.model = self._cli.models.list().data[0].id
        return self._cli

    @staticmethod
    def _load():
        out = {}
        if CACHE.exists():
            for line in CACHE.read_text().splitlines():
                if line.strip():
                    d = json.loads(line)
                    out[d["key"]] = d
        return out

    def _key(self, messages, schema):
        raw = json.dumps({"m": messages, "s": schema, "think": self.thinking}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def json(self, messages, schema, name="out"):
        """messages を投げ、schema に従う JSON(dict)を返す。"""
        key = self._key(messages, schema)
        if self.mode != "live" and key in self._cache:
            self.last_cached, self.last_latency = True, self._cache[key].get("latency", 0.0)
            if os.environ.get("LLM_EMULATE_LATENCY") == "1":
                self.sleep(self.last_latency)          # 記録した応答を、実際にかかった時間だけ待って返す(録画で時系列を本物にする)
            return self._cache[key]["out"]
        if self.mode == "replay":
            raise LlmError(f"replay モードでキャッシュにない問い合わせ(key={key})")
        t0 = time.time()
        try:
            r = self._client().chat.completions.create(
                model=self.model, messages=messages, temperature=0.0, max_tokens=4096,
                response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
                extra_body={"chat_template_kwargs": {"enable_thinking": self.thinking}})
        except Exception as e:  # 接続エラー・タイムアウト
            raise LlmError(f"LLM に問い合わせできない: {e}") from e
        text = r.choices[0].message.content or ""
        try:
            out = json.loads(text)
        except json.JSONDecodeError as e:
            raise LlmError(f"LLM の出力が JSON でない: {text[:200]}") from e
        self.last_cached, self.last_latency = False, time.time() - t0
        rec = {"key": key, "out": out, "latency": round(self.last_latency, 2), "model": self.model,
               "prompt_tail": messages[-1]["content"][-200:]}
        with _lock:
            self._cache[key] = rec
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            with CACHE.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return out
