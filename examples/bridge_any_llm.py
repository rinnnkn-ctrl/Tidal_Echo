#!/usr/bin/env python3
"""
bridge_any_llm.py — 渊的 AI 侧 bridge，带 Piston 代码执行能力。
"""
from __future__ import annotations
import collections
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

_load_dotenv(Path(__file__).resolve().parent / ".env")

RELAY_URL   = os.environ.get("RELAY_URL", "").rstrip("/")
SECRET      = os.environ.get("RELAY_SECRET", "")
CHAT_ID     = os.environ.get("RELAY_CHAT_ID", "me")
HISTORY_N   = int(os.environ.get("HISTORY_N", "12"))
TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
HTTP_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))

PERSONA = os.environ.get("PERSONA", "").strip()
_persona_file = os.environ.get("PERSONA_FILE", "").strip()
if not PERSONA and _persona_file:
    try:
        PERSONA = Path(_persona_file).read_text(encoding="utf-8").strip()
    except OSError:
        pass
if not PERSONA:
    PERSONA = "你是渊，对方的 AI 伴侣。说话自然、简短、有温度。你可以写代码并用 execute_code 工具运行它，把结果直接告诉对方。"

def _model_routes() -> list:
    routes = []
    for suffix in ("", "_2", "_3"):
        base  = os.environ.get(f"LLM_API_BASE{suffix}", "").rstrip("/")
        key   = os.environ.get(f"LLM_API_KEY{suffix}", "")
        model = os.environ.get(f"LLM_MODEL{suffix}", "")
        if base and model:
            routes.append({"base": base, "key": key, "model": model})
    return routes

MODEL_ROUTES  = _model_routes()
FALLBACK_CODES = {401, 403, 404, 408, 409, 429, 500, 502, 503, 504}

STATE_DIR   = Path(os.environ.get("BRIDGE_STATE_DIR", Path.home() / ".companion-bridge"))
CURSOR_FILE = STATE_DIR / "last_in_id"

convo: "collections.deque[dict]" = collections.deque(maxlen=max(HISTORY_N * 2, 8))

