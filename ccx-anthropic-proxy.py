"""Loopback-only Messages adapter for ccx's OpenAI-format providers (stdlib)."""
import hashlib
import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue
import select
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import os
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("AI_SUITE_DIR") or str(Path(__file__).resolve().parents[1] / "ai-suite"))
from ai_suite.service import (AIService, CLI_TIMEOUT, COMMANDCODE_EXIT_MEANINGS, background_process_options,
                              commandcode_executable)

PORT = 10532
UPSTREAMS = {
    "openai-oauth": "http://127.0.0.1:10531/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "commandcode": "https://api.commandcode.ai/provider/v1",
    # Go serves most models (MiMo, GLM, Kimi, DeepSeek) only over /chat/completions.
    "opencode-go": "https://opencode.ai/zen/go/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama-cloud": "https://ollama.com/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}
# The status line reads ratelimit-<route>.json from its own state folder.
RATE_DIR = os.path.expanduser("~/.claude/statusline")
RATE_RE = re.compile(r"x-ratelimit-(limit|remaining|reset)-(requests|tokens)(-1h)?$", re.I)
DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
# (route, key) -> when a key that hit 429/402 may be tried again.
COOLDOWN = {}
# Command Code's Go plan answers only its own CLI, so this route runs `cmdc -p` per request.
CLI_ROUTE = "commandcode-cli"
# Google AI Pro answers through Google's Antigravity CLI: one `agy` run per request, text tool protocol.
AGY_ROUTE = "antigravity-cli"
IDENTITY = {"service": "ccx-anthropic-proxy", "version": 39}
# `ccx official` (plain claude) comes in under this route: passed to Anthropic as sent, login and all,
# until /model routes its launch to another provider.
OFFICIAL_ROUTE = "official"
# Other harnesses (pi, Hermes, ...) post to /ccx/<launch>/v1/messages: the path names their launch, and the
# model names the part: ccx-auto / ccx-tools the helpers, ccx-live-<profile> a subagent, anything else the main model.
HARNESS_ROUTE = "ccx"
HARNESS_LAUNCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
HARNESS_HELPERS = {"ccx-auto": "auto", "ccx-tools": "tools"}
OFFICIAL_UPSTREAM = "https://api.anthropic.com"
# `ccx official` sets ANTHROPIC_BASE_URL to this host (Claude Code keeps Code Mode only for it) and HTTP_PROXY
# to the adapter, so its requests arrive here with the full URL.
OFFICIAL_PROXIED = "http://api.anthropic.com"
HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "accept-encoding", "x-ccx-launch",
               "proxy-connection", "proxy-authorization"}
# MiMo also closes with `</tool_call_<hash>>`, or not at all at the end of its reply.
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call[^>]*>|$)", re.S)
XML_FUNCTION_RE = re.compile(r"<function=([^>\s]+)>(.*?)(?:</function>|$)", re.S)
XML_PARAMETER_RE = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.S)
JSONISH_RE = re.compile(r"-?\d+(?:\.\d+)?|true|false|null|\[.*\]|\{.*\}", re.S)
TOOL_PROTOCOL = """You are the model behind a client application that runs tools for you.
Your own built-in tools are disabled: never call them, and never claim to have run anything yourself.
Ignore the working directory and environment your own instructions describe; the client's system prompt describes the real ones.
The client's tools, as JSON Schema:
{tools}
To call client tools, end your reply with one or more blocks, each exactly:
<tool_call>{{"name": "<tool name>", "input": {{<arguments>}}}}</tool_call>
Then stop. Results arrive in the next user turn as <tool_result> blocks.{choice}"""


