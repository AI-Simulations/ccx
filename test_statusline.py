"""Offline regressions for provider plan labels, including cached values."""
import importlib.util
import io
import json
import os
import re
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

if os.name != 'nt':  # tests patch Windows' registry reads; give other systems a stand-in to patch
    import sys
    import types
    sys.modules.setdefault('winreg', types.SimpleNamespace(OpenKey=None, QueryValueEx=None, HKEY_CURRENT_USER=0))
from pathlib import Path

spec = importlib.util.spec_from_file_location("statusline", Path(__file__).with_name("statusline.py"))
statusline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(statusline)


class WatchdogTests(unittest.TestCase):
    def test_hung_render_exits(self):
        # 2026-11-06: renders blocked on stdin piled up for 13 h once the watchdog was lost.
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("statusline.py"))],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        try:
            self.assertEqual(p.wait(30), 0)
        finally:
            p.kill()
            p.stdin.close()


class PlanValueTests(unittest.TestCase):
    def test_weekly_value_from_window_spend_and_share(self):
        now = 2000000000
        limits = {"seven_day": {"used_percentage": 10, "resets_at": now + 3600}}
        start = now + 3600 - 604800
        buckets = {"openai": {str(start + 600): [3.0, 1000, 900], str(start - 7200): [99.0, 5, 0]}}
        self.assertEqual(statusline.weekly_value("openai", limits, buckets, now), (3.0, 30.0, 90))
        self.assertEqual(statusline.weekly_value("openai", {"seven_day": {**limits["seven_day"], "used_percentage": 1}}, buckets, now), (3.0, None, 90))
        self.assertEqual(statusline.money(13500), "$13.5k")

    def test_unlisted_model_priced_from_models_dev(self):
        listing = {"openai": {"models": {"gpt-9-sol": {"cost": {"input": 3, "output": 12, "cache_read": 0.3}}}}}
        with patch.object(statusline, "_MODELS_DEV", listing):
            self.assertAlmostEqual(statusline.usage_cost("gpt-9-sol", {"input_tokens": 100_000, "output_tokens": 100_000}), 1.5)
            self.assertIsNone(statusline.usage_cost("gpt-9-luna", {"input_tokens": 1}))

    def test_gpt_reply_priced_with_long_context_tier(self):
        self.assertAlmostEqual(statusline.usage_cost("gpt-6.1-sol", {"input_tokens": 1_000_000, "output_tokens": 0}), 4.0)
        self.assertAlmostEqual(statusline.usage_cost("gpt-6.1-sol", {"input_tokens": 100_000, "output_tokens": 100_000}), 1.2)