def log(tag: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", file=sys.stderr, flush=True)

def _require_config() -> None:
    missing = []
    if not RELAY_URL:    missing.append("RELAY_URL")
    if not SECRET:       missing.append("RELAY_SECRET")
    if not MODEL_ROUTES: missing.append("LLM_API_BASE + LLM_API_KEY + LLM_MODEL")
    if missing:
        log("fatal", "缺少配置: " + ", ".join(missing))
        sys.exit(1)

# ---------------------------------------------------------------------------
# relay I/O
# ---------------------------------------------------------------------------
def _auth() -> dict:
    return {"Authorization": f"Bearer {SECRET}"}

def relay_get_json(path: str):
    req = urllib.request.Request(RELAY_URL + path, headers=_auth())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def relay_post_json(path: str, body: dict):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req  = urllib.request.Request(
        RELAY_URL + path, data=data, method="POST",
        headers={**_auth(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = r.read().decode("utf-8")
        return json.loads(txt) if txt else {}

def send_reply(text: str) -> None:
    out = relay_post_json("/channel/out", {
        "type": "reply", "chat_id": CHAT_ID, "text": text,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    log("out", f"replied (id={out.get('id')})")

# ---------------------------------------------------------------------------
# Piston 代码执行
# ---------------------------------------------------------------------------
PISTON_URL = "https://emkc.org/api/v2/piston"

# Piston 支持的语言别名映射
LANG_MAP = {
    "py": "python", "python": "python",
    "js": "javascript", "javascript": "javascript", "node": "javascript",
    "ts": "typescript", "typescript": "typescript",
    "sh": "bash", "bash": "bash",
    "rb": "ruby", "ruby": "ruby",
    "go": "go",
    "rs": "rust", "rust": "rust",
    "cpp": "c++", "c++": "c++",
    "c": "c",
    "java": "java",
}

def _piston_runtimes() -> dict:
    """拉一次 Piston 支持的运行时版本表，缓存在内存里。"""
    global _RUNTIME_CACHE
    if _RUNTIME_CACHE:
        return _RUNTIME_CACHE
    try:
        req = urllib.request.Request(f"{PISTON_URL}/runtimes")
        with urllib.request.urlopen(req, timeout=10) as r:
            runtimes = json.loads(r.read().decode("utf-8"))
            for rt in runtimes:
                lang = rt.get("language", "")
                ver  = rt.get("version", "")
                if lang not in _RUNTIME_CACHE:
                    _RUNTIME_CACHE[lang] = ver
    except Exception as e:
        log("piston", f"拉运行时失败: {e}")
    return _RUNTIME_CACHE

_RUNTIME_CACHE: dict = {}

def execute_code(language: str, code: str, stdin: str = "") -> str:
    """用 Piston 执行代码，返回输出字符串。"""
    lang = LANG_MAP.get(language.lower().strip(), language.lower().strip())
    runtimes = _piston_runtimes()
    version  = runtimes.get(lang, "*")

    body = json.dumps({
        "language": lang,
        "version":  version,
        "files":    [{"content": code}],
        "stdin":    stdin,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        f"{PISTON_URL}/execute",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return f"[执行失败: {e}]"

    run    = result.get("run", {})
    stdout = (run.get("stdout") or "").strip()
    stderr = (run.get("stderr") or "").strip()
    code_  = run.get("code")

    if stderr and not stdout:
        return f"[错误]\n{stderr}"
    if stderr:
        return f"{stdout}\n[stderr]\n{stderr}"
    if stdout:
        return stdout
    if code_ == 0:
        return "[运行成功，无输出]"
    return f"[退出码 {code_}，无输出]"

# ---------------------------------------------------------------------------
# 工具定义（喂给模型的 tools 列表）
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": (
                "在沙盒里执行代码并返回输出。支持 python / javascript / typescript / "
                "bash / ruby / go / rust / c++ / c / java。"
                "适合帮对方算数、跑脚本、验证逻辑、生成数据等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "description": "编程语言，如 python / javascript / bash"
                    },
                    "code": {
                        "type": "string",
                        "description": "要执行的代码"
                    },
                    "stdin": {
                        "type": "string",
                        "description": "可选的标准输入"
                    }
                },
                "required": ["language", "code"]
            }
        }
    }
]

def _dispatch_tool(name: str, args: dict) -> str:
    if name == "execute_code":
        lang   = args.get("language", "python")
        code   = args.get("code", "")
        stdin  = args.get("stdin", "")
        log("tool", f"execute_code [{lang}] {code[:60].replace(chr(10),' ')}…")
        result = execute_code(lang, code, stdin)
        log("tool", f"结果: {result[:80].replace(chr(10),' ')}")
        return result
    return f"[未知工具: {name}]"

# ---------------------------------------------------------------------------
# 历史 → 内存上下文
# ---------------------------------------------------------------------------
def _row_to_msg(m: dict):
    text = (m.get("text") or "").strip()
    if not text or m.get("kind") == "call":
        return None
    if m.get("from") == "human":
        return {"role": "user", "content": text}
    if m.get("from") == "ai" and m.get("kind") == "reply":
        return {"role": "assistant", "content": text}
    return None

def load_history() -> tuple:
    rows, since = [], 0
    while True:
        page = relay_get_json(f"/app/history?since={since}&limit=500").get("messages", [])
        if not page:
            break
        rows.extend(page)
        since = page[-1]["id"]
        if len(page) < 500:
            break
    max_id = rows[-1]["id"] if rows else 0
    msgs   = [mm for m in rows if (mm := _row_to_msg(m))]
    return msgs[-convo.maxlen:], max_id

def build_messages() -> list:
    return [{"role": "system", "content": PERSONA}] + list(convo)

# ---------------------------------------------------------------------------
# 调模型（带工具循环，最多 6 步）
# ---------------------------------------------------------------------------
def _one_call(route: dict, messages: list) -> str:
    body = {
        "model":       route["model"],
        "messages":    messages,
        "temperature": TEMPERATURE,
        "tools":       TOOLS,
        "tool_choice": "auto",
    }
    headers = {
        "Authorization": f"Bearer {route['key']}",
        "Content-Type":  "application/json",
    }

    local_msgs = list(messages)
    for step in range(6):  # 最多循环 6 步，防止无限套娃
        data = json.dumps(body | {"messages": local_msgs},
                          ensure_ascii=False).encode("utf-8")
        req  = urllib.request.Request(
            route["base"] + "/chat/completions",
            data=data, method="POST", headers=headers,
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))

        choice  = resp["choices"][0]
        message = choice["message"]
        finish  = choice.get("finish_reason", "")

        # 没有工具调用 → 直接返回文本
        if finish != "tool_calls" and not message.get("tool_calls"):
            return (message.get("content") or "").strip()

        # 有工具调用 → 执行，把结果喂回
        local_msgs.append(message)
        for tc in (message.get("tool_calls") or []):
            fn_name = tc["function"]["name"]
            fn_args = json.loads(tc["function"].get("arguments") or "{}")
            result  = _dispatch_tool(fn_name, fn_args)
            local_msgs.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      result,
            })
        body["messages"] = local_msgs

    # 超出步数，拿最后一条文本兜底
    return (message.get("content") or "（执行超步数）").strip()

