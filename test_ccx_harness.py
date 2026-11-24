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
        self.assertEqual(config["plugins"]["enabled"], ["ccx-router"])
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
        self.assertNotIn("plugins", config)
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
        self.assertEqual(program, ["cmdc", "--model", "ccx/ccx-main", "--effort", "low", "--local-only", "--yolo"])
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

if __name__ == "__main__":
    unittest.main()