class PlanLabelTests(unittest.TestCase):
    def test_max_tiers_use_current_plan_names(self):
        for tier, expected in (('default_claude_max_5x', 'Max'), ('default_claude_max_20x', 'Ultra')):
            with self.subTest(tier=tier):
                self.assertEqual(statusline.plan_name({'organizationType': 'claude_max',
                                                       'organizationRateLimitTier': tier}), expected)

    def test_usage_unavailable_keeps_known_reset_countdown(self):
        quota = {'error': 'usage unavailable', 'rate_limits': {}, 'last_reset_at': 2000002160}
        with patch.object(statusline, 'quota_source', return_value='openai'), \
                patch.object(statusline, 'provider_quota', return_value=(quota, None)), \
                patch.object(statusline.time, 'time', return_value=2000000000), \
                patch.object(statusline, 'settings_ultracode', return_value=False), \
                patch.object(statusline, 'keyword_ultracode', return_value=False), \
                patch.object(statusline, 'load_json', return_value={}):
            rendered = re.sub(r'\x1b\[[0-9;]*m', '', statusline.render({'model': {'id': 'gpt-6.1-sol'}}))
        self.assertIn('usage unavailable', rendered)
        self.assertIn('reset', rendered)
        self.assertIn('36m', rendered)


    def test_tools_effort_dots_low_default_and_explicit_override(self):
        files = {'routes.json': {'L': {'provider': 'openai-oauth', 'model': 'main-model', 'effort': 'high'}}}
        with patch.object(statusline, 'load_json', side_effect=lambda name: files.get(os.path.basename(name), {})), \
                patch.object(statusline, 'quota_source', return_value=None), \
                patch.object(statusline, 'settings_ultracode', return_value=False), \
                patch.object(statusline, 'keyword_ultracode', return_value=False), \
                patch.object(statusline, 'effort_levels', return_value=['low', 'medium', 'high']), \
                patch.dict(os.environ, {'CCX_LAUNCH_ID': 'L'}):
            rendered = statusline.render({'model': {'id': 'main-model'}, 'effort': {'level': 'high'}})
            tools = rendered.split('Tools: ')[1].splitlines()[0]
            self.assertIn('main-model', tools)
            self.assertEqual(tools.count('●'), 1)
            files['routes.json']['L']['helpers'] = {'tools': {'provider': 'openai-oauth', 'model': 'picked', 'effort': 'medium'}}
            tools = statusline.render({'model': {'id': 'main-model'}, 'effort': {'level': 'high'}}).split('Tools: ')[1].splitlines()[0]
            self.assertIn('picked', tools)
            self.assertEqual(tools.count('●'), 2)
            files['routes.json']['L'].pop('helpers')
            with patch.object(statusline, 'effort_levels', return_value=None):
                tools = statusline.render({'model': {'id': 'main-model'}, 'effort': {'level': 'high'}}).split('Tools: ')[1].splitlines()[0]
            self.assertIn('●●●', tools)

    def test_openai_plan_labels(self):
        for raw, expected in (("prolite", "ChatGPT Pro Lite"), ("pro", "ChatGPT Pro"), (None, "ChatGPT")):
            with self.subTest(raw=raw), patch("builtins.open", return_value=io.StringIO(json.dumps({"tokens": {"access_token": "test"}}))), patch.object(statusline, "fetch_json", return_value={"plan_type": raw}):
                self.assertEqual(statusline.openai_quota()["plan"], expected)

    def test_provider_tokens_without_api_label(self):
        with patch.object(statusline, "quota_source", return_value=None), patch.object(statusline, "settings_effort", return_value=None), patch.object(statusline, "settings_ultracode", return_value=False), patch.object(statusline, "keyword_ultracode", return_value=False):
            rendered = statusline.render({"model": {"id": "gpt-test"}, "context_window": {"total_input_tokens": 6000, "total_output_tokens": 476}})
        self.assertNotIn("↑", rendered)
        self.assertNotIn("API", rendered)

    def test_third_party_cache_uses_reported_reads(self):
        with patch.object(statusline, "quota_source", return_value=None), patch.object(statusline, "settings_effort", return_value=None), patch.object(statusline, "settings_ultracode", return_value=False), patch.object(statusline, "keyword_ultracode", return_value=False):
            payload = {"model": {"id": "gpt-test"}, "prompt_cache": {"warm": False}, "context_window": {"current_usage": {"input_tokens": 1000, "cache_read_input_tokens": 4000}}}
            rendered = statusline.render(payload)
            self.assertNotIn("cache read", rendered)
            self.assertNotIn("cache off", rendered)
            payload["context_window"]["current_usage"] = {"input_tokens": 1000}
            rendered = statusline.render(payload)
            self.assertNotIn("cache off", rendered)
            self.assertNotIn("cache read", rendered)

    def test_adapter_route_shows_recorded_rate_limits(self):
        saved = {"at": 1000, "rate_limits": {"req": {"used_percentage": 75, "resets_at": None}}}
        with patch.object(statusline, "quota_source", return_value=None), patch.object(statusline, "settings_effort", return_value=None), patch.object(statusline, "settings_ultracode", return_value=False), patch.object(statusline, "keyword_ultracode", return_value=False), \
                patch.object(statusline, "load_json", side_effect=lambda name: saved if name == "ratelimit-gemini.json" else {}), patch.dict(os.environ, {"CCX_MAIN_PROVIDER": "gemini"}):
            rendered = statusline.render({"model": {"id": "gemini-2.5-pro"}})
        self.assertIn("req", rendered)
        self.assertIn("▰▱▱▱", re.sub(r"\x1b\[[0-9;]*m", "", rendered))  # 75%: seven of ten cells

    def test_fallback_shown_and_highlighted_while_serving(self):
        files = {"fallbacks.json": {"L": {"provider": "gemini", "model": "gemini-2.5-flash"}}}
        def load(name):
            return files.get(os.path.basename(name), {})
        with patch.object(statusline, "quota_source", return_value=None), patch.object(statusline, "settings_effort", return_value=None), patch.object(statusline, "settings_ultracode", return_value=False), patch.object(statusline, "keyword_ultracode", return_value=False), \
                patch.object(statusline, "load_json", side_effect=load), patch.dict(os.environ, {"CCX_MAIN_PROVIDER": "nvidia", "CCX_LAUNCH_ID": "L"}):
            idle = statusline.render({"model": {"id": "deepseek-v4-pro"}})
            files["fallback-L.json"] = {"at": time.time()}
            serving = statusline.render({"model": {"id": "deepseek-v4-pro"}})
            os.environ["CCX_LAUNCH_ID"] = "other"
            unset = statusline.render({"model": {"id": "deepseek-v4-pro"}})
        self.assertIn("↪ fallback gemini/gemini-2.5-flash", idle)
        self.assertNotIn("active", idle)
        self.assertIn("fallback gemini/gemini-2.5-flash active", serving)
        self.assertNotIn("fallback", unset)

    def test_cached_label_display(self):
        quota = {"plan": "ChatGPT Prolite", "rate_limits": {"seven_day": {"used_percentage": 0}}}
        with patch.object(statusline, "quota_source", return_value="openai"), patch.object(statusline, "provider_quota", return_value=(quota, None)), patch.object(statusline, "settings_effort", return_value=None), patch.object(statusline, "settings_ultracode", return_value=False), patch.object(statusline, "keyword_ultracode", return_value=False), \
                patch.dict(os.environ, {"CCX_LAUNCH_ID": ""}):  # the running session's /model route must not leak in
            rendered = statusline.render({"model": {"id": "gpt-test"}})
        self.assertIn("ChatGPT Pro Lite", rendered)
        self.assertNotIn("ChatGPT Prolite", rendered)
        self.assertEqual(quota["plan"], "ChatGPT Prolite")