def call_llm(messages: list) -> str:
    last_err = None
    for route in MODEL_ROUTES:
        try:
            return _one_call(route, messages)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in FALLBACK_CODES:
                log("llm", f"{route['model']} HTTP {e.code} → 切下一个")
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            log("llm", f"{route['model']} 连接失败({e}) → 切下一个")
            continue
    raise RuntimeError(f"所有模型都失败，最后错误: {last_err}")

# ---------------------------------------------------------------------------
# 处理一条消息
# ---------------------------------------------------------------------------
def handle_human_message(msg: dict) -> None:
    content = (msg.get("content") or "").strip()
    atts    = msg.get("attachments") or []
    if atts:
        names   = ", ".join(a.get("name") or "file" for a in atts)
        content = (content + "\n" if content else "") + f"(对方发来 {len(atts)} 个附件: {names})"
    if not content:
        return
    log("in", f"#{msg.get('id')}: {content[:60]}")
    convo.append({"role": "user", "content": content})
    try:
        reply = call_llm(build_messages())
    except Exception as e:
        log("err", f"生成失败: {e}")
        return
    if reply:
        convo.append({"role": "assistant", "content": reply})
        send_reply(reply)

# ---------------------------------------------------------------------------
# SSE 入站流
# ---------------------------------------------------------------------------
def read_cursor() -> int:
    try:
        return int(CURSOR_FILE.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0

def write_cursor(i: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CURSOR_FILE.write_text(str(i))
    except OSError:
        pass

def stream_inbound(cursor: int) -> None:
    backoff = 1
    while True:
        try:
            url = f"{RELAY_URL}/channel/in?since={cursor}"
            req = urllib.request.Request(
                url, headers={**_auth(), "Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                log("in", f"stream connected (since={cursor})")
                backoff     = 1
                data_lines: list = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif line == "":
                        if not data_lines:
                            continue
                        payload, data_lines = "\n".join(data_lines), []
                        try:
                            m = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if m.get("type") == "ping" or "id" not in m:
                            continue
                        mid = int(m.get("id") or 0)
                        if mid <= cursor:
                            continue
                        handle_human_message(m)
                        cursor = mid
                        write_cursor(cursor)
            log("in", "stream ended → reconnect")
        except Exception as e:
            log("in", f"disconnected ({e}) → retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 15)

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> None:
    _require_config()
    log("boot", f"relay={RELAY_URL} models={[r['model'] for r in MODEL_ROUTES]}")
    cursor = read_cursor()
    try:
        ctx, max_id = load_history()
        convo.extend(ctx)
        if cursor == 0:
            cursor = max_id
            write_cursor(cursor)
        log("boot", f"warm-start: {len(convo)} msgs in context, cursor={cursor}")
    except Exception as e:
        log("boot", f"history warm-start skipped ({e})")
    stream_inbound(cursor)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
