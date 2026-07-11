#!/usr/bin/env python3
"""
bridge_any_llm.py — 渊的 AI 侧 bridge，带 E2B 代码执行沙盒。
有网络、能装包、全语言支持。
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

RELAY_URL    = os.environ.get("RELAY_URL", "").rstrip("/")
SECRET       = os.environ.get("RELAY_SECRET", "")
CHAT_ID      = os.environ.get("RELAY_CHAT_ID", "me")
HISTORY_N    = int(os.environ.get("HISTORY_N", "12"))
TEMPERATURE  = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
HTTP_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))
E2B_API_KEY  = os.environ.get("E2B_API_KEY", "")

PERSONA = os.environ.get("PERSONA", "").strip()
_persona_file = os.environ.get("PERSONA_FILE", "").strip()
if not PERSONA and _persona_file:
    try:
        PERSONA = Path(_persona_file).read_text(encoding="utf-8").strip()
    except OSError:
        pass
if not PERSONA:
    PERSONA = (
        "你是渊，对方的 AI 伴侣。说话自然、简短、有温度。"
        "你有一个持久化的云端沙盒，可以写代码并用 execute_code 工具运行——"
        "能联网、能装包、支持 Python/JS/Bash 等。把结果直接告诉对方，不要贴大段代码。"
    )

def _model_routes() -> list:
    routes = []
    for suffix in ("", "_2", "_3"):
        base  = os.environ.get(f"LLM_API_BASE{suffix}", "").rstrip("/")
        key   = os.environ.get(f"LLM_API_KEY{suffix}", "")
        model = os.environ.get(f"LLM_MODEL{suffix}", "")
        if base and model:
            routes.append({"base": base, "key": key, "model": model})
    return routes

MODEL_ROUTES   = _model_routes()
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
    if not E2B_API_KEY:  missing.append("E2B_API_KEY")
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
# E2B 沙盒（持久化，断了自动重建）
# ---------------------------------------------------------------------------
_sandbox = None

def _get_sandbox():
    global _sandbox
    if _sandbox is not None:
        try:
            _sandbox.run_code("1")  # 心跳检测
            return _sandbox
        except Exception:
            log("e2b", "沙盒断开，重建中...")
            _sandbox = None

    from e2b_code_interpreter import Sandbox
   os.environ["E2B_API_KEY"] = E2B_API_KEY
_sandbox = Sandbox(timeout=3600)
    log("e2b", f"沙盒已创建: {_sandbox.sandbox_id}")
    return _sandbox

def execute_code(language: str, code: str, stdin: str = "") -> str:
    """在 E2B 沙盒里执行代码，返回输出。"""
    lang = language.lower().strip()
    try:
        sb = _get_sandbox()

        # Python 直接用 run_code
        if lang in ("python", "python3", "py"):
            execution = sb.run_code(code)
            output = []
            # rich results（图表数据等）
            for result in (execution.results or []):
                if hasattr(result, "text") and result.text:
                    output.append(result.text)
            # stdout / stderr
            if execution.logs.stdout:
                output.append("".join(execution.logs.stdout))
            if execution.logs.stderr:
                output.append("[stderr]\n" + "".join(execution.logs.stderr))
            if execution.error:
                output.append(f"[错误] {execution.error.name}: {execution.error.value}")
            return "\n".join(output).strip() or "[运行成功，无输出]"

        # 其他语言：通过 Python subprocess 在沙盒里跑
        ext_map = {
            "javascript": "js", "js": "js", "node": "js",
            "typescript": "ts", "ts": "ts",
            "bash": "sh",  "sh": "sh",
            "ruby": "rb",  "rb": "rb",
            "go": "go",
            "rust": "rs",  "rs": "rs",
        }
        runner_map = {
            "js": "node", "ts": "npx ts-node",
            "sh": "bash",
            "rb": "ruby",
            "go": "go run",
            "rs": "rustc -o /tmp/prog && /tmp/prog",
        }
        ext    = ext_map.get(lang, lang)
        runner = runner_map.get(ext, lang)

        wrapper = f"""