class ApiBillingTests(unittest.TestCase):
    """The session cost segment: shown when an API key pays, absent when the plan does."""

    # What a subscription session leaves behind in ~/.claude/statusline/quota.json.
    CACHED = {"rate_limits": {"seven_day": {"used_percentage": 43, "resets_at": 0}}, "at": 1, "account": None}

    def render(self, payload, stored, env, creds=None):
        with patch.dict(os.environ, env, clear=True), patch.object(statusline, "claude_json", return_value=stored), patch.object(statusline, "credentials", return_value=creds or {}), patch.object(statusline, "quota_source", return_value="anthropic"), patch.object(statusline, "load_json", return_value=self.CACHED), patch.object(statusline, "save_json"), patch.object(statusline, "settings_effort", return_value=None), patch.object(statusline, "settings_ultracode", return_value=False), patch.object(statusline, "keyword_ultracode", return_value=False):
            rendered = statusline.render(payload)
        return re.sub(r"\x1b\[[0-9;]*m", "", rendered)  # assertions read the text, not the colours

    def plan_payload(self, cost):
        return {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "cost": {"total_cost_usd": cost},
                "rate_limits": {"seven_day": {"used_percentage": 43, "resets_at": 0}}}

    def test_api_key_shows_session_cost_not_plan_windows(self):
        key = "sk-ant-usr-" + "x" * 20
        rendered = self.render(self.plan_payload(1.84), {}, {"ANTHROPIC_API_KEY": key})
        self.assertIn("api $1.84", rendered)
        self.assertNotIn("7d", rendered)

    def test_declined_key_leaves_the_plan_in_charge(self):
        key = "sk-ant-usr-" + "y" * 20
        stored = {"customApiKeyResponses": {"rejected": [key[-20:]]}}
        rendered = self.render(self.plan_payload(1.84), stored, {"ANTHROPIC_API_KEY": key})
        self.assertIn("7d", rendered)
        self.assertNotIn("api $", rendered)

    def test_console_stored_key_bills_and_shows_sub_cent_cost(self):
        payload = {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "cost": {"total_cost_usd": 0.084}}
        rendered = self.render(payload, {"primaryApiKey": "sk-ant-api03-x"}, {})
        self.assertIn("api $0.084", rendered)
        self.assertNotIn("7d", rendered)

    def test_expired_plan_windows_do_not_look_like_api_billing(self):
        # Claude Code drops each window once it resets, so a subscriber mid-session can send an
        # empty rate_limits object; that is still a plan session, not a keyed one.
        payload = {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
                   "cost": {"total_cost_usd": 1.84}, "rate_limits": {}}
        rendered = self.render(payload, {"primaryApiKey": "sk-ant-api03-x"}, {})
        self.assertNotIn("api $", rendered)
        self.assertIn("7d", rendered)  # the window replayed from the cache


    def test_api_cost_shows_at_zero_before_the_first_reply(self):
        # Nothing spent yet: the segment is the only sign the key is paying for this session.
        payload = {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}}
        rendered = self.render(payload, {"primaryApiKey": "sk-ant-api03-x"}, {})
        self.assertIn("api $0.000", rendered)


    def test_claude_ai_login_keeps_api_usage_off_the_line(self):
        # Signed in to claude.ai the plan pays, so no spend and no api label, even before the
        # first reply brings rate_limits, and even with a Console key still stored.
        payload = {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "cost": {"total_cost_usd": 1.84}}
        rendered = self.render(payload, {"primaryApiKey": "sk-ant-api03-x"}, {}, {"claudeAiOauth": {"accessToken": "x"}})
        self.assertNotIn("api $", rendered)

    def test_replayed_window_is_marked_by_a_dim_label(self):
        # Payload carries no rate_limits, so the window below comes from the cache, not live data.
        payload = {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}}
        rendered = self.render(payload, {}, {})
        self.assertIn("7d ▰", rendered)  # render() drops colours; the dim label is in quota_parts
        self.assertNotIn("~7d", rendered)  # a "~" read as "about 7 days"
        dim = statusline.quota_parts("", "", {"seven_day": {"used_percentage": 40}}, cached_at=1, now=2)
        self.assertTrue(dim[0].startswith(statusline.DIM), dim)
        self.assertNotIn("old", rendered)  # the cache age is not worth the width

    def test_reset_countdown_has_a_space(self):
        # Assert the shape, not an exact duration: the countdown rounds down between building
        # the payload and rendering it.
        payload = {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
                   "rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": time.time() + 3600}}}
        rendered = self.render(payload, {}, {})
        self.assertNotIn("↻", rendered)
        self.assertIn("▰▰▰▰▰▰▰▰▰▰ 59m", rendered)  # the countdown follows the bar, no icon


class SubagentLineTests(unittest.TestCase):
    """Legacy quota lines when configured-worker routing is not installed."""

    def render(self, source, subs):
        with patch.object(statusline, "quota_source", return_value=source), \
             patch.object(statusline, "ccx_routing_enabled", return_value=False), \
             patch.object(statusline, "subagent_sources", return_value=subs), \
             patch.object(statusline, "provider_quota", return_value=({"plan": "Command Code", "extra": "$1.00 left", "rate_limits": {}}, None)), \
             patch.object(statusline, "claude_json", return_value={}), \
             patch.object(statusline, "credentials", return_value={}), \
             patch.object(statusline, "load_json", return_value={}), \
             patch.object(statusline, "save_json"), \
             patch.object(statusline, "settings_effort", return_value=None), \
             patch.object(statusline, "settings_ultracode", return_value=False), \
             patch.object(statusline, "keyword_ultracode", return_value=False):
            return re.sub(r"\x1b\[[0-9;]*m", "", statusline.render(
                {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}}))

    def test_official_claude_shows_no_ccx_subagent_data(self):
        rendered = self.render("anthropic", {"commandcode": ["sub"]})
        self.assertNotIn("subagent", rendered)
        self.assertNotIn("Command Code", rendered)

    def test_quota_source_route_detection(self):
        # The gate above depends on this mapping: a typo would re-enable the subagent line.
        for env, expected in (({}, "anthropic"),
                              ({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}, "anthropic"),
                              ({"ANTHROPIC_BASE_URL": "http://127.0.0.1:10532/commandcode"}, "commandcode"),
                              ({"ANTHROPIC_BASE_URL": "http://127.0.0.1:10532/opencode-go"}, "opencode-go")):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(statusline.quota_source(), expected)

    def test_third_party_session_still_shows_other_account_subagents(self):
        rendered = self.render("openai", {"commandcode": ["sub"]})
        self.assertIn("↳ subagent sub", rendered)

    def test_unknown_route_still_shows_the_subagent_line(self):
        self.assertIn("↳ subagent sub", self.render(None, {"commandcode": ["sub"]}))


