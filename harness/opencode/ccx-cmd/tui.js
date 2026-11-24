// ccx's /ccx command for opencode's TUI: runs ccx on this launch (CCX_LAUNCH_ID) with no model turn.
// `/ccx` alone opens ccx's model picker window; `/ccx route <provider> <model>`, `/ccx fallback ...`,
// `/ccx helper auto ...`, `/ccx agents ...` change this session's parts. Loaded by `ccx --harness opencode`.
import { spawn } from "node:child_process";

function runCcx(words) {
	return new Promise((resolve) => {
		const python = process.env.CCX_PYTHON || "python";
		const child = spawn(python, ["-B", process.env.CCX_SWITCH || "ccx-switch.py", ...words], {
			env: process.env,
			windowsHide: true,
		});
		let out = "";
		child.stdout.on("data", (chunk) => (out += chunk));
		child.stderr.on("data", (chunk) => (out += chunk));
		const timer = setTimeout(() => child.kill(), 120000);
		child.on("error", (error) => {
			clearTimeout(timer);
			resolve({ ok: false, text: String(error) });
		});
		child.on("close", (code) => {
			clearTimeout(timer);
			resolve({ ok: code === 0, text: out.trim() });
		});
	});
}

export default {
	id: "ccx.cmd",
	setup(context) {
		context.keymap.layer(() => ({
			mode: "global",
			commands: [
				{
					id: "ccx.run",
					title: "ccx: this session's models (route, fallback, helpers, subagents, picker)",
					group: "ccx",
					palette: true,
					slash: { name: "ccx", arguments: true },
					run: async (input) => {
						// opencode may hand over the whole line, `/ccx` included: only the words after it are ccx's
						const words = (String(input ?? "").match(/"[^"]*"|'[^']*'|\S+/g) ?? []).map((w) => w.replace(/^(["'])(.*)\1$/, "$2"));
						if (/^\/?ccx$/i.test(words[0] ?? "")) words.shift();
						const { ok, text } = await runCcx(words);
						const message = text || (ok ? "Done." : "ccx failed.");
						if (message.includes("\n")) {
							await context.ui.dialog.alert({ title: "ccx " + words.join(" "), message });
						} else {
							context.ui.toast.show({ message, variant: ok ? "success" : "error" });
						}
					},
				},
			],
		}));
	},
};