def content_parts(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise ValueError("Content must be text or a list of blocks")
    result = []
    for part in content:
        kind = part.get("type")
        if kind == "text":
            result.append({"type": "text", "text": part["text"]})
        elif kind == "image":
            source = part["source"]
            url = source["url"] if source["type"] == "url" else (
                f"data:{source['media_type']};base64,{source['data']}")
            result.append({"type": "image_url", "image_url": {"url": url}})
        elif kind in ("thinking", "redacted_thinking", "tool_addition", "tool_removal"):
            # Provider-specific signatures cannot be replayed to another API. Claude Code
            # records MCP/connector tools it added mid-session as content blocks; they are
            # tool-list bookkeeping, not text (the active tool list arrives in `tools`).
            continue
        else:
            raise ValueError(f"Unsupported content block: {kind}")
    return result


def to_chat(body):
    if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not body["model"].strip():
        raise ValueError("A nonempty model is required")
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise ValueError("messages must be a nonempty list")
    messages = []
    if body.get("system"):
        system = content_parts(body["system"])
        if system:  # bookkeeping-only blocks must not become an empty system message
            messages.append({"role": "system", "content": system})
    for message in body["messages"]:
        role = message["role"]
        if role not in ("user", "assistant", "system"):
            raise ValueError("Unsupported message role")
        content = message["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        normal, calls, results = [], [], []
        for block in blocks:
            if block.get("type") == "tool_use":
                if role != "assistant":
                    raise ValueError("tool_use requires an assistant message")
                calls.append({"id": block["id"], "type": "function", "function": {
                    "name": block["name"], "arguments": json.dumps(block["input"])}})
            elif block.get("type") == "tool_result":
                if role != "user":
                    raise ValueError("tool_result requires a user message")
                parts = content_parts(block.get("content", ""))
                if block.get("is_error"):
                    parts.insert(0, {"type": "text", "text": "Tool error:"})
                results.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                    "content": "\n".join(part["text"] for part in parts if part["type"] == "text")})
                normal.extend(part for part in parts if part["type"] == "image_url")
            else:
                normal.extend(content_parts([block]))
        messages.extend(results)
        if normal or calls:
            item = {"role": role, "content": normal or None}
            if calls:
                item["tool_calls"] = calls
            messages.append(item)
    request = {"model": body["model"], "messages": messages, "stream": bool(body.get("stream"))}
    if request["stream"]:
        request["stream_options"] = {"include_usage": True}
    for key in ("max_tokens", "temperature", "top_p"):
        if key in body:
            request[key] = body[key]
    if body.get("stop_sequences"):
        request["stop"] = body["stop_sequences"]
    if body.get("tools"):
        request["tools"] = []
        for tool in body["tools"]:
            if "input_schema" not in tool:
                raise ValueError(f"Unsupported server tool type: {tool.get('type')}")
            request["tools"].append({"type": "function", "function": {
                "name": tool["name"], "description": tool.get("description", ""),
                "parameters": tool["input_schema"]}})
    choice = body.get("tool_choice", {})
    if choice.get("type") == "tool":
        request["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    elif choice.get("type") in ("auto", "any", "none"):
        request["tool_choice"] = {"any": "required"}.get(choice["type"], choice["type"])
    if "disable_parallel_tool_use" in choice:
        request["parallel_tool_calls"] = not choice["disable_parallel_tool_use"]
    effort = openai_effort(body)
    if effort:
        request["reasoning_effort"] = effort
    return request


def cache_key(body):
    """One prompt_cache_key per Claude Code session (its metadata.user_id names the session),
    so the Codex backend sends each turn to the cache the previous turn filled; None without one."""
    user = (body.get("metadata") or {}).get("user_id")
    return hashlib.sha256(user.encode()).hexdigest()[:32] if isinstance(user, str) and user else None


def openai_effort(body):
    """Claude Code's /effort (output_config.effort) in OpenAI's spelling; None when unset.
    OpenAI tops out at xhigh, so Claude's max lands there."""
    effort = (body.get("output_config") or {}).get("effort")
    return {"max": "xhigh"}.get(effort, effort) if isinstance(effort, str) and effort else None


def usage(value):
    value = value or {}
    cached = (value.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return {"input_tokens": max(0, value.get("prompt_tokens", 0) - cached),
            "output_tokens": value.get("completion_tokens", 0), "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0}


def reasoning(message):
    """Chat Completions reasoning text: `reasoning_content` (DeepSeek, NVIDIA) or
    `reasoning` (OpenRouter-style proxies). Claude Code shows it as a thinking block."""
    value = message.get("reasoning_content") or message.get("reasoning")
    return value if isinstance(value, str) else ""


def tool_block(call):
    arguments = json.loads(call["function"].get("arguments") or "{}")
    if not isinstance(arguments, dict):
        raise ValueError("Upstream tool arguments must be an object")
    return {"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": arguments}


def stop_reason(reason, tools=False):
    return "max_tokens" if reason == "length" else "tool_use" if tools else "end_turn"


# A lone "." (or "...", "…") says nothing: a model asked to continue sometimes emits one. Text is held
# until it carries a letter or digit, so such a turn never reaches the person as the whole answer.
NO_ANSWER = "The model returned no usable answer (punctuation only); retry or pick another model."


def speaks(text):
    """Whether a reply says anything: at least one letter or digit."""
    return any(ch.isalnum() for ch in text)


class Answer:
    """Streamed text held back until it says something; what is left over at the end."""

    def __init__(self):
        self.pending, self.said = "", False

    def feed(self, piece):
        self.pending += piece
        if not speaks(piece):
            return ""
        out, self.pending, self.said = self.pending, "", True
        return out

    def rest(self, calls):
        """The held text once the reply has spoken; the notice when it never did; nothing before tools."""
        if self.pending and self.said:
            out, self.pending = self.pending, ""
            return out
        if self.pending and not calls and not self.said:
            return NO_ANSWER
        return ""


def to_message(data, model):
    choice = data["choices"][0]
    message = choice["message"]
    content = []
    if reasoning(message):
        content.append({"type": "thinking", "thinking": reasoning(message), "signature": ""})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    calls = message.get("tool_calls") or []
    if content and content[-1]["type"] == "text" and not calls and not speaks(content[-1]["text"]):
        content[-1]["text"] = NO_ANSWER
    content.extend(tool_block(call) for call in calls)
    return {"id": data.get("id") or "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": stop_reason(choice.get("finish_reason"), bool(calls)),
            "stop_sequence": None, "usage": usage(data.get("usage"))}


class Blocks:
    """Streamed content blocks: a new one whenever the kind changes, so Claude Code
    shows thinking live, then the answer, each counted in its token total."""

    def __init__(self):
        self.index, self.kind = -1, None

    def add(self, kind, piece):
        events = []
        if self.kind != kind:
            events += self.close()
            self.index, self.kind = self.index + 1, kind
            events.append({"type": "content_block_start", "index": self.index, "content_block":
                           {"type": "thinking", "thinking": "", "signature": ""} if kind == "thinking" else {"type": "text", "text": ""}})
        events.append({"type": "content_block_delta", "index": self.index, "delta":
                       {"type": "thinking_delta", "thinking": piece} if kind == "thinking" else {"type": "text_delta", "text": piece}})
        return events

    def close(self):
        if not self.kind:
            return []
        self.kind = None
        return [{"type": "content_block_stop", "index": self.index}]

    def tool(self, block):
        self.close()
        self.index += 1
        return [{"type": "content_block_start", "index": self.index, "content_block": {**block, "input": {}}},
                {"type": "content_block_delta", "index": self.index,
                 "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}},
                {"type": "content_block_stop", "index": self.index}]


def message_start(model):
    return {"type": "message_start", "message": {"id": "msg_" + uuid.uuid4().hex,
        "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": usage(None)}}


def stream_events(response, model):
    yield message_start(model)
    blocks, calls, tokens, reason, answer = Blocks(), {}, usage(None), None, Answer()
    for line in response:
        if line.startswith(b":"):
            yield {"type": "ping"}
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            break
        data = json.loads(payload)
        if data.get("error"):
            raise ValueError("Upstream returned a stream error")
        if data.get("usage"):
            tokens = usage(data["usage"])
        for choice in data.get("choices", []):
            delta = choice.get("delta") or {}
            sent = False
            for kind, piece in (("thinking", reasoning(delta)), ("text", delta.get("content"))):
                if isinstance(piece, str) and piece:
                    out = answer.feed(piece) if kind == "text" else piece
                    if not out:
                        continue
                    yield from blocks.add(kind, out)
                    sent = True
            # ponytail: buffer tool arguments until complete; stream them if tool latency matters.
            for part in delta.get("tool_calls", []):
                call = calls.setdefault(part["index"], {"id": "", "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    call["id"] = part["id"]
                for key in ("name", "arguments"):
                    call["function"][key] += (part.get("function") or {}).get(key) or ""
            reason = choice.get("finish_reason") or reason
            if not sent:
                yield {"type": "ping"}
    if reason is None:
        raise ValueError("Upstream stream ended without a finish reason")
    tail = answer.rest(calls)
    if tail:
        yield from blocks.add("text", tail)
    yield from blocks.close()
    for call in calls.values():
        yield from blocks.tool(tool_block(call))
    yield {"type": "message_delta", "delta": {"stop_reason": stop_reason(reason, bool(calls)), "stop_sequence": None}, "usage": tokens}
    yield {"type": "message_stop"}


def cli_text(content):
    return "\n".join(part["text"] if part["type"] == "text" else "[image omitted: this route is text-only]"
                     for part in content_parts(content))


def to_prompt(body):
    """The whole Messages request as one CLI prompt; tools become a text protocol."""
    to_chat(body)  # same validation as the HTTP routes
    sections = []
    if body.get("tools"):
        choice = body.get("tool_choice") or {}
        rule = {"tool": f" You must call {choice.get('name')}.", "any": " You must call a tool.",
                "none": " Do not call any tool this turn."}.get(choice.get("type"), "")
        tools = json.dumps([{k: t.get(k) for k in ("name", "description", "input_schema")} for t in body["tools"]])
        sections.append(TOOL_PROTOCOL.format(tools=tools, choice=rule))
    if body.get("system"):
        system = cli_text(body["system"])
        if system:  # bookkeeping-only blocks leave no empty section
            sections.append(f"<system>\n{system}\n</system>")
    for message in body["messages"]:
        content = message["content"]
        lines = []
        for block in [{"type": "text", "text": content}] if isinstance(content, str) else content:
            kind = block.get("type")
            if kind == "tool_use":
                lines.append("<tool_call>" + json.dumps({"name": block["name"], "input": block["input"]}) + "</tool_call>")
            elif kind == "tool_result":
                error = ' is_error="true"' if block.get("is_error") else ""
                lines.append(f'<tool_result id="{block["tool_use_id"]}"{error}>\n{cli_text(block.get("content", ""))}\n</tool_result>')
            elif kind not in ("thinking", "redacted_thinking", "tool_addition", "tool_removal"):
                lines.append(cli_text([block]))
        sections.append(f"<{message['role']}>\n" + "\n".join(lines) + f"\n</{message['role']}>")
    sections.append("Write the next assistant turn only.")
    return "\n\n".join(sections)


def native_calls(content, tools):
    """cmdc's own tool calls as the client's (Claude Code's) tools; unmatched ones by name."""
    names = {tool.get("name") for tool in tools or []}

    def first(args, *keys, empty=False):
        return next((args[k] for k in keys if k in args and (empty or args[k] not in (None, ""))), None)

    calls, skipped = [], []
    for block in content:
        if block.get("type") != "tool_use":
            continue
        name, args = block.get("name"), block.get("input") if isinstance(block.get("input"), dict) else {}
        path = first(args, "file_path", "filePath", "absolute_path", "path", "target_file")
        if name in names:
            mapped = [(name, args)]
        elif name == "shell_command":
            mapped = [("Bash" if "Bash" in names else "PowerShell",
                       {"command": first(args, "command", "cmd"), "description": first(args, "description")})]
        elif name == "read_file":
            mapped = [("Read", {"file_path": path, "offset": first(args, "offset"), "limit": first(args, "limit")})]
        elif name == "read_multiple_files":
            mapped = [("Read", {"file_path": p}) for p in first(args, "paths", "file_paths", "files") or []]
        elif name == "write_file":
            mapped = [("Write", {"file_path": path, "content": first(args, "content", "text", empty=True)})]
        elif name == "edit_file":
            mapped = [("Edit", {"file_path": path, "old_string": first(args, "old_string", "oldString", "old_str", empty=True),
                                "new_string": first(args, "new_string", "newString", "new_str", empty=True),
                                "replace_all": first(args, "replace_all", "replaceAll")})]
        elif name in ("grep", "glob"):
            mapped = [(name.capitalize(), {"pattern": first(args, "pattern", "query", "regex", "glob"),
                                           "path": first(args, "path", "directory", "dir")})]
        elif name == "web_fetch":
            mapped = [("WebFetch", {"url": first(args, "url"), "prompt": first(args, "prompt") or "Return the page's main content."})]
        elif name == "web_search":
            mapped = [("WebSearch", {"query": first(args, "query")})]
        else:
            mapped = []
        if not mapped or any(tool not in names for tool, _ in mapped):
            skipped.append(name)
            continue
        # Missing arguments stay missing: Claude Code reports them to the model as a tool error.
        calls += [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex, "name": tool,
                   "input": {k: v for k, v in tool_input.items() if v is not None}} for tool, tool_input in mapped]
    return calls, skipped


def _loads_unclosed(raw):
    """json.loads, forgiving the one or two final braces MiMo sometimes drops from a call."""
    for tail in ("", "}", "}}"):
        try:
            return json.loads(raw + tail)
        except ValueError:
            pass
    return None


def _parse_call(raw):
    """A text tool call as {name, input}: our JSON protocol, or the XML form MiMo sometimes falls back to
    (<function=Bash><parameter=command>...</parameter></function>)."""
    call = _loads_unclosed(raw)
    if call is not None:
        return call
    function = XML_FUNCTION_RE.match(raw)
    if not function:
        return None
    args = {}
    for key, value in XML_PARAMETER_RE.findall(function.group(2)):
        if JSONISH_RE.fullmatch(value.strip()):
            try:
                value = json.loads(value)  # numbers, flags and lists keep their JSON types
            except ValueError:
                pass
        args[key] = value
    return {"name": function.group(1), "input": args}


def from_cli(text, tokens, stop, model, native=(), tools=()):
    calls, skipped = native_calls(native, tools)
    if skipped:
        text += f"\n\n(Command Code's own tool {', '.join(map(str, skipped))} has no match among this client's tools.)"
    for raw in TOOL_CALL_RE.findall(text):
        call = _parse_call(raw)
        if call is None:
            continue
        if isinstance(call, dict) and isinstance(call.get("name"), str) and isinstance(call.get("input", {}), dict):
            calls.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex, "name": call["name"], "input": call.get("input", {})})
    text = (TOOL_CALL_RE.sub("", text) if calls else text).strip()
    tokens = tokens or {}
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model,
            "content": ([{"type": "text", "text": text}] if text else []) + calls,
            "stop_reason": "tool_use" if calls else "max_tokens" if stop in ("max_tokens", "length") else "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": tokens.get("inputTokens", 0),
            "output_tokens": tokens.get("outputTokens", 0), "cache_read_input_tokens": tokens.get("cacheReadTokens", 0),
            "cache_creation_input_tokens": tokens.get("cacheWriteTokens", 0)}}


def cmdc_events(body):
    """cmdc's NDJSON events as they arrive (`--output-format json`); the last item is its
    `result` line. A failed run raises with the CLI's own reason."""
    # --max-turns 1: one model call; if the model reaches for cmdc's own tools, it exits 8.
    args = [commandcode_executable(), "-p", "--output-format", "json", "--model", body["model"], "--max-turns", "1",
            "--skip-onboarding", "--no-skills", "--no-session", "--no-auto-update"]
    # cmdc takes Claude's own effort names (low..max); `off`/`max` come straight from the
    # catalog, so no OpenAI spelling is applied here.
    effort = (body.get("output_config") or {}).get("effort")
    if isinstance(effort, str) and effort and effort_supported(CLI_ROUTE, body["model"], effort):
        args += ["--effort", effort]
    flags = background_process_options()
    # Neutral directory, prompt on stdin (Windows caps argv at 32k), as ai_suite's _run_cli.
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", cwd=tempfile.gettempdir(), **flags)

    def kill():  # the .cmd shim's node child holds the pipes: kill the whole tree
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, **flags)
        proc.kill()

    timer = threading.Timer(CLI_TIMEOUT, kill)
    timer.start()
    errors = []
    threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
    try:
        proc.stdin.write(to_prompt(body))
        proc.stdin.close()
    except OSError:
        pass  # it died early; the exit code says why
    result, content, tokens = {}, [], None
    try:
        for line in proc.stdout:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("type") == "result":
                result = item
            elif isinstance(item, dict) and isinstance(item.get("event"), dict):
                event = item["event"]
                if event.get("type") == "message_end" and isinstance(event.get("content"), list):
                    content = event["content"]
                elif event.get("type") == "turn_end":
                    tokens = event.get("usage")
                yield event
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:  # the client went away mid-reply
            kill()
    if proc.returncode in (8, 9) and any(block.get("type") == "tool_use" for block in content):
        # The model called cmdc's own tools (which --max-turns 1 stops before running)
        # instead of the text protocol; from_cli hands those calls to the client.
        result = {"type": "result", "usage": tokens, "stopReason": "tool_use", "nativeCalls": content,
                  "finalText": "".join(block.get("text", "") for block in content if block.get("type") == "text")}
    elif proc.returncode or not result:
        # The CLI's own error names the cause, e.g. "You've reached your weekly usage limit. Resets in 16h".
        detail = result.get("error") or COMMANDCODE_EXIT_MEANINGS.get(proc.returncode) or "".join(errors)[-300:].strip() or "no result"
        failure = RuntimeError(f"commandcode exited {proc.returncode}: {detail}"[:400])
        failure.status_code = {3: 401, 4: 403, 5: 429, 10: 402}.get(proc.returncode, 502)
        failure.public = True
        failure.exit_code = proc.returncode
        raise failure
    yield result


def cmdc_retrying(body, tries=3):
    """cmdc_events, run again when the model answered nothing (exit 9, or 8 with no
    usable call): usually transient, and Claude Code's own retry would surface an
    API error first. Never repeats a run whose answer text already went out."""
    for attempt in range(1, tries + 1):
        answered = False
        try:
            for event in cmdc_events(body):
                answered = answered or event.get("type") == "text_delta"
                yield event
            return
        except RuntimeError as exc:
            if attempt == tries or answered or getattr(exc, "exit_code", None) not in (8, 9):
                raise


def agy_args(executable, model):
    """agy's argv. Flags go before -p, which takes the next word as its prompt; the prompt itself goes on stdin."""
    return [executable, "--output-format", "stream-json", "--input-format", "stream-json", "--print-timeout", f"{CLI_TIMEOUT}s",
            "--disable-slash-commands", "--model", model, "--settings", json.dumps({"toolPermission": "strict"}), "-p", ""]


def agy_stdin(prompt):
    """The whole prompt as agy's one stream-json user event (Windows caps argv at 32k)."""
    return json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"


def agy_failure(error, stderr, code):
    """A failed agy run as a public error: sign-in, quota (429) or agy's own reason."""
    text = (error or "").strip() or stderr.strip()[-300:] or f"agy exited {code}"
    low = f"{error or ''} {stderr}".lower()
    if any(word in low for word in ("not authenticated", "unauthenticated", "not logged in", "not signed in", "please log in", "please sign in")):
        failure = RuntimeError("Antigravity CLI is not signed in: run 'agy' once in a terminal and sign in with your Google account.")
        failure.status_code = 401
    else:
        failure = RuntimeError(f"agy exited {code}: {text}"[:400])
        quota = ("429", "quota", "resource_exhausted", "resource exhausted", "rate limit", "rate-limit", "usage limit")
        failure.status_code = 429 if any(word in low for word in quota) else 502
    failure.public = True
    return failure


