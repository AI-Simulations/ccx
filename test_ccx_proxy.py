"""Offline protocol regression check; no credentials or external generation."""
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("proxy", Path(__file__).with_name("ccx-anthropic-proxy.py"))
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

key = proxy.cache_key({"metadata": {"user_id": "user_x_session_1"}})
assert key and key == proxy.cache_key({"metadata": {"user_id": "user_x_session_1"}}), "One stable key per session"
assert key != proxy.cache_key({"metadata": {"user_id": "user_x_session_2"}}) and proxy.cache_key({}) is None

# Classifier detection uses its protocol, not every tool-less request.
check_body = {"model": "claude-sonnet-5[1m]", "max_tokens": 64, "thinking": {"type": "disabled"},
              "stop_sequences": ["</severity>"], "messages": [{"role": "user", "content": "classify"}]}
assert proxy.helper_kind(check_body) == "auto"
assert proxy.helper_kind({**check_body, "stop_sequences": ["</block>"]}) == "auto"
assert proxy.helper_kind({**check_body, "stop_sequences": []}) == "tools"
assert proxy.helper_kind(check_body, "main") is None
assert proxy.helper_kind(check_body, "compaction") is None
assert proxy.helper_kind({**check_body, "tools": [{"name": "Read"}]}) is None
assert proxy.helper_kind({**check_body, "tools": [{"name": "Read"}]}, "auxiliary") == "auto"
# First-party sessions tag web search, side questions and other tool-carrying side requests
# `auxiliary`: they are not WebFetch/hook extraction and keep their own route.
assert proxy.helper_kind({**check_body, "stop_sequences": [], "tools": [{"type": "web_search_20250305", "name": "web_search"}]}, "auxiliary") is None
assert proxy.helper_kind({**check_body, "stop_sequences": []}, "auxiliary") == "tools"

# Tools keep the main model, default low where supported, and retain explicit choices.
from unittest.mock import patch
from types import SimpleNamespace
for lowest, inherited, expected in [('low', 'high', 'low'), (None, 'high', 'high'), (None, None, None)]:
    fake = SimpleNamespace(load_routes=lambda: {}, lowest_effort=lambda *args: lowest, helper_default=lambda *args: None)
    with patch.object(proxy, '_load_ccx_switch', return_value=fake):
        target = proxy.helper_target('L', 'tools', {'provider': 'openai-oauth', 'model': 'main-model', 'effort': inherited})
    assert target['model'] == 'main-model' and target['effort'] == expected
fake = SimpleNamespace(load_routes=lambda: {'L': {'helpers': {'tools': {'provider': 'openai-oauth', 'model': 'picked', 'effort': 'medium'}}}},
                       lowest_effort=lambda *args: 'low')
with patch.object(proxy, '_load_ccx_switch', return_value=fake), patch.object(proxy, 'provider_target', side_effect=lambda p, m, e: {'model': m, 'effort': e}):
    assert proxy.helper_target('L', 'tools', {'effort': 'high'}) == {'model': 'picked', 'effort': 'medium'}

# Safety defaults follow the effective provider; explicit launch selections win.
expected_auto = {
    'openai-oauth': ('gpt-6.1-terra', 'low'),
    'commandcode': ('deepseek/deepseek-v4.1-flash', 'high'),
    'commandcode-cli': ('deepseek/deepseek-v4.1-flash', 'high'),
    'opencode-go': ('deepseek-v4.1-flash', 'high'),
    'opencode-zen': ('deepseek-v4.1-flash', 'high'),
    'nvidia': ('deepseek-ai/deepseek-v4.1-flash', 'high'),
    'official': ('claude-haiku-5-5', 'low'),
}
switch = proxy._load_ccx_switch()
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, 'helper-defaults.json').write_text(json.dumps({'auto': {
        p: {'model': m, 'effort': e} for p, (m, e) in expected_auto.items()}}), encoding='utf-8')
    with patch.object(switch, 'CFG_DIR', tmp), patch.object(switch, 'load_routes', return_value={}), \
            patch.object(proxy, 'provider_target', side_effect=lambda p, m, e: {'provider': p, 'model': m, 'effort': e}):
        for provider, (model, effort) in expected_auto.items():
            target = proxy.helper_target('L', 'auto', {'provider': provider, 'model': 'main'})
            assert target == {'provider': provider, 'model': model, 'effort': effort}
        assert proxy.helper_target('L', 'auto', None)['model'] == 'claude-haiku-5-5'
        explicit = {'L': {'helpers': {'auto': {'provider': 'openai-oauth', 'model': 'custom', 'effort': 'high'}}}}
        with patch.object(switch, 'load_routes', return_value=explicit):
            assert proxy.helper_target('L', 'auto', {'provider': 'nvidia'})['model'] == 'custom'
proxy._ccx_switch = None

# The CLI route's discovery list comes from ccx-switch.py beside the adapter.
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, "ccx-switch.py").write_text("def get_models(provider):\n    return [{'id': provider + '-model'}]\n", encoding="utf-8")
    original_file, proxy.__file__ = proxy.__file__, str(Path(tmp) / "ccx-anthropic-proxy.py")
    try:
        assert proxy.ccx_models("cli") == [{"id": "cli-model"}]
        proxy._ccx_switch = None  # a fresh adapter process
        Path(tmp, "ccx-switch.py").unlink()
        assert proxy.ccx_models("cli") == [], "Without the sibling script discovery is empty"
    finally:
        proxy.__file__ = original_file
        proxy._ccx_switch = None

# An in-process ccx worker names its profile by model; the launch's own profiles win over the shared ones.
class _Live:
    PROVIDERS = {"openai-oauth": {"format": "openai", "key": "oauth"}, "minimax": {"format": "anthropic", "base": "https://mm/anthropic", "key": "minimax"},
                 "opencode-go": {"format": "anthropic", "base": "https://go", "key": "opencode-go", "header": "x-api-key"}}
    def _session_agents(self, launch):
        return {"sub": {"provider": "minimax", "model": "MiniMax-M3", "effort": "high"}} if launch == "L1" else None
    def load_agents(self, session=True):
        return {"sub": {"provider": "openai-oauth", "model": "gpt-6.1-sol"}, "go": {"provider": "opencode-go", "model": "glm-5.3"},
                "claude": {"provider": "official"}, "bare": {"provider": "minimax"}}
    def auth_key(self, name):
        return name + "-key"
    def go_messages(self, model):
        return False
proxy._ccx_switch = _Live()
assert proxy.live_target("claude-opus-5-5", "L1") is None, "Ordinary models pass through"
assert proxy.live_target("ccx-live-sub", "L1") == {"provider": "minimax", "model": "MiniMax-M3", "effort": "high", "key": "minimax-key",
                                                  "format": "anthropic", "base": "https://mm/anthropic", "header": None}
assert proxy.live_target("ccx-live-sub", "other")["format"] == "chat" and proxy.live_target("ccx-live-sub", None)["key"] == "openai-oauth"
assert proxy.live_target("ccx-live-go", None)["format"] == "chat", "Go models without /messages go through chat completions"
assert proxy.live_target("ccx-live-claude", None) == {"provider": "official", "model": proxy.LIVE_OFFICIAL_DEFAULT, "effort": None, "key": "", "format": "official"}
for missing in ("ccx-live-nobody", "ccx-live-bare"):
    try:
        proxy.live_target(missing, None)
    except ValueError:
        pass
    else:
        raise AssertionError(f"{missing} was accepted")

