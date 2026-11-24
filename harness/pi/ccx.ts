/**
 * ccx for pi: every model part of a `ccx --harness pi ...` launch runs through the ccx adapter.
 *
 * ccx sets, per launch:
 *   CCX_ADAPTER        the adapter's URL (http://127.0.0.1:10532)
 *   CCX_LAUNCH_ID      this launch; the adapter maps its placeholder models to the picked ones
 *   CCX_PI_EXTENSION   this file, so subagent runs load it too
 *   CCX_HARNESS_PARTS  JSON {router, subagents: [{name, model, description, range?}], auto, tools, context?}
 *
 * Placeholder models (provider `ccx`): ccx-main (the main model), ccx-live-<profile> (a subagent or router
 * level), ccx-auto (the Auto mode classifier), ccx-tools (side calls such as compaction summaries).
 * What pi lacks, this adds: a `subagent` tool (one model, or the difficulty router), an Auto mode gate
 * on tool calls, and compaction on the Tools model. Fallback needs nothing here: the adapter does it.
 */
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { convertToLlm, serializeConversation } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

interface Subagent {
	name: string;
	model: string;
	description: string;
	range?: [number, number];
}

interface Parts {
	router: boolean;
	subagents: Subagent[];
	auto: boolean;
	tools: boolean;
	context?: number;
}

const PROVIDER = "ccx";
// Tools that only read: Auto mode lets them run without asking the classifier.
const READ_ONLY = new Set(["read", "grep", "find", "ls"]);

function readParts(): Parts {
	const raw = process.env.CCX_HARNESS_PARTS;
	const parsed = raw ? (JSON.parse(raw) as Partial<Parts>) : {};
	return {
		router: Boolean(parsed.router),
		subagents: Array.isArray(parsed.subagents) ? parsed.subagents : [],
		auto: Boolean(parsed.auto),
		tools: Boolean(parsed.tools),
		context: typeof parsed.context === "number" && parsed.context > 0 ? parsed.context : undefined,
	};
}

function model(id: string, name: string, context: number) {
	return {
		id,
		name,
		reasoning: true,
		input: ["text", "image"] as ("text" | "image")[],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: context,
		maxTokens: 32000,
	};
}

function piInvocation(args: string[]): { command: string; args: string[] } {
	const script = process.argv[1];
	if (script && fs.existsSync(script)) return { command: process.execPath, args: [script, ...args] };
	return { command: "pi", args };
}

/** One subagent run: a headless pi on the subagent's placeholder model, with this extension loaded. The task
 * goes in a file attachment: as an argument, a leading '-' or '@' would be read as an option or a file. */
async function runSubagent(sub: Subagent, task: string, cwd: string, signal?: AbortSignal): Promise<string> {
	const dir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "ccx-pi-"));
	const file = path.join(dir, "task.md");
	await fs.promises.writeFile(file, task, "utf-8");
	const args = ["-p", "--no-session", "-e", process.env.CCX_PI_EXTENSION ?? "", "--model", `${PROVIDER}/${sub.model}`,
		`@${file}`, "Do the task in the attached file."];
	const { command, args: argv } = piInvocation(args);
	return new Promise<string>((resolve, reject) => {
		const child = spawn(command, argv, {
			cwd,
			env: { ...process.env, CCX_PI_DEPTH: "1" }, // subagents do not delegate again
			stdio: ["ignore", "pipe", "pipe"],
			windowsHide: true,
		});
		let out = "";
		let err = "";
		child.stdout.on("data", (chunk) => (out += chunk));
		child.stderr.on("data", (chunk) => (err += chunk));
		const abort = () => child.kill();
		signal?.addEventListener("abort", abort, { once: true });
		child.on("error", (error) => {
			fs.rmSync(dir, { recursive: true, force: true });
			reject(error);
		});
		child.on("close", (code) => {
			signal?.removeEventListener("abort", abort);
			fs.rmSync(dir, { recursive: true, force: true });
			if (code === 0) resolve(out.trim() || "(no output)");
			else reject(new Error(`subagent ${sub.name} exited ${code}: ${(err || out).trim().slice(-2000)}`));
		});
	});
}

function text(message: { content?: unknown }): string {
	const content = Array.isArray(message.content) ? message.content : [];
	return content
		.filter((c): c is { type: "text"; text: string } => c?.type === "text")
		.map((c) => c.text)
		.join("\n");
}

