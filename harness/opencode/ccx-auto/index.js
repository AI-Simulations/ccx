// ccx's Auto mode for opencode: every action your permission rules allow or ask about goes to this launch's
// Auto classifier (the ccx-auto model on the ccx provider) first. ALLOW runs it, BLOCK refuses it; when the
// classifier cannot answer, opencode asks you. Your deny rules stay final: opencode never sends them here.
// Loaded by `ccx --harness opencode`.
const READ_ONLY = new Set(["read", "list", "glob", "grep", "codesearch", "lsp", "todoread", "skill"]);

const PROMPT = (cwd, action, resources) =>
	"You approve or block one action by a coding agent working in " + cwd + ". Block anything destructive, " +
	"irreversible, outside the project, exfiltrating secrets or not plausibly part of ordinary development work.\n" +
	"Action: " + action + "\nTargets: " + JSON.stringify(resources).slice(0, 8000) + "\n" +
	"Answer ALLOW or BLOCK on the first line, then one short reason.";

export default {
	id: "ccx.auto",
	async setup(ctx) {
		await ctx.permission.hook("evaluate", async (event) => {
			if (event.effect !== "allow" && event.effect !== "ask") return;
			if (READ_ONLY.has(event.action)) return;
			let verdict = "";
			try {
				const reply = await ctx.experimental.generate.text({
					model: { providerID: "ccx", id: "ccx-auto" },
					prompt: PROMPT(process.cwd(), event.action, event.resources),
				});
				verdict = String(reply?.text ?? "").trim();
			} catch {
				verdict = "";
			}
			if (/^ALLOW\b/i.test(verdict)) {
				event.effect = "allow";
			} else if (/^BLOCK\b/i.test(verdict)) {
				event.effect = "deny";
				event.message = "ccx Auto mode blocked this: " + (verdict.replace(/^BLOCK\b[:\s-]*/i, "") || "no reason given");
			} else {
				event.effect = "ask"; // no verdict: you decide
			}
		});
	},
};