# A running worker's request takes its profile's current effort, or none when the profile has none.
class _Agents:
    efforts = {"sub": "low", "plain": None}
    def load_agents(self):
        return {name: {"effort": effort} for name, effort in self.efforts.items()}
proxy._ccx_switch = _Agents()
try:
    body = {"output_config": {"effort": "xhigh"}, "messages": ["kept"]}
    proxy.live_worker_effort(body, "sub")
    assert body == {"output_config": {"effort": "low"}, "messages": ["kept"]}
    proxy.live_worker_effort(body, "plain")
    assert "effort" not in body["output_config"]
    proxy.live_worker_effort(body := {"output_config": {"effort": "high"}}, "gone")
    assert body["output_config"]["effort"] == "high", "an unknown profile keeps the started effort"
finally:
    proxy._ccx_switch = None

body = {"model": "gpt-6-astra", "max_tokens": 100, "stream": True,
    "system": [{"type": "text", "text": "System", "cache_control": {"type": "ephemeral"}}],
    "messages": [{"role": "user", "content": "Read it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "test.txt"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "hello"}, {"type": "text", "text": "Explain"}]}],
    "tools": [{"name": "Read", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}}],
    "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}
chat = proxy.to_chat(body)
assert [m["role"] for m in chat["messages"]] == ["system", "user", "assistant", "tool", "user"]
assert chat["messages"][3]["content"] == "hello"
assert chat["model"] == "gpt-6-astra" and chat["parallel_tool_calls"] is False
assert chat["tools"][0]["function"]["name"] == "Read"
assert json.loads(chat["messages"][2]["tool_calls"][0]["function"]["arguments"]) == {"file_path": "test.txt"}
# Claude Code's /effort rides output_config; OpenAI tops out at xhigh.
assert "reasoning_effort" not in chat
assert proxy.to_chat({**body, "output_config": {"effort": "low"}})["reasoning_effort"] == "low"
assert proxy.to_chat({**body, "output_config": {"effort": "max"}})["reasoning_effort"] == "xhigh"
fitted = {"thinking": {"type": "disabled"}, "output_config": {"effort": "max"}}
proxy.fit_thinking(fitted)
assert "thinking" not in fitted  # max with thinking disabled is a 400: omit it, run adaptive
kept = {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}
proxy.fit_thinking(kept)
assert kept["thinking"] == {"type": "disabled"}
assert proxy.content_parts([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}}])[0]["image_url"]["url"] == "data:image/png;base64,abc"
try:
    proxy.content_parts([{"type": "document"}])
except ValueError:
    pass
else:
    raise AssertionError("Unsupported content silently discarded")
# Claude Code records dynamically added tools (MCP connectors) as system content blocks.
# They are metadata, not content: they must not fail the request nor reach the model.
dynamic = {**body, "messages": [
    {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_definition",
        "definition": {"name": "mcp__docs__batch", "input_schema": {"type": "object"}}}}]},
    *body["messages"]]}
assert [m["role"] for m in proxy.to_chat(dynamic)["messages"]] == ["system", "user", "assistant", "tool", "user"]
assert "mcp__docs__batch" not in proxy.to_prompt(dynamic)
# The same bookkeeping can arrive in the top-level system field: no empty system message.
assert "system" not in [m["role"] for m in proxy.to_chat({**body, "system": [{"type": "tool_addition", "tool": {}}]})["messages"]]
assert "<system>" not in proxy.to_prompt({**body, "system": [{"type": "tool_addition", "tool": {}}]})
# And it leaves no blank line in the CLI prompt.
assert "<user>\ngo\n</user>" in proxy.to_prompt({**body, "system": None, "tools": None, "messages": [
    {"role": "user", "content": [{"type": "tool_addition", "tool": {}}, {"type": "text", "text": "go"}]}]})