def agy_events(body):
    """One agy run as cmdc_events-style events: its reply's text deltas as they arrive, then one `result`
    item (finalText, usage). agy's own tool steps are ignored; a failed run raises with agy's reason."""
    ccx = _load_ccx_switch()
    effort = (body.get("output_config") or {}).get("effort")
    model = ccx.agy_model_id(body["model"], effort if isinstance(effort, str) else None)
    try:
        executable = ccx.agy_executable()
    except RuntimeError as exc:
        exc.public = True
        raise
    prompt = agy_stdin(to_prompt(body))
    flags = background_process_options()
    proc = subprocess.Popen(agy_args(executable, model), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", cwd=tempfile.gettempdir(), **flags)

    def kill():
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, **flags)
        proc.kill()

    timer = threading.Timer(CLI_TIMEOUT + 60, kill)  # agy's own --print-timeout normally ends it first
    timer.start()
    errors = []
    threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass  # it died early; the exit code says why
    result = {}
    try:
        for line in proc.stdout:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if item.get("event") == "result" and isinstance(item.get("result"), dict):
                result = item["result"]
            elif item.get("event") == "step_update" and isinstance(item.get("step_update"), dict):
                step = item["step_update"]
                delta = step.get("text_delta")
                if step.get("step_type") == "agent_response" and isinstance(delta, str) and delta:
                    yield {"type": "text_delta", "delta": delta}
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:  # the client went away mid-reply
            kill()
    if proc.returncode or result.get("status") != "SUCCESS":
        raise agy_failure(result.get("error"), "".join(errors), proc.returncode)
    text = result.get("response") if isinstance(result.get("response"), str) else ""
    if not text.strip():
        failure = RuntimeError("agy answered no text: it tried its own tools instead of the client's tool protocol.")
        failure.public = True
        raise failure
    tokens = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    yield {"type": "result", "finalText": text, "stopReason": None,
           "usage": {"inputTokens": tokens.get("input_tokens") or 0,
                     "outputTokens": (tokens.get("output_tokens") or 0) + (tokens.get("thinking_tokens") or 0),
                     "cacheReadTokens": tokens.get("cache_read_tokens") or 0}}


GENERATE_URL = "https://api.commandcode.ai/alpha/generate"


def generate_body(body):
    """Claude's /v1/messages as the body cmdc itself posts to /alpha/generate (its toWireMessages):
    native tools and the server's prompt cache, unlike one cmdc run per request."""
    system = body.get("system") or []
    if isinstance(system, str):
        system = [{"type": "text", "text": system}]
    messages, names = [], {}
    for message in body.get("messages") or []:
        content = message.get("content")
        content = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        if message.get("role") == "assistant":
            parts = []
            for block in content:
                if block.get("type") == "text" and block.get("text"):
                    parts.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "thinking" and block.get("thinking"):
                    parts.append({"type": "reasoning", "text": block["thinking"]})
                elif block.get("type") == "tool_use":
                    names[block["id"]] = block["name"]
                    parts.append({"type": "tool-call", "toolCallId": block["id"], "toolName": block["name"], "input": block.get("input") or {}})
            messages.append({"role": "assistant", "content": parts or [{"type": "text", "text": "(no content)"}]})
            continue
        results, user = [], []
        for block in content:
            if block.get("type") == "text" and block.get("text"):
                user.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "image" and (block.get("source") or {}).get("type") == "base64":
                source = block["source"]
                user.append({"type": "image", "image": f"data:{source['media_type']};base64,{source['data']}", "mimeType": source["media_type"]})
            elif block.get("type") == "tool_result":
                value = block.get("content")
                value = value if isinstance(value, str) else "\n".join(b.get("text", "") for b in value or [] if b.get("type") == "text")
                results.append({"type": "tool-result", "toolCallId": block["tool_use_id"], "toolName": names.get(block["tool_use_id"], "unknown"),
                                "output": {"type": "text", "value": value}})
        if results:
            messages.append({"role": "tool", "content": results})
        if user:
            messages.append({"role": "user", "content": user})
    params = {"model": body["model"], "messages": messages, "stream": True, "max_tokens": body.get("max_tokens") or 64000,
              "tools": [{"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("input_schema") or {"type": "object"}}
                        for t in body.get("tools") or [] if t.get("name")],
              "system": [{"type": "text", "text": b.get("text", ""), **({"cache_control": {"type": "ephemeral"}} if b.get("cache_control") else {})}
                         for b in system if b.get("type") == "text"]}
    effort = (body.get("output_config") or {}).get("effort")
    if isinstance(effort, str) and effort and effort_supported(CLI_ROUTE, body["model"], effort):
        params["reasoning_effort"] = effort
    if isinstance(body.get("temperature"), (int, float)):
        params["temperature"] = body["temperature"]
    # One thread per Claude Code session, as cmdc keeps one per conversation.
    thread = str(uuid.uuid5(uuid.NAMESPACE_URL, cache_key(body) or uuid.uuid4().hex))
    config = {"workingDir": tempfile.gettempdir(), "date": time.strftime("%Y-%m-%d"), "environment": sys.platform, "structure": [],
              "isGitRepo": False, "currentBranch": "", "mainBranch": "", "gitStatus": "", "recentCommits": []}
    return thread, {"config": config, "memory": None, "taste": None, "skills": None, "permissionMode": "standard",
                    "threadId": thread, "params": params}


def cmdc_login():
    """The key and version of the signed-in Command Code CLI, as its own requests send them."""
    key = json.loads(Path.home().joinpath(".commandcode", "auth.json").read_text(encoding="utf-8")).get("apiKey")
    if not key:
        raise RuntimeError("Command Code CLI is not signed in; run cmdc once and log in.")
    try:
        version = json.loads(Path(commandcode_executable()).resolve().parent.joinpath("node_modules", "command-code", "package.json")
                             .read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, RuntimeError):
        version = "1.88.2"  # the CLI version this was written against
    return key, version


def generate_stream(body, tries=2):
    """/alpha/generate's NDJSON (AI SDK stream parts) as Claude stream events.
    A turn that says nothing (a lone ".") is retried once before the person is told: the
    degenerate reply is transient, and an empty turn costs them the whole request."""
    thread, request = generate_body(body)
    key, version = cmdc_login()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "cli",
               "x-cli-environment": "production", "x-command-code-version": version, "x-session-id": thread}

    def open_response():
        try:
            return urllib.request.urlopen(urllib.request.Request(GENERATE_URL, json.dumps(request).encode(), headers), timeout=CLI_TIMEOUT)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            failure = RuntimeError(f"Command Code {exc.code}: {detail}"[:400])
            failure.status_code, failure.public = exc.code, True
            raise failure from None

    # Open the first attempt before the stream starts: a refused request keeps its real status.
    response = open_response()
    yield message_start(body["model"])
    blocks, tokens, finish = Blocks(), {}, None
    for attempt in range(1, tries + 1):
        if attempt > 1:
            response = open_response()
        calls, answer = [], Answer()
        tokens, finish = {}, None  # each attempt reports its own counters, never the last one's
        with response:
            for raw in response:
                try:
                    part = json.loads(raw)
                except ValueError:
                    continue
                kind = part.get("type")
                if kind == "text-delta" and part.get("text"):
                    out = answer.feed(part["text"])
                    if out:
                        yield from blocks.add("text", out)
                elif kind == "reasoning-delta" and part.get("text"):
                    yield from blocks.add("thinking", part["text"])
                elif kind == "tool-call":
                    args = part.get("input", part.get("args")) or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except ValueError:
                            args = {"raw": args}
                    calls.append({"type": "tool_use", "id": part.get("toolCallId") or "toolu_" + uuid.uuid4().hex,
                                  "name": part.get("toolName", ""), "input": args if isinstance(args, dict) else {"value": args}})
                elif kind == "finish":
                    tokens, finish = part.get("totalUsage") or {}, part.get("finishReason")
                elif kind == "error":
                    message = str((part.get("error") or {}).get("message") if isinstance(part.get("error"), dict) else part.get("error") or part)
                    failure = RuntimeError(f"Command Code: {message}"[:400])
                    failure.public = True
                    raise failure
        tail = answer.rest(calls)
        if not calls and not answer.said and attempt < tries:
            continue  # a lone punctuation mark says nothing: ask for the same turn again
        if tail:
            yield from blocks.add("text", tail)
        break
    yield from blocks.close()
    for call in calls:
        yield from blocks.tool(call)
    details = tokens.get("inputTokenDetails") or {}
    used = {"input_tokens": details.get("noCacheTokens", tokens.get("inputTokens", 0)), "output_tokens": tokens.get("outputTokens", 0),
            "cache_read_input_tokens": details.get("cacheReadTokens", 0), "cache_creation_input_tokens": details.get("cacheWriteTokens", 0)}
    reason = "tool_use" if calls else "max_tokens" if finish == "length" else "end_turn"
    yield {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None}, "usage": used}
    yield {"type": "message_stop"}


def collect(events, model):
    """A Claude stream folded into one non-streamed message."""
    message, content = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model,
                        "stop_sequence": None}, []
    for event in events:
        if event["type"] == "content_block_start":
            content.append(dict(event["content_block"]))
        elif event["type"] == "content_block_delta":
            delta, block = event["delta"], content[-1]
            if delta["type"] == "input_json_delta":
                block["input"] = json.loads(delta["partial_json"])
            else:
                field = "thinking" if delta["type"] == "thinking_delta" else "text"
                block[field] = block.get(field, "") + delta[field]
        elif event["type"] == "message_delta":
            message.update(stop_reason=event["delta"]["stop_reason"], usage=event["usage"])
    return {**message, "content": content}


def commandcode_cli(body, source=None):
    """One text-protocol CLI run (cmdc, or agy_events) as a non-streamed message."""
    *_, result = (source or cmdc_retrying)(body)
    message = from_cli(result.get("finalText") or "", result.get("usage"), result.get("stopReason"), body["model"],
                       result.get("nativeCalls") or (), body.get("tools"))
    text = next((block["text"] for block in message["content"] if block["type"] == "text"), "")
    if text and not speaks(text) and not any(block["type"] == "tool_use" for block in message["content"]):
        message["content"] = [{"type": "text", "text": NO_ANSWER}]
    return message


def cli_stream(body, source=None):
    """cmdc's (or agy_events') reply as Claude stream events, live: thinking and text as they arrive.
    Text from the first <tool_call> on is held back and becomes tool_use blocks, and a
    punctuation-only prefix is held until the reply says something."""
    yield message_start(body["model"])
    blocks, text, sent, held, spoken, result = Blocks(), "", 0, False, False, {}
    mark = "<tool_call>"
    for event in (source or cmdc_retrying)(body):
        kind, delta = event.get("type"), event.get("delta")
        if kind == "result":
            result = event
        elif not isinstance(delta, str) or not delta:
            continue
        elif kind == "thinking_delta":
            yield from blocks.add("thinking", delta)
        elif kind == "text_delta" and not held:
            text += delta
            if not spoken and not speaks(text):
                continue  # a lone "." is not an answer: hold it until a letter or digit arrives
            spoken = True
            cut = text.find(mark)
            held = cut >= 0
            if not held:  # keep back a tail that may be the start of a <tool_call>
                cut = len(text) - next((k for k in range(min(len(mark) - 1, len(text)), 0, -1)
                                        if mark.startswith(text[-k:])), 0)
            if cut > sent:
                yield from blocks.add("text", text[sent:cut])
                sent = cut
    message = from_cli(result.get("finalText") or "", result.get("usage"), result.get("stopReason"), body["model"],
                       result.get("nativeCalls") or (), body.get("tools"))
    calls = [block for block in message["content"] if block["type"] == "tool_use"]
    # Whatever the final text adds to what streamed: held-back text that held no
    # parseable call, or the note on an unmatched native tool.
    final = next((block["text"] for block in message["content"] if block["type"] == "text"), "")
    streamed = text[:sent].strip()
    if not spoken and not calls:
        # A run whose text arrived only in the result event still counts as the answer.
        yield from blocks.add("text", final if speaks(final) else NO_ANSWER)
    elif spoken and final.startswith(streamed):
        rest = final[len(streamed):]
        if rest:
            yield from blocks.add("text", rest)
    elif calls and speaks(final):
        yield from blocks.add("text", final)
    yield from blocks.close()
    for call in calls:
        yield from blocks.tool(call)
    yield {"type": "message_delta", "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None}, "usage": message["usage"]}
    yield {"type": "message_stop"}


_ccx_switch = None


def reset_seconds(value, default=60.0):
    """Seconds until a rate-limit window resets: OpenAI sends "6m0s", Nous plain seconds,
    OpenRouter epoch milliseconds."""
    value = (value or "").strip()
    try:
        number = float(value)
    except ValueError:
        parts = DURATION_RE.findall(value)
        if not parts:
            return default
        return sum(float(n) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit] for n, unit in parts)
    if number > 1e12:
        return max(0.0, number / 1000 - time.time())
    if number > 1e9:
        return max(0.0, number - time.time())
    return number