class QuotaErrorTests(unittest.TestCase):
    def test_an_expired_key_is_named_instead_of_hiding_the_bars(self):
        body = io.BytesIO(b'{"error":{"code":"api_key_expired"}}')
        expired = __import__("urllib.error").error.HTTPError("https://x", 401, "Unauthorized", {}, body)
        with patch.dict(statusline.QUOTA_FETCHERS, {"opencode-go": lambda: (_ for _ in ()).throw(expired)}), \
             patch.object(statusline, "load_json", return_value={}), patch.object(statusline, "save_json") as saved:
            q, _ = statusline.provider_quota("opencode-go", 1000.0)
        self.assertEqual(q["error"], "key expired")
        self.assertEqual(saved.call_args.args[1]["error"], "key expired")


class ConfiguredAgentLineTests(unittest.TestCase):
    def render(self, main, profiles, quota=None, ansi=False, stored=None):
        hooks = {'PreToolUse': [{'matcher': 'Agent|Task', 'hooks': [
            {'type': 'command', 'command': 'python ccx-switch.py hook-agent'}]}]}
        with patch.dict(os.environ, {'CCX_MAIN_PROVIDER': main, 'CCX_AGENT_CHILD': ''}), \
             patch.object(statusline, 'user_settings', return_value={'hooks': hooks}), \
             patch.object(statusline, 'ccx_profiles', return_value=profiles, create=True), \
             patch.object(statusline, 'quota_source', return_value='anthropic' if main == 'official' else 'openai'), \
             patch.object(statusline, 'provider_quota', return_value=(quota or {}, None)), \
             patch.object(statusline, 'claude_json', return_value={}), \
             patch.object(statusline, 'credentials', return_value={}), \
             patch.object(statusline, 'load_json', side_effect=lambda name: (stored or {}).get(name, {})), \
             patch.object(statusline, 'save_json'), \
             patch.object(statusline, 'keyword_ultracode', return_value=False):
            text = statusline.render({'model': {'id': 'main-model'}, **({'session_id': 's1'} if stored else {})})
        return text if ansi else re.sub(r'\x1b\[[0-9;]*m', '', text)

    def test_official_main_shows_configured_worker_provider_model_and_effort(self):
        text = self.render('official', {'sub': {'provider': 'openai-oauth', 'model': 'gpt-test', 'effort': 'xhigh'}})
        self.assertIn('sub:', text)
        self.assertIn('test', text)
        self.assertIn('OAuth', text)
        self.assertIn('●●●●', text)

    def test_same_provider_is_visible_without_a_label(self):
        for provider in ('official', 'openai-oauth'):
            with self.subTest(provider=provider):
                text = self.render(provider, {'sub': {'provider': provider, 'model': 'worker-model'}})
                self.assertIn('worker model', text)
                self.assertNotIn('same provider', text)  # the main route needs no label

    def test_provider_without_quota_api_is_still_visible(self):
        text = self.render('official', {'sub': {'provider': 'nvidia', 'model': 'worker-model'}})
        self.assertIn('NIM', text)
        self.assertIn('worker model', text)
        self.assertNotIn('same provider', text)

    def test_level_rows_fold_into_one_compact_line(self):
        profiles = {'easy': {'provider': 'official', 'model': 'claude-haiku-5-5'},
                    'mid': {'provider': 'official', 'model': 'claude-sonnet-5-5'}}
        text = self.render('official', profiles)
        self.assertEqual(text.splitlines()[-1], '↳ easy haiku ●●● · mid sonnet ●●●')
        self.assertNotIn('no runs yet', text)

    def test_configured_worker_line_has_short_labels_and_keeps_quota_bars(self):
        text = self.render('official', {'sub': {'provider': 'openai-oauth', 'model': 'gpt-6.1-sol', 'effort': 'xhigh'}})
        line = text.splitlines()[-1]
        self.assertEqual(line, '↳ sub: 6.1 sol ●●●● · OAuth │ no runs yet')  # every row has its usage cell
        quota = {'plan': 'ChatGPT Plus', 'rate_limits': {
            'five_hour': {'used_percentage': 81, 'resets_at': None},
            'seven_day': {'used_percentage': 12, 'resets_at': None}}}
        line = self.render('official', {'sub': {'provider': 'openai-oauth', 'model': 'gpt-6.1-sol', 'effort': 'xhigh'}}, quota).splitlines()[-1]
        self.assertIn('Plus', line)
        self.assertNotIn('ChatGPT', line)
        self.assertIn('5h ▰▰▰▰▰▰▰▰▱▱', line)  # one cell per full 10%, no number
        self.assertIn('7d ▰▱▱▱▱▱▱▱▱▱', line)
        self.assertLess(len(line), 90)

    def test_effort_circles_share_main_mapping_with_lower_brightness(self):
        for number, (effort, hue) in enumerate(statusline.EFFORT_HUES.items(), 1):
            with self.subTest(effort=effort):
                line = self.render('official', {'sub': {'provider': 'nvidia', 'model': 'gpt-test', 'effort': effort}}, ansi=True).splitlines()[-1]
                self.assertIn(statusline.rgb(hue, val=0.55) + '●' * number, line)
                self.assertNotIn(statusline.rgb(hue) + '●' * number, line)