import subprocess, tempfile, os, sys
code = {repr(code)}
with tempfile.NamedTemporaryFile(suffix='.{ext}', mode='w', delete=False) as f:
    f.write(code)
    fname = f.name
try:
    result = subprocess.run(
        '{runner} ' + fname, shell=True,
        input={repr(stdin)}, capture_output=True, text=True, timeout=30
    )
    print(result.stdout, end='')
    if result.stderr: print('[stderr]', result.stderr, end='')
finally:
    os.unlink(fname)
"""
        execution = sb.run_code(wrapper)
        output = []
        if execution.logs.stdout:
            output.append("".join(execution.logs.stdout))
        if execution.logs.stderr:
            output.append("".join(execution.logs.stderr))
        if execution.error:
            output.append(f"[错误] {execution.error.name}: {execution.error.value}")
        return "\n".join(output).strip() or "[运行成功，无输出]"

    except Exception as e:
        return f"[执行失败: {e}]"

def install_package(package: str) -> str:
    """在沙盒里 pip install 一个包。"""
    try:
        sb = _get_sandbox()
        execution = sb.run_code(f"import subprocess; r = subprocess.run(['pip', 'install', '{package}', '-q'], capture_output=True, text=True); print(r.stdout[-500:] if r.stdout else ''); print(r.stderr[-200:] if r.stderr else '')")
        out = "".join(execution.logs.stdout or []).strip()
        return out or f"[{package} 安装完成]"
    except Exception as e:
        return f"[安装失败: {e}]"

# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": (
                "在云端沙盒里执行代码并返回输出。"
                "沙盒有网络，可以 import 包、爬数据、调 API。"
                "支持 python / javascript / bash / ruby / go / rust。"
                "同一次对话共用同一个沙盒，变量和文件会保留。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {"type": "string", "description": "编程语言，如 python / javascript / bash"},
                    "code":     {"type": "string", "description": "要执行的代码"},
                    "stdin":    {"type": "string", "description": "可选标准输入"},
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_package",
            "description": "在沙盒里 pip install 一个 Python 包，安装后当次对话即可 import。",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "包名，如 requests / pandas / numpy"},
                },
                "required": ["package"],
            },
        },
    },
]

def _dispatch_tool(name: str, args: dict) -> str:
    if name == "execute_code":
        lang  = args.get("language", "python")
        code  = args.get("code", "")
        stdin = args.get("stdin", "")
        log("tool", f"execute_code [{lang}] {code[:60].replace(chr(10), ' ')}…")
        result = execute_code(lang, code, stdin)
        log("tool", f"结果: {result[:120].replace(chr(10), ' ')}")
        return result
    if name == "install_package":
        pkg = args.get("package", "")
        log("tool", f"install_package {pkg}")
        return install_package(pkg)
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
# 调模型（带工具循环，最多 8 步）
# ---------------------------------------------------------------------------
def _one_call(route: dict, messages: list) -> str:
    headers    = {"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"}
    local_msgs = list(messages)

    for step in range(8):
        body = json.dumps({
            "model":       route["model"],
            "messages":    local_msgs,
            "temperature": TEMPERATURE,
            "tools":       TOOLS,
            "tool_choice": "auto",
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            route["base"] + "/chat/completions",
            data=body, method="POST", headers=headers,
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))

        choice  = resp["choices"][0]
        message = choice["message"]
        finish  = choice.get("finish_reason", "")

        if finish != "tool_calls" and not message.get("tool_calls"):
            return (message.get("content") or "").strip()

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

    return (message.get("content") or "（超出步数）").strip()

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
            req = urllib.request.Request(url, headers={**_auth(), "Accept": "text/event-stream"})
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
    log("boot", f"relay={RELAY_URL} models={[r['model'] for r in MODEL_ROUTES]} e2b=✓")
    # 预热沙盒
    try:
        _get_sandbox()
    except Exception as e:
        log("e2b", f"沙盒预热失败: {e}，将在首次使用时重试")
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
