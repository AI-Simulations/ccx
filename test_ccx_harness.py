"""ccx launching other harnesses (pi, Hermes) with every model part routed through the ccx adapter."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load_switch():
    spec = importlib.util.spec_from_file_location("ccx_switch_harness_test", HERE / "ccx-switch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LEVELS = {"easy": {"provider": "nvidia", "model": "small", "upto": 60, "description": "Easy work."},
          "hard": {"provider": "openai-oauth", "model": "big"}}


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        ccx = self.ccx = load_switch()
        ccx.CFG_DIR = str(root)
        for name, file in (("AGENTS_FILE", "agents.json"), ("SESSION_AGENTS_FILE", "session-agents.json"),
                           ("ROUTES_FILE", "routes.json"), ("FALLBACKS_FILE", "fallbacks.json"),
                           ("CLAUDE_SETTINGS", "claude-settings.json"), ("APPLY_FILE", "apply.ps1")):
            setattr(ccx, name, str(root / file))
        ccx.AGENTS_DIR = str(root / "agents")
        ccx.sync_agents = lambda: None
        ccx.load_state = lambda: {}
        ccx.save_state = lambda state: None
        ccx.model_efforts = lambda provider, model: None
        ccx.default_effort = lambda provider, model: None
        ccx.get_models = lambda provider: []
        ccx.ensure_anthropic_proxy = lambda: None
        ccx.ensure_key = lambda name: None
        ccx.ensure_oauth_proxy = lambda: None
        ccx.HOMES_DIR = str(root / "homes")
        ccx.hermes_create_profile = lambda name: (self.user_hermes / "profiles" / name).mkdir(parents=True)
        ccx.hermes_reload_plugins = lambda name: None
        (root / "agents.json").write_text(json.dumps({"sub": {"provider": "nvidia", "model": "worker"}, **LEVELS}),
                                          encoding="utf-8")
        self.user_hermes = root / "user-hermes"
        self.env = patch.dict(os.environ, {"CCX_LAUNCH_ID": "", "CLAUDECODE": "", "ANTHROPIC_BASE_URL": "",
                                           "CCX_MAIN_PROVIDER": "", "HERMES_HOME": str(self.user_hermes)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def launch(self, *words):
        """`ccx <words>`: what it would run (program, env) and the launch id."""
        ccx = self.ccx
        with patch.object(ccx.sys, "argv", ["ccx", *words]), patch.object(ccx, "quit_on_ctrl_c"), \
                patch.object(ccx, "write_apply") as write, patch("sys.stdout", new_callable=io.StringIO):
            ccx.main()
        env, _delete, run = write.call_args.args[:3]
        self.assertTrue(run)
        return write.call_args.kwargs["program"], env, env["CCX_LAUNCH_ID"]

    def test_pi_launch_routes_every_part_through_the_adapter(self):
        ccx = self.ccx
        program, env, launch = self.launch("--harness", "pi", "nvidia", "main-m", "--effort", "high",
                                           "--auto-model", "openai-oauth/luna@low", "--fallback", "openai-oauth/gpt-x",
                                           "--level", "easy=nvidia/tiny", "--", "--verbose")
        ext = str(HERE / "harness" / "pi" / "ccx.ts")
        self.assertEqual(program, ["pi", "-e", ext, "--model", "ccx/ccx-main", "--thinking", "high", "--verbose"])
        self.assertEqual(env["CCX_PI_EXTENSION"], ext)
        self.assertEqual(env["CCX_ADAPTER"], ccx.ADAPTER_URL)
        parts = json.loads(env["CCX_HARNESS_PARTS"])
        self.assertTrue(parts["router"] and parts["auto"])
        self.assertFalse(parts["tools"])
        self.assertEqual([(s["name"], s["model"]) for s in parts["subagents"]],
                         [("easy", "ccx-live-easy"), ("hard", "ccx-live-hard")])
        self.assertEqual(parts["subagents"][0]["range"], [0, 60])
        # the adapter's state for this launch: main route, helper, fallback, subagent profiles
        route = ccx.load_routes()[launch]
        self.assertEqual((route["provider"], route["model"], route["effort"]), ("nvidia", "main-m", "high"))
        self.assertEqual(route["helpers"], {"auto": {"provider": "openai-oauth", "model": "luna", "effort": "low"}})
        self.assertEqual(ccx.load_fallbacks()[launch]["model"], "gpt-x")
        self.assertEqual(ccx._session_agents(launch)["easy"]["model"], "tiny")
        self.assertEqual(json.loads(Path(ccx.AGENTS_FILE).read_text())["easy"]["model"], "small", "global profiles untouched")

    def test_parts_follow_the_subagent_mode(self):
        ccx = self.ccx
        _, env, _ = self.launch("--harness", "pi", "nvidia", "m", "--subagent", "nvidia/one", "--tools-model", "nvidia/t")
        parts = json.loads(env["CCX_HARNESS_PARTS"])
        self.assertFalse(parts["router"])
        self.assertTrue(parts["tools"])
        self.assertEqual([(s["name"], s["model"]) for s in parts["subagents"]], [("sub", "ccx-live-sub")])
        _, env, _ = self.launch("--harness", "pi", "nvidia", "m", "--subagent", "off")
        self.assertEqual(json.loads(env["CCX_HARNESS_PARTS"])["subagents"], [])
        # a profile on the claude.ai login cannot serve another harness: left out, with a warning
        Path(ccx.AGENTS_FILE).write_text(json.dumps({"sub": {"provider": "official"}, **LEVELS}), encoding="utf-8")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            _, env, _ = self.launch("--harness", "pi", "nvidia", "m", "--subagent", "router", "--level", "easy=official/claude-x")
        self.assertEqual([s["name"] for s in json.loads(env["CCX_HARNESS_PARTS"])["subagents"]], ["hard"])
        self.assertIn("easy", err.getvalue())

    def test_hermes_runs_on_a_ccx_profile_per_set_of_picks(self):
        ccx = self.ccx
        created = []

        def create(name):
            created.append(name)
            (self.user_hermes / "profiles" / name).mkdir(parents=True)

        ccx.hermes_create_profile = create
        words = ("--harness", "hermes", "nvidia", "main-m", "--effort", "low", "--auto-model", "openai-oauth/luna",
                 "--tools-model", "nvidia/t", "-q", "hi")
        program, env, launch = self.launch(*words)
        profile = program[2]
        self.assertEqual(program[:2], ["hermes", "-p"])
        self.assertRegex(profile, r"^ccx[0-9a-f]{10}$")
        self.assertEqual(program[3:], ["chat", "--cli", "--provider", "ccx", "-m", "ccx-main", "--reasoning", "low", "-q", "hi"])
        self.assertNotIn("HERMES_HOME", env, "the profile, served by the user's own Hermes gateway")
        home = self.user_hermes / "profiles" / profile
        config = json.loads((home / "config.yaml").read_text(encoding="utf-8"))  # JSON is YAML
        route = {"base_url": f"{ccx.ADAPTER_URL}/ccx/{launch}", "api_key": "ccx-local", "api_mode": "anthropic_messages"}
        self.assertEqual(config["model"], {"default": "ccx-main", "provider": "ccx"})
        self.assertEqual(config["providers"]["ccx"], {"name": "ccx", **route})
        self.assertEqual(config["agent"]["reasoning_effort"], "low")
        self.assertEqual(config["skills"]["external_dirs"], [str(self.user_hermes / "skills")])
        self.assertEqual(config["delegation"]["backend"], "ccx-router")
        router = config["delegation"]["backends"]["ccx-router"]
        self.assertEqual({k: router[k] for k in route}, route)
        self.assertEqual([lvl["model"] for lvl in router["levels"]], ["ccx-live-easy", "ccx-live-hard"])
        self.assertEqual(config["plugins"]["enabled"], ["ccx-cmd", "ccx-router"])
        self.assertTrue((home / "plugins" / "ccx-router" / "__init__.py").is_file())
        self.assertEqual(config["auxiliary"]["approval"], {"provider": "custom", "model": "ccx-auto", **route})
        self.assertEqual(config["auxiliary"]["compression"]["model"], "ccx-tools")
        # the same picks reuse the profile and its launch; a running session there keeps its own picks
        again, _, launch2 = self.launch(*words)
        self.assertEqual((again[2], launch2, created), (profile, launch, [profile]))
        other, _, launch3 = self.launch(*[w if w != "main-m" else "other-m" for w in words])
        self.assertNotEqual((other[2], launch3), (profile, launch))
        self.assertIn(launch, json.loads((home / "config.yaml").read_text())["providers"]["ccx"]["base_url"])
        self.assertEqual(ccx.load_routes()[launch]["model"], "main-m")
        single, _, _ = self.launch("--harness", "hermes", "nvidia", "m", "--subagent", "nvidia/one")
        config = json.loads((self.user_hermes / "profiles" / single[2] / "config.yaml").read_text())
        self.assertEqual(config["delegation"]["model"], "ccx-live-sub")
        self.assertEqual(config["delegation"]["provider"], "custom")
        self.assertEqual(config["plugins"]["enabled"], ["ccx-cmd"], "the /ccx command, without the router")
        self.assertNotIn("auxiliary", config, "unset helpers stay Hermes's own")
        off, _, _ = self.launch("--harness", "hermes", "nvidia", "m", "--subagent", "off")
        config = json.loads((self.user_hermes / "profiles" / off[2] / "config.yaml").read_text())
        self.assertIn("delegation", config["agent"]["disabled_toolsets"])
        self.assertEqual(os.listdir(self.user_hermes), ["profiles"], "only ccx profiles in the user's Hermes home")

    def test_harness_launch_refusals(self):
        for words in (["--harness", "pi", "nvidia"], ["--harness", "nope", "nvidia", "m"], ["--harness", "pi", "official", "m"],
                      ["--harness", "hermes", "nvidia", "m", "--fallback", "nvidia/x"]):
            with self.subTest(words=words), self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
                self.launch(*words)
        self.assertFalse(Path(self.ccx.ROUTES_FILE).exists(), "a refused launch writes nothing")
        # --harness claude is the usual launch
        with patch.object(self.ccx.sys, "argv", ["ccx", "--harness", "claude", "nvidia", "m"]), \
                patch.object(self.ccx, "quit_on_ctrl_c"), patch.object(self.ccx, "apply") as apply:
            self.ccx.main()
        self.assertEqual(apply.call_args.args[:2], ("nvidia", "m"))

    def test_write_apply_runs_the_harness_program(self):
        ccx = self.ccx
        ccx.write_apply({"CCX_LAUNCH_ID": "L1"}, [], True, program=["pi", "-e", "C:/x/ccx.ts", "--model", "ccx/ccx-main"])
        script = Path(ccx.APPLY_FILE).read_text(encoding="utf-8")
        self.assertIn("pi '-e' 'C:/x/ccx.ts' '--model' 'ccx/ccx-main'" if os.name == "nt" else "pi -e C:/x/ccx.ts", script)
        self.assertNotIn("claude", script)

    def test_opencode_launch_uses_inline_config(self):
        ccx = self.ccx
        program, env, launch = self.launch("--harness", "opencode", "nvidia", "main-m", "--effort", "high",
                                           "--auto-model", "openai-oauth/luna", "--tools-model", "nvidia/t", "--", "C:/work")
        self.assertEqual(program, ["opencode", "--standalone", "C:/work"], "standalone: the shared service ignores env config")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        provider = config["providers"]["ccx"]
        self.assertEqual(provider["package"], "@opencode/ai/providers/anthropic")
        self.assertEqual(provider["settings"]["baseURL"], f"{ccx.ADAPTER_URL}/ccx/{launch}/v1")
        self.assertEqual(set(provider["models"]), {"ccx-main", "ccx-live-easy", "ccx-live-hard", "ccx-auto", "ccx-tools"})
        self.assertEqual(provider["models"]["ccx-main"]["capabilities"]["reasoning"]["effort"], ccx.EFFORTS)
        agents = config["agents"]
        self.assertEqual(agents["build"]["model"], "ccx/ccx-main#high")
        self.assertEqual(agents["plan"]["model"], "ccx/ccx-main#high")
        self.assertEqual(agents["ccx-easy"], {"mode": "subagent", "model": "ccx/ccx-live-easy",
                                              "description": "Easy work. (difficulty 0-60)"})
        self.assertEqual(agents["general"], {"disabled": True}, "the router's levels take the general work")
        for helper in ("title", "summary", "compaction"):
            self.assertEqual(agents[helper]["model"], "ccx/ccx-tools")
        self.assertNotIn("permissions", config, "the user's own rules stand; the plugin judges what they allow")
        plugin = config["plugins"][0]["package"]
        self.assertTrue(Path(plugin, "index.js").is_file())
        # without helpers or effort: opencode's own title model and no ask rules
        _, env, _ = self.launch("--harness", "opencode", "nvidia", "m", "--subagent", "off")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["agents"]["build"]["model"], "ccx/ccx-main")
        self.assertNotIn("title", config["agents"])
        self.assertNotIn("permissions", config)
        self.assertEqual(config["agents"]["general"], {"disabled": True})
        self.assertEqual(config["agents"]["explore"], {"disabled": True})

    def test_commandcode_launch_gets_a_ccx_home_mirroring_the_users(self):
        ccx = self.ccx
        real = Path(self.tmp.name) / "real-home"
        (real / ".ssh").mkdir(parents=True)
        (real / ".gitconfig").write_text("[user]\n\tname = me\n", encoding="utf-8")
        cc_real = real / ".commandcode"
        (cc_real / "skills").mkdir(parents=True)
        (cc_real / "auth.json").write_text('{"token": "t"}', encoding="utf-8")
        (cc_real / "AGENTS.md").write_text("mine", encoding="utf-8")
        users = {"model": "theirs", "permissions": {"deny": ["shell(git push*)"]}}
        (cc_real / "settings.json").write_text(json.dumps(users), encoding="utf-8")
        ccx.HOMES_DIR = str(Path(self.tmp.name) / "homes")
        with patch.object(ccx.Path, "home", return_value=real):
            program, env, launch = self.launch("--harness", "commandcode", "nvidia", "main-m", "--effort", "low",
                                               "--auto-model", "openai-oauth/luna", "--tools-model", "nvidia/t")
        home = Path(env["HOME"])
        self.assertEqual(env["USERPROFILE"], str(home))
        self.assertTrue(home.is_relative_to(Path(ccx.HOMES_DIR)), "outside ~/.config, so linking ~/.config loops nowhere")
        self.assertEqual(program, ["cmdc", "--model", "ccx/ccx-main", "--effort", "low", "--local-only",
                                   "--mod", str(HERE / "harness" / "commandcode" / "ccx.ts"), "--yolo"])
        for entry in (".ssh", ".gitconfig", ".commandcode/skills", ".commandcode/auth.json", ".commandcode/AGENTS.md"):
            with self.subTest(entry=entry):
                self.assertTrue(os.path.samefile(home / entry, real / entry), "the user's own, linked")
        cc = home / ".commandcode"
        providers = json.loads((cc / "providers.json").read_text())["provider"]["ccx"]
        self.assertEqual((providers["api"], providers["baseURL"], providers["apiKey"]),
                         ("anthropic-messages", f"{ccx.ADAPTER_URL}/ccx/{launch}", False))
        self.assertEqual(providers["models"]["ccx-main"]["reasoningEfforts"], ccx.EFFORTS)
        settings = json.loads((cc / "settings.json").read_text())
        self.assertEqual(settings["model"], "ccx/ccx-main")
        self.assertEqual(settings["permissions"], users["permissions"], "the user's settings stay")
        self.assertEqual(settings["featureModels"]["compaction"], "ccx/ccx-tools")
        hook = settings["hooks"]["PreToolUse"][-1]
        self.assertEqual(hook["matcher"], ".*", "--yolo skips every prompt: the classifier sees every tool")
        self.assertIn(sys.executable, hook["hooks"][0]["command"])
        self.assertIn(f"harness-auto {launch}", hook["hooks"][0]["command"])
        agent = (cc / "agents" / "ccx-easy.md").read_text(encoding="utf-8")
        self.assertIn("model: ccx/ccx-live-easy", agent)
        self.assertIn('tools: "*"', agent, "a missing tools line gives a subagent no tools")
        self.assertEqual(json.loads((cc_real / "settings.json").read_text()), users, "the user's file untouched")
        # without Auto: no hook and no --yolo, so Command Code asks as usual
        with patch.object(ccx.Path, "home", return_value=real):
            program, env, _ = self.launch("--harness", "commandcode", "nvidia", "m")
        self.assertNotIn("--yolo", program)
        self.assertNotIn("hooks", json.loads(Path(env["HOME"], ".commandcode", "settings.json").read_text()))

    def test_harness_arguments_after_double_dash_pass_whole(self):
        program, _, _ = self.launch("--harness", "pi", "nvidia", "m", "--", "-q", "--", "x")
        self.assertEqual(program[-3:], ["-q", "--", "x"])
        program, _, _ = self.launch("--harness", "pi", "nvidia", "m", "--effort", "low", "--", "--effort", "max")
        self.assertIn("low", program)
        self.assertEqual(program[-2:], ["--effort", "max"])
        with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
            self.launch("--harness", "pi", "nvidia", "m", "--autocompact", "50")

    def test_auto_hook_asks_the_launch_classifier(self):
        ccx = self.ccx
        sent = []

        def post(launch, body):
            sent.append((launch, body))
            return "BLOCK deletes the repo" if "rm -rf" in json.dumps(body) else "ALLOW fine"

        allow = ccx.harness_auto("L1", {"tool_name": "shell_command", "tool_input": {"command": "ls"}, "cwd": "C:/w"}, post=post)
        self.assertEqual(allow["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(sent[-1][0], "L1")
        self.assertEqual(sent[-1][1]["model"], "ccx-auto")
        deny = ccx.harness_auto("L1", {"tool_name": "shell_command", "tool_input": {"command": "rm -rf /"}}, post=post)
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("deletes the repo", deny["hookSpecificOutput"]["permissionDecisionReason"])
        calls = len(sent)
        for tool in ("read_file", "read_directory", "grep", "glob"):
            read = ccx.harness_auto("L1", {"tool_name": tool, "tool_input": {"absolute_path": "x"}}, post=post)
            self.assertEqual((read["hookSpecificOutput"]["permissionDecision"], len(sent)), ("allow", calls), tool)

        def broken(launch, body):
            raise OSError("adapter down")

        failed = ccx.harness_auto("L1", {"tool_name": "write_file", "tool_input": {}}, post=broken)
        self.assertEqual(failed["hookSpecificOutput"]["permissionDecision"], "deny", "fails closed")

    def test_a_new_hermes_profile_loads_its_plugins_in_the_running_gateway(self):
        ccx = self.ccx
        reloads = []
        ccx.hermes_reload_plugins = reloads.append
        (self.user_hermes).mkdir(parents=True, exist_ok=True)
        (self.user_hermes / "config.yaml").write_text(
            "_config_version: 54\ntelemetry:\n  shared_metrics:\n    enabled: true\n    send: false\n    offer_version: 2\n",
            encoding="utf-8")
        program, _, launch = self.launch("--harness", "hermes", "nvidia", "m")
        # the gateway already serves the new profile with its default config: it rediscovers the plugins
        self.assertEqual(reloads, [program[2]])
        self.launch("--harness", "hermes", "nvidia", "m")
        self.assertEqual(reloads, [program[2]], "an existing profile's plugins are loaded already")
        config = json.loads((self.user_hermes / "profiles" / program[2] / "config.yaml").read_text())
        self.assertEqual(config["_config_version"], 54, "the user's config version: no migration warning")
        self.assertEqual(config["telemetry"]["shared_metrics"], {"enabled": True, "send": False, "offer_version": 2},
                         "the user's own answer: a ccx profile does not ask again")

    def test_codex_runs_on_the_adapter_with_per_launch_config_only(self):
        ccx = self.ccx
        program, env, launch = self.launch("--harness", "codex", "nvidia", "main-m", "--effort", "high",
                                           "--auto-model", "openai-oauth/luna", "--tools-model", "nvidia/t",
                                           "--", "--search")
        self.assertEqual(program[0], "codex")
        self.assertEqual(program[-1], "--search")
        config = {}
        for flag, pair in zip(program, program[1:]):
            if flag == "-c":
                key, _, value = pair.partition("=")
                config[key] = json.loads(value)
        self.assertEqual(program[program.index("-m") + 1], "ccx-main")
        self.assertEqual((config["model_provider"], config["model_providers.ccx.wire_api"]), ("ccx", "responses"))
        self.assertEqual(config["model_providers.ccx.base_url"], f"{ccx.ADAPTER_URL}/ccx/{launch}/v1")
        self.assertEqual(env[config["model_providers.ccx.env_key"]], "ccx-local")
        # router levels become Codex agent roles on their placeholder models
        self.assertIn("(difficulty 0-60)", config["agents.ccx-easy.description"])
        role = Path(config["agents.ccx-hard.config_file"]).read_text(encoding="utf-8")
        self.assertIn('model = "ccx-live-hard"', role)
        self.assertIn('model_provider = "ccx"', role)
        # Auto: Codex's own reviewer, whose model the adapter maps to the Auto pick; Tools: /review and memories
        self.assertEqual((config["approvals_reviewer"], config["approval_policy"]), ("auto_review", "on-request"))
        self.assertEqual(config["review_model"], "ccx-tools")
        self.assertEqual(config["memories.extract_model"], "ccx-tools")
        program, _, _ = self.launch("--harness", "codex", "nvidia", "m", "--subagent", "off")
        self.assertIn('agents.enabled=false', program)
        program, _, _ = self.launch("--harness", "codex", "nvidia", "m", "--subagent", "nvidia/one")
        default = next(p for p in program if p.startswith("agents.default.config_file="))
        self.assertIn('model = "ccx-live-sub"', Path(json.loads(default.split("=", 1)[1])).read_text(encoding="utf-8"),
                      "a spawn without a role (Codex's default role) runs on the one profile; a model name would "
                      "have to be in Codex's catalog")
        self.assertFalse(any(p.startswith("approvals_reviewer") for p in program), "Auto unset: Codex's own setting")

    def test_agy_runs_in_gateway_mode_on_the_adapter(self):
        ccx = self.ccx
        program, env, launch = self.launch("--harness", "agy", "nvidia", "main-m", "--", "-p", "hi")
        self.assertEqual(program, ["agy", "--model", "ccx-main", "-p", "hi"])
        self.assertEqual(env["AGY_LLM_GATEWAY_URL"], f"{ccx.ADAPTER_URL}/ccx/{launch}")
        self.assertEqual(env["AGY_LLM_GATEWAY_WIRE_PROTOCOL"], "openai")
        self.assertEqual(env["AGY_LLM_GATEWAY_API_KEY"], "ccx-local")
        self.assertEqual(env["AGY_LLM_GATEWAY_MODELS"].split(","), ["ccx-main", "ccx-live-easy", "ccx-live-hard"])

    def test_every_harness_offers_ccx_commands_for_its_own_launch(self):
        ccx = self.ccx
        for harness in ("pi", "hermes", "opencode", "commandcode", "codex", "agy"):
            with self.subTest(harness=harness):
                program, env, launch = self.launch("--harness", harness, "nvidia", "m")
                self.assertEqual((env["CCX_HARNESS"], env["CCX_MAIN_PROVIDER"]), (harness, "nvidia"))
                shims = env["PATH"].split(os.pathsep)[0]
                self.assertTrue(Path(shims, "ccx.cmd" if os.name == "nt" else "ccx").is_file())
                if harness == "commandcode":
                    self.assertEqual(program[program.index("--mod") + 1], str(HERE / "harness" / "commandcode" / "ccx.ts"))
                if harness == "opencode":
                    plugins = json.loads(env["OPENCODE_CONFIG_CONTENT"])["plugins"]
                    self.assertIn(str(HERE / "harness" / "opencode" / "ccx-cmd"), [p["package"] for p in plugins])
                if harness == "hermes":
                    home = self.user_hermes / "profiles" / program[2]
                    self.assertIn("ccx-cmd", json.loads((home / "config.yaml").read_text())["plugins"]["enabled"])
                    saved = json.loads((home / "plugins" / "ccx-cmd" / "ccx.json").read_text())
                    self.assertEqual((saved["launch"], saved["provider"]), (launch, "nvidia"))
        shim = Path(shims, "ccx.cmd" if os.name == "nt" else "ccx").read_text(encoding="utf-8")
        self.assertIn(str(HERE / "ccx-switch.py"), shim)
        # inside a harness session, ccx edits that session; a bare `ccx` opens the model picker
        with patch.dict(os.environ, {"CCX_LAUNCH_ID": "L9", "CCX_HARNESS": "pi", "CLAUDECODE": ""}):
            self.assertEqual(ccx.session_launch(), "L9")
            self.assertTrue(ccx.on_adapter())
            with patch.object(ccx.sys, "argv", ["ccx"]), patch.object(ccx, "gui_command") as gui:
                ccx.main()
            gui.assert_called_once_with(["model"])
            # no terminal menu there (nothing can answer it), and no launch: it would save a new default route
            with patch.object(ccx.sys, "argv", ["ccx", "agents"]), patch.object(ccx, "gui_command") as gui:
                ccx.main()
            gui.assert_called_once_with(["subagent"])
            for words in (["nvidia", "m"], ["--resume"], ["plain"], ["--harness", "pi", "nvidia", "m"]):
                with self.subTest(words=words), patch.object(ccx.sys, "argv", ["ccx", *words]), \
                        patch.object(ccx, "apply") as apply, patch.object(ccx, "launch_harness") as launch_harness, \
                        patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                    ccx.main()
                self.assertFalse(apply.called or launch_harness.called)

    def test_hermes_ccx_command_keeps_a_quoted_word_whole_without_its_quotes(self):
        spec = importlib.util.spec_from_file_location("ccx_cmd", HERE / "harness" / "hermes" / "ccx-cmd" / "__init__.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        settings = Path(self.tmp.name) / "ccx.json"
        settings.write_text(json.dumps({"launch": "L1", "switch": "ccx-switch.py", "python": "python"}), encoding="utf-8")
        done = subprocess.CompletedProcess([], 0, "ok", "")
        with patch.object(plugin, "SETTINGS", settings), patch.object(plugin.subprocess, "run", return_value=done) as run:
            self.assertEqual(plugin.run_ccx('agents set easy --description "cheap work"'), "ok")
        self.assertEqual(run.call_args.args[0][3:], ["agents", "set", "easy", "--description", "cheap work"])
        self.assertEqual(run.call_args.kwargs["env"]["CCX_LAUNCH_ID"], "L1")

    def test_review_round_three(self):
        ccx = self.ccx
        # the Auto hook fails closed: a crash or timeout under --yolo must not let the tool run
        real = Path(self.tmp.name) / "real-home"
        (real / ".commandcode").mkdir(parents=True)
        auth = real / ".commandcode" / "auth.json"
        auth.write_text('{"token": "old"}', encoding="utf-8")
        with patch.object(ccx.Path, "home", return_value=real):
            _, env, _ = self.launch("--harness", "commandcode", "nvidia", "m", "--auto-model", "openai-oauth/luna")
        cc = Path(env["HOME"], ".commandcode")
        hook = json.loads((cc / "settings.json").read_text())["hooks"]["PreToolUse"][-1]["hooks"][0]
        self.assertIs(hook.get("failClosed"), True)
        # a file the user's tools rewrote (temp file + rename) is linked afresh on the next launch
        tmp = auth.with_suffix(".tmp")
        tmp.write_text('{"token": "new"}', encoding="utf-8")
        os.replace(tmp, auth)
        with patch.object(ccx.Path, "home", return_value=real):
            self.launch("--harness", "commandcode", "nvidia", "m", "--auto-model", "openai-oauth/luna")
        self.assertEqual(json.loads((cc / "auth.json").read_text())["token"], "new")
        # after an opening `--` every word is the harness's, --autocompact included
        program, _, _ = self.launch("--harness", "pi", "nvidia", "m", "--", "--autocompact", "50")
        self.assertEqual(program[-2:], ["--autocompact", "50"])
        # reads outside the project go to the classifier too
        sent = []
        post = lambda launch, body: sent.append(body) or "BLOCK secret"
        outside = ccx.harness_auto("L1", {"tool_name": "read_file", "cwd": "C:/work",
                                          "tool_input": {"absolute_path": "C:/Users/me/.ssh/id_rsa"}}, post=post)
        self.assertEqual((outside["hookSpecificOutput"]["permissionDecision"], len(sent)), ("deny", 1))
        inside = ccx.harness_auto("L1", {"tool_name": "read_file", "cwd": "C:/work",
                                         "tool_input": {"absolute_path": "C:/work/src/a.py"}}, post=post)
        self.assertEqual((inside["hookSpecificOutput"]["permissionDecision"], len(sent)), ("allow", 1))
        many = ccx.harness_auto("L1", {"tool_name": "read_multiple_files", "cwd": "C:/work",
                                       "tool_input": {"paths": ["src/a.py", "C:/work/b.py"]}}, post=post)
        self.assertEqual((many["hookSpecificOutput"]["permissionDecision"], len(sent)), ("allow", 1))


class HermesRouterPluginTests(unittest.TestCase):
    """The Hermes delegation backend ccx ships, against a stub of Hermes's plugin base class."""

    def setUp(self):
        base = types.ModuleType("agent.delegation_provider")

        class DelegationRouteError(ValueError):
            pass

        class DelegationProvider:
            def settings(self, config):
                own = (config.get("backends") or {}).get(self.name)
                return own if isinstance(own, dict) else {}

        base.DelegationProvider, base.DelegationRouteError = DelegationProvider, DelegationRouteError
        self.modules = patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.delegation_provider": base})
        self.modules.start()
        spec = importlib.util.spec_from_file_location("ccx_router_test", HERE / "harness" / "hermes" / "ccx-router" / "__init__.py")
        self.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plugin)
        self.error = DelegationRouteError

    def tearDown(self):
        self.modules.stop()

    def test_router_routes_by_difficulty(self):
        router = self.plugin.CcxRouter()
        config = {"backend": "ccx-router", "backends": {"ccx-router": {
            "base_url": "http://127.0.0.1:10532/ccx/L1", "api_key": "ccx-local", "api_mode": "anthropic_messages",
            "levels": [{"name": "easy", "model": "ccx-live-easy", "description": "Easy work."},
                       {"name": "hard", "model": "ccx-live-hard", "description": "Hard work."}]}}}
        params = router.task_parameters(config)
        self.assertEqual(params["difficulty"]["enum"], ["easy", "hard"])
        self.assertIn("Easy work.", params["difficulty"]["description"])
        self.assertIn("easy, hard", router.description(config))
        self.assertEqual(router.route_task({"goal": "g", "difficulty": "hard"}, config),
                         {"provider": "custom", "base_url": "http://127.0.0.1:10532/ccx/L1", "api_key": "ccx-local",
                          "api_mode": "anthropic_messages", "model": "ccx-live-hard", "label": "ccx hard"})
        for task in ({"goal": "g"}, {"goal": "g", "difficulty": "mid"}):
            with self.subTest(task=task), self.assertRaises(self.error):
                router.route_task(task, config)
        self.assertEqual(router.task_parameters({}), {})
        registered = []
        self.plugin.register(types.SimpleNamespace(register_delegation_provider=registered.append))
        self.assertEqual(registered[0].name, "ccx-router")