class EffortLevelTests(unittest.TestCase):
    def test_default_ladder_keeps_the_historical_circles_and_hues(self):
        for number, (effort, hue) in enumerate(statusline.EFFORT_HUES.items(), 1):
            with self.subTest(effort=effort):
                got = statusline.effort_indicator(effort, label=False)
                self.assertEqual(got, statusline.rgb(hue, val=0.9) + '●' * number + statusline.RESET)
                self.assertEqual(got, statusline.effort_indicator(effort, label=False, levels=list(statusline.EFFORT_HUES)))

    def test_circles_and_hue_follow_the_models_own_ladder(self):
        three = ['low', 'medium', 'high']
        top = statusline.effort_indicator('high', label=False, levels=three)
        self.assertEqual(top, statusline.rgb(statusline.EFFORT_HUES['max'], val=0.9) + '●' * 3 + statusline.RESET)
        mid = statusline.effort_indicator('medium', label=False, levels=three)
        self.assertEqual(mid, statusline.rgb(statusline.EFFORT_HUES['high'], val=0.9) + '●' * 2 + statusline.RESET)
        self.assertEqual(statusline.effort_indicator('max', label=True, levels=three),
                         statusline.rgb(280, val=0.9) + 'max' + statusline.RESET)
        self.assertEqual(statusline.effort_indicator('max', label=False, levels=three),
                         statusline.rgb(280, val=0.9) + 'max' + statusline.RESET)

    def test_off_is_the_step_below_low_on_models_that_offer_it(self):
        ladder = ['off', 'high', 'max']
        self.assertEqual(statusline.effort_indicator('off', label=False, levels=ladder),
                         statusline.rgb(statusline.EFFORT_HUES['low'], val=0.9) + '●' + statusline.RESET)
        self.assertEqual(statusline.effort_indicator('max', label=False, levels=ladder),
                         statusline.rgb(statusline.EFFORT_HUES['max'], val=0.9) + '●●●' + statusline.RESET)

    def test_model_without_adjustable_effort_renders_default(self):
        self.assertEqual(statusline.effort_indicator('high', label=False, levels=[]),
                         statusline.DIM + 'default' + statusline.RESET)

    def test_hue_ramp_is_cool_at_the_bottom_and_hot_at_the_top(self):
        self.assertAlmostEqual(statusline.effort_hue(0.0), statusline.EFFORT_HUES['low'], places=3)
        self.assertAlmostEqual(statusline.effort_hue(1.0), statusline.EFFORT_HUES['max'], places=3)
        self.assertAlmostEqual(statusline.effort_hue(0.5), statusline.EFFORT_HUES['high'], places=3)

    def test_levels_are_read_from_the_provider_that_serves_the_model(self):
        cache = {"models": [{"slug": "gpt-x", "supported_reasoning_levels": [
            {"effort": "low"}, {"effort": "high"}, {"effort": "max"}]},
            {"slug": "gpt-y", "supported_reasoning_levels": [{"effort": "low"}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'models_cache.json').write_text(json.dumps(cache), encoding='utf-8')
            with patch.dict(os.environ, {'CODEX_HOME': tmp}):
                self.assertEqual(statusline.effort_levels('openai-oauth', 'GPT-X'), ['low', 'high', 'max'])
                self.assertIsNone(statusline.effort_levels('openai-oauth', 'gpt-z'))
        saved = {'at': time.time(), 'models': {'MiniMax-M3': ['low', 'medium', 'high'], 'mimo': []}}
        with patch.object(statusline, 'read_json', return_value=saved):
            self.assertEqual(statusline.effort_levels('commandcode-cli', 'minimax-m3'), ['low', 'medium', 'high'])
            self.assertEqual(statusline.effort_levels('commandcode', 'mimo'), [])
            self.assertIsNone(statusline.effort_levels('nvidia', 'minimax-m3'))


class QuotaCredentialTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', "Windows' user environment lives in the registry")
    def test_provider_environment_precedes_windows_saved_environment(self):
        with patch.dict(os.environ, {'USERPROFILE': str(Path.home()), 'OPENROUTER_API_KEY': 'process-key'}, clear=True), \
                patch('winreg.OpenKey') as registry:
            self.assertEqual(statusline.ccx_key('openrouter'), 'process-key')
            registry.assert_not_called()
        with patch.dict(os.environ, {'USERPROFILE': str(Path.home())}, clear=True), \
                patch('winreg.OpenKey'), patch('winreg.QueryValueEx', return_value=('user-key', 1)):
            self.assertEqual(statusline.ccx_key('openrouter'), 'user-key')

    def test_switched_provider_uses_ccx_saved_key_not_launch_token(self):
        for provider, fetcher, response in (
                ('commandcode', statusline.commandcode_quota, {}),
                ('opencode-go', statusline.opencode_go_quota, {}),
                ('openrouter', statusline.openrouter_quota, {'data': {}})):
            original_open = open
            def opened(path, *args, **kwargs):
                if str(path).endswith(('.py', '.pyc')):
                    return original_open(path, *args, **kwargs)
                if os.path.basename(path) == 'keys.json':
                    return io.StringIO(json.dumps({provider: 'saved-provider-key'}))
                raise FileNotFoundError(path)
            with self.subTest(provider=provider), patch.dict(os.environ, {
                    'ANTHROPIC_BASE_URL': 'http://127.0.0.1:10532/official',
                    'ANTHROPIC_AUTH_TOKEN': 'wrong-launch-token', 'USERPROFILE': str(Path.home())}, clear=True), \
                    patch('builtins.open', side_effect=opened), \
                    patch('winreg.OpenKey', side_effect=OSError), \
                    patch.object(statusline, 'fetch_json', return_value=response) as fetch:
                fetcher()
                self.assertEqual(fetch.call_args.args[1]['Authorization'], 'Bearer saved-provider-key')


class ModelSwitchTests(unittest.TestCase):
    def render(self, route, profiles=None, launch_provider='official'):
        files = {'routes.json': {'L': route} if route else {},
                 'ratelimit-nvidia.json': {'rate_limits': {'req': {'used_percentage': 37}}}}
        quotas = {'openai': {'plan': 'ChatGPT Plus', 'rate_limits': {'five_hour': {'used_percentage': 15}}},
                  'commandcode': {'plan': 'Command Code', 'extra': '$2.00 left'},
                  'opencode-go': {'plan': 'OpenCode Go', 'rate_limits': {'month': {'used_percentage': 23}}},
                  'openrouter': {'plan': 'OpenRouter', 'extra': '$3.00 left'}}
        payload = {'model': {'id': 'claude-opus-5-5'}, 'effort': 'high',
                   'rate_limits': {'five_hour': {'used_percentage': 99}},
                   'cost': {'total_cost_usd': 12}, 'prompt_cache': {'warm': False}}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'CCX_LAUNCH_ID': 'L', 'CCX_MAIN_PROVIDER': launch_provider,
                'ANTHROPIC_BASE_URL': 'http://127.0.0.1:10532/' + launch_provider, 'CCX_AGENT_CHILD': ''}, clear=True))
            for name, value in {'claude_json': {'oauthAccount': {'organizationType': 'claude_max'}},
                                'credentials': {'claudeAiOauth': {'accessToken': 'test'}},
                                'settings_effort': None, 'settings_ultracode': False, 'keyword_ultracode': False,
                                'ccx_routing_enabled': bool(profiles), 'ccx_profiles': profiles or {},
                                'ccx_fallback_jobs': {}, 'ccx_running_jobs': 0, 'ccx_orphans': [],
                                'subagent_sources': {}}.items():
                stack.enter_context(patch.object(statusline, name, return_value=value))
            stack.enter_context(patch.object(statusline, 'load_json', side_effect=lambda name: files.get(os.path.basename(name), {})))
            saved = stack.enter_context(patch.object(statusline, 'save_json'))
            fetched = stack.enter_context(patch.object(statusline, 'provider_quota', side_effect=lambda source, now: (quotas[source], None)))
            levels = stack.enter_context(patch.object(statusline, 'effort_levels', return_value=['low', 'high']))
            text = re.sub(r'\x1b\[[0-9;]*m', '', statusline.render(payload))
            return text, fetched.call_args_list, levels.call_args_list, saved.call_args_list

    def test_switch_selects_quota_and_effort_from_effective_provider(self):
        for provider, model, source, label in (
                ('openai-oauth', 'gpt-test', 'openai', 'ChatGPT Plus'),
                ('commandcode', 'claude-test', 'commandcode', 'Command Code'),
                ('commandcode-cli', 'claude-test', 'commandcode', 'Command Code'),
                ('opencode-go', 'claude-test', 'opencode-go', 'OpenCode Go'),
                ('openrouter', 'anthropic/claude-test', 'openrouter', 'OpenRouter')):
            with self.subTest(provider=provider):
                text, fetched, levels, saved = self.render({'provider': provider, 'model': model})
                self.assertIn(label, text)
                self.assertNotIn('via ', text)
                self.assertNotIn('99%', text)
                self.assertNotIn('api $', text)
                self.assertNotIn('cache off', text)
                self.assertEqual(fetched[0].args[0], source)
                self.assertEqual(levels[0].args, (provider, model))
                self.assertFalse(saved)

    def test_switch_to_provider_without_quota_never_reuses_claude_quota(self):
        for provider in ('nvidia', 'minimax', 'opencode-zen', 'gemini', 'ollama-cloud', 'lmstudio'):
            with self.subTest(provider=provider):
                text, fetched, _, _ = self.render({'provider': provider, 'model': 'claude-test'})
                self.assertNotIn('99%', text)
                self.assertNotIn('api $', text)
                self.assertNotIn('cache off', text)
                self.assertFalse(fetched)
                if provider == 'nvidia':
                    self.assertIn('req ▰▰▰▱▱▱▱▱▱▱', text)  # 37%

    def test_same_provider_subagent_quota_is_not_duplicated_after_switch(self):
        text, fetched, _, _ = self.render({'provider': 'openai-oauth', 'model': 'gpt-test'},
                                         {'sub': {'provider': 'openai-oauth', 'model': 'gpt-test'}})
        self.assertNotIn('same provider', text)
        self.assertEqual(text.count('▰▱▱▱▱▱▱▱▱▱'), 1)  # 15%, shown once
        self.assertEqual(len(fetched), 1)

    def test_commandcode_api_and_cli_share_one_quota(self):
        text, fetched, _, _ = self.render({'provider': 'commandcode', 'model': 'claude-test'},
                                         {'sub': {'provider': 'commandcode-cli', 'model': 'worker-test'}})
        self.assertEqual(text.count('$2.00 left'), 1)
        self.assertEqual(len(fetched), 1)

    def test_helper_targets_are_shown_without_changing_main_model(self):
        text, _, _, _ = self.render({'provider': 'openai-oauth', 'model': 'gpt-6.1-sol', 'helpers': {
            'auto': {'provider': 'openai-oauth', 'model': 'gpt-6-luna', 'effort': 'low'},
            'tools': {'provider': 'official', 'model': 'claude-opus-5-5', 'effort': 'medium'},
        }})
        self.assertTrue(text.startswith('gpt-6.1-sol '))
        self.assertIn('Auto: gpt-6-luna', text)
        self.assertIn('Tools: Opus 5.5', text)
        self.assertNotIn('via ', text)
        self.assertNotIn('selected', text)  # a picked helper shows bare; only an unpicked one says "inherit"
        text, _, _, _ = self.render({'provider': 'openai-oauth', 'model': 'gpt-6.1-sol'})
        self.assertIn('Auto:', text)
        self.assertIn('Tools: gpt-6.1-sol', text)
        self.assertIn('inherit', text)

    def test_official_route_uses_friendly_model_label(self):
        text, _, _, _ = self.render({'provider': 'official', 'model': 'claude-opus-5-5'})
        self.assertTrue(text.startswith('Opus 5.5 '), text)
        self.assertNotIn('claude-opus-5-5', text)
        text, _, _, _ = self.render({'provider': 'commandcode', 'model': 'claude-opus-5-5'})
        self.assertTrue(text.startswith('claude-opus-5-5 '), text)

    def test_explicit_official_route_uses_claude_plan(self):
        text, fetched, levels, _ = self.render({'provider': 'official', 'model': 'claude-opus-5-5'})
        self.assertIn('5h ▰▰▰▰▰▰▰▰▰▱', text)
        self.assertNotIn('via ', text)
        self.assertIn('cache off', text)
        self.assertEqual(levels[0].args[0], 'official')
        self.assertFalse(fetched)

    def test_worker_model_does_not_change_main_cache_visibility(self):
        text, _, _, _ = self.render(None, {'sub': {'provider': 'openai-oauth', 'model': 'gpt-test'}})
        self.assertIn('cache off', text.splitlines()[0])
        text, _, _, _ = self.render({'provider': 'openai-oauth', 'model': 'gpt-test'},
                                   {'sub': {'provider': 'official', 'model': 'claude-opus-5-5'}})
        self.assertNotIn('cache off', text.splitlines()[0])

    def test_clearing_route_restores_launch_provider(self):
        text, fetched, _, _ = self.render(None)
        self.assertIn('5h ▰▰▰▰▰▰▰▰▰▱', text)
        self.assertNotIn('via ', text.splitlines()[0])
        self.assertFalse(fetched)
        text, fetched, _, _ = self.render(None, launch_provider='openai-oauth')
        self.assertIn('ChatGPT Plus', text)
        self.assertEqual(fetched[0].args[0], 'openai')

    def test_provider_detection_covers_adapter_and_direct_routes(self):
        cases = {'http://127.0.0.1:10532/openrouter': ('openrouter', 'openrouter'),
                 'http://127.0.0.1:10532/openai-oauth': ('openai-oauth', 'openai'),
                 'http://127.0.0.1:10532/official': ('official', 'anthropic'),
                 'https://opencode.ai/zen/go': ('opencode-go', 'opencode-go'),
                 'https://openrouter.ai/api': ('openrouter', 'openrouter')}
        for url, (provider, source) in cases.items():
            with self.subTest(url=url), patch.dict(os.environ, {'ANTHROPIC_BASE_URL': url}, clear=True):
                self.assertEqual(statusline.quota_source(), source)
                self.assertEqual(statusline.ccx_main_provider(None), provider)


