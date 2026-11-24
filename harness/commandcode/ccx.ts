// ccx's /ccx command for Command Code (loaded with `cmdc --mod <this file>` by `ccx --harness commandcode`):
// runs ccx on this launch (CCX_LAUNCH_ID) with no model turn. `/ccx` alone opens ccx's model picker window;
// `/ccx route <provider> <model>`, `/ccx fallback ...`, `/ccx helper auto ...`, `/ccx agents ...` change this
// session's parts.
declare const process: { env: Record<string, string | undefined> };

export default function (cmd: any): void {
	cmd.addCommand({
		name: "ccx",
		description: "ccx: this session's models (route, fallback, helpers, subagents, picker)",
		argumentHint: "[route|fallback|helper|agents|gui ...]",
		handler: async ({ args }: { args?: string }) => {
			// words, a quoted one kept whole
			const words = (String(args ?? "").match(/"[^"]*"|'[^']*'|\S+/g) ?? []).map((w) => w.replace(/^(["'])(.*)\1$/, "$2"));
			const result = await cmd.exec({
				command: process.env.CCX_PYTHON || "python",
				args: ["-B", process.env.CCX_SWITCH || "ccx-switch.py", ...words],
			});
			const text = String(result.stdout || "").trim() + (result.stderr ? "\n" + String(result.stderr).trim() : "");
			return { message: text.trim() || (result.code === 0 ? "Done." : "ccx failed.") };
		},
	});
}