def quota_reset(value, now, relative=False):
    """Only provider-supplied finite reset times, never a made-up retry window."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        if relative:
            number += now
        elif number > 1e12:
            number /= 1000
    except (ValueError, TypeError):
        if not isinstance(value, str):
            return None
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            number = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            try:
                number = parsedate_to_datetime(value).timestamp()
            except (ValueError, TypeError, OverflowError):
                return None
    return math.ceil(number) if math.isfinite(number) and now < number < now + 366 * 86400 else None


def provider_error(exc, provider, upstream=None):
    """Map provider failures onto Claude's gateway quota-header contract (`upstream`: a body already read)."""
    status = exc.code if isinstance(exc, urllib.error.HTTPError) else getattr(exc, 'status_code', None) or 502
    payload, headers = {}, {}
    if isinstance(exc, urllib.error.HTTPError):
        try:
            if upstream is None:
                upstream = exc.read(65536).decode('utf-8', 'replace')
            payload = json.loads(upstream)
        except (OSError, ValueError):
            pass
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
    detail = payload.get('error') if isinstance(payload, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    text = ' '.join(str(detail.get(k) or '') for k in ('type', 'code', 'message')).lower() + ' ' + (upstream or '').lower()
    quota = status not in (401, 402, 403) and (headers.get('anthropic-ratelimit-unified-status') == 'rejected'
            or any(word in text for word in ('usage_limit', 'usage limit', 'quota exceeded', 'quota_exceeded', 'plan quota', 'weekly limit', 'daily limit', 'credit balance is too low')))
    outgoing = {}
    if status == 429 and headers.get('retry-after'):
        retry = quota_reset(headers['retry-after'], time.time(), relative=True)
        if retry:
            outgoing['retry-after'] = str(max(1, math.ceil(retry - time.time())))
    if not quota:
        return status, {'type': 'error', 'error': {'type': 'rate_limit_error' if status == 429 else 'authentication_error' if status in (401, 403) else 'api_error',
            'message': f'{provider} temporarily rate-limited requests.' if status == 429 else f'{provider} request failed (HTTP {status}).'}}, outgoing
    now = time.time()
    reset = quota_reset(headers.get('anthropic-ratelimit-unified-reset'), now)
    reset = reset or next((r for key in ('resets_at', 'reset_at', 'reset_time') if (r := quota_reset(detail.get(key), now))), None)
    reset = reset or quota_reset(detail.get('resets_in_seconds'), now, relative=True)
    reset = reset or quota_reset(headers.get('retry-after'), now, relative=True)
    claim = headers.get('anthropic-ratelimit-unified-representative-claim')
    claim = claim if claim in ('five_hour', 'seven_day', 'overage') else 'overage'
    # The status line already polls account usage; share its fresh snapshot without another API call.
    source = {'openai-oauth': 'openai', 'commandcode-cli': 'commandcode', 'commandcode': 'commandcode', 'opencode-go': 'opencode-go'}.get(provider)
    if not reset and source:
        try:
            with open(os.path.join(RATE_DIR, f'quota-{source}.json'), encoding='utf-8') as f:
                cached = json.load(f)
            if 0 <= now - cached.get('at', 0) <= 120:
                windows = [(r, name) for name, window in cached.get('rate_limits', {}).items()
                           if isinstance(window, dict) and isinstance(window.get('used_percentage'), (int, float))
                           and window['used_percentage'] >= 100 and (r := quota_reset(window.get('resets_at'), now))]
                if windows:
                    reset, name = max(windows)
                    claim = name if name in ('five_hour', 'seven_day') else 'overage'
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    outgoing['x-should-retry'] = 'false'
    if reset:
        outgoing.update({'anthropic-ratelimit-unified-status': 'rejected',
                         'anthropic-ratelimit-unified-reset': str(reset),
                         'anthropic-ratelimit-unified-representative-claim': claim,
                         'anthropic-ratelimit-unified-overage-status': 'rejected',
                         'retry-after': str(max(1, math.ceil(reset - now)))})
    message = (f'{provider} usage limit reached; resets at {datetime.fromtimestamp(reset, timezone.utc).isoformat()}. '
               'Use /rate-limit-options to wait and continue.' if reset else
               f'{provider} usage limit reached; reset time unavailable. Choose another route or retry after the provider resets.')
    return 429, {'type': 'error', 'error': {'type': 'rate_limit_error', 'message': message}}, outgoing


def record_rate_limits(route, headers):
    """Saves the x-ratelimit-* windows an upstream reported, for the status line's quota bars."""
    windows = {}
    for name, value in (headers or {}).items():
        match = RATE_RE.match(name)
        if match:
            label = {"requests": "req", "tokens": "tok"}[match[2].lower()] + (" 1h" if match[3] else "")
            windows.setdefault(label, {})[match[1].lower()] = value
    now, limits = time.time(), {}
    for label, w in windows.items():
        try:
            limit, left = float(w["limit"]), float(w["remaining"])
        except (KeyError, ValueError):
            continue
        if limit > 0:
            limits[label] = {"used_percentage": max(0, min(100, round(100 - left * 100 / limit))),
                             "resets_at": now + reset_seconds(w.get("reset"), 0) if w.get("reset") else None}
    if not limits:
        return
    try:
        os.makedirs(RATE_DIR, exist_ok=True)
        path = os.path.join(RATE_DIR, f"ratelimit-{route}.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"at": now, "rate_limits": limits}, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass  # the status line just keeps its last figure


def open_upstream(route, request, key, strict=False, session=None):
    """Opens the route's chat completion. Like Hermes's credential pool, a key out of quota
    (429/402) rests until its retry-after while the next key in <KEY>_API_KEYS serves.
    strict: with every key resting, refuse at once (a fallback is waiting) instead of asking anyway.
    session: opencode.ai's x-opencode-session; it routes by it, so a random one per request
    scatters a conversation over backends and misses the prompt cache (2026-10-30 probe)."""
    keys = list(dict.fromkeys([key, *spare_keys(route)]))
    ready = [k for k in keys if COOLDOWN.get((route, k), 0) <= time.time()]
    if strict and not ready:
        raise urllib.error.HTTPError(UPSTREAMS[route], 429, "Every key is resting after a 429/402", {}, None)
    failure = None
    for attempt in ready or keys[:1]:
        service = AIService(allow_auth_prompt=False, config_overrides={
            "provider": "http", "base_url": UPSTREAMS[route], "api_key": attempt,
            "use_openai_client": False, "timeout": 240,
            "usage_state_path": "", "groq_rate_state_path": "",
            **({"headers": {"x-opencode-session": session}} if session else {}),
        })
        try:
            response = service.chat_completion_response(request)
        except urllib.error.HTTPError as exc:
            record_rate_limits(route, exc.headers)
            if exc.code not in (402, 429):
                raise
            COOLDOWN[(route, attempt)] = time.time() + reset_seconds(exc.headers.get("retry-after"))
            failure = exc
            continue
        record_rate_limits(route, response.headers)
        return response
    raise failure


def fallback_for(launch):
    """{provider, model, key} of the launch's fallback route, or None (the default)."""
    if not launch:
        return None
    try:
        ccx = _load_ccx_switch()
        target = (ccx.load_agents().get(launch[6:], {}).get("fallback") if launch.startswith("agent-")
                  else ccx.load_fallbacks().get(launch))
        if not target or target.get("provider") not in UPSTREAMS:
            return None
        name = ccx.PROVIDERS[target["provider"]]["key"]
        key = "" if name in ("oauth", "cli") else ccx.auth_key(name)
    except Exception:
        return None
    return {**target, "key": key or target["provider"]}


def route_for(launch):
    """{provider, model, effort, key} of the launch's /model override, or None."""
    if not launch:
        return None
    try:
        target = _load_ccx_switch().load_routes().get(launch)
    except Exception:
        return None
    if not isinstance(target, dict):
        return None
    provider, model = target.get("provider"), target.get("model")
    if not isinstance(model, str) or not model:
        return None
    try:
        return provider_target(provider, model, target.get("effort"))
    except Exception:
        return None


# An in-process ccx subagent's model (its agent file says `model: ccx-live-<profile>`): the adapter
# resolves the profile on every request, so a /model or picker change applies at the next turn.
LIVE_MODEL = "ccx-live-"
# shortcut: a profile with no model on official runs this, follow the newest Claude generation
LIVE_OFFICIAL_DEFAULT = "claude-opus-5-5"


def live_target(model, launch):
    """{provider, model, effort, key, format} of the ccx profile an in-process subagent names, or None."""
    if not (isinstance(model, str) and model.startswith(LIVE_MODEL)):
        return None
    name = model[len(LIVE_MODEL):]
    try:
        ccx = _load_ccx_switch()
        agents = (ccx._session_agents(launch) if launch else None) or ccx.load_agents(session=False)
        profile = agents.get(name)
    except (Exception, SystemExit):
        profile = None
    if not isinstance(profile, dict):
        raise ValueError(f"No ccx subagent profile '{name}' for this session.")
    provider, model = profile.get("provider", "official"), profile.get("model")
    if provider != "official" and not model:
        raise ValueError(f"ccx subagent '{name}' names no model for {provider}; pick one in /model's Subagent tab.")
    return provider_target(provider, model, profile.get("effort"))


def provider_target(provider, model, effort):
    """{provider, model, effort, key, format: official|chat|anthropic, base, header} of one ccx provider's model."""
    if provider == "official":
        return {"provider": provider, "model": model or LIVE_OFFICIAL_DEFAULT, "effort": effort, "key": "", "format": "official"}
    ccx = _load_ccx_switch()
    p = ccx.PROVIDERS[provider]
    key_id = p.get("key")
    key = ccx.auth_key(key_id) if key_id and key_id not in ("oauth", "cli", "lmstudio") else ""
    # route_env's split: Command Code serves non-Claude models, and OpenCode Go most models, over chat completions.
    chat = (p["format"] == "openai" or (provider == "commandcode" and not model.startswith("claude-"))
            or (provider == "opencode-go" and not ccx.go_messages(model)))
    return {"provider": provider, "model": model, "effort": effort, "key": key or provider,
            "format": "chat" if chat else "anthropic", "base": p.get("base", ""), "header": p.get("header")}


def note_fallback(launch, target):
    """Tells the status line the fallback just answered, so the switch is visible."""
    try:
        os.makedirs(RATE_DIR, exist_ok=True)
        with open(os.path.join(RATE_DIR, f"fallback-{launch}.json"), "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "provider": target["provider"], "model": target["model"]}, f)
    except OSError:
        pass


def spare_keys(route):
    """Extra keys for a route from <KEY>_API_KEYS (comma-separated), tried in turn after a 429/402."""
    try:
        ccx = _load_ccx_switch()
        env = ccx.PROVIDERS[route]["key"].upper().replace("-", "_") + "_API_KEYS"
        listed = os.environ.get(env) or ccx.user_env(env) or ""
    except Exception:
        return []
    return [k.strip() for k in listed.split(",") if k.strip()]


def _load_ccx_switch():
    """Import ccx-switch.py beside this file, again whenever it changes: it owns the plan and effort
    metadata, and a stale copy (one without a newly listed model) would refuse that model's requests."""
    global _ccx_switch
    directory = str(Path(__file__).resolve().parent)
    loaded = getattr(_ccx_switch, "_ccx_loaded_mtime", None)  # absent on an injected stand-in
    try:
        mtime = Path(directory, "ccx-switch.py").stat().st_mtime
    except OSError:
        mtime = loaded
    if _ccx_switch is None or (loaded is not None and mtime != loaded):
        import importlib.util
        if directory not in sys.path:
            sys.path.insert(0, directory)  # ccx-switch imports the vendored ai_suite beside it
        spec = importlib.util.spec_from_file_location("ccx_switch_models", Path(directory, "ccx-switch.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._ccx_loaded_mtime = mtime
        _ccx_switch = module
    return _ccx_switch


_CUSTOM_ROUTES = set()


def refresh_upstreams():
    """Custom OpenAI-format providers (ccx providers.json) join the routes without an adapter restart."""
    try:
        custom = {n: p["base"] for n, p in _load_ccx_switch().custom_providers().items() if p["format"] == "openai"}
    except Exception:
        return  # a standalone adapter has only the built-in routes
    for name in _CUSTOM_ROUTES - set(custom):
        UPSTREAMS.pop(name, None)
    UPSTREAMS.update({n: b for n, b in custom.items() if n not in UPSTREAMS or n in _CUSTOM_ROUTES})
    _CUSTOM_ROUTES.clear()
    _CUSTOM_ROUTES.update(custom)


def ccx_models(provider):
    """The provider's plan-filtered models from ccx-switch.py, when it sits beside this file."""
    try:
        return _load_ccx_switch().get_models(provider)
    except Exception:
        return []  # a standalone adapter simply offers no discovery list


# What Claude Code adds only for Anthropic's own API (an official session runs as first party behind
# the adapter); from 2.1.318. A third-party Anthropic-format provider never receives them.
FIRST_PARTY_BETAS = ("prompt-caching-scope-", "tool-provenance-", "thinking-binding-controls-", "cache-diagnosis-",
                     "claude-code-side-query-", "auto-mode-classifier-", "program-use-case-ids-")
FIRST_PARTY_FIELDS = ("diagnostics",)


BILLING_PREFIX = "x-anthropic-billing-header:"
# Per-request stamps in the billing line: they would change a third party's prompt prefix every turn.
FIRST_PARTY_STAMPS = re.compile(r"\s*\b(?:cch|ccx_prev_\w+|ccx_prompt_\w+|ccx_turn_\w+)=[^;]*;")


def third_party_beta(beta):
    if beta is None:
        return None
    return ",".join(b for b in (s.strip() for s in beta.split(",")) if b and not b.startswith(FIRST_PARTY_BETAS))


def third_party_body(body):
    """The request as a non-Anthropic provider should see it: no first-party-only fields or stamps."""
    body = {k: v for k, v in body.items() if k not in FIRST_PARTY_FIELDS}
    system = body.get("system")
    if isinstance(system, list):
        body["system"] = [{**block, "text": FIRST_PARTY_STAMPS.sub("", block["text"])}
                          if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].startswith(BILLING_PREFIX)
                          else block for block in system]
    return body


def helper_kind(body, request_class=None):
    """Classify auxiliary requests without confusing ordinary tool-calling turns with checks."""
    if request_class in ("main", "subagent", "workflow", "compaction", "side_reply"):
        return None
    if body.get("tools") and request_class != "auxiliary":
        return None
    stops = body.get("stop_sequences") or []
    if any(stop in ("</severity>", "</block>") for stop in stops):
        return "auto"
    # Fast classifier variants omit stop sequences but retain the fixed verdict instruction.
    system = json.dumps(body.get("system") or "")
    if "<severity>" in system and "<transcript>" in json.dumps(body.get("messages") or []):
        return "auto"
    # WebFetch extraction and model-backed hooks carry no tools; tool-carrying side requests
    # (web search, side questions) keep the session's route.
    return None if body.get("tools") else "tools"


def helper_target(launch, kind, current):
    """An explicit helper pick, else the selected provider without main-turn effort."""
    picked = (_load_ccx_switch().load_routes().get(launch, {}).get("helpers") or {}).get(kind) if launch else None
    if picked:
        effort = picked.get("effort")
        if kind == "tools" and not effort:
            effort = _load_ccx_switch().lowest_effort(picked["provider"], picked["model"]) or (current or {}).get("effort")
        return provider_target(picked["provider"], picked["model"], effort)
    default = _load_ccx_switch().helper_default(kind, current["provider"] if current else "official")
    if default:
        return provider_target(default["provider"], default["model"], default["effort"])
    if not current:
        return None  # no effective main model is known
    if kind == "tools":
        return {**current, "effort": _load_ccx_switch().lowest_effort(current["provider"], current["model"]) or current.get("effort")}
    if current["provider"] == "official":
        return None  # official classifiers retain Claude Code's own model and thinking settings
    target = {**current, "effort": None}
    if kind == "auto" and target["provider"] == "openai-oauth":
        # The local Codex catalog is the access/effort authority; never guess Luna access.
        levels = _load_ccx_switch().model_efforts("openai-oauth", "gpt-6-luna")
        if levels and "low" in levels:
            target.update(model="gpt-6-luna", effort="low")
    return target


def harness_target(launch, model):
    """The target a harness launch's placeholder `model` names (see HARNESS_ROUTE): a subagent profile,
    else the main model. The helpers (HARNESS_HELPERS) start from the main model on the helper path."""
    main = route_for(launch)
    if not main:
        raise ValueError(f"No ccx launch '{launch}': start this harness with `ccx --harness ...` again.")
    return live_target(model, launch) or main


def fit_thinking(body):
    """Drop `thinking: disabled` when the effort is xhigh/max: Claude rejects that pair with a 400 (Haiku 5.5,
    Opus 5), and omitting `thinking` runs adaptive. Claude Code sends helpers with thinking disabled."""
    effort = (body.get("output_config") or {}).get("effort")
    if effort in ("xhigh", "max") and (body.get("thinking") or {}).get("type") == "disabled":
        body.pop("thinking")


def live_worker_effort(body, profile):
    """Apply the profile's current effort to a running ccx worker's next request; its context is untouched."""
    try:
        agent = _load_ccx_switch().load_agents().get(profile)
    except (Exception, SystemExit):
        return  # an unreadable agents.json keeps the effort the worker started with
    if agent is None:
        return
    config = body.setdefault("output_config", {})
    if agent.get("effort"):
        config["effort"] = agent["effort"]
    else:
        config.pop("effort", None)


def publishes_max(provider, model):
    """True when the provider lists `max` for this model (gpt-6 does; older OpenAI models stop at xhigh)."""
    try:
        return "max" in (_load_ccx_switch().model_efforts(provider, model) or ())
    except Exception:
        return False


def effort_supported(provider, model, effort):
    """False only when the provider says this model takes no such reasoning effort."""
    try:
        supported = _load_ccx_switch().model_efforts(provider, model)
    except Exception:
        return True  # unknown: keep the request as the client sent it
    if supported is None or not isinstance(effort, str):
        return True
    # Claude's max is OpenAI's xhigh, so compare in the provider's own spelling.
    candidates = {effort, openai_effort({"output_config": {"effort": effort}}) or effort} if provider in UPSTREAMS else {effort}
    return bool(candidates & set(supported))


# ---------------- OpenAI-format clients (Codex, Antigravity CLI) ----------------
# Codex speaks the Responses API (POST /ccx/<launch>/v1/responses) and agy's gateway mode chat completions
# (POST /ccx/<launch>/v1/chat/completions). Both become an Anthropic Messages request on this adapter's own
# /ccx/<launch>/v1/messages route, so routing, keys, helpers and the fallback stay in one place; the Anthropic
# stream that comes back is retold in the client's format.

OPENAI_FRONTS = {("v1", "responses"): "responses", ("v1", "chat", "completions"): "chat"}
NAMESPACE_JOIN = "__"
# Codex's own side models, as the launch's parts: its approval reviewer is the Auto pick
CODEX_PART_MODELS = {"codex-auto-review": "ccx-auto"}
JSON_ANSWER = ("Answer with one JSON object and nothing else (no prose, no code fence). It must match this "
               "JSON schema:\n")  # a Responses namespace tool `ns` + `name` travels as the Anthropic tool `ns__name`
DEFAULT_MAX_TOKENS = 32000


def _text_of(content):
    """The text of an OpenAI content value: a string, or a list of text-like parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text") or "" for p in content if isinstance(p, dict) and p.get("type") in
                         ("text", "input_text", "output_text", "summary_text", "refusal") and p.get("text"))
    return "" if content is None else str(content)


def _image_block(url):
    """An Anthropic image block for an OpenAI image URL (a data: URL or a plain one)."""
    if not isinstance(url, str) or not url:
        return None
    match = re.match(r"data:([^;,]+);base64,(.*)", url, re.S)
    if match:
        return {"type": "image", "source": {"type": "base64", "media_type": match.group(1), "data": match.group(2)}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _parts(content):
    """Anthropic content blocks for an OpenAI message's content."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text") and part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        elif kind in ("input_image", "image_url"):
            url = part.get("image_url")
            image = _image_block(url.get("url") if isinstance(url, dict) else url)
            if image:
                blocks.append(image)
    return blocks


def _arguments(raw):
    """A tool call's arguments as the object Anthropic wants (unparsable text kept under `input`)."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"input": raw}
    return value if isinstance(value, dict) else {"input": value}


class _Conversation:
    """Anthropic messages built in order, same-role neighbours merged (Anthropic alternates roles)."""

    def __init__(self):
        self.system, self.messages = [], []

    def add(self, role, blocks):
        blocks = [b for b in blocks if b]
        if not blocks:
            return
        if self.messages and self.messages[-1]["role"] == role:
            self.messages[-1]["content"] += blocks
        else:
            self.messages.append({"role": role, "content": blocks})

    def answer_orphans(self):
        """Gives every tool call that got no result a stub one (an interrupted turn): Anthropic refuses a
        tool_use the next message does not answer."""
        at = 0
        while at < len(self.messages):
            message, after = self.messages[at], self.messages[at + 1] if at + 1 < len(self.messages) else None
            at += 1
            if message["role"] != "assistant":
                continue
            answered = {b.get("tool_use_id") for b in (after or {}).get("content", ()) if b.get("type") == "tool_result"}
            stubs = [{"type": "tool_result", "tool_use_id": b["id"], "content": "(no result: the call was interrupted)"}
                     for b in message["content"] if b.get("type") == "tool_use" and b["id"] not in answered]
            if stubs and after:
                after["content"] = stubs + after["content"]
            elif stubs:
                self.messages.append({"role": "user", "content": stubs})

    def body(self, model, max_tokens, tools, effort, stream=True):
        self.answer_orphans()
        if self.messages and self.messages[0]["role"] != "user":
            self.messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
        body = {"model": model or "ccx-main", "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS),
                "messages": self.messages or [{"role": "user", "content": [{"type": "text", "text": "(empty)"}]}],
                "stream": stream}
        if self.system:
            body["system"] = "\n\n".join(t for t in self.system if t)
        if tools:
            body["tools"] = tools
        if effort:
            body["output_config"] = {"effort": effort}
        return body


def _sent_name(namespace, name):
    """The Anthropic tool name of a Responses tool: namespace and name joined, cut to the 64 characters a
    tool name may have (a hash keeps cut names apart)."""
    joined = (namespace + NAMESPACE_JOIN if namespace else "") + (name or "")
    if len(joined) > 64:
        import hashlib
        joined = joined[:55] + "_" + hashlib.sha1(joined.encode()).hexdigest()[:8]
    return joined


def _openai_code(error):
    """The OpenAI error code for an Anthropic error: the ones Codex acts on (retry, compaction)."""
    kind, message = error.get("type") or "api_error", str(error.get("message") or "").lower()
    if "prompt is too long" in message or "context length" in message or "context window" in message:
        return "context_length_exceeded"
    return {"rate_limit_error": "rate_limit_exceeded", "overloaded_error": "server_is_overloaded"}.get(kind, kind)


def _function_tool(name, description, parameters):
    schema = parameters if isinstance(parameters, dict) and parameters else {"type": "object", "properties": {}}
    return {"name": name, "description": description or "", "input_schema": schema}


def responses_request(body):
    """An Anthropic Messages body for a Responses API request (Codex), the names of its freeform tools, and
    the (namespace, name) of each tool sent under another name (_sent_name)."""
    talk, custom, names, tools = _Conversation(), set(), {}, []
    if body.get("instructions"):
        talk.system.append(_text_of(body["instructions"]))
    for tool in body.get("tools") or ():
        kind = tool.get("type")
        if kind == "function":
            sent = _sent_name(None, tool["name"])
            if sent != tool["name"]:
                names[sent] = (None, tool["name"])
            tools.append(_function_tool(sent, tool.get("description"), tool.get("parameters")))
        elif kind == "namespace":
            for inner in tool.get("tools") or ():
                if inner.get("type") == "function":
                    sent = _sent_name(tool["name"], inner["name"])
                    names[sent] = (tool["name"], inner["name"])
                    tools.append(_function_tool(sent, inner.get("description"), inner.get("parameters")))
        elif kind == "custom":
            custom.add(tool["name"])
            grammar = (tool.get("format") or {}).get("definition") or ""
            tools.append(_function_tool(tool["name"], (tool.get("description") or "")
                                        + ("\n\nThe input follows this grammar:\n" + grammar if grammar else ""),
                                        {"type": "object", "properties": {"input": {"type": "string"}},
                                         "required": ["input"]}))
        # built-in tools (web_search, image_generation, local_shell) need OpenAI's servers: left out
    items = body.get("input")
    items = [{"type": "message", "role": "user", "content": items}] if isinstance(items, str) else items or []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or ("message" if item.get("role") else None)
        if kind == "message":
            role = item.get("role")
            if role in ("system", "developer"):
                talk.system.append(_text_of(item.get("content")))
            elif role == "assistant":
                talk.add("assistant", [b for b in _parts(item.get("content")) if b["type"] == "text"])
            else:
                talk.add("user", _parts(item.get("content")))
        elif kind == "function_call":
            talk.add("assistant", [{"type": "tool_use", "id": item.get("call_id") or item.get("id") or "call",
                                    "name": _sent_name(item.get("namespace"), item.get("name") or ""),
                                    "input": _arguments(item.get("arguments"))}])
        elif kind == "custom_tool_call":
            talk.add("assistant", [{"type": "tool_use", "id": item.get("call_id") or item.get("id") or "call",
                                    "name": item.get("name") or "", "input": {"input": item.get("input") or ""}}])
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output = item.get("output")  # text, or parts (view_image answers with an image)
            content = _parts(output) if isinstance(output, list) else _text_of(output)
            talk.add("user", [{"type": "tool_result", "tool_use_id": item.get("call_id") or "call",
                               "content": content or "(no output)"}])
        # reasoning items carry no signature another provider could verify: dropped, as is OpenAI-only state
    form = (body.get("text") or {}).get("format") or {}
    if form.get("type") == "json_schema":
        talk.system.append(JSON_ANSWER + json.dumps(form.get("schema") or {}))
    elif form.get("type") == "json_object":
        talk.system.append(JSON_ANSWER + "{}")
    effort = (body.get("reasoning") or {}).get("effort")
    model = CODEX_PART_MODELS.get(body.get("model"), body.get("model"))
    anthropic = talk.body(model, body.get("max_output_tokens"), tools, effort, stream=True)
    choice = body.get("tool_choice")
    if choice == "required":
        anthropic["tool_choice"] = {"type": "any"}
    elif choice == "none":
        anthropic.pop("tools", None)
    elif isinstance(choice, dict) and choice.get("type") in ("function", "custom") and choice.get("name") and tools:
        anthropic["tool_choice"] = {"type": "tool", "name": _sent_name(choice.get("namespace"), choice["name"])}
    return anthropic, custom, names


def chat_request(body):
    """An Anthropic Messages body for a chat completions request (agy's gateway mode)."""
    talk, tools = _Conversation(), []
    for tool in body.get("tools") or ():
        function = tool.get("function") if tool.get("type") == "function" else None
        if function and function.get("name"):
            tools.append(_function_tool(function["name"], function.get("description"), function.get("parameters")))
    for message in body.get("messages") or ():
        role = message.get("role")
        if role in ("system", "developer"):
            talk.system.append(_text_of(message.get("content")))
        elif role == "assistant":
            blocks = [b for b in _parts(message.get("content")) if b["type"] == "text"]
            for call in message.get("tool_calls") or ():
                function = call.get("function") or {}
                blocks.append({"type": "tool_use", "id": call.get("id") or "call", "name": function.get("name") or "",
                               "input": _arguments(function.get("arguments"))})
            talk.add("assistant", blocks)
        elif role == "tool":
            talk.add("user", [{"type": "tool_result", "tool_use_id": message.get("tool_call_id") or "call",
                               "content": _text_of(message.get("content")) or "(no output)"}])
        else:
            talk.add("user", _parts(message.get("content")))
    form = body.get("response_format") or {}
    if form.get("type") == "json_schema":
        talk.system.append(JSON_ANSWER + json.dumps((form.get("json_schema") or {}).get("schema") or {}))
    elif form.get("type") == "json_object":
        talk.system.append(JSON_ANSWER + "{}")
    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens")
    anthropic = talk.body(body.get("model"), max_tokens, tools, body.get("reasoning_effort"), stream=True)
    choice = body.get("tool_choice")
    if choice == "required":
        anthropic["tool_choice"] = {"type": "any"}
    elif choice == "none":
        anthropic.pop("tools", None)
    elif isinstance(choice, dict) and (choice.get("function") or {}).get("name") and tools:
        anthropic["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    return anthropic


def anthropic_sse(response):
    """The events of an Anthropic Messages stream, as dicts."""
    data = []
    for raw in response:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            try:
                yield json.loads("\n".join(data))
            except ValueError:
                pass
            data = []
    if data:
        try:
            yield json.loads("\n".join(data))
        except ValueError:
            pass


def _usage(start, delta):
    """Input and output token counts from message_start and message_delta usage."""
    used = {**(start or {}), **{k: v for k, v in (delta or {}).items() if v is not None}}
    fresh = used.get("input_tokens") or 0
    cached = (used.get("cache_read_input_tokens") or 0)
    written = used.get("cache_creation_input_tokens") or 0
    return fresh + cached + written, cached, used.get("output_tokens") or 0


STOP_TO_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length",
                  "refusal": "content_filter"}


def json_answer(text):
    """The JSON object in a model's answer to a JSON-only request (a code fence or a preamble removed)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return text
    try:
        return json.dumps(json.loads(text[start:end + 1]))
    except ValueError:
        return text


def responses_events(events, model, custom=(), json_mode=False, names=None):
    """Retells an Anthropic stream as Responses API events (what Codex reads). Ends with response.completed
    (response.incomplete when the output limit cut it), or response.failed on an error or a stream that
    stopped early: Codex retries a stream left open.
    json_mode: the request asked for JSON only; its text goes out whole, cleaned to the JSON object.
    names: responses_request's renamed tools, to give each call its own name and namespace back."""
    rid = "resp_" + uuid.uuid4().hex
    shell = {"id": rid, "object": "response", "created_at": int(time.time()), "model": model}
    yield {"type": "response.created", "response": {**shell, "status": "in_progress", "output": []}}
    output, open_blocks, start_usage, delta_usage, finished, stop = [], {}, {}, {}, False, None
    for event in events:
        kind = event.get("type")
        if kind == "message_start":
            start_usage = (event.get("message") or {}).get("usage") or {}
        elif kind == "content_block_start":
            block, index = event.get("content_block") or {}, event.get("index", 0)
            position = len(output) + len(open_blocks)
            if block.get("type") == "text":
                item = {"type": "message", "id": "msg_" + uuid.uuid4().hex, "role": "assistant",
                        "status": "in_progress", "content": []}
                state = {"item": item, "text": block.get("text") or "", "at": position}
                yield {"type": "response.output_item.added", "output_index": position, "item": item}
                yield {"type": "response.content_part.added", "item_id": item["id"], "output_index": position,
                       "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}
            elif block.get("type") in ("thinking", "redacted_thinking"):
                item = {"type": "reasoning", "id": "rs_" + uuid.uuid4().hex, "summary": []}
                state = {"item": item, "text": block.get("thinking") or "", "at": position}
                yield {"type": "response.output_item.added", "output_index": position, "item": item}
            elif block.get("type") == "tool_use":
                name = block.get("name") or ""
                if name in custom:
                    item = {"type": "custom_tool_call", "id": "ctc_" + uuid.uuid4().hex,
                            "call_id": block.get("id") or "call_" + uuid.uuid4().hex, "name": name,
                            "status": "in_progress", "input": ""}
                else:
                    item = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex,
                            "call_id": block.get("id") or "call_" + uuid.uuid4().hex, "name": name,
                            "status": "in_progress", "arguments": ""}
                    if name in (names or {}):
                        namespace, item["name"] = names[name]
                        if namespace:
                            item["namespace"] = namespace
                state = {"item": item, "text": json.dumps(block["input"]) if block.get("input") else "", "at": position}
                yield {"type": "response.output_item.added", "output_index": position, "item": item}
            else:
                continue
            open_blocks[index] = state
        elif kind == "content_block_delta":
            state, delta = open_blocks.get(event.get("index", 0)), event.get("delta") or {}
            if not state:
                continue
            item = state["item"]
            if delta.get("type") == "text_delta" and delta.get("text"):
                state["text"] += delta["text"]
                if json_mode:
                    continue
                yield {"type": "response.output_text.delta", "item_id": item["id"], "output_index": state["at"],
                       "content_index": 0, "delta": delta["text"]}
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                state["text"] += delta["thinking"]
                yield {"type": "response.reasoning_summary_text.delta", "item_id": item["id"],
                       "output_index": state["at"], "summary_index": 0, "delta": delta["thinking"]}
            elif delta.get("type") == "input_json_delta":
                state["text"] += delta.get("partial_json") or ""
        elif kind == "content_block_stop":
            state = open_blocks.pop(event.get("index", 0), None)
            if not state:
                continue
            item = state["item"]
            if item["type"] == "message":
                if json_mode:
                    state["text"] = json_answer(state["text"])
                    yield {"type": "response.output_text.delta", "item_id": item["id"], "output_index": state["at"],
                           "content_index": 0, "delta": state["text"]}
                part = {"type": "output_text", "text": state["text"], "annotations": []}
                yield {"type": "response.output_text.done", "item_id": item["id"], "output_index": state["at"],
                       "content_index": 0, "text": state["text"]}
                yield {"type": "response.content_part.done", "item_id": item["id"], "output_index": state["at"],
                       "content_index": 0, "part": part}
                item = {**item, "status": "completed", "content": [part]}
            elif item["type"] == "reasoning":
                item = {**item, "summary": [{"type": "summary_text", "text": state["text"]}] if state["text"] else [],
                        "encrypted_content": None}
            elif item["type"] == "custom_tool_call":
                item = {**item, "status": "completed", "input": _arguments(state["text"] or "{}").get("input", "")}
            else:
                item = {**item, "status": "completed", "arguments": state["text"] or "{}"}
            output.append(item)
            yield {"type": "response.output_item.done", "output_index": state["at"], "item": item}
        elif kind == "message_delta":
            delta_usage = event.get("usage") or {}
            stop = (event.get("delta") or {}).get("stop_reason") or stop
        elif kind == "message_stop":
            finished = True
            break
        elif kind == "error":
            error = event.get("error") or {}
            yield {"type": "response.failed", "response": {**shell, "status": "failed", "output": output,
                   "error": {"code": _openai_code(error), "message": error.get("message") or "Upstream error"}}}
            return
    if not finished:
        yield {"type": "response.failed", "response": {**shell, "status": "failed", "output": output,
               "error": {"code": "stream_ended", "message": "The model's stream ended before it finished."}}}
        return
    total_in, cached, total_out = _usage(start_usage, delta_usage)
    cut = stop == "max_tokens"  # the output limit: not a finished answer
    yield {"type": "response.incomplete" if cut else "response.completed", "response": {
        **shell, "status": "incomplete" if cut else "completed", "output": output,
        **({"incomplete_details": {"reason": "max_output_tokens"}} if cut else {}), "usage": {
            "input_tokens": total_in, "input_tokens_details": {"cached_tokens": cached},
            "output_tokens": total_out, "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": total_in + total_out}}}


def chat_chunks(events, model, include_usage=False):
    """Retells an Anthropic stream as chat completion chunks (what agy's gateway mode reads)."""
    cid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    chunk = lambda delta, finish=None: {"id": cid, "object": "chat.completion.chunk", "created": created,
                                        "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    yield chunk({"role": "assistant", "content": ""})
    calls, start_usage, delta_usage, stop, finished = {}, {}, {}, None, False
    for event in events:
        kind = event.get("type")
        if kind == "message_start":
            start_usage = (event.get("message") or {}).get("usage") or {}
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                at = calls[event.get("index", 0)] = len(calls)
                yield chunk({"tool_calls": [{"index": at, "id": block.get("id") or "call_" + uuid.uuid4().hex,
                                             "type": "function", "function": {"name": block.get("name") or "",
                                                                              "arguments": ""}}]})
            elif block.get("type") == "text" and block.get("text"):
                yield chunk({"content": block["text"]})
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield chunk({"content": delta["text"]})
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                yield chunk({"reasoning_content": delta["thinking"]})
            elif delta.get("type") == "input_json_delta" and event.get("index", 0) in calls:
                yield chunk({"tool_calls": [{"index": calls[event.get("index", 0)],
                                             "function": {"arguments": delta.get("partial_json") or ""}}]})
        elif kind == "message_delta":
            delta_usage = event.get("usage") or {}
            stop = (event.get("delta") or {}).get("stop_reason") or stop
        elif kind == "message_stop":
            finished = True
            break
        elif kind == "error":
            error = event.get("error") or {}
            yield {"error": {"type": error.get("type") or "api_error", "message": error.get("message") or "Upstream error"}}
            return
    if not finished:  # a stream cut short is not a finished answer
        yield {"error": {"type": "api_error", "message": "The model's stream ended before it finished."}}
        return
    yield chunk({}, STOP_TO_FINISH.get(stop, "tool_calls" if calls else "stop"))
    if include_usage:
        total_in, cached, total_out = _usage(start_usage, delta_usage)
        yield {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [],
               "usage": {"prompt_tokens": total_in, "completion_tokens": total_out, "total_tokens": total_in + total_out,
                         "prompt_tokens_details": {"cached_tokens": cached}}}


def chat_completion(chunks):
    """One chat.completion object from chat_chunks (a client that did not ask to stream)."""
    text, reasoning, calls, finish, usage_row, model, cid = "", "", {}, "stop", None, "", ""
    for chunk in chunks:
        if "error" in chunk:
            raise UpstreamFailure(chunk["error"].get("message") or "Upstream error")
        cid, model = chunk.get("id", cid), chunk.get("model", model)
        usage_row = chunk.get("usage") or usage_row
        for choice in chunk.get("choices") or ():
            delta = choice.get("delta") or {}
            text += delta.get("content") or ""
            reasoning += delta.get("reasoning_content") or ""
            for call in delta.get("tool_calls") or ():
                row = calls.setdefault(call["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                row["id"] = call.get("id") or row["id"]
                row["function"]["name"] += (call.get("function") or {}).get("name") or ""
                row["function"]["arguments"] += (call.get("function") or {}).get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    message = {"role": "assistant", "content": text or None}
    if reasoning:
        message["reasoning_content"] = reasoning
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}], **({"usage": usage_row} if usage_row else {})}


class UpstreamFailure(Exception):
    """The model's stream reported an error (shown to the client as is)."""
    public = True


def codex_request_kind(headers):
    """Codex's x-codex-turn-metadata request kind ("turn", "compaction", ...), or ''."""
    try:
        return str(json.loads(headers.get("x-codex-turn-metadata") or "{}").get("request_kind") or "")
    except (ValueError, AttributeError):
        return ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log prompts or credentials.

    def send_json(self, status, body, retry=True, headers=None):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if not retry and 'x-should-retry' not in (headers or {}):  # the Anthropic SDK skips its retries: a spent quota does not come back in seconds
            self.send_header("x-should-retry", "false")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def parse_request(self):
        ok = super().parse_request()
        if ok and self.path.startswith(OFFICIAL_PROXIED + "/"):
            self.path = self.path[len(OFFICIAL_PROXIED):]
        return ok

    def relay_foreign(self, method):
        """A program started inside a ccx session inherits HTTP_PROXY: its plain-http requests pass through."""
        # shortcut: GET and POST only, add other methods when a tool needs them over plain http
        if not self.path.startswith("http://"):
            return False
        length = int(self.headers.get("Content-Length") or 0)
        self.forward_official(method, self.rfile.read(length) if length else None, url=self.path)
        return True

    def do_CONNECT(self):
        """Claude Code's https traffic, which follows HTTP_PROXY here too: a blind tunnel, nothing read."""
        host, _, port = self.path.rpartition(":")
        try:
            upstream = socket.create_connection((host.strip("[]"), int(port)), timeout=30)
        except (OSError, ValueError):
            self.send_error(502)
            return
        self.close_connection = True
        with upstream:
            try:
                self.send_response(200, "Connection Established")
                self.end_headers()
                upstream.settimeout(None)
                while True:
                    ready = select.select([self.connection, upstream], [], [], 3600)[0]
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is self.connection else self.connection).sendall(data)
            except OSError:
                pass

    def forward_live(self, live, body):
        """An in-process ccx subagent on an Anthropic-format provider: its /v1/messages with the profile's key."""
        headers = {k: v for k, v in self.headers.items() if k.lower() in ("anthropic-version", "accept")}
        beta = third_party_beta(self.headers.get("anthropic-beta"))
        if beta:
            headers["anthropic-beta"] = beta
        headers.update({"Content-Type": "application/json", "Authorization": f"Bearer {live['key']}", "User-Agent": "Mozilla/5.0 ccx"})
        if live.get("header") == "x-api-key":
            headers["x-api-key"] = live["key"]
        if live["provider"] == "opencode-go" and cache_key(body):
            headers["x-opencode-session"] = cache_key(body)  # Go routes by it; one per session keeps its cache
        self.forward_official("POST", json.dumps(body).encode(), url=live["base"].rstrip("/") + "/v1/messages",
                              headers=headers, provider=live["provider"])

    def forward_official(self, method, data=None, url=None, headers=None, provider=None):
        """Passes a request to Anthropic unchanged (or to `url` with `headers`) and streams its answer back as it arrives."""
        if self.headers.get("Origin"):
            self.send_json(403, {"error": "Browser requests are not allowed"})
            return
        headers = headers or {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        request = urllib.request.Request(url or OFFICIAL_UPSTREAM + self.path[len("/" + OFFICIAL_ROUTE):], data=data,
                                         headers=headers, method=method)
        prefix = b""
        try:
            response = urllib.request.urlopen(request, timeout=600)
        except urllib.error.HTTPError as exc:
            response = exc
            if provider:
                # A /model-switched provider's spent plan gets Claude's native usage-limit headers and reset.
                prefix = exc.read(65536)
                status, error, mapped = provider_error(exc, provider, prefix.decode("utf-8", "replace"))
                if status == 429:
                    with exc:
                        self.send_json(status, error, headers=mapped)
                    return
        except OSError as exc:
            self.send_json(502, {"type": "error", "error": {"type": "api_error", "message": f"Anthropic unreachable ({type(exc).__name__})"}})
            return
        with response:
            self.send_response(response.status if hasattr(response, "status") else response.code)
            for k, v in response.headers.items():
                if k.lower() not in HOP_HEADERS and k.lower() != "content-encoding":
                    self.send_header(k, v)
            self.end_headers()
            try:
                if prefix:
                    self.wfile.write(prefix)
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        if self.relay_foreign("GET"):
            return
        refresh_upstreams()
        path = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
        if path[0] == OFFICIAL_ROUTE:
            if path[1:] == ["v1", "models"] and route_for(self.headers.get("x-ccx-launch")):
                self.gateway_models(OFFICIAL_ROUTE)
            else:
                self.forward_official("GET")
            return
        if self.path == "/health":
            self.send_json(200, IDENTITY)
        elif len(path) == 3 and path[0] in (*UPSTREAMS, CLI_ROUTE, AGY_ROUTE) and path[1:] == ["v1", "models"]:
            self.gateway_models(path[0])
        else:
            self.send_json(404, {"error": "Not found"})

    def gateway_models(self, provider):
        """Claude Code's /model discovery: this route's models, or none when it cannot ask."""
        override = route_for(self.headers.get("x-ccx-launch"))
        if override:
            # /model's Model tab pinned this session's model: list exactly the one it serves, so /model
            # cannot offer picks the adapter would ignore.
            self.send_json(200, {"data": [{"id": override["model"], "display_name": override["model"]}],
                                 "has_more": False})
            return
        if provider in (CLI_ROUTE, AGY_ROUTE):
            # ccx-switch.py sits beside this file and owns the plan filter; without it,
            # listing the CLI's models could offer ones the subscription cannot use.
            self.send_json(200, {"data": [{"id": m["id"], "display_name": m["id"]} for m in ccx_models(provider)],
                                 "has_more": False})
            return
        if self.headers.get("Origin"):
            self.send_json(403, {"error": "Browser requests are not allowed"})
            return
        key = self.headers.get("x-api-key") or self.headers.get("Authorization", "").removeprefix("Bearer ")
        headers = {"Authorization": "Bearer " + key, "x-api-key": key}
        models = []
        try:
            request = urllib.request.Request(UPSTREAMS[provider] + "/models", headers=headers)
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
            for model in payload.get("data") or []:
                if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"]:
                    models.append({"id": model["id"], "display_name": model.get("name") or model["id"]})
        except Exception:
            pass  # discovery is optional: Claude Code keeps its env-defined options
        self.send_json(200, {"data": models, "has_more": False})

    def do_POST(self):
        if self.relay_foreign("POST"):
            return
        refresh_upstreams()
        streaming = False
        try:
            route = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
            harness_launch = None
            if route[0] == HARNESS_ROUTE and len(route) > 2 and tuple(route[2:]) in OPENAI_FRONTS \
                    and HARNESS_LAUNCH.fullmatch(route[1]):
                self.answer_openai(route[1], OPENAI_FRONTS[tuple(route[2:])])
                return
            if route[0] == HARNESS_ROUTE:
                if len(route) != 4 or route[2:] != ["v1", "messages"] or not HARNESS_LAUNCH.fullmatch(route[1]):
                    self.send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": "Not found"}})
                    return
                harness_launch, route = route[1], [HARNESS_ROUTE, "v1", "messages"]
            if route[0] == OFFICIAL_ROUTE and route[1:] != ["v1", "messages"]:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 32 * 1024 * 1024:
                    raise ValueError("Invalid request size (maximum 32 MiB)")
                self.forward_official("POST", self.rfile.read(length) if length else b"")
                return
            if len(route) != 3 or route[0] not in (*UPSTREAMS, CLI_ROUTE, AGY_ROUTE, OFFICIAL_ROUTE, HARNESS_ROUTE) \
                    or route[1:] != ["v1", "messages"]:
                self.send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": "Not found"}})
                return
            if self.headers.get("Origin"):
                self.send_json(403, {"error": "Browser requests are not allowed"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32 * 1024 * 1024:
                raise ValueError("Invalid request size (maximum 32 MiB)")
            raw = self.rfile.read(length)
            body = json.loads(raw)
            if harness_launch:
                # a helper placeholder takes the helper path below, exactly as Claude Code's helpers do
                launch, harness_main = harness_launch, harness_target(harness_launch, None)
                kind = HARNESS_HELPERS.get(body.get("model"))
                override = harness_main if kind else harness_target(launch, body.get("model"))
            else:
                kind = helper_kind(body, self.headers.get("x-claude-code-request-class"))
                if not kind and self.headers.get("x-ccx-worker-profile"):
                    live_worker_effort(body, self.headers["x-ccx-worker-profile"])
                launch = self.headers.get("x-ccx-launch")
                # All model-backed helpers follow the effective provider, not the launch's /official URL.
                override = live_target(body.get("model"), launch) or route_for(launch)
            if kind:
                current = override or ({"provider": route[0], "model": body.get("model"), "effort": None,
                                       "format": "chat"} if route[0] != OFFICIAL_ROUTE else None)
                selected = helper_target(launch, kind, current)
                if selected and current is not override and selected.get("key") is None:
                    # A direct non-official route already carries its key in the request headers.
                    override = None
                    body["model"] = selected["model"]
                else:
                    override = selected or (harness_main if harness_launch else None)
                if selected and selected["provider"] != "official":
                    # Auto effort is independent; Tools falls back to the main request's effort.
                    if body.get("output_config") and (kind != "tools" or selected.get("effort")):
                        body["output_config"] = {k: v for k, v in body["output_config"].items() if k != "effort"}
                    body["max_tokens"] = max(body.get("max_tokens") or 0, 4096)
                    if selected.get("effort"):
                        body["output_config"] = {**(body.get("output_config") or {}), "effort": selected["effort"]}
                    elif kind == "auto" and (body.get("thinking") or {}).get("type") == "disabled" and selected["provider"] == "openai-oauth":
                        body["output_config"] = {**(body.get("output_config") or {}), "effort": "low"}
            if override:
                body["model"] = override["model"]
                if override.get("effort"):
                    body["output_config"] = {**(body.get("output_config") or {}), "effort": override["effort"]}
                fit_thinking(body)
                if override["format"] == "official":
                    if harness_launch:
                        raise ValueError("Claude official (the claude.ai login) serves Claude Code only; "
                                         "pick another provider for this part.")
                    if route[0] != OFFICIAL_ROUTE:
                        raise ValueError("Claude official needs a session started with `ccx official`.")
                    self.forward_official("POST", json.dumps(body).encode())
                    return
                if override["format"] == "anthropic":
                    self.forward_live(override, third_party_body(body))
                    return
            if route[0] == OFFICIAL_ROUTE and not override:
                self.forward_official("POST", raw)
                return
            body = third_party_body(body)  # everything below leaves Anthropic
            active = override["provider"] if override else route[0]
            if active in (CLI_ROUTE, AGY_ROUTE):
                self.answer_cli(body, agy=active == AGY_ROUTE)
                return
            request = to_chat(body)
            if active == "openai-oauth" and cache_key(body):
                request["prompt_cache_key"] = cache_key(body)
            raw_effort = (body.get("output_config") or {}).get("effort")
            if raw_effort == "max" and publishes_max(active, body["model"]):
                request["reasoning_effort"] = "max"
            if request.get("reasoning_effort") and raw_effort and not effort_supported(active, body["model"], raw_effort):
                # MiMo and similar models reject a reasoning effort they cannot adjust.
                request.pop("reasoning_effort")
            key = override["key"] if override else (
                self.headers.get("x-api-key") or self.headers.get("Authorization", "").removeprefix("Bearer "))
            if not key:
                self.send_json(401, {"type": "error", "error": {"type": "authentication_error", "message": "API key required"}})
                return
            target = None if kind else fallback_for(launch)
            served = body["model"]  # the reply is attributed to the model that answered it
            try:
                response = open_upstream(active, request, key, strict=bool(target), session=cache_key(body))
            except OSError as exc:
                # The session's fallback (ccx fallback, off unless chosen) gets one try at what the route refused.
                if not target or (isinstance(exc, urllib.error.HTTPError) and exc.code not in (402, 429) and exc.code < 500):
                    raise
                request["model"] = target["model"]
                # Caches never cross providers or models, but an openai-oauth fallback still needs the key so later turns hit what this one fills.
                request.pop("prompt_cache_key", None)
                if target["provider"] == "openai-oauth" and cache_key(body):
                    request["prompt_cache_key"] = cache_key(body)
                if target.get("effort"):
                    raw_effort = target["effort"]
                    request["reasoning_effort"] = openai_effort({"output_config": {"effort": raw_effort}})
                if request.get("reasoning_effort") and raw_effort and not effort_supported(target["provider"], target["model"], raw_effort):
                    request.pop("reasoning_effort")
                active = target['provider']
                response = open_upstream(active, request, target["key"], session=cache_key(body))
                served = target["model"]
                note_fallback(launch, target)
            with response:
                if request["stream"]:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    streaming = True
                    for event in stream_events(response, served):
                        self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                        self.wfile.flush()
                else:
                    self.send_json(200, to_message(json.load(response), served))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (Exception, SystemExit) as exc:
            # ccx-switch helpers die() with SystemExit; uncaught, it ended the thread and the client saw ECONNRESET.
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else getattr(exc, "status_code", None) or (400 if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)) and not streaming else 502)
            # A ValueError describes the request this adapter built or converted, never
            # credentials or upstream payloads: name the cause so the fix is diagnosable.
            error = {"type": "error", "error": {"type": "invalid_request_error" if status == 400 else "api_error",
                "message": str(exc) if getattr(exc, "public", False) or isinstance(exc, (ValueError, SystemExit)) else
                f"Adapter request failed ({type(exc).__name__}); check model access and provider availability."}}
            response_headers = {}
            if isinstance(exc, urllib.error.HTTPError):
                status, error, response_headers = provider_error(exc, active)
            if streaming:
                self.wfile.write(("event: error\ndata: " + json.dumps(error) + "\n\n").encode())
            else:
                self.send_json(status, error, headers=response_headers)

    def answer_openai(self, launch, front):
        """Codex (front "responses") or agy (front "chat") on harness launch `launch`: the request runs as an
        Anthropic Messages request on this adapter's own harness route, and the reply is retold (OPENAI_FRONTS)."""
        if self.headers.get("Origin"):
            self.send_json(403, {"error": {"type": "forbidden", "message": "Browser requests are not allowed"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 32 * 1024 * 1024:
            raise ValueError("Invalid request size (maximum 32 MiB)")
        body = json.loads(self.rfile.read(length))
        custom = ()
        if front == "responses":
            anthropic, custom, names = responses_request(body)
            if codex_request_kind(self.headers) == "compaction":
                anthropic["model"] = "ccx-tools"  # Codex's own compaction is a side task: the Tools model, else main
        else:
            anthropic = chat_request(body)
        model = body.get("model") or "ccx-main"
        key = self.headers.get("Authorization", "").removeprefix("Bearer ").strip() or "ccx-local"
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/{HARNESS_ROUTE}/{launch}/v1/messages",
                                         data=json.dumps(anthropic).encode(),
                                         headers={"Content-Type": "application/json", "x-api-key": key})
        try:
            response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=900)
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read()).get("error") or {}
            except ValueError:
                error = {}
            headers = {k: exc.headers[k] for k in ("retry-after",) if exc.headers.get(k)}
            self.send_json(exc.code, {"error": {"type": error.get("type") or "api_error", "code": _openai_code(error),
                                                "message": error.get("message") or f"HTTP {exc.code}"}}, headers=headers)
            return
        with response:
            events = anthropic_sse(response)
            if front == "responses":
                form = ((body.get("text") or {}).get("format") or {}).get("type")
                stream = responses_events(events, model, custom, json_mode=form in ("json_schema", "json_object"),
                                          names=names)
            else:
                stream = chat_chunks(events, model, bool((body.get("stream_options") or {}).get("include_usage")))
            if not body.get("stream"):
                if front == "chat":
                    self.send_json(200, chat_completion(stream))
                    return
                final = None
                for event in stream:
                    final = event.get("response") if event["type"] in (
                        "response.completed", "response.incomplete", "response.failed") else final
                if final and final.get("status") != "failed":
                    self.send_json(200, final)
                else:
                    self.send_json(502, {"error": (final or {}).get("error") or {"code": "api_error",
                                                                                 "message": "No response"}})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                for event in stream:
                    if front == "responses":
                        self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                    else:
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:  # the stream has started: the failure travels inside it
                message = f"Adapter stream failed ({type(exc).__name__})."
                if front == "responses":
                    failure = {"type": "response.failed", "response": {"id": "resp_failed", "status": "failed",
                                                                       "error": {"code": "api_error", "message": message}}}
                    self.wfile.write(("event: response.failed\ndata: " + json.dumps(failure) + "\n\n").encode())
                else:
                    self.wfile.write(("data: " + json.dumps({"error": {"type": "api_error", "message": message}}) + "\n\n").encode())
                return
            if front == "chat":
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

    def answer_cli(self, body, agy=False):
        """Streams Command Code's reply as it arrives, with pings while it is quiet: straight from
        /alpha/generate on the CLI's login, or (CCX_CMDC_TEXT=1) one cmdc run with the text tool protocol.
        agy: Google's Antigravity CLI, always one run with the text tool protocol."""
        text_protocol = agy or os.environ.get("CCX_CMDC_TEXT") == "1"
        source = agy_events if agy else None
        if text_protocol:
            to_prompt(body)  # reject bad input before any response is sent
        if not body.get("stream"):
            self.send_json(200, commandcode_cli(body, source) if text_protocol else collect(generate_stream(body), body["model"]))
            return
        events, gone = queue.Queue(), threading.Event()
        label = "Antigravity CLI" if agy else "Command Code CLI"

        def run():
            stream = cli_stream(body, source) if text_protocol else generate_stream(body)
            try:
                for event in stream:
                    if gone.is_set():
                        break
                    events.put(event)
            except Exception as exc:  # reported to the client as a stream error event
                events.put({"type": "error", "error": {"type": "api_error",
                            "message": f"{label} request failed: {type(exc).__name__}: {exc}"[:400]}})
            finally:
                stream.close()  # a client that left kills the CLI run
                events.put(None)

        threading.Thread(target=run, daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                try:
                    event = events.get(timeout=5)
                except queue.Empty:
                    event = {"type": "ping"}
                if event is None:
                    break
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        finally:
            gone.set()


if __name__ == "__main__":
    if os.environ.get("HTTP_PROXY", "").endswith(f":{PORT}"):
        del os.environ["HTTP_PROXY"]  # started from inside a ccx session: never proxy through itself
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
