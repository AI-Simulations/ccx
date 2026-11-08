"""Per-session subagent profiles, /model route overrides and relay stop nudges."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load_switch():
    spec = importlib.util.spec_from_file_location("ccx_switch_under_test", HERE / "ccx-switch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CcSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ccx = load_switch()
        self.ccx.CFG_DIR = str(root)
        self.ccx.AGENTS_FILE = str(root / "agents.json")
        self.ccx.SESSION_AGENTS_FILE = str(root / "session-agents.json")
        self.ccx.ROUTES_FILE = str(root / "routes.json")
        self.ccx.FALLBACKS_FILE = str(root / "fallbacks.json")
        self.ccx.CLAUDE_SETTINGS = str(root / "claude-settings.json")
        self.ccx.JOBS_DIR = str(root / "jobs")
        self.ccx.AGENTS_DIR = str(root / "agents")
        self.ccx.sync_agents = lambda: None
        (root / "jobs").mkdir()
        self.global_agents = {"sub": {"provider": "official", "effort": "high"}}
        (root / "agents.json").write_text(json.dumps(self.global_agents), encoding="utf-8")
        self.old_launch = os.environ.get("CCX_LAUNCH_ID")
        os.environ["CCX_LAUNCH_ID"] = "launch-a"
        self.claudecode = patch.dict(os.environ, {"CLAUDECODE": "1"})  # edits come from inside the session
        self.claudecode.start()

    def tearDown(self):
        self.claudecode.stop()
        if self.old_launch is None:
            os.environ.pop("CCX_LAUNCH_ID", None)
        else:
            os.environ["CCX_LAUNCH_ID"] = self.old_launch
        self.tmp.cleanup()

    def test_subagent_edit_is_isolated_to_the_launch(self):
        changed = {"sub": {"provider": "official", "effort": "low"}}
        self.ccx.save_agents(changed)
        self.assertEqual(self.ccx.load_agents()["sub"]["effort"], "low")
        os.environ["CCX_LAUNCH_ID"] = "launch-b"
        self.assertEqual(self.ccx.load_agents()["sub"]["effort"], "high")
        self.assertEqual(json.loads(Path(self.ccx.AGENTS_FILE).read_text()), self.global_agents)

    def test_subagents_off_is_per_session_and_survives_profile_edits(self):
        call = {"tool_name": "Agent", "tool_input": {"subagent_type": "ccx-sub", "prompt": "x"}}
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.set_subagents_off(True)
        self.assertEqual(self.ccx.load_agents(), self.global_agents, "off keeps the global profiles")
        self.assertEqual(self.ccx.agent_hook(call)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Subagents are off", self.ccx.hook_context({})["hookSpecificOutput"]["additionalContext"])
        self.ccx.save_agents({"sub": {"provider": "official", "effort": "low"}})
        self.assertTrue(self.ccx.subagents_off(), "a profile edit keeps the switch")
        self.ccx.link_session("sid-1")
        os.environ["CCX_LAUNCH_ID"] = "launch-b"
        self.assertFalse(self.ccx.subagents_off(), "other sessions keep their subagents")
        self.ccx.link_session("sid-1")
        self.assertTrue(self.ccx.subagents_off(), "a resume keeps the switch")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.set_subagents_off(False)
        self.assertFalse(self.ccx.subagents_off())
        self.assertEqual(self.ccx.load_agents()["sub"]["effort"], "low")

    def test_context_is_resent_only_on_session_start_or_change(self):
        self.ccx.CTX_SENT_DIR = str(Path(self.tmp.name) / "ctx-sent")
        ctx = lambda ev: self.ccx.hook_context({"session_id": "sid-ctx", "hook_event_name": ev})
        self.assertIn("additionalContext", ctx("SessionStart")["hookSpecificOutput"])
        self.assertEqual(ctx("UserPromptSubmit"), {}, "an unchanged rule set is not repeated per prompt")
        self.assertIn("additionalContext", ctx("SessionStart")["hookSpecificOutput"], "compaction/clear resends")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.set_subagents_off(True)
        self.assertIn("Subagents are off", ctx("UserPromptSubmit")["hookSpecificOutput"]["additionalContext"])

    def test_router_offers_levels_or_sub_per_session(self):
        levels = {n: {"provider": "official", "description": f"{n} tasks"} for n in ("easy", "mid", "hard")}
        Path(self.ccx.AGENTS_FILE).write_text(json.dumps({**self.global_agents, **levels}), encoding="utf-8")
        call = lambda kind: {"tool_name": "Agent", "tool_input": {"subagent_type": kind, "prompt": "x"}}
        decision = lambda kind: (self.ccx.agent_hook(call(kind)).get("hookSpecificOutput") or {}).get("permissionDecision")
        self.assertEqual(list(self.ccx.offered_agents()), ["easy", "mid", "hard"], "the router by default")
        self.assertEqual(decision("ccx-sub"), "deny")
        context = self.ccx.hook_context({})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("(easy tasks)", context)
        self.assertIn("0 (trivial) to 100", context)
        self.assertIn("ccx-easy 0-75, ccx-mid 76-89, ccx-hard 90-100", context)
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.set_ranges(["20", "90"])
        self.assertIn("ccx-easy 0-20, ccx-mid 21-90, ccx-hard 91-100",
                      self.ccx.hook_context({})["hookSpecificOutput"]["additionalContext"])
        with self.assertRaises(SystemExit):  # tops must rise and stay under 100
            self.ccx.set_ranges(["90", "20"])
        self.assertEqual(self.ccx.level_ranges(self.ccx.load_agents())["mid"], (21, 90), "a rejected edit changes nothing")
        self.assertIn("Review every ccx worker", context)
        self.assertNotIn("ccx-sub:", context)
        self.assertEqual(decision("ccx-sub"), "deny")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.set_router(False)
        self.assertEqual(list(self.ccx.offered_agents()), ["sub"])
        self.assertEqual(decision("ccx-easy"), "deny")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.set_router(True)
        self.ccx.link_session("sid-r")
        os.environ["CCX_LAUNCH_ID"] = "launch-b"
        self.assertTrue(self.ccx.router_on(), "other sessions start on the router")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.set_router(False)
        os.environ["CCX_LAUNCH_ID"] = "launch-a"
        self.ccx.link_session("sid-r")
        self.assertTrue(self.ccx.router_on(), "a resume keeps the switch")
        self.assertFalse(self.ccx.router_on() and os.environ.update(CCX_LAUNCH_ID="launch-b") or self.ccx.router_on(),
                         "launch-b keeps its own off")
        Path(self.ccx.AGENTS_FILE).write_text(json.dumps(self.global_agents), encoding="utf-8")
        os.environ["CCX_LAUNCH_ID"] = "launch-c"  # a session that never saved levels of its own
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.set_router(True)
        self.assertEqual(list(self.ccx.offered_agents()), ["sub"], "no levels: the switch changes nothing")

    def test_router_logs_scores_and_calibrates_ranges(self):
        levels = {n: {"provider": "official"} for n in ("easy", "mid", "hard")}
        Path(self.ccx.AGENTS_FILE).write_text(json.dumps({**self.global_agents, **levels}), encoding="utf-8")
        self.assertIn("[35] rename helper", self.ccx.hook_context({})["hookSpecificOutput"]["additionalContext"])
        call = lambda kind, desc, mode="default": self.ccx.agent_hook(
            {"tool_name": "Agent", "transcript_path": self.tmp.name + "/t.jsonl", "permission_mode": mode,
             "tool_input": {"subagent_type": kind, "description": desc, "prompt": "x"}})
        for score in range(0, 100, 4):  # 25 calls: 0, 4, ..., 96
            call("ccx-easy", f"[{score}] task")
        call("ccx-easy", "no score")
        call("ccx-easy", "[150] out of range")
        self.assertEqual(call("ccx-easy", "[50] denied once", "auto")["hookSpecificOutput"]["permissionDecision"], "deny")
        call("ccx-sub", "[50] not a router level")
        logged = [json.loads(line) for line in Path(self.ccx.router_log()).read_text().splitlines()]
        self.assertEqual(len(logged), 25, "only valid scores are kept")
        self.assertEqual([r["level"] for r in logged if r["score"] in (4, 76, 92)], ["easy", "mid", "hard"],
                         "the score picks the level, not the named type")
        # the pick: any named level runs where the score belongs; no score is refused
        kind = lambda result: result["hookSpecificOutput"].get("updatedInput", {}).get("subagent_type")
        self.assertEqual(kind(call("ccx-hard", "[30] rename")), "ccx-easy")
        self.assertEqual(kind(call("ccx-easy", "[95] redesign")), "ccx-hard")
        denied = call("ccx-mid", "no score")["hookSpecificOutput"]
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn('"[35] rename helper"', denied["permissionDecisionReason"])
        auto = call("ccx-hard", "[30] rename", "auto")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn('subagent_type "ccx-easy"', auto, "auto mode is told the level to resubmit on")
        Path(self.ccx.router_log()).write_text("".join(json.dumps(r) + "\n" for r in logged))
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.ccx.calibrate_ranges(["60", "20"])
        self.assertEqual(self.ccx.level_ranges(self.ccx.load_agents()), {"easy": (0, 56), "mid": (57, 76), "hard": (77, 100)})
        self.assertIn("easy 60%, mid 20%, hard 20%", out.getvalue())
        for bad in (["60"], ["60", "40"], ["x", "1"], ["0", "20"]):
            with self.assertRaises(SystemExit):
                self.ccx.calibrate_ranges(bad)
        Path(self.ccx.router_log()).write_text('{"score": 5}\n' * 3)
        with self.assertRaises(SystemExit):  # too few scores
            self.ccx.calibrate_ranges(["60", "20"])
        Path(self.ccx.router_log()).write_text('{"score": 100}\n' * 30)  # all ties at the top still give rising tops
        with patch("sys.stdout", io.StringIO()):
            self.ccx.calibrate_ranges(["60", "20"])
        self.assertEqual(self.ccx.level_ranges(self.ccx.load_agents())["mid"], (99, 99))

    def test_router_levels_survive_presets_and_reach_old_sessions(self):
        levels = {"easy": {"provider": "official", "model": "claude-haiku-5-5"},
                  "hard": {"provider": "official", "model": "claude-opus-5-5"}}
        Path(self.ccx.AGENTS_FILE).write_text(json.dumps({**self.global_agents, **levels}), encoding="utf-8")
        self.ccx.saved_bundle = lambda name: {"subagent": {"provider": "official", "model": "claude-sonnet-5-5"}}
        seeded = self.ccx.preset_agents("official")
        self.assertEqual(seeded["sub"]["model"], "claude-sonnet-5-5")
        self.assertEqual(seeded["hard"]["model"], "claude-opus-5-5", "a preset never flattens the levels")
        # a session that saved only `sub` before the levels existed still gets them
        self.ccx.save_agents({"sub": {"provider": "official", "effort": "low"}})
        agents = self.ccx.load_agents()
        self.assertEqual(agents["sub"]["effort"], "low")
        self.assertEqual(agents["hard"]["model"], "claude-opus-5-5")

    def test_router_preset_saves_seeds_and_applies_levels(self):
        levels = {"easy": {"provider": "official", "model": "claude-haiku-5-5", "effort": "max", "upto": 40},
                  "mid": {"provider": "official", "model": "claude-sonnet-5-5", "upto": 80},
                  "hard": {"provider": "official", "model": "claude-opus-5-5", "effort": "medium"}}
        main = {"provider": "official", "model": "claude-opus-5-5"}
        bundle = {"model": main, "subagent": {"provider": "router", "levels": levels}, "fallback": {"provider": "off"},
                  "auto": main, "tools": main}
        self.ccx.model_efforts = lambda provider, model: None
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.presets_command(["set-bundle", "mine", json.dumps(bundle)])
        self.assertEqual(self.ccx.saved_bundle("mine")["subagent"]["levels"]["mid"]["upto"], 80)
        bad = {**bundle, "subagent": {"provider": "router", "levels": {**levels, "mid": {**levels["mid"], "upto": 30}}}}
        with self.assertRaises(SystemExit), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            self.ccx.presets_command(["set-bundle", "mine", json.dumps(bad)])  # tops must rise
        with patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.set_router(False)
            self.ccx.presets_command(["use-subagent", "mine"])
        self.assertTrue(self.ccx.router_on())
        agents = self.ccx.load_agents()
        self.assertEqual(agents["sub"], self.global_agents["sub"], "a router preset leaves the one-model subagent")
        self.assertEqual(self.ccx.level_ranges(agents), {"easy": (0, 40), "mid": (41, 80), "hard": (81, 100)})
        self.assertEqual(agents["easy"]["model"], "claude-haiku-5-5")
        # a new launch seeds the same levels and turns the router on
        os.environ["CCX_LAUNCH_ID"] = "launch-new"
        self.ccx.seed_launch_from_preset("mine", "launch-new", adapter=False, providers=[])
        self.assertEqual(self.ccx.load_agents()["hard"]["model"], "claude-opus-5-5")
        self.assertTrue(self.ccx.router_on())
        # the default preset subagent is the router on the levels as set
        Path(self.ccx.AGENTS_FILE).write_text(json.dumps({**self.global_agents, **levels}), encoding="utf-8")
        self.assertEqual(self.ccx.default_router_target()["levels"]["easy"]["upto"], 40)

    def test_best_levels_pick_by_aa_and_price(self):
        row = lambda id, aa, price, score: {"id": id, "aa": aa, "in": price, "out": price * 5, "score": score}
        rows = [row("opus", 59, 4, 75), row("sonnet", 54, 2, 74), row("fable", 50, 10, 53),
                row("haiku", 36, 0.1, 67), row("tiny", 20, 0.01, 60), {"id": "alias", "aa": None, "in": 1, "out": 1}]
        # very hard: top AA; every other level: the cheapest pick reaching its share of that AA (tiny is too weak)
        self.assertEqual(self.ccx.best_levels(rows), {"very-easy": ("haiku", None), "easy": ("sonnet", None),
                                                      "mid": ("sonnet", None), "hard": ("sonnet", None),
                                                      "very-hard": ("opus", None)})
        self.assertEqual(self.ccx.best_levels([{"id": "x", "aa": None}]), {})
        # per-effort AA scores make (model, effort) pairs; a higher effort costs more (EFFORT_COST)
        rows = [{"id": "opus", "aa": 59, "in": 4, "out": 20, "aa_efforts": {"max": 59, "high": 52, "low": 35}},
                {"id": "haiku", "aa": 36.5, "in": 0.1, "out": 0.5, "aa_efforts": {"max": 36.5, "xhigh": 34.6}}]
        self.assertEqual(self.ccx.best_levels(rows), {"very-easy": ("haiku", "xhigh"), "easy": ("opus", "high"),
                                                      "mid": ("opus", "high"), "hard": ("opus", "max"),
                                                      "very-hard": ("opus", "max")})
        # efforts a model does not take on this provider are skipped
        only = lambda model: ["low", "high"] if model == "opus" else None
        self.assertEqual(self.ccx.best_levels(rows, only)["very-hard"], ("opus", "high"))
        # each pick's effort comes from that model's own ladder
        effort = lambda levels: [self.ccx.level_effort(l, levels) for l in self.ccx.ROUTER_LEVELS]
        self.assertEqual(effort(["low", "medium", "high", "xhigh", "max"]), ["xhigh", "xhigh", "high", "medium", "high"])
        self.assertEqual(effort(["low", "high"]), ["low", "low", "high", "high", "high"])

    def test_router_preset_levels_own_their_compact_window(self):
        old = {"sub": {}, "easy": {"provider": "official", "autocompact": "100k", "description": "mine"},
               "hard": {"provider": "official", "autocompact": 50}}
        target = {"provider": "router", "levels": {"easy": {"provider": "official", "model": "h"},
                                                    "hard": {"provider": "official", "model": "o", "autocompact": "200k"}}}
        profiles = self.ccx.preset_profiles(target, old)
        self.assertNotIn("autocompact", profiles["easy"], "a level without one inherits, not the old window")
        self.assertEqual(profiles["easy"]["description"], "mine")
        self.assertEqual(profiles["hard"]["autocompact"], "200k")

    def test_router_falls_back_to_a_level_the_session_knows(self):
        ranges = {"very-easy": (0, 30), "easy": (31, 75), "mid": (76, 89), "hard": (90, 95), "very-hard": (96, 100)}
        known = {"ccx-easy", "ccx-easy-live", "ccx-mid-live", "ccx-hard"}
        self.assertEqual(self.ccx.usable_level("very-easy", ranges, known), "easy")
        self.assertEqual(self.ccx.usable_level("very-hard", ranges, known), "hard")
        self.assertEqual(self.ccx.usable_level("mid", ranges, known), "mid")
        self.assertEqual(self.ccx.usable_level("very-easy", ranges, None), "very-easy", "no listing: trust the pick")
        transcript = Path(self.tmp.name) / "t.jsonl"
        listing = lambda added, removed=(): json.dumps({"type": "attachment", "attachment": {
            "type": "agent_listing_delta", "addedTypes": list(added), "removedTypes": list(removed)}})
        transcript.write_text("\n".join([listing(["ccx-easy", "ccx-sub"]), listing(["ccx-hard"], ["ccx-sub"])]) + "\n")
        self.assertEqual(self.ccx.listed_types(str(transcript)), {"ccx-easy", "ccx-hard"})
        levels = {n: {"provider": "official"} for n in ranges}
        Path(self.ccx.AGENTS_FILE).write_text(json.dumps({**self.global_agents, **levels}), encoding="utf-8")
        result = self.ccx.agent_hook({"tool_name": "Agent", "transcript_path": str(transcript),
                                      "tool_input": {"subagent_type": "ccx-mid", "description": "[10] x", "prompt": "x"}})
        self.assertEqual(result["hookSpecificOutput"]["updatedInput"]["subagent_type"], "ccx-easy")

    def test_review_hook_reminds_only_after_ccx_workers(self):
        out = self.ccx.review_hook({"tool_name": "Agent", "tool_input": {"subagent_type": "ccx-mid"}})
        self.assertIn("Review every ccx worker", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.ccx.review_hook({"tool_name": "Agent", "tool_input": {"subagent_type": "Explore"}}), {})

    def test_new_launch_starts_on_the_saved_provider_preset(self):
        bundle = {"model": {"provider": "official", "model": "claude-opus-5-5", "effort": "medium"},
                  "subagent": {"provider": "official", "model": "claude-haiku-5-5", "effort": "max"},
                  "fallback": {"provider": "off"},
                  "auto": {"provider": "official", "model": "claude-haiku-5-5", "effort": "low"},
                  "tools": {"provider": "official", "model": "claude-haiku-5-5", "effort": "max"}}
        Path(self.ccx.CFG_DIR, "presets.json").write_text(json.dumps({"official": {"model": "claude-opus-5-5", "settings": bundle}}), encoding="utf-8")
        self.assertEqual(self.ccx.default_effort("official", "claude-opus-5-5"), "medium")
        self.assertIsNone(self.ccx.default_effort("official", "claude-sonnet-5-5"))
        self.ccx.seed_launch_from_preset("official", "launch-a", adapter=True, providers=list(self.ccx.PROVIDERS))
        self.assertEqual(self.ccx.load_agents()["sub"], {"provider": "official", "model": "claude-haiku-5-5", "effort": "max"})
        self.assertEqual(self.ccx.load_routes()["launch-a"]["helpers"]["auto"]["model"], "claude-haiku-5-5")
        self.assertEqual(json.loads(Path(self.ccx.AGENTS_FILE).read_text()), self.global_agents, "global profiles untouched")
        self.assertIsNone(self.ccx.preset_fallback("official"))
        self.assertFalse(self.ccx.router_on(), "a one-model preset starts the launch off the router")
        os.environ["CCX_LAUNCH_ID"] = "launch-b"
        self.ccx.save_agents({"sub": {"provider": "official", "effort": "low"}})
        self.ccx.seed_launch_from_preset("official", "launch-b", adapter=False, providers=[])
        self.assertEqual(self.ccx.load_agents()["sub"]["effort"], "low", "an explicit pick wins over the preset")

    def test_resume_keeps_the_sessions_subagents(self):
        self.ccx.save_agents({"sub": {"provider": "official", "effort": "medium"}})
        self.ccx.link_session("sid-1")
        os.environ["CCX_LAUNCH_ID"] = "launch-resumed"
        self.assertEqual(self.ccx.load_agents()["sub"]["effort"], "high")
        self.ccx.link_session("sid-1")
        self.assertEqual(self.ccx.load_agents()["sub"]["effort"], "medium")

    def test_agent_runs_in_process_through_the_adapter_and_relays_otherwise(self):
        root = Path(self.tmp.name)
        main = root / "main.jsonl"
        listing = {"type": "attachment", "attachment": {"type": "agent_listing_delta", "addedTypes": ["ccx-sub-live"], "removedTypes": []}}
        main.write_text(json.dumps(listing) + "\n", encoding="utf-8")
        call = {"tool_name": "Agent", "transcript_path": str(root / "main.jsonl"),
                "tool_input": {"subagent_type": "ccx-sub", "prompt": "do it", "description": "d"}}
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.ccx.ADAPTER_PORT}/official"}):
            live = self.ccx.agent_hook(call)["hookSpecificOutput"]["updatedInput"]
            self.assertEqual((live["subagent_type"], live["prompt"]), ("ccx-sub-live", "do it"))
            main.write_text('{"type":"attachment","attachment":{"type":"agent_listing_delta","addedTypes":["ccx-sub"]}}\n', encoding="utf-8")
            fresh = self.ccx.agent_hook(call)["hookSpecificOutput"]["updatedInput"]
            self.assertEqual(fresh["subagent_type"], "ccx-sub", "A session that never listed the twin relays")
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": ""}):
            relayed = self.ccx.agent_hook({**call, "tool_input": {**call["tool_input"], "subagent_type": "ccx-sub-live"}})
        updated = relayed["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["subagent_type"], "ccx-sub", "A direct session relays even a ccx-*-live call")
        self.assertTrue(updated["prompt"].startswith(self.ccx.CONTEXT_PREFIX))

    def test_auto_mode_gets_the_input_to_resubmit_instead_of_a_rewrite(self):
        # auto mode refuses hook-rewritten calls: the hook denies once with the exact envelope, the resubmitted
        # call (same envelope, caller's token kept) passes through unchanged
        main = Path(self.tmp.name) / "main.jsonl"
        main.write_text("{}\n", encoding="utf-8")
        call = {"tool_name": "Agent", "transcript_path": str(main), "permission_mode": "auto",
                "tool_input": {"subagent_type": "ccx-sub", "prompt": "do it", "description": "d"}}
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": ""}):
            out = self.ccx.agent_hook(call)["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")
            line = next(x for x in out["permissionDecisionReason"].splitlines() if x.startswith(self.ccx.CONTEXT_PREFIX))
            again = {**call, "tool_input": {**call["tool_input"], "prompt": line + "\ndo it"}}
            self.assertEqual(self.ccx.agent_hook(again), {}, "resubmitted call runs as reviewed")
            forged = json.loads(line[len(self.ccx.CONTEXT_PREFIX):])
            forged["source"] = str(Path(self.tmp.name) / "other.jsonl")
            bad = {**call, "tool_input": {**call["tool_input"],
                                          "prompt": self.ccx.CONTEXT_PREFIX + json.dumps(forged) + "\ndo it"}}
            self.assertEqual(self.ccx.agent_hook(bad)["hookSpecificOutput"]["permissionDecision"], "deny",
                             "a caller-made envelope pointing elsewhere is never accepted")
            plain = self.ccx.agent_hook({**call, "permission_mode": "default"})["hookSpecificOutput"]
            self.assertTrue(plain["updatedInput"]["prompt"].startswith(self.ccx.CONTEXT_PREFIX), "other modes rewrite")

    def test_worker_context_cuts_a_long_unattended_turn_at_a_step(self):
        prompt = {"type": "user", "uuid": "p", "message": {"role": "user", "content": "go"}}
        turn = []
        for i in range(10):
            turn.append({"type": "assistant", "uuid": f"a{i}", "message": {"role": "assistant", "content": "x" * 4000}})
            turn.append({"type": "user", "uuid": f"r{i}", "message": {"role": "user", "content": [{"type": "tool_result", "content": "y"}]}})
        tail = self.ccx._context_tail([prompt, *turn], budget=2500)
        self.assertEqual([e["uuid"] for e in tail], ["p", "a8", "r8", "a9", "r9"])
        self.assertEqual(tail[1]["parentUuid"], "p")
        self.assertEqual(len(self.ccx._context_tail([prompt, *turn], budget=100000)), 21, "A turn that fits stays whole")

    def test_a_terminal_left_with_an_old_launch_id_edits_the_global_profiles(self):
        changed = {"sub": {"provider": "official", "effort": "low"}}
        with patch.dict(os.environ, {"CLAUDECODE": ""}):
            self.ccx.save_agents(changed)
        self.assertEqual(json.loads(Path(self.ccx.AGENTS_FILE).read_text()), changed)
        self.assertFalse(Path(self.ccx.SESSION_AGENTS_FILE).exists())

    def test_startup_prompt_offers_the_last_launch_picks(self):
        last = {"sub": {"provider": "opencode-go", "model": "mimo-v2.6-pro", "effort": "high"}}
        Path(self.ccx.SESSION_AGENTS_FILE).write_text(json.dumps({"old": {"agents": last, "at": 1}}), encoding="utf-8")
        os.environ["CCX_LAUNCH_ID"] = "new-launch"
        shown = {}
        with patch.object(self.ccx.sys.stdin, "isatty", return_value=True),                 patch.object(self.ccx.sys.stdout, "isatty", return_value=True),                 patch.object(self.ccx, "edit_agent", side_effect=lambda agents, *a, **k: shown.update(agents)):
            self.ccx.startup_subagent()
        self.assertEqual(shown, last)
        self.assertEqual(self.ccx.load_agents(), last, "keeping the offered pick keeps it for this launch")

    def test_startup_router_menu_sets_ranges_and_switch_and_preset_keeps_router(self):
        levels = {n: {"provider": "official", "model": "claude-" + n, "effort": "high"} for n in ("easy", "mid", "hard")}
        Path(self.ccx.AGENTS_FILE).write_text(json.dumps({"sub": {"provider": "official"}, **levels}), encoding="utf-8")
        picks = iter(["ranges", "toggle", "done"])
        with patch.object(self.ccx.sys.stdin, "isatty", return_value=True),                 patch.object(self.ccx.sys.stdout, "isatty", return_value=True),                 patch.object(self.ccx.catalog, "_arrow_menu", side_effect=lambda *a, **k: next(picks)),                 patch("builtins.input", return_value="40 80"):
            self.ccx.startup_subagent()
        self.assertEqual(self.ccx.level_ranges(self.ccx.load_agents()), {"easy": (0, 40), "mid": (41, 80), "hard": (81, 100)})
        self.assertFalse(self.ccx.router_on())
        # the preset editor keeps a router preset a router, on this launch's levels
        menus = iter(["official", "router", "done"])
        with patch.object(self.ccx, "provider_menu", side_effect=lambda *a, **k: next(menus)),                 patch.object(self.ccx.catalog, "_arrow_menu", side_effect=lambda *a, **k: next(menus)),                 patch.object(self.ccx, "model_menu", return_value="claude-opus-5-5"),                 patch.object(self.ccx, "effort_menu", return_value="high"),                 patch.object(self.ccx, "fallback_menu", return_value=None),                 patch("sys.stdout", new_callable=io.StringIO):
            self.ccx.edit_preset()
        sub = self.ccx.saved_bundle("official")["subagent"]
        self.assertEqual(sub["provider"], "router")
        self.assertEqual({n: v.get("upto") for n, v in sub["levels"].items()}, {"easy": 40, "mid": 80, "hard": None})

    def test_custom_providers_join_the_routes_and_leave_again(self):
        ccx = self.ccx
        ccx.CUSTOM_PROVIDERS_FILE = str(Path(self.tmp.name) / "providers.json")
        with patch("sys.stdout", new_callable=io.StringIO):
            ccx.providers_command(["add", "local", "http://127.0.0.1:9000/v1", "--no-key"])
            ccx.providers_command(["add", "gate", "https://gate.example/anthropic", "--format", "anthropic", "--x-api-key"])
        self.assertEqual(ccx.PROVIDERS["local"]["format"], "openai")
        with patch.object(ccx, "ensure_anthropic_proxy"):
            env = ccx.route_env("local", "some-model")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], f"http://127.0.0.1:{ccx.ADAPTER_PORT}/local", "keyless: no key demanded")
        with patch.object(ccx, "auth_key", return_value="k"):
            env = ccx.route_env("gate", "m")
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_CUSTOM_HEADERS"]), ("https://gate.example/anthropic", "x-api-key: k"))
        for bad in (["add", "nvidia", "http://x"], ["add", "ok", "ftp://x"], ["add", "ok", "http://x", "--format", "soap"]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
                ccx.providers_command(bad)
        with patch("sys.stdout", new_callable=io.StringIO):
            ccx.providers_command(["remove", "local"])
        self.assertNotIn("local", ccx.PROVIDERS)
        self.assertIn("gate", ccx.PROVIDERS)

    def test_fallback_refuses_a_session_that_connects_directly(self):
        with patch.dict(os.environ, {"CCX_MAIN_PROVIDER": "nvidia"}), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.ccx.fallback_command(["nvidia", "deepseek-ai/x"])
        self.assertIn("Fallback: nvidia/deepseek-ai/x", out.getvalue())
        with patch.dict(os.environ, {"CCX_MAIN_PROVIDER": "official", "ANTHROPIC_BASE_URL": ""}), self.assertRaises(SystemExit):
            self.ccx.fallback_command([])

    def test_tools_default_low_only_when_supported(self):
        for provider, levels, expected in [('openai-oauth', ['low', 'high'], 'low'),
                                            ('commandcode-cli', ['high', 'max'], None),
                                            ('nvidia', None, None), ('commandcode-cli', [], None),
                                            ('official', None, 'low')]:
            with self.subTest(provider=provider, levels=levels), patch.object(self.ccx, 'model_efforts', return_value=levels):
                self.assertEqual(self.ccx.lowest_effort(provider, 'main-model'), expected)

    def test_sol_default_is_high_without_overriding_explicit_effort(self):
        ccx = self.ccx
        with patch.object(ccx, 'route_env', return_value={}), patch.object(ccx, 'load_state', return_value={}), \
                patch.object(ccx, 'save_state'), patch.object(ccx, 'write_apply') as write, \
                patch('sys.stdout', new_callable=io.StringIO):
            ccx.apply('openai-oauth', 'gpt-6.1-sol')
            self.assertEqual(write.call_args.args[3], ['--effort', 'high'])
            ccx.apply('openai-oauth', 'gpt-6.1-sol', effort='low')
            self.assertEqual(write.call_args.args[3], ['--effort', 'low'])
            ccx.apply('openai-oauth', 'gpt-6.1-sol', claude_args=['--effort=medium'])
            self.assertEqual(write.call_args.args[3], ['--effort=medium'])
            ccx.apply('nvidia', 'test/model')
            self.assertEqual(write.call_args.args[3], ())
        ccx.set_route('launch-a', 'openai-oauth', 'gpt-6.1-sol')
        self.assertEqual(ccx.load_routes()['launch-a']['effort'], 'high')
        ccx.set_route('launch-a', 'openai-oauth', 'gpt-6.1-sol', 'low')
        self.assertEqual(ccx.load_routes()['launch-a']['effort'], 'low')

    def test_plain_effort_default_is_launch_only_and_resume_is_untouched(self):
        ccx = self.ccx
        for args, expected in [([], 'medium'), (['--effort', 'high'], 'medium'),
                               (['--resume', 'sid'], None), (['-c'], None),
                               (['--model', 'sonnet'], None)]:
            with self.subTest(args=args), patch.object(ccx.sys, 'argv', ['ccx', 'plain', *args]), \
                    patch.object(ccx, 'APPLY_FILE', str(Path(self.tmp.name) / 'apply.ps1')), \
                    patch.object(ccx, 'quit_on_ctrl_c'), patch.object(ccx, 'apply') as apply:
                ccx.main()
            self.assertEqual(apply.call_args.kwargs['effort'], expected)
            self.assertEqual(apply.call_args.kwargs['claude_args'], args)
        with patch.object(ccx, 'route_env', return_value={}), patch.object(ccx, 'load_state', return_value={}), \
                patch.object(ccx, 'save_state'), patch.object(ccx, 'write_apply') as write, \
                patch('sys.stdout', new_callable=io.StringIO):
            ccx.apply('official', effort='medium', claude_args=['--effort', 'high'])
            self.assertEqual(write.call_args.args[3], ['--effort', 'high'])

    def test_official_keeps_the_adapter_and_counts_as_first_party(self):
        adapter = f"http://127.0.0.1:{self.ccx.ADAPTER_PORT}/official"
        flag = "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"
        self.assertIn(flag, self.ccx.CCX_VARS, "other routes clear a flag left by an official launch")
        for env, expected in [({}, adapter), ({"CCX_OFFICIAL_DIRECT": "1"}, None)]:
            with self.subTest(env=env), patch.dict(os.environ, {"CCX_OFFICIAL_DIRECT": "", **env}), \
                    patch.object(self.ccx, "ensure_anthropic_proxy"):
                got = self.ccx.route_env("official", None, passthrough=True)
            self.assertEqual(got.get("ANTHROPIC_BASE_URL"), expected)
            # Claude Code keeps its first-party helpers (Haiku agent summaries) behind the adapter
            self.assertEqual(got.get(flag), "1" if expected else None)

    def test_official_route_effort_reaches_claude_settings(self):
        settings = Path(self.tmp.name) / 'settings.json'
        settings.write_text(json.dumps({'theme': 'dark', 'modelSettings': {'claude-opus-5-5': {
            'effortLevel': 'high', 'autoCompactWindow': 400000}}}), encoding='utf-8')
        with patch.object(self.ccx, 'CLAUDE_SETTINGS', str(settings)):
            self.ccx.set_route('launch-a', 'official', 'claude-opus-5-5', 'medium')
            data = json.loads(settings.read_text(encoding='utf-8'))
            self.assertEqual(data['modelSettings']['claude-opus-5-5'], {'effortLevel': 'medium', 'autoCompactWindow': 400000})
            self.assertEqual(data['theme'], 'dark')
            for args in [('official', 'claude-opus-5-5', 'max'), ('openai-oauth', 'claude-opus-5-5', 'low')]:
                self.ccx.set_route('launch-a', *args)
            self.assertEqual(json.loads(settings.read_text(encoding='utf-8')), data)

    def test_route_override_set_and_clear(self):
        self.ccx.set_route("launch-a", "gemini", "gemini-3.8-flash")
        self.assertEqual(self.ccx.load_routes()["launch-a"]["model"], "gemini-3.8-flash")
        self.ccx.set_route("launch-a")
        self.assertNotIn("launch-a", self.ccx.load_routes())

    def test_helper_picks_preserve_main_route_and_other_sessions(self):
        self.ccx.set_route('launch-a', 'openai-oauth', 'gpt-6.1-sol', 'high')
        self.ccx.set_route('launch-b', 'gemini', 'gemini-test')
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.ccx.ADAPTER_PORT}/official'}), patch('sys.stdout', new_callable=io.StringIO):
            self.ccx.helper_command(['auto', 'openai-oauth', 'gpt-6-luna', 'low'])
            self.ccx.helper_command(['tools', 'openai-oauth', 'gpt-6-luna', 'medium'])
        row = self.ccx.load_routes()['launch-a']
        self.assertEqual(row['model'], 'gpt-6.1-sol')
        self.assertEqual(row['effort'], 'high')
        self.assertEqual(row['helpers']['auto']['effort'], 'low')
        self.assertEqual(row['helpers']['tools']['effort'], 'medium')
        self.ccx.set_route('launch-a', 'nvidia', 'other')
        self.assertEqual(self.ccx.load_routes()['launch-a']['helpers'], row['helpers'])
        self.ccx.set_route('launch-a')
        self.assertNotIn('model', self.ccx.load_routes()['launch-a'])
        self.assertEqual(self.ccx.load_routes()['launch-a']['helpers'], row['helpers'])
        self.assertEqual(self.ccx.load_routes()['launch-b']['model'], 'gemini-test')
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.ccx.ADAPTER_PORT}/official'}), patch('sys.stdout', new_callable=io.StringIO):
            self.ccx.helper_command(['auto', 'inherit'])
        self.assertNotIn('auto', self.ccx.load_routes()['launch-a']['helpers'])
        self.assertIn('tools', self.ccx.load_routes()['launch-a']['helpers'])

    def test_helper_pick_can_exist_without_main_override(self):
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.ccx.ADAPTER_PORT}/official'}), patch('sys.stdout', new_callable=io.StringIO):
            self.ccx.helper_command(['tools', 'openai-oauth', 'gpt-6-luna', 'low'])
        self.assertNotIn('model', self.ccx.load_routes()['launch-a'])
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://api.anthropic.com'}), self.assertRaises(SystemExit):
            self.ccx.helper_command(['auto', 'openai-oauth', 'gpt-6-luna'])

    def test_helper_pick_rejects_invalid_model_and_effort(self):
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.ccx.ADAPTER_PORT}/official'}):
            for words in (['auto', 'openai-oauth', '../bad'], ['tools', 'openai-oauth', 'gpt-6-luna', 'nonsense']):
                with self.subTest(words=words), self.assertRaises(SystemExit):
                    self.ccx.helper_command(words)
        self.assertEqual(self.ccx.load_routes(), {})

    def test_relay_pending_blocks_stop_once(self):
        job = "12345678-1234-1234-1234-123456789abc"
        Path(self.ccx.JOBS_DIR, job + ".out").write_text("", encoding="utf-8")
        transcript = Path(self.tmp.name, "agent.jsonl")
        transcript.write_text("CCX_RELAY_PENDING " + job + "\n", encoding="utf-8")
        data = {"agent_type": "ccx-sub", "agent_transcript_path": str(transcript)}
        first = self.ccx.subagent_stop(data)
        self.assertEqual(first.get("decision"), "block")
        self.assertIn("agent-wait " + job, first["reason"])
        self.assertEqual(self.ccx.subagent_stop(data), {})


class EffortSourceTests(unittest.TestCase):
    """The reasoning levels each provider publishes for one model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ccx = load_switch()
        self.ccx.EFFORTS_FILE = str(Path(self.tmp.name) / "efforts.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_model_cache_lists_an_openai_models_own_levels(self):
        cache = {"models": [{"slug": "gpt-x", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
                           {"slug": "gpt-y", "supported_reasoning_levels": [{"effort": "max"}]}]}
        Path(self.tmp.name, "models_cache.json").write_text(json.dumps(cache), encoding="utf-8")
        with patch.dict(os.environ, {"CODEX_HOME": self.tmp.name}):
            self.assertEqual(self.ccx.model_efforts("openai-oauth", "GPT-X"), ["low", "high"])
            self.assertIsNone(self.ccx.model_efforts("openai-oauth", "gpt-z"))
            self.assertIsNone(self.ccx.model_efforts("nvidia", "gpt-x"))

    def test_commandcode_levels_are_cached_on_disk_for_the_status_line(self):
        rows = [{"id": "a", "reasoningEfforts": ["low", "high"]}, {"id": "b"}]
        with patch.object(self.ccx, "commandcode_catalog", return_value=rows):
            self.assertEqual(self.ccx.model_efforts("commandcode", "a"), ["low", "high"])
            self.assertEqual(self.ccx.model_efforts("commandcode-cli", "b"), [])
            self.assertIsNone(self.ccx.model_efforts("commandcode", "c"))
        self.assertEqual(json.loads(Path(self.ccx.EFFORTS_FILE).read_text(encoding="utf-8"))["models"],
                         {"a": ["low", "high"], "b": []})
        with patch.object(self.ccx, "commandcode_catalog", side_effect=AssertionError("no network")):
            self.assertEqual(self.ccx.model_efforts("commandcode", "a"), ["low", "high"])
            self.assertIsNone(self.ccx.model_efforts("commandcode", "c"))


if __name__ == "__main__":
    unittest.main()