chunks = [
    {"choices": [{"delta": {"reasoning_content": "Plan: "}}]},
    {"choices": [{"delta": {"reasoning": "read it."}}]},
    {"choices": [{"delta": {"content": "Hello "}}]},
    {"choices": [{"delta": {"content": "world"}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_2", "function": {"name": "Read", "arguments": '{"file_'}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'path":"test.txt"}'}}]}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 8, "prompt_tokens_details": {"cached_tokens": 2}}},
]
wire = b"".join(("data: " + json.dumps(c) + "\n\n").encode() for c in chunks) + b"data: [DONE]\n\n"
events = list(proxy.stream_events(io.BytesIO(wire), body["model"]))
assert events[0]["type"] == "message_start" and events[-1]["type"] == "message_stop"
assert "".join(e["delta"]["text"] for e in events if e.get("delta", {}).get("type") == "text_delta") == "Hello world"
assert next(e for e in events if e.get("delta", {}).get("type") == "input_json_delta")["delta"]["partial_json"] == '{"file_path": "test.txt"}'
assert events[-2]["delta"]["stop_reason"] == "tool_use"
assert events[-2]["usage"]["input_tokens"] == 10
# Reasoning streams as Claude's thinking block, before the answer and tools, each block closed.
starts = [e["content_block"]["type"] for e in events if e["type"] == "content_block_start"]
assert starts == ["thinking", "text", "tool_use"], starts
assert [e["index"] for e in events if e["type"] == "content_block_start"] == [0, 1, 2]
assert [e["index"] for e in events if e["type"] == "content_block_stop"] == [0, 1, 2]
assert "".join(e["delta"]["thinking"] for e in events if e.get("delta", {}).get("type") == "thinking_delta") == "Plan: read it."
assert proxy.to_message({"choices": [{"message": {"reasoning_content": "hm", "content": "ok"}, "finish_reason": "stop"}]}, "m")["content"] == [
    {"type": "thinking", "thinking": "hm", "signature": ""}, {"type": "text", "text": "ok"}]
try:
    list(proxy.stream_events(io.BytesIO(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'), "test"))
except ValueError:
    pass
else:
    raise AssertionError("Truncated stream reported success")

# A model that answers only "." said nothing: the person gets the notice, never a bare dot. A dot
# that leads a real answer is kept.
dotted = list(proxy.stream_events(io.BytesIO(
    b'data: {"choices":[{"delta":{"content":"."}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'), "m"))
assert "".join(e["delta"]["text"] for e in dotted if e.get("delta", {}).get("type") == "text_delta") == proxy.NO_ANSWER
assert proxy.to_message({"choices": [{"message": {"content": "."}, "finish_reason": "stop"}]}, "m")["content"] == [
    {"type": "text", "text": proxy.NO_ANSWER}]
mixed = list(proxy.stream_events(io.BytesIO(
    b'data: {"choices":[{"delta":{"content":"."}},{"delta":{"content":" done"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'), "m"))
assert "".join(e["delta"]["text"] for e in mixed if e.get("delta", {}).get("type") == "text_delta") == ". done"
# Punctuation after a real answer is kept: only a turn that never speaks is replaced.
finished = list(proxy.stream_events(io.BytesIO(
    b'data: {"choices":[{"delta":{"content":"Hello"}},{"delta":{"content":"."}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'), "m"))
assert "".join(e["delta"]["text"] for e in finished if e.get("delta", {}).get("type") == "text_delta") == "Hello."
# The dot guard also covers the Command Code generate relay, where the observed "." came from.
class FakeUpstream:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.lines)

dot_lines = [json.dumps({"type": "text-delta", "text": "."}).encode(),
             json.dumps({"type": "finish", "finishReason": "stop", "totalUsage": {}}).encode()]
with patch.object(proxy, "generate_body", return_value=("thread", {})), \
     patch.object(proxy, "cmdc_login", return_value=("key", "1")), \
     patch.object(proxy.urllib.request, "urlopen", return_value=FakeUpstream(dot_lines)):
    relayed = list(proxy.generate_stream({"model": "m", "messages": []}))
assert "".join(e["delta"]["text"] for e in relayed if e.get("delta", {}).get("type") == "text_delta") == proxy.NO_ANSWER

# A silent relay turn is transient: retry it once before telling the person it failed.
good_lines = [json.dumps({"type": "text-delta", "text": "Recovered."}).encode(),
              json.dumps({"type": "finish", "finishReason": "stop", "totalUsage": {}}).encode()]
opened = []


def open_twice(*args, **kwargs):
    opened.append(args)
    return FakeUpstream(dot_lines if len(opened) == 1 else good_lines)


with patch.object(proxy, "generate_body", return_value=("thread", {})), \
     patch.object(proxy, "cmdc_login", return_value=("key", "1")), \
     patch.object(proxy.urllib.request, "urlopen", side_effect=open_twice):
    retried = list(proxy.generate_stream({"model": "m", "messages": []}))
text = "".join(e["delta"]["text"] for e in retried if e.get("delta", {}).get("type") == "text_delta")
assert text == "Recovered." and proxy.NO_ANSWER not in text, text
assert len(opened) == 2

# Both attempts silent: the notice, after exactly one retry.
with patch.object(proxy, "generate_body", return_value=("thread", {})), \
     patch.object(proxy, "cmdc_login", return_value=("key", "1")), \
     patch.object(proxy.urllib.request, "urlopen", return_value=FakeUpstream(dot_lines)) as open_dots:
    silent = list(proxy.generate_stream({"model": "m", "messages": []}))
assert "".join(e["delta"]["text"] for e in silent if e.get("delta", {}).get("type") == "text_delta") == proxy.NO_ANSWER
assert open_dots.call_count == 2

# A tool call is progress: punctuation beside it is dropped, and the turn is not retried.
call_lines = [json.dumps({"type": "text-delta", "text": "."}).encode(),
              json.dumps({"type": "tool-call", "toolCallId": "t1", "toolName": "Read",
                          "input": {"file_path": "x"}}).encode(),
              json.dumps({"type": "finish", "finishReason": "tool-calls", "totalUsage": {}}).encode()]
with patch.object(proxy, "generate_body", return_value=("thread", {})), \
     patch.object(proxy, "cmdc_login", return_value=("key", "1")), \
     patch.object(proxy.urllib.request, "urlopen", return_value=FakeUpstream(call_lines)) as open_call:
    called = list(proxy.generate_stream({"model": "m", "messages": []}))
assert any(e.get("content_block", {}).get("type") == "tool_use" for e in called)
assert open_call.call_count == 1
assert proxy.NO_ANSWER not in "".join(e["delta"].get("text", "") for e in called if e.get("delta", {}).get("type") == "text_delta")

# The text-protocol CLI path (CCX_CMDC_TEXT=1) holds a punctuation-only answer too.
def dot_events(*args, **kwargs):
    yield {"type": "text_delta", "delta": "."}
    yield {"type": "result", "finalText": ".", "usage": {}, "stopReason": "end_turn"}


with patch.object(proxy, "cmdc_retrying", side_effect=dot_events):
    cli = list(proxy.cli_stream({"model": "m", "messages": []}))
assert "".join(e["delta"].get("text", "") for e in cli if e.get("delta", {}).get("type") == "text_delta") == proxy.NO_ANSWER
with patch.object(proxy, "cmdc_retrying", side_effect=dot_events):
    quiet = proxy.commandcode_cli({"model": "m", "messages": []})
assert quiet["content"] == [{"type": "text", "text": proxy.NO_ANSWER}], quiet


def good_events(*args, **kwargs):
    yield {"type": "text_delta", "delta": "."}
    yield {"type": "text_delta", "delta": " Done."}
    yield {"type": "result", "finalText": ". Done.", "usage": {}, "stopReason": "end_turn"}


with patch.object(proxy, "cmdc_retrying", side_effect=good_events):
    cli = list(proxy.cli_stream({"model": "m", "messages": []}))
assert "".join(e["delta"].get("text", "") for e in cli if e.get("delta", {}).get("type") == "text_delta") == ". Done."


def final_only_events(*args, **kwargs):
    yield {"type": "result", "finalText": "Hello there", "usage": {}, "stopReason": "end_turn"}


with patch.object(proxy, "cmdc_retrying", side_effect=final_only_events):
    cli = list(proxy.cli_stream({"model": "m", "messages": []}))
assert "".join(e["delta"].get("text", "") for e in cli if e.get("delta", {}).get("type") == "text_delta") == "Hello there"

# A retry starts its own counters: a second stream that never finishes must not inherit the
# gainless first attempt's stop reason and token counts.
length_lines = [json.dumps({"type": "text-delta", "text": "."}).encode(),
                json.dumps({"type": "finish", "finishReason": "length",
                            "totalUsage": {"inputTokens": 100, "outputTokens": 7}}).encode()]
unfinished_lines = [json.dumps({"type": "text-delta", "text": "Recovered."}).encode()]
tries = []


def open_unfinished(*args, **kwargs):
    tries.append(args)
    return FakeUpstream(length_lines if len(tries) == 1 else unfinished_lines)


with patch.object(proxy, "generate_body", return_value=("thread", {})), \
     patch.object(proxy, "cmdc_login", return_value=("key", "1")), \
     patch.object(proxy.urllib.request, "urlopen", side_effect=open_unfinished):
    recovered = list(proxy.generate_stream({"model": "m", "messages": []}))
assert len(tries) == 2
end = next(e for e in recovered if e["type"] == "message_delta")
assert end["delta"]["stop_reason"] == "end_turn", end
assert end["usage"]["input_tokens"] == 0 and end["usage"]["output_tokens"] == 0, end

# A relay that refuses the first request fails before the stream starts, so the client gets a
# real status (and Claude Code's own retry) instead of an empty message.
def refused(*args, **kwargs):
    raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b'{"error":{"message":"quota"}}'))


with patch.object(proxy, "generate_body", return_value=("thread", {})), \
     patch.object(proxy, "cmdc_login", return_value=("key", "1")), \
     patch.object(proxy.urllib.request, "urlopen", side_effect=refused):
    relay = proxy.generate_stream({"model": "m", "messages": []})
    try:
        next(relay)
    except RuntimeError as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("A refused relay must raise before the stream starts")

# Provider quota exhaustion carries a real reset to Claude's native quota-resume UI.
with tempfile.TemporaryDirectory() as rate_dir, patch.object(proxy, 'RATE_DIR', rate_dir), patch.object(proxy.time, 'time', return_value=2000000000):
    def limited(payload, status=429, headers=None):
        return urllib.error.HTTPError('http://local', status, 'limited', headers or {}, io.BytesIO(json.dumps(payload).encode()))
    status, error, headers = proxy.provider_error(limited({'error': {'type': 'usage_limit_reached', 'resets_at': 2000013000}}), 'openai-oauth')
    assert status == 429 and error['error']['type'] == 'rate_limit_error'
    assert headers['anthropic-ratelimit-unified-status'] == 'rejected'
    assert headers['anthropic-ratelimit-unified-reset'] == '2000013000'
    assert headers['retry-after'] == '13000'
    assert headers['x-should-retry'] == 'false'
    # Legacy OAuth 500 wording uses only fresh, exhausted quota windows, not guessed delays.
    Path(rate_dir, 'quota-openai.json').write_text(json.dumps({'at': 1999999995, 'rate_limits': {
        'five_hour': {'used_percentage': 100, 'resets_at': 2000010000},
        'seven_day': {'used_percentage': 100, 'resets_at': 2000020000}}}), encoding='utf-8')
    status, error, headers = proxy.provider_error(limited({'error': {'message': 'The usage limit has been reached'}}, 500), 'openai-oauth')
    assert status == 429 and headers['anthropic-ratelimit-unified-reset'] == '2000020000'
    assert headers['anthropic-ratelimit-unified-representative-claim'] == 'seven_day'
    Path(rate_dir, 'quota-openai.json').unlink()
    status, error, headers = proxy.provider_error(limited({'error': {'message': 'The usage limit has been reached'}}, 500), 'openai-oauth')
    assert status == 429 and 'anthropic-ratelimit-unified-reset' not in headers
    assert 'reset time unavailable' in error['error']['message']
    # Burst throttles do not masquerade as plan quotas; genuine Retry-After survives.
    status, error, headers = proxy.provider_error(limited({'error': {'message': 'Too many requests'}}, headers={'Retry-After': '30'}), 'nvidia')
    assert status == 429 and headers['retry-after'] == '30'
    assert 'anthropic-ratelimit-unified-status' not in headers
    assert error['error']['type'] == 'rate_limit_error'
    # Payment and authentication errors never arm quota auto-continue.
    for code in (401, 402, 403):
        status, error, headers = proxy.provider_error(limited({'error': {'message': 'insufficient credits'}}, code), 'nvidia')
        assert status == code and 'anthropic-ratelimit-unified-status' not in headers
    # Malformed dates/numbers and secret upstream details never reach the client.
    status, error, headers = proxy.provider_error(limited({'error': {'message': 'quota exceeded secret-token', 'resets_at': 'nan'}}), 'nvidia')
    assert 'anthropic-ratelimit-unified-reset' not in headers and 'secret-token' not in str(error)
print('provider quota metadata ok')

received = []
# Discovery and effort metadata come from ccx-switch; the suites stub that boundary.
proxy.effort_supported = lambda provider, model, effort: True
class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/v1/models" and self.headers.get("Authorization") == "Bearer ":
            self.send_response(401)
            self.end_headers()
        elif self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "claude-alpha", "name": "Alpha"}, {"id": "gpt-x"}]}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        received.append((self.path, self.headers.get("Authorization"), data))
        if data['model'] == 'plan-quota':
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': {'type': 'usage_limit_reached', 'message': 'The usage limit has been reached',
                                                  'resets_at': int(time.time()) + 13000}}).encode())
            return
        if data["model"] == "fail" or self.headers.get("Authorization") == "Bearer spent":
            self.send_response(429)
            self.send_header("retry-after", "30")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if data["stream"] else "application/json")
        self.send_header("x-ratelimit-limit-requests", "100")
        self.send_header("x-ratelimit-remaining-requests", "25")
        self.send_header("x-ratelimit-reset-requests", "6m0s")
        self.end_headers()
        self.wfile.write(wire if data["stream"] else json.dumps({"choices": [{"message": {"content": "Ready"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode())

proxy.RATE_DIR = tempfile.mkdtemp()  # never the real status line folder
upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
adapter = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
for server in (upstream, adapter):
    threading.Thread(target=server.serve_forever, daemon=True).start()
proxy.UPSTREAMS = {provider: f"http://127.0.0.1:{upstream.server_port}/v1" for provider in proxy.UPSTREAMS}
try:
    for provider in proxy.UPSTREAMS:
        url = f"http://127.0.0.1:{adapter.server_port}/{provider}/v1/messages?beta=true"
        for stream in (False, True):
            request = urllib.request.Request(url, data=json.dumps({**body, "stream": stream}).encode(), headers={"Content-Type": "application/json", "x-api-key": "fake-" + provider})
            with urllib.request.urlopen(request, timeout=10) as response:
                result = response.read()
            assert received[-1][0] == "/v1/chat/completions"
            assert received[-1][1] == "Bearer fake-" + provider
            assert received[-1][2]["model"] == body["model"]
            assert b"message_stop" in result if stream else json.loads(result)["content"][0]["text"] == "Ready"
        # Claude Code's /model discovery reads the same route's model list.
        request = urllib.request.Request(f"http://127.0.0.1:{adapter.server_port}/{provider}/v1/models?limit=1000", headers={"x-api-key": "fake"})
        with urllib.request.urlopen(request, timeout=10) as response:
            discovered = json.loads(response.read())
        assert [m["id"] for m in discovered["data"]] == ["claude-alpha", "gpt-x"]
        # A rejected key (or an unreachable upstream) leaves discovery empty, not an error.
        request = urllib.request.Request(f"http://127.0.0.1:{adapter.server_port}/{provider}/v1/models?limit=1000")
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.loads(response.read())["data"] == []
    # A model with no adjustable effort (MiMo) must not receive reasoning_effort.
    proxy.effort_supported = lambda provider, model, effort: False
    request = urllib.request.Request(url, data=json.dumps({**body, "stream": False, "output_config": {"effort": "high"}}).encode(), headers={"x-api-key": "fake"})
    urllib.request.urlopen(request, timeout=10).read()
    assert "reasoning_effort" not in received[-1][2]
    proxy.effort_supported = lambda provider, model, effort: True
    # `ccx official`: passed to Anthropic as sent (path, login, body, stream) until /model routes the launch.
    proxy.OFFICIAL_UPSTREAM = f"http://127.0.0.1:{upstream.server_port}"
    route_for = proxy.route_for
    proxy.route_for = lambda launch: None
    official = f"http://127.0.0.1:{adapter.server_port}/official/v1/messages?beta=true"
    request = urllib.request.Request(official, data=json.dumps({**body, "stream": True}).encode(), headers={"Authorization": "Bearer oauth-login", "x-ccx-launch": "L"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.read() == wire, 'The official stream comes back byte for byte'
    assert received[-1] == ("/v1/messages?beta=true", "Bearer oauth-login", {**body, "stream": True})
    # Routed by /model: converted for the picked provider, with its key and model.
    proxy.route_for = lambda launch: {"provider": "nvidia", "model": "routed", "effort": None, "key": "nv-key", "format": "chat"}
    stamped = [{"type": "text", "text": "x-anthropic-billing-header: ccx_version=1; cch=abc; ccx_turn_index=3;"}]
    request = urllib.request.Request(official, data=json.dumps({**body, "stream": False, "system": stamped}).encode(), headers={"Authorization": "Bearer oauth-login", "x-ccx-launch": "L"})
    assert json.loads(urllib.request.urlopen(request, timeout=10).read())["content"][0]["text"] == "Ready"
    assert received[-1][:2] == ("/v1/chat/completions", "Bearer nv-key") and received[-1][2]["model"] == "routed"
    assert "cch=" not in json.dumps(received[-1][2]) and "ccx_version=1;" in json.dumps(received[-1][2]), 'per-request stamps stay with Anthropic'
    # Routed back to another Claude model: passed to Anthropic with the login, the model swapped.
    proxy.route_for = lambda launch: {"provider": "official", "model": "claude-other", "effort": None, "key": "", "format": "official"}
    request = urllib.request.Request(official, data=json.dumps({**body, "stream": True}).encode(), headers={"Authorization": "Bearer oauth-login", "x-ccx-launch": "L"})
    urllib.request.urlopen(request, timeout=10).read()
    assert received[-1][:2] == ("/v1/messages?beta=true", "Bearer oauth-login") and received[-1][2]["model"] == "claude-other"
    # Auto mode's check is the safety classifier: it follows the provider's helper default (haiku
    # on official), not the main model, even while the main turn passes through to Anthropic.
    # The suite supplies that default itself; it must not read the machine's helper-defaults.json.
    class FakeSwitch:
        @staticmethod
        def load_routes():
            return {}

        @staticmethod
        def helper_default(kind, provider):
            return {"provider": "official", "model": "claude-haiku-5-5", "effort": "low"}

        @staticmethod
        def lowest_effort(provider, model):
            return "low"

    check = {"model": "claude-sonnet-5", "max_tokens": 64, "stream": False, "thinking": {"type": "disabled"}, "stop_sequences": ["</severity>"], "messages": [{"role": "user", "content": "x"}]}
    request = urllib.request.Request(official, data=json.dumps(check).encode(), headers={"Authorization": "Bearer oauth-login", "x-ccx-launch": "L"})
    with patch.object(proxy, "_load_ccx_switch", return_value=FakeSwitch()):
        urllib.request.urlopen(request, timeout=10).read()
    sent = received[-1][2]
    assert sent["model"] == "claude-haiku-5-5" and sent["output_config"]["effort"] == "low", sent
    # An official launch switched to OAuth must route both classifiers and WebFetch extraction.
    proxy.route_for = lambda launch: {"provider": "openai-oauth", "model": "gpt-6.1-sol", "effort": "xhigh", "key": "oa-key", "format": "chat"}
    helper_target = proxy.helper_target
    proxy.helper_target = lambda launch, kind, current: {**current, "model": "gpt-6-luna", "effort": "low"} if kind == "auto" else {**current, "effort": "low"}
    for helper, expected in ((check, "gpt-6-luna"), ({**check, "stop_sequences": [], "output_config": {"effort": "xhigh"}}, "gpt-6.1-sol")):
        request = urllib.request.Request(official, data=json.dumps(helper).encode(), headers={"Authorization": "Bearer oauth-login", "x-ccx-launch": "L"})
        urllib.request.urlopen(request, timeout=10).read()
        sent = received[-1]
        assert sent[:2] == ("/v1/chat/completions", "Bearer oa-key")
        assert sent[2]["model"] == expected and sent[2]["reasoning_effort"] == "low"
        assert sent[2]["max_tokens"] >= 4096 and sent[2]["messages"][-1]["content"][0]["text"] == "x"
    # Explicit helper routing can exist with no main override; it must not redirect main turns.
    proxy.route_for = lambda launch: None
    proxy.helper_target = lambda launch, kind, current: {"provider": "openai-oauth", "model": "gpt-6-luna", "effort": "low", "key": "oa-key", "format": "chat"}
    request = urllib.request.Request(official, data=json.dumps(check).encode(), headers={"Authorization": "Bearer oauth-login", "x-ccx-launch": "L"})
    urllib.request.urlopen(request, timeout=10).read()
    assert received[-1][2]["model"] == "gpt-6-luna"
    request = urllib.request.Request(official, data=json.dumps({**body, "stream": True}).encode(), headers={"Authorization": "Bearer oauth-login", "x-ccx-launch": "L"})
    assert urllib.request.urlopen(request, timeout=10).read() == wire
    proxy.helper_target = helper_target
    proxy.route_for = route_for
    # Both streaming and non-streaming callers receive native usage-limit headers before any SSE starts.
    for stream in (False, True):
        request = urllib.request.Request(url, data=json.dumps({**body, 'model': 'plan-quota', 'stream': stream}).encode(), headers={'x-api-key': 'fake'})
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == 429 and exc.headers['anthropic-ratelimit-unified-status'] == 'rejected'
            assert int(exc.headers['anthropic-ratelimit-unified-reset']) > time.time() + 12900
            assert json.load(exc)['error']['type'] == 'rate_limit_error'
        else:
            raise AssertionError('quota exhaustion did not return HTTP 429')
    # /model switched to an Anthropic-format provider whose plan is also spent: the native banner gets its reset.
    live_base = f'http://127.0.0.1:{upstream.server_port}'
    for model, expect in (('plan-quota', 429), ('ok-model', 200)):
        proxy.route_for = lambda launch, model=model: {'provider': 'opencode-go', 'model': model, 'effort': None,
                                                       'key': 'go-key', 'format': 'anthropic', 'base': live_base}
        # an official session runs as first party: its Anthropic-only diagnostics stay with Anthropic
        request = urllib.request.Request(official, data=json.dumps({**body, 'stream': False, 'diagnostics': {'x': 1}}).encode(),
                                         headers={'Authorization': 'Bearer oauth-login', 'x-ccx-launch': 'L'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                assert expect == 200 and response.status == 200 and json.loads(response.read())['choices']
        except urllib.error.HTTPError as exc:
            assert expect == 429 and exc.code == 429, exc.code
            assert exc.headers['anthropic-ratelimit-unified-status'] == 'rejected'
            assert int(exc.headers['anthropic-ratelimit-unified-reset']) > time.time() + 12900
        assert received[-1][:2] == ('/v1/messages', 'Bearer go-key')
        assert 'diagnostics' not in received[-1][2], 'first-party diagnostics never reach a third-party provider'
    proxy.route_for = route_for
    # ...nor do the betas Claude Code adds only for Anthropic's own API; shared ones remain.
    assert proxy.third_party_beta('claude-code-20250219,prompt-caching-scope-2026-01-05,tool-provenance-2026-10-26,'
                                  'context-management-2025-06-27,thinking-binding-controls-2026-08-01,cache-diagnosis-2026-04-07,'
                                  'claude-code-side-query-2026-09-29,auto-mode-classifier-2026-07-16,program-use-case-ids-2026-09-17') \
        == 'claude-code-20250219,context-management-2025-06-27'
    assert proxy.third_party_beta(None) is None
    # First party, Claude Code stamps per-request ids into its billing line (the first system block);
    # a third party would see a new prompt prefix every turn and never hit its cache.
    billing = ('x-anthropic-billing-header: ccx_version=2.1.318.aa4; ccx_entrypoint=sdk-cli; cch=d582e; ccx_prompt_id=e09; '
               'ccx_turn_origin=sdk; ccx_prev_req=req_1; ccx_prompt_index=0; ccx_turn_index=1;')
    clean = proxy.third_party_body({'system': [{'type': 'text', 'text': billing}, {'type': 'text', 'text': 'cch=keep;'}],
                                    'diagnostics': {}, 'model': 'm'})
    assert clean == {'system': [{'type': 'text', 'text': 'x-anthropic-billing-header: ccx_version=2.1.318.aa4; ccx_entrypoint=sdk-cli;'},
                                {'type': 'text', 'text': 'cch=keep;'}], 'model': 'm'}, clean
    assert proxy.third_party_body({'system': 'plain', 'model': 'm'}) == {'system': 'plain', 'model': 'm'}
    # A key out of quota rests while a spare from <KEY>_API_KEYS serves; the upstream's
    # x-ratelimit-* headers land where the status line reads them.
    proxy.spare_keys = lambda route: ["spare"]
    for _ in range(2):  # the second request skips the resting key outright
        request = urllib.request.Request(url, data=json.dumps({**body, "stream": False}).encode(), headers={"x-api-key": "spent"})
        assert json.loads(urllib.request.urlopen(request, timeout=10).read())["content"][0]["text"] == "Ready"
        assert received[-1][1] == "Bearer spare"
    assert [r[1] for r in received[-3:]] == ["Bearer spent", "Bearer spare", "Bearer spare"]
    saved = json.loads(Path(proxy.RATE_DIR, f"ratelimit-{provider}.json").read_text(encoding="utf-8"))
    window = saved["rate_limits"]["req"]
    assert window["used_percentage"] == 75 and 350 < window["resets_at"] - saved["at"] <= 360
    proxy.spare_keys = lambda route: []
    assert proxy.reset_seconds("1.5s") == 1.5 and proxy.reset_seconds("20ms") == 0.02 and proxy.reset_seconds("bogus") == 60
    # A refused request gets one try on the session's fallback route; other sessions keep the error.
    proxy.fallback_for = lambda launch: {"provider": "nvidia", "model": "backup", "key": "fb"} if launch == "L" else None
    request = urllib.request.Request(url, data=json.dumps({**body, "model": "fail", "stream": False}).encode(), headers={"x-api-key": "fake", "x-ccx-launch": "L"})
    assert json.loads(urllib.request.urlopen(request, timeout=10).read())["content"][0]["text"] == "Ready"
    assert received[-1][1] == "Bearer fb" and received[-1][2]["model"] == "backup"
    assert json.loads(Path(proxy.RATE_DIR, "fallback-L.json").read_text(encoding="utf-8"))["provider"] == "nvidia"
    # While the refused key rests, the next request goes straight to the fallback.
    sent = len(received)
    assert json.loads(urllib.request.urlopen(request, timeout=10).read())["content"][0]["text"] == "Ready"
    assert len(received) == sent + 1 and received[-1][1] == "Bearer fb"
    for model, status in [("", 400), ("fail", 429)]:
        request = urllib.request.Request(url, data=json.dumps({**body, "model": model}).encode(), headers={"x-api-key": "fake"})
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == status
            assert json.load(exc)["type"] == "error"
        else:
            raise AssertionError("Invalid input/upstream error was accepted")
    # A rejected block names itself: the generic adapter message hid this class of failure.
    bad = {**body, "messages": [{"role": "user", "content": [{"type": "document"}]}]}
    request = urllib.request.Request(url, data=json.dumps(bad).encode(), headers={"x-api-key": "fake"})
    try:
        urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400 and "Unsupported content block: document" in json.load(exc)["error"]["message"]
    else:
        raise AssertionError("Unsupported content block was accepted")
finally:
    for server in (adapter, upstream):
        server.shutdown()
        server.server_close()

# Command Code CLI route: a fake cmdc answers with a text tool call.
prompt = proxy.to_prompt(body)
assert '"name": "Read"' in prompt and '<tool_result id="call_1">\nhello\n</tool_result>' in prompt and "<system>\nSystem" in prompt
reply = proxy.from_cli('Checking.\n<tool_call>{"name": "Read", "input": {"file_path": "a.txt"}}</tool_call>', {"inputTokens": 5, "outputTokens": 3}, "end_turn", "m")
assert [b["type"] for b in reply["content"]] == ["text", "tool_use"] and reply["content"][1]["input"] == {"file_path": "a.txt"}
assert reply["stop_reason"] == "tool_use" and reply["usage"]["input_tokens"] == 5
assert proxy.from_cli("<tool_call>not json</tool_call>", None, "end_turn", "m")["content"][0]["type"] == "text"
# A fake cmdc: streams NDJSON events like `cmdc -p --output-format json` and logs its argv.
fake_dir = Path(tempfile.mkdtemp(prefix="fake-cmdc-"))
(fake_dir / "cmdc.py").write_text(r'''import json, sys
from pathlib import Path
Path(__file__).with_name("argv.json").write_text(json.dumps(sys.argv[1:]))
sys.stdin.read()
model = sys.argv[sys.argv.index("--model") + 1]
def event(**e): print(json.dumps({"type": "event", "event": e}), flush=True)
if model == "broke":
    sys.exit(10)
flag = Path(__file__).with_name("flaky.done")
if model == "flaky" and not flag.exists():  # the first run answers nothing (exit 9)
    flag.write_text("")
    event(type="thinking_delta", delta="...")
    sys.exit(9)
if model == "native":  # the model reached for cmdc's own tools: --max-turns 1 exits 8
    event(type="message_end", content=[{"type": "text", "text": "Looking."},
          {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"filePath": "a.txt"}},
          {"type": "tool_use", "id": "c2", "name": "herdr", "input": {}}])
    event(type="turn_end", usage={"inputTokens": 7, "outputTokens": 2})
    sys.exit(8)
event(type="thinking_delta", delta="Hmm")
event(type="text_delta", delta="Checking.\n<tool")
event(type="text_delta", delta='_call>{"name": "Read", "input": {}}</tool_call>')
print(json.dumps({"type": "result", "finalText": 'Checking.\n<tool_call>{"name": "Read", "input": {}}</tool_call>', "usage": {"inputTokens": 1}}))
''', encoding="utf-8")
if proxy.os.name == "nt":
    shim = fake_dir / "cmdc.cmd"
    shim.write_text(f'@"{sys.executable}" "%~dp0cmdc.py" %*\n', encoding="utf-8")
else:
    shim = fake_dir / "cmdc"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake_dir / "cmdc.py"}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
proxy.commandcode_executable = lambda: str(shim)
proxy.ccx_models = lambda provider: [{"id": "claude-x"}]  # the plan-filtered list ccx-switch owns
argv = lambda: json.loads((fake_dir / "argv.json").read_text())
proxy.os.environ["CCX_CMDC_TEXT"] = "1"  # the cmdc text-protocol path; /alpha/generate is tested below
adapter = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
threading.Thread(target=adapter.serve_forever, daemon=True).start()
try:
    url = f"http://127.0.0.1:{adapter.server_port}/commandcode-cli/v1/messages"
    for stream in (False, True):
        request = urllib.request.Request(url, data=json.dumps({**body, "model": "deepseek/deepseek-v4-flash", "stream": stream}).encode())
        with urllib.request.urlopen(request, timeout=30) as response:
            result = response.read()
        assert argv()[0] == "-p" and "--max-turns" in argv() and "--effort" not in argv()
        if stream:  # thinking and text arrive live; the <tool_call> text never shows
            events = [json.loads(line[5:]) for line in result.decode().splitlines() if line.startswith("data:")]
            deltas = [e["delta"] for e in events if e["type"] == "content_block_delta"]
            assert [d.get("thinking") for d in deltas if d["type"] == "thinking_delta"] == ["Hmm"]
            assert "".join(d["text"] for d in deltas if d["type"] == "text_delta") == "Checking.\n"
            assert any(d["type"] == "input_json_delta" for d in deltas) and events[-1]["type"] == "message_stop"
        else:
            assert json.loads(result)["content"][1]["name"] == "Read"
    request = urllib.request.Request(url, data=json.dumps({**body, "stream": False, "output_config": {"effort": "high"}}).encode())
    urllib.request.urlopen(request, timeout=30).read()
    assert argv()[-2:] == ["--effort", "high"]
    # The same request against a model that publishes no efforts: cmdc's --effort is omitted.
    proxy.effort_supported = lambda provider, model, effort: False
    request = urllib.request.Request(url, data=json.dumps({**body, "stream": False, "output_config": {"effort": "high"}}).encode())
    urllib.request.urlopen(request, timeout=30).read()
    assert "--effort" not in argv(), 'A model without adjustable effort gets no --effort'
    proxy.effort_supported = lambda provider, model, effort: True
    # cmdc's own read_file becomes the client's Read; an unmatched tool is named, not an error.
    request = urllib.request.Request(url, data=json.dumps({**body, "model": "native", "stream": False}).encode())
    native = json.loads(urllib.request.urlopen(request, timeout=30).read())
    assert native["content"][1] == {**native["content"][1], "name": "Read", "input": {"file_path": "a.txt"}}
    assert "herdr" in native["content"][0]["text"] and native["stop_reason"] == "tool_use"
    assert native["usage"]["input_tokens"] == 7
    # An empty answer (exit 9) is run again instead of reaching Claude as an API error.
    request = urllib.request.Request(url, data=json.dumps({**body, "model": "flaky", "stream": True}).encode())
    flaky = urllib.request.urlopen(request, timeout=30).read()
    assert b"input_json_delta" in flaky and b'"type": "error"' not in flaky and (fake_dir / "flaky.done").exists()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=json.dumps({**body, "model": "broke", "stream": False}).encode()), timeout=30)
    except urllib.error.HTTPError as exc:
        assert exc.code == 402 and "out of credits" in json.load(exc)["error"]["message"]
    else:
        raise AssertionError("CLI failure was accepted")
    with urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{adapter.server_port}/commandcode-cli/v1/models?limit=1000", headers={"x-api-key": "x"}), timeout=10) as response:
        assert json.loads(response.read())["data"] == [{"id": "claude-x", "display_name": "claude-x"}], 'The CLI route offers its plan-filtered ids'
    # Default path: /alpha/generate on the CLI's login, native tool calls, cache tokens passed through.
    del proxy.os.environ["CCX_CMDC_TEXT"]
    posted = []

    class Generate(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            posted.append((self.headers, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            for part in ({"type": "reasoning-delta", "text": "Hmm"}, {"type": "text-delta", "text": "Reading."},
                         {"type": "tool-call", "toolCallId": "c9", "toolName": "Read", "input": {"file_path": "a.txt"}},
                         {"type": "finish", "finishReason": "tool-calls", "totalUsage": {"inputTokens": 100, "outputTokens": 3,
                          "inputTokenDetails": {"noCacheTokens": 10, "cacheReadTokens": 90}}}):
                self.wfile.write((json.dumps(part) + "\n").encode())

    generate = ThreadingHTTPServer(("127.0.0.1", 0), Generate)
    threading.Thread(target=generate.serve_forever, daemon=True).start()
    proxy.GENERATE_URL = f"http://127.0.0.1:{generate.server_port}/alpha/generate"
    proxy.cmdc_login = lambda: ("login-key", "9.9.9")
    history = {**body, "model": "deepseek/deepseek-v4-flash", "metadata": {"user_id": "session-1"}, "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "t"}, {"type": "tool_use", "id": "c1", "name": "Read", "input": {"file_path": "b"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": [{"type": "text", "text": "data"}]}, {"type": "text", "text": "go"}]}]}
    for stream in (False, True):
        result = urllib.request.urlopen(urllib.request.Request(url, data=json.dumps({**history, "stream": stream}).encode()), timeout=30).read()
        message = proxy.collect([json.loads(line[5:]) for line in result.decode().splitlines() if line.startswith("data:")], "m") if stream else json.loads(result)
        assert [b["type"] for b in message["content"]] == ["thinking", "text", "tool_use"] and message["content"][2]["input"] == {"file_path": "a.txt"}
        assert message["stop_reason"] == "tool_use" and message["usage"]["input_tokens"] == 10 and message["usage"]["cache_read_input_tokens"] == 90
    headers, sent = posted[-1]
    assert headers["Authorization"] == "Bearer login-key" and headers["x-command-code-version"] == "9.9.9"
    assert [m["role"] for m in sent["params"]["messages"]] == ["user", "assistant", "tool", "user"]
    assert sent["params"]["messages"][1]["content"] == [{"type": "reasoning", "text": "t"}, {"type": "tool-call", "toolCallId": "c1", "toolName": "Read", "input": {"file_path": "b"}}]
    assert sent["params"]["messages"][2]["content"][0]["output"] == {"type": "text", "value": "data"} and sent["params"]["messages"][2]["content"][0]["toolName"] == "Read"
    assert posted[0][1]["threadId"] == sent["threadId"] == headers["x-session-id"], 'One thread per Claude Code session keeps the cache warm'
    generate.shutdown()
finally:
    adapter.shutdown()
    adapter.server_close()
print("PASS: Messages conversion, streamed text/tools/usage, truncation detection, both provider routes, authentication, HTTP errors; no external requests.")


def test_ccx_switch_reloads_after_edit(tmp_path, monkeypatch):
    # A stale ccx-switch (one without a newly listed model) made the adapter drop those requests.
    saved = proxy._ccx_switch
    try:
        proxy._ccx_switch = None
        first = proxy._load_ccx_switch()
        assert proxy._load_ccx_switch() is first
        first._ccx_loaded_mtime -= 1  # as if ccx-switch.py changed on disk
        assert proxy._load_ccx_switch() is not first
    finally:
        proxy._ccx_switch = saved

# `ccx official` reaches the adapter as HTTP_PROXY: full URLs for Anthropic's host, CONNECT tunnels for https.
import http.client
class Echo(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        body = self.path.encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
echo = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
adapter = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
for server in (echo, adapter):
    threading.Thread(target=server.serve_forever, daemon=True).start()
saved_upstream, proxy.OFFICIAL_UPSTREAM = proxy.OFFICIAL_UPSTREAM, f"http://127.0.0.1:{echo.server_port}"
try:
    for target, expected in [("http://api.anthropic.com/official/v1/x?a=1", b"/v1/x?a=1"),
                             (f"http://127.0.0.1:{echo.server_port}/other", b"/other")]:
        conn = http.client.HTTPConnection("127.0.0.1", adapter.server_port, timeout=10)
        conn.request("GET", target)
        assert conn.getresponse().read() == expected, target
    conn = http.client.HTTPConnection("127.0.0.1", adapter.server_port, timeout=10)
    conn.set_tunnel("127.0.0.1", echo.server_port)
    conn.request("GET", "/tunnelled")
    assert conn.getresponse().read() == b"/tunnelled"
    conn.close()
finally:
    proxy.OFFICIAL_UPSTREAM = saved_upstream
    echo.shutdown(); adapter.shutdown()
print("PASS: proxied official URL, plain-http relay, CONNECT tunnel")


# Antigravity CLI route: argv, stdin shape and agy's stream-json, with a fake process (no agy run).
class _AgyPipe:
    def __init__(self):
        self.text, self.closed = "", False
    def write(self, text):
        self.text += text
    def close(self):
        self.closed = True


class _AgyProc:
    def __init__(self, lines, code=0, err=""):
        self.stdin, self.stdout, self.stderr = _AgyPipe(), iter(lines), iter([err] if err else [])
        self.code, self.returncode, self.pid = code, None, 1
    def wait(self):
        self.returncode = self.code
        return self.code
    def poll(self):
        return self.returncode
    def kill(self):
        pass


def _agy_run(lines, code=0, err="", body=None, stream=False):
    from types import SimpleNamespace
    from unittest.mock import patch
    seen = {}
    def popen(args, **kw):
        seen["args"], seen["kw"] = args, kw
        seen["proc"] = _AgyProc([json.dumps(x) + "\n" for x in lines], code, err)
        return seen["proc"]
    fake = SimpleNamespace(agy_model_id=lambda m, e: f"{m}-{e or 'medium'}", agy_executable=lambda: "agy.exe")
    body = body or {"model": "gemini-3.8-flash", "max_tokens": 64, "output_config": {"effort": "low"},
                    "messages": [{"role": "user", "content": "hi"}]}
    with patch.object(proxy, "_load_ccx_switch", return_value=fake), patch.object(proxy.subprocess, "Popen", side_effect=popen):
        out = list(proxy.cli_stream(body, proxy.agy_events)) if stream else proxy.commandcode_cli(body, proxy.agy_events)
    return out, seen


def _agy_delta(text, state="ACTIVE", kind="agent_response"):
    return {"event": "step_update", "step_update": {"step_type": kind, "state": state, "text_delta": text}}


def _agy_result(response, status="SUCCESS", error=""):
    return {"event": "result", "result": {"status": status, "response": response, "error": error,
            "usage": {"input_tokens": 9956, "output_tokens": 80, "thinking_tokens": 5, "cache_read_tokens": 7, "total_tokens": 10048}}}


def test_agy_args_and_stdin():
    args = proxy.agy_args("agy.exe", "gemini-3.8-flash-low")
    assert args[-2:] == ["-p", ""] and args[args.index("--model") + 1] == "gemini-3.8-flash-low"
    assert args[args.index("--output-format") + 1] == "stream-json" and args[args.index("--input-format") + 1] == "stream-json"
    assert json.loads(args[args.index("--settings") + 1]) == {"toolPermission": "strict"} and "--disable-slash-commands" in args
    assert args[args.index("--print-timeout") + 1].endswith("s")
    line = proxy.agy_stdin('say "hi"\nnow')
    assert line.endswith("\n") and line.count("\n") == 1
    assert json.loads(line) == {"event": "user", "message": {"content": 'say "hi"\nnow'}}


def test_agy_success_text_and_tool_call():
    tools = [{"name": "Read", "description": "read", "input_schema": {"type": "object"}}]
    body = {"model": "gemini-3.8-flash", "max_tokens": 64, "output_config": {"effort": "low"}, "tools": tools,
            "messages": [{"role": "user", "content": "read a.txt"}]}
    reply = 'Reading.<tool_call>{"name": "Read", "input": {"file_path": "a.txt"}}</tool_call>'
    lines = [{"event": "init"}, _agy_delta("", kind="user_input"), _agy_delta("Reading.<tool_"),
             _agy_delta('call>{"name": "Read", "input": {"file_path": "a.txt"}}</tool_call>', "DONE"), _agy_result(reply)]
    message, seen = _agy_run(lines, body=body)
    assert seen["args"][seen["args"].index("--model") + 1] == "gemini-3.8-flash-low" and seen["args"][0] == "agy.exe"
    assert seen["kw"]["cwd"] == tempfile.gettempdir() and seen["proc"].stdin.closed
    sent = json.loads(seen["proc"].stdin.text)
    assert sent["event"] == "user" and "<tool_call>" in sent["message"]["content"] and "read a.txt" in sent["message"]["content"]
    assert message["stop_reason"] == "tool_use"
    assert [b["type"] for b in message["content"]] == ["text", "tool_use"] and message["content"][1]["input"] == {"file_path": "a.txt"}
    assert message["usage"]["input_tokens"] == 9956 and message["usage"]["output_tokens"] == 85
    assert message["usage"]["cache_read_input_tokens"] == 7
    # Streamed: the text before the tag goes out, never half a <tool_call>.
    events, _ = _agy_run(lines, body=body, stream=True)
    texts = "".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta")
    assert texts == "Reading." and "<tool" not in texts
    assert any(e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use" for e in events)
    assert events[-2]["delta"]["stop_reason"] == "tool_use" and events[-1]["type"] == "message_stop"
    # Plain text streams as it arrives; agy's own tool steps are ignored.
    plain = [_agy_delta("Hel"), _agy_delta("ignored", kind="tool_call"), _agy_delta("lo", "DONE"), _agy_result("Hello")]
    events, _ = _agy_run(plain, stream=True)
    assert "".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta") == "Hello"
    assert _agy_run(plain)[0]["content"] == [{"type": "text", "text": "Hello"}]


def test_agy_failures():
    import pytest
    with pytest.raises(RuntimeError) as err:
        _agy_run([_agy_result("", "ERROR", "model overloaded")], code=1)
    assert "model overloaded" in str(err.value) and err.value.status_code == 502 and err.value.public
    with pytest.raises(RuntimeError) as err:
        _agy_run([_agy_result("", "ERROR", "")], code=1, err="Error: not authenticated\n")
    assert "sign in" in str(err.value) and err.value.status_code == 401
    with pytest.raises(RuntimeError) as err:
        _agy_run([_agy_result("", "ERROR", "429 RESOURCE_EXHAUSTED: quota")], code=1)
    assert err.value.status_code == 429
    # Only agy's own tool steps and no answer text: a clear error, not an empty reply.
    with pytest.raises(RuntimeError) as err:
        _agy_run([_agy_delta("x", kind="run_command"), _agy_result("")])
    assert "own tools" in str(err.value)
    with pytest.raises(RuntimeError):
        _agy_run([_agy_delta("partial")], code=0)  # no result line at all