@unittest.skipUnless(shutil.which("pi"), "pi is not installed")
class PiExtensionSmokeTests(unittest.TestCase):
    """pi loads the extension offline and lists the placeholder models it registers (no model call)."""

    def test_extension_registers_the_ccx_models(self):
        ext = HERE / "harness" / "pi" / "ccx.ts"
        parts = {"router": True, "auto": True, "tools": True, "subagents": [
            {"name": "easy", "model": "ccx-live-easy", "description": "Easy.", "range": [0, 60]},
            {"name": "hard", "model": "ccx-live-hard", "description": "Hard.", "range": [61, 100]}]}
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "CCX_LAUNCH_ID": "L1", "CCX_ADAPTER": "http://127.0.0.1:1", "PI_OFFLINE": "1",
                   "CCX_HARNESS_PARTS": json.dumps(parts), "CCX_PI_EXTENSION": str(ext), "PI_CODING_AGENT_DIR": home}
            run = subprocess.run([shutil.which("pi"), "--offline", "-ne", "-e", str(ext), "--list-models", "ccx"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        for model in ("ccx-main", "ccx-live-easy", "ccx-live-hard", "ccx-auto", "ccx-tools"):
            self.assertIn(model, run.stdout)



@unittest.skipUnless(shutil.which("opencode"), "opencode is not installed")
class OpencodeConfigSmokeTests(unittest.TestCase):
    """opencode accepts the inline config ccx writes and lists the ccx models (standalone, no model call)."""

    def test_inline_config_lists_the_ccx_models(self):
        ccx = load_switch()
        parts = {"router": True, "auto": True, "tools": True, "subagents": [
            {"name": "easy", "model": "ccx-live-easy", "description": "Easy.", "range": [0, 60]},
            {"name": "hard", "model": "ccx-live-hard", "description": "Hard.", "range": [61, 100]}]}
        program, env = ccx.opencode_launch("L1", parts, "high", [])
        # outside the user's home: opencode reads every .opencode folder above its working directory
        with tempfile.TemporaryDirectory(dir=HERE, prefix=".oc-smoke-") as tmp:
            run = subprocess.run([shutil.which("opencode"), "models", "--standalone"], cwd=tmp, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=180,
                                 env={**os.environ, **env, "OPENCODE_DB": str(Path(tmp, "oc.db")), "PWD": tmp,
                                      "HOME": tmp, "USERPROFILE": tmp, "XDG_CONFIG_HOME": str(Path(tmp, "config")),
                                      "XDG_DATA_HOME": str(Path(tmp, "data")), "XDG_STATE_HOME": str(Path(tmp, "state")),
                                      "XDG_CACHE_HOME": str(Path(tmp, "cache"))})
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        for model in ("ccx/ccx-main", "ccx/ccx-live-easy", "ccx/ccx-auto", "ccx/ccx-tools"):
            self.assertIn(model, run.stdout)

def load_proxy():
    spec = importlib.util.spec_from_file_location("ccx_proxy_openai_test", HERE / "ccx-anthropic-proxy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def anthropic_stream(*blocks, stop="end_turn"):
    """An Anthropic Messages SSE body: text blocks (str) and tool calls ((id, name, input))."""
    events = [{"type": "message_start", "message": {"id": "m", "role": "assistant", "content": [],
                                                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 5}}}]
    for i, block in enumerate(blocks):
        if isinstance(block, str):
            events += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block[:2]}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block[2:]}}]
        elif block[0] == "think":
            events += [{"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": ""}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": block[1]}}]
        else:
            raw = json.dumps(block[2])
            events += [{"type": "content_block_start", "index": i,
                        "content_block": {"type": "tool_use", "id": block[0], "name": block[1], "input": {}}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": raw[:3]}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": raw[3:]}}]
        events.append({"type": "content_block_stop", "index": i})
    events += [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 7}},
               {"type": "message_stop"}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class OpenAIFrontTests(unittest.TestCase):
    """Codex (Responses API) and agy (chat completions) reach every model part through the adapter."""

    @classmethod
    def setUpClass(cls):
        cls.proxy = load_proxy()

    def test_responses_request_becomes_an_anthropic_request(self):
        body = {"model": "ccx-main", "stream": True, "reasoning": {"effort": "high", "summary": "auto"},
                "tools": [{"type": "function", "name": "exec_command", "description": "Run.", "strict": False,
                           "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
                          {"type": "namespace", "name": "multi_agent_v1", "tools": [
                              {"type": "function", "name": "spawn_agent", "parameters": {"type": "object"}}]},
                          {"type": "custom", "name": "apply_patch", "description": "Patch.",
                           "format": {"type": "grammar", "definition": "start: x"}},
                          {"type": "web_search", "external_web_access": True}],
                "input": [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Rules."}]},
                          {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
                          {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": None},
                          {"type": "function_call", "name": "exec_command", "arguments": "{\"cmd\": \"ls\"}", "call_id": "c1"},
                          {"type": "function_call", "namespace": "multi_agent_v1", "name": "spawn_agent",
                           "arguments": "{}", "call_id": "c2"},
                          {"type": "function_call_output", "call_id": "c1", "output": "a.py"},
                          {"type": "function_call_output", "call_id": "c2", "output": "agent 7"},
                          {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin", "call_id": "c3"},
                          {"type": "custom_tool_call_output", "call_id": "c3", "output": "Done"}]}
        anthropic, custom, names = self.proxy.responses_request(body)
        self.assertEqual(custom, {"apply_patch"})
        self.assertEqual(names, {"multi_agent_v1__spawn_agent": ("multi_agent_v1", "spawn_agent")})
        self.assertEqual(anthropic["system"], "Rules.")
        self.assertEqual(anthropic["output_config"], {"effort": "high"})
        self.assertEqual([t["name"] for t in anthropic["tools"]],
                         ["exec_command", "multi_agent_v1__spawn_agent", "apply_patch"])
        self.assertEqual([m["role"] for m in anthropic["messages"]], ["user", "assistant", "user", "assistant", "user"])
        calls = anthropic["messages"][1]["content"]
        self.assertEqual([(c["id"], c["name"], c["input"]) for c in calls],
                         [("c1", "exec_command", {"cmd": "ls"}), ("c2", "multi_agent_v1__spawn_agent", {})])
        self.assertEqual([(r["tool_use_id"], r["content"]) for r in anthropic["messages"][2]["content"]],
                         [("c1", "a.py"), ("c2", "agent 7")])
        self.assertEqual(anthropic["messages"][3]["content"][0]["input"], {"input": "*** Begin"})

    def test_chat_request_becomes_an_anthropic_request(self):
        body = {"model": "ccx-main", "stream": True, "max_completion_tokens": 4096,
                "tools": [{"type": "function", "function": {"name": "run_command", "parameters": {"type": "object"}}}],
                "messages": [{"role": "system", "content": [{"type": "text", "text": "Sys."}]},
                             {"role": "user", "content": "Go"},
                             {"role": "assistant", "tool_calls": [{"id": "k1", "type": "function", "function": {
                                 "name": "run_command", "arguments": "{\"CommandLine\":\"echo\"}"}}]},
                             {"role": "tool", "tool_call_id": "k1", "content": "{\"output\":\"ok\"}"}]}
        anthropic = self.proxy.chat_request(body)
        self.assertEqual((anthropic["system"], anthropic["max_tokens"]), ("Sys.", 4096))
        self.assertEqual(anthropic["messages"][1]["content"][0],
                         {"type": "tool_use", "id": "k1", "name": "run_command", "input": {"CommandLine": "echo"}})
        self.assertEqual(anthropic["messages"][2]["content"][0]["tool_use_id"], "k1")

    def test_an_anthropic_stream_retold_for_codex(self):
        events = list(self.proxy.anthropic_sse(io.BytesIO(anthropic_stream(
            ("think", "Plan."), "Hello", ("c9", "multi_agent_v1__spawn_agent", {"task": "x"}),
            ("c8", "apply_patch", {"input": "*** Begin"}), stop="tool_use")).readlines()))
        out = list(self.proxy.responses_events(iter(events), "ccx-main", {"apply_patch"}, names={"multi_agent_v1__spawn_agent": ("multi_agent_v1", "spawn_agent")}))
        self.assertEqual(out[0]["type"], "response.created")
        done = [e["item"] for e in out if e["type"] == "response.output_item.done"]
        self.assertEqual([i["type"] for i in done], ["reasoning", "message", "function_call", "custom_tool_call"])
        self.assertEqual(done[0]["summary"], [{"type": "summary_text", "text": "Plan."}])
        self.assertEqual(done[1]["content"][0]["text"], "Hello")
        self.assertEqual((done[2]["namespace"], done[2]["name"], json.loads(done[2]["arguments"]), done[2]["call_id"]),
                         ("multi_agent_v1", "spawn_agent", {"task": "x"}, "c9"))
        self.assertEqual(done[3]["input"], "*** Begin")
        last = out[-1]
        self.assertEqual(last["type"], "response.completed")
        self.assertEqual(last["response"]["usage"]["input_tokens"], 15)
        self.assertEqual(last["response"]["usage"]["total_tokens"], 22)
        # a stream that stops early fails the response instead of leaving Codex to retry an open stream
        cut = [e for e in events if e["type"] != "message_stop"]
        self.assertEqual(list(self.proxy.responses_events(iter(cut), "m"))[-1]["type"], "response.failed")
        error = [*events[:2], {"type": "error", "error": {"type": "overloaded_error", "message": "Busy"}}]
        failed = list(self.proxy.responses_events(iter(error), "m"))[-1]
        self.assertEqual((failed["type"], failed["response"]["error"]["message"]), ("response.failed", "Busy"))

    def test_an_anthropic_stream_retold_for_agy(self):
        events = list(self.proxy.anthropic_sse(io.BytesIO(anthropic_stream(
            "Hi", ("k1", "run_command", {"CommandLine": "echo"}), stop="tool_use")).readlines()))
        chunks = list(self.proxy.chat_chunks(iter(events), "ccx-main", include_usage=True))
        message = self.proxy.chat_completion(iter(chunks))["choices"][0]
        self.assertEqual(message["finish_reason"], "tool_calls")
        self.assertEqual(message["message"]["content"], "Hi")
        call = message["message"]["tool_calls"][0]
        self.assertEqual((call["id"], call["function"]["name"], json.loads(call["function"]["arguments"])),
                         ("k1", "run_command", {"CommandLine": "echo"}))
        self.assertEqual(chunks[-1]["usage"]["total_tokens"], 22)

    def test_codex_auto_review_runs_on_the_auto_pick_and_answers_json(self):
        schema = {"type": "object", "properties": {"outcome": {"type": "string"}}}
        body = {"model": "codex-auto-review", "stream": True,
                "text": {"format": {"type": "json_schema", "name": "verdict", "schema": schema}},
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Judge."}]}]}
        anthropic, _, _ = self.proxy.responses_request(body)
        self.assertEqual(anthropic["model"], "ccx-auto", "Codex's reviewer model is the launch's Auto pick")
        self.assertIn(json.dumps(schema), anthropic["system"])
        events = list(self.proxy.anthropic_sse(io.BytesIO(anthropic_stream(
            'Sure:\n```json\n{"outcome": "allow"}\n```')).readlines()))
        out = list(self.proxy.responses_events(iter(events), "codex-auto-review", json_mode=True))
        message = [e["item"] for e in out if e["type"] == "response.output_item.done"][0]
        self.assertEqual(json.loads(message["content"][0]["text"]), {"outcome": "allow"})
        chat = self.proxy.chat_request({"model": "ccx-main", "messages": [{"role": "user", "content": "x"}],
                                        "response_format": {"type": "json_schema", "json_schema": {"schema": schema}}})
        self.assertIn(json.dumps(schema), chat["system"])

    def test_review_round_five(self):
        proxy = self.proxy
        user = {"type": "message", "role": "user", "content": "Go"}
        # only a namespace tool is split on the way back: an MCP tool keeps its own `__` name
        long = "n" * 40 + "__" + "t" * 40
        body = {"model": "ccx-main", "tool_choice": {"type": "function", "name": "mcp__github__get_issue"},
                "tools": [{"type": "function", "name": "mcp__github__get_issue", "parameters": {"type": "object"}},
                          {"type": "namespace", "name": "mcp__srv", "tools": [{"type": "function", "name": "do_it"}]},
                          {"type": "function", "name": long}],
                "input": [user, {"type": "function_call", "name": "view_image", "arguments": "{}", "call_id": "c1"},
                          {"type": "function_call_output", "call_id": "c1", "output": [
                              {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
                          {"type": "function_call", "name": "a", "arguments": "{}", "call_id": "c2"},
                          {"type": "function_call_output", "call_id": "c2", "output": []},
                          {"type": "function_call", "name": "b", "arguments": "{}", "call_id": "c3"},
                          {"type": "message", "role": "user", "content": "Stop that."}]}
        anthropic, _, names = proxy.responses_request(body)
        sent = [t["name"] for t in anthropic["tools"]]
        self.assertEqual(sent[:2], ["mcp__github__get_issue", "mcp__srv__do_it"])
        self.assertLessEqual(len(sent[2]), 64)  # a tool name may have 64 characters
        self.assertEqual(names, {"mcp__srv__do_it": ("mcp__srv", "do_it"), sent[2]: (None, long)})
        self.assertEqual(anthropic["tool_choice"], {"type": "tool", "name": "mcp__github__get_issue"})
        results = [b for m in anthropic["messages"] if m["role"] == "user" for b in m["content"] if b["type"] == "tool_result"]
        self.assertEqual(results[0]["content"][0]["source"], {"type": "base64", "media_type": "image/png", "data": "AAAA"})
        self.assertEqual(results[1]["content"], "(no output)")
        # a call the user interrupted gets a stub result: Anthropic refuses an unanswered tool_use
        self.assertEqual((results[2]["tool_use_id"], anthropic["messages"][-1]["content"][-1]["text"]), ("c3", "Stop that."))
        calls = [("k1", "mcp__github__get_issue", {}), ("k2", "mcp__srv__do_it", {}), ("k3", sent[2], {})]
        events = list(proxy.anthropic_sse(io.BytesIO(anthropic_stream(*calls, stop="tool_use")).readlines()))
        done = [e["item"] for e in proxy.responses_events(iter(events), "m", names=names) if e["type"] == "response.output_item.done"]
        self.assertEqual([(i.get("namespace"), i["name"]) for i in done],
                         [(None, "mcp__github__get_issue"), ("mcp__srv", "do_it"), (None, long)])
        chat = proxy.chat_request({"messages": [{"role": "user", "content": "x"}],
                                   "tools": [{"type": "function", "function": {"name": "f"}}],
                                   "tool_choice": {"type": "function", "function": {"name": "f"}}})
        self.assertEqual(chat["tool_choice"], {"type": "tool", "name": "f"})
        # the output limit is not a finished answer, and a chat stream cut short is an error
        events = list(proxy.anthropic_sse(io.BytesIO(anthropic_stream("Half an ans", stop="max_tokens")).readlines()))
        last = list(proxy.responses_events(iter(events), "m"))[-1]
        self.assertEqual((last["type"], last["response"]["status"], last["response"]["incomplete_details"]),
                         ("response.incomplete", "incomplete", {"reason": "max_output_tokens"}))
        self.assertEqual(list(proxy.chat_chunks(iter(events), "m"))[-1]["choices"][0]["finish_reason"], "length")
        cut = [e for e in events if e["type"] != "message_stop"]
        self.assertIn("error", list(proxy.chat_chunks(iter(cut), "m"))[-1])
        # the error codes Codex acts on
        for error, code in (({"type": "rate_limit_error"}, "rate_limit_exceeded"), ({"type": "overloaded_error"}, "server_is_overloaded"),
                            ({"type": "invalid_request_error", "message": "prompt is too long: 9 > 8"}, "context_length_exceeded"),
                            ({"type": "authentication_error"}, "authentication_error")):
            failed = list(proxy.responses_events(iter([{"type": "error", "error": error}]), "m"))[-1]
            self.assertEqual(failed["response"]["error"]["code"], code)

    def test_codex_and_agy_reach_the_launch_route_end_to_end(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import urllib.request
        proxy, seen = self.proxy, []

        class Live(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                payload = anthropic_stream(("k1", "run_command", {"CommandLine": "dir"}), stop="tool_use")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        live = ThreadingHTTPServer(("127.0.0.1", 0), Live)
        adapter = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        for server in (live, adapter):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: [s.shutdown() for s in (live, adapter)])
        main = {"provider": "opencode-go", "model": "real-main", "effort": None, "key": "k", "format": "anthropic",
                "base": f"http://127.0.0.1:{live.server_port}"}
        with patch.object(proxy, "route_for", lambda launch: dict(main) if launch == "L1" else None), \
                patch.object(proxy, "refresh_upstreams", lambda: None), \
                patch.object(proxy, "helper_target", lambda launch, kind, current: {**current, "model": kind + "-model"}), \
                patch.object(proxy, "fallback_for", lambda launch: None):
            base = f"http://127.0.0.1:{adapter.server_port}/ccx/L1/v1"
            codex = {"model": "ccx-main", "stream": True, "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list"}]}]}
            request = urllib.request.Request(base + "/responses", data=json.dumps(codex).encode(),
                                             headers={"Authorization": "Bearer ccx-local", "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=20) as response:
                self.assertEqual(response.headers["Content-Type"], "text/event-stream")
                lines = [l for l in response.read().decode().splitlines() if l.startswith("data: ")]
            out = [json.loads(l[6:]) for l in lines]
            self.assertEqual(out[-1]["type"], "response.completed")
            self.assertIn("function_call", [e["item"]["type"] for e in out if e["type"] == "response.output_item.done"])
            self.assertEqual((seen[-1][0], seen[-1][1]["model"]), ("/v1/messages", "real-main"))
            # Codex's compaction request is a side task: the Tools model
            compaction = {"x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})}
            request = urllib.request.Request(base + "/responses", data=json.dumps(codex).encode(),
                                             headers={"Authorization": "Bearer ccx-local", **compaction})
            urllib.request.urlopen(request, timeout=20).read()
            self.assertEqual(seen[-1][1]["model"], "tools-model")
            agy = {"model": "ccx-main", "stream": True, "stream_options": {"include_usage": True},
                   "messages": [{"role": "user", "content": "list"}]}
            request = urllib.request.Request(base + "/chat/completions", data=json.dumps(agy).encode(),
                                             headers={"Authorization": "Bearer ccx-local"})
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode()
            self.assertTrue(body.rstrip().endswith("data: [DONE]"))
            chunks = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: {")]
            self.assertEqual(self.proxy.chat_completion(iter(chunks))["choices"][0]["finish_reason"], "tool_calls")
            # a request the route refuses comes back as an OpenAI-shaped error
            with patch.object(proxy, "route_for", lambda launch: None):
                request = urllib.request.Request(base + "/chat/completions", data=json.dumps(agy).encode())
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=20)
                self.assertIn("No ccx launch", json.loads(caught.exception.read())["error"]["message"])


if __name__ == "__main__":
    unittest.main()