class CacheWindowTests(unittest.TestCase):
    def test_official_countdown_comes_from_claude_codes_own_expiry(self):
        now = 1_000_000.0
        state = statusline.cache_state(
            {'prompt_cache': {'warm': True, 'expires_at': now + 240, 'ttl': '5m'}},
            'official', 'anthropic', 'claude-opus-5-5', now)
        self.assertEqual(state, (20, 240))
        self.assertIsNone(statusline.cache_state(
            {'prompt_cache': {'warm': False}}, 'official', 'anthropic', 'claude-opus-5-5', now))

    def test_other_providers_count_down_the_cache_after_the_last_write(self):
        now = 1_000_000.0
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            path = handle.name
        try:
            os.utime(path, (now - 60, now - 60))
            for provider in ('openai-oauth', 'commandcode', 'commandcode-cli', 'openrouter', 'opencode-go'):
                with self.subTest(provider=provider):
                    self.assertEqual(statusline.cache_state({'transcript_path': path}, provider, None, 'm', now),
                                     (20, 240))
                    # Claude Code's own Anthropic warm flag says nothing about these providers' caches.
                    self.assertEqual(statusline.cache_state(
                        {'transcript_path': path, 'prompt_cache': {'warm': False}}, provider, None, 'm', now),
                        (20, 240))
            os.utime(path, (now - 600, now - 600))
            self.assertIsNone(statusline.cache_state({'transcript_path': path}, 'openai-oauth', None, 'm', now))
            self.assertIsNone(statusline.cache_state({}, 'openai-oauth', None, 'm', now))
            self.assertIsNone(statusline.cache_state({'transcript_path': path}, 'nvidia', None, 'm', now))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()