export default function (pi: ExtensionAPI) {
	const adapter = process.env.CCX_ADAPTER ?? "http://127.0.0.1:10532";
	const launch = process.env.CCX_LAUNCH_ID;
	if (!launch) return; // loaded outside a ccx launch: nothing to route
	const parts = readParts();
	const context = parts.context ?? 200000;
	const subagents = process.env.CCX_PI_DEPTH ? [] : parts.subagents;

	pi.registerProvider(PROVIDER, {
		name: "ccx",
		baseUrl: `${adapter}/ccx/${launch}`,
		apiKey: "ccx-local", // the adapter holds the real keys
		api: "anthropic-messages",
		models: [
			model("ccx-main", "ccx main model", context),
			...parts.subagents.map((s) => model(s.model, `ccx ${s.name}`, context)),
			model("ccx-auto", "ccx Auto mode classifier", context),
			model("ccx-tools", "ccx Tools helper", context),
		],
	});

	if (subagents.length) {
		const names = subagents.map((s) => s.name) as [string, ...string[]];
		const listing = subagents
			.map((s) => `- ${s.name}${s.range ? ` (difficulty ${s.range[0]}-${s.range[1]})` : ""}: ${s.description}`)
			.join("\n");
		pi.registerTool({
			name: "subagent",
			label: "Subagent",
			description: [
				"Delegate a self-contained task to a subagent with its own context; it returns its final answer.",
				parts.router
					? "Pick the level that matches the task's difficulty: easier levels run on cheaper, faster models."
					: "Pick the subagent that fits the task.",
				listing,
			].join("\n"),
			parameters: Type.Object({
				[parts.router ? "difficulty" : "agent"]: StringEnum(names),
				task: Type.String({ description: "The complete task, with every detail the subagent needs." }),
			}),
			async execute(_id, params, signal, _onUpdate, ctx) {
				const name = (params as Record<string, string>)[parts.router ? "difficulty" : "agent"];
				const sub = subagents.find((s) => s.name === name);
				if (!sub) throw new Error(`Unknown subagent ${name}; use one of ${names.join(", ")}.`);
				const answer = await runSubagent(sub, (params as { task: string }).task, ctx.cwd, signal);
				return { content: [{ type: "text", text: answer }], details: { subagent: sub.name } };
			},
		});
	}

	if (parts.auto) {
		pi.on("tool_call", async (event, ctx) => {
			if (READ_ONLY.has(event.toolName)) return undefined;
			let verdict = "";
			try {
				const classifier = ctx.modelRegistry.find(PROVIDER, "ccx-auto");
				if (!classifier) throw new Error("no ccx-auto model");
				const reply = await ctx.modelRegistry.complete(
					classifier,
					{
						messages: [
							{
								role: "user",
								content: [
									{
										type: "text",
										text:
											"You approve or block one tool call by a coding agent working in " +
											`${ctx.cwd}. Block anything destructive, irreversible, outside the project, ` +
											"exfiltrating secrets or not plausibly part of ordinary development work.\n" +
											`Tool: ${event.toolName}\nInput: ${JSON.stringify(event.input).slice(0, 8000)}\n` +
											"Answer ALLOW or BLOCK on the first line, then one short reason.",
									},
								],
								timestamp: Date.now(),
							},
						],
					},
					{ maxTokens: 256, signal: ctx.signal },
				);
				verdict = text(reply).trim();
			} catch (error) {
				verdict = `BLOCK classifier unavailable (${error instanceof Error ? error.message : String(error)})`;
			}
			if (/^ALLOW\b/i.test(verdict)) return undefined;
			const reason = verdict.replace(/^BLOCK\b[:\s-]*/i, "") || "no reason given";
			if (ctx.hasUI) {
				const choice = await ctx.ui.select(`Auto mode blocked ${event.toolName}: ${reason}\nRun it anyway?`, ["No", "Yes"]);
				if (choice === "Yes") return undefined;
			}
			return { block: true, reason: `Auto mode blocked this: ${reason}` };
		});
	}

	if (parts.tools) {
		pi.on("session_before_compact", async (event, ctx) => {
			const helper = ctx.modelRegistry.find(PROVIDER, "ccx-tools");
			if (!helper) return undefined;
			const { messagesToSummarize, turnPrefixMessages, tokensBefore, firstKeptEntryId, previousSummary } = event.preparation;
			const conversation = serializeConversation(convertToLlm([...messagesToSummarize, ...turnPrefixMessages]));
			try {
				const reply = await ctx.modelRegistry.complete(
					helper,
					{
						messages: [
							{
								role: "user",
								content: [
									{
										type: "text",
										text:
											"Summarize this coding session so the work can continue from the summary alone: goals, " +
											"decisions and why, files and code changed, current state, open problems, next steps." +
											(previousSummary ? `\n\nEarlier summary:\n${previousSummary}` : "") +
											`\n\n<conversation>\n${conversation}\n</conversation>`,
									},
								],
								timestamp: Date.now(),
							},
						],
					},
					{ maxTokens: 8192, signal: event.signal },
				);
				const summary = text(reply).trim();
				if (!summary) return undefined; // pi's own compaction then runs on the main model
				return { compaction: { summary, firstKeptEntryId, tokensBefore, usage: reply.usage } };
			} catch {
				return undefined;
			}
		});
	}
}