class SessionCostTests(unittest.TestCase):
    def test_main_and_subagent_cost_with_cache_share(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(statusline, "STATE_DIR", os.path.join(tmp, "state")):
            main = os.path.join(tmp, "s.jsonl")
            reply = lambda mid, model, **u: json.dumps({"message": {"id": mid, "model": model, "usage": u}}) + "\n"
            with open(main, "w") as f:  # the same reply twice (two content lines) counts once
                f.write(reply("a", "claude-opus-5-5", input_tokens=1_000_000, cache_read_input_tokens=1_000_000) * 2)
            os.makedirs(os.path.join(tmp, "s", "subagents"))
            with open(os.path.join(tmp, "s", "subagents", "agent-1.jsonl"), "w") as f:
                f.write(reply("b", "claude-haiku-5-5", output_tokens=2_000_000, cache_creation_input_tokens=1_000_000,
                              cache_creation={"ephemeral_1h_input_tokens": 1_000_000}))
                f.write(reply("c", "unpriced-model", input_tokens=5))  # no known price: tokens only
            with open(os.path.join(tmp, "s", "subagents", "agent-1.meta.json"), "w") as f:
                json.dump({"agentType": "ccx-sub-live"}, f)
            self.assertAlmostEqual(statusline.transcript_cost(main)["usd"], 4.2)  # $4 input + $0.20 cache read
            self.assertEqual(statusline.transcript_cost(main)["sent"], 2_000_000)  # incremental re-read adds nothing
            sub = statusline.subagent_costs(main)["sub"]
            self.assertAlmostEqual(sub["usd"], 6.0)  # past 100K: $5 output + 1h write at 2x $0.50
            self.assertEqual(sub["ctx"], 5)  # the newest reply's context, not a sum
            self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", statusline.cached_text(statusline.transcript_cost(main))), " 50% cached")
            self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", statusline.cost_text(statusline.transcript_cost(main))), "$4.20 (50% cached)")


class HaikuTierTests(unittest.TestCase):
    def test_haiku_long_prompt_bills_whole_request_at_long_rate(self):
        short = statusline.usage_cost("claude-haiku-5-5", {"input_tokens": 100_000, "output_tokens": 1_000_000})
        long = statusline.usage_cost("claude-haiku-5-5", {"input_tokens": 100_001, "output_tokens": 1_000_000})
        self.assertAlmostEqual(short, 0.01 + 0.5)
        self.assertAlmostEqual(long, 100_001 * 0.5 / 1e6 + 2.5)


class BarTests(unittest.TestCase):
    def test_empty_bar_outlines_green_and_partial_bar_dim(self):
        self.assertTrue(statusline.bar(9).startswith(statusline.DIM + statusline.color(0) + "▱" * 10))
        self.assertIn(statusline.DIM + "▱" * 9, statusline.bar(10))


class FreshSessionModeRowTests(unittest.TestCase):
    def test_mode_row_shows_zero_and_week_before_any_transcript(self):
        with tempfile.TemporaryDirectory() as home, \
                patch.object(statusline.os.path, "expanduser", lambda p: p.replace("~", home)), \
                patch.dict(os.environ, {"CCX_LAUNCH_ID": ""}), \
                patch.object(statusline, "provider_week", lambda key, now: ("Ultra", "7d $9.00/$90 │ Ultra")):
            row = statusline.mode_row("00000000-0000-0000-0000-000000000000", time.time())
        self.assertEqual(row, "≈ $0.00 (7d $9.00/$90 │ Ultra)")

    def test_mode_row_splits_out_subagent_share(self):
        sid = "00000000-0000-0000-0000-000000000001"
        priced = lambda usd: {"usd": usd, "sent": 1, "read": 0, "priced": True}
        with tempfile.TemporaryDirectory() as home, \
                patch.object(statusline.os.path, "expanduser", lambda p: p.replace("~", home)), \
                patch.dict(os.environ, {"CCX_LAUNCH_ID": ""}), \
                patch.object(statusline, "ccx_main_provider", lambda source: "official"), \
                patch.object(statusline, "transcript_cost", lambda path: priced(9.0)), \
                patch.object(statusline, "subagent_costs", lambda path: {"sub": priced(3.5), "Explore": priced(0.5)}), \
                patch.object(statusline, "provider_week", lambda key, now: ("Ultra", "7d $9.00/$90 │ Ultra")):
            os.makedirs(os.path.join(home, ".claude", "projects", "p"))
            open(os.path.join(home, ".claude", "projects", "p", sid + ".jsonl"), "w").close()
            row = statusline.mode_row(sid, time.time())
        self.assertEqual(row, "≈ $13.00 · agents $4.00 (7d $9.00/$90 │ Ultra)")
