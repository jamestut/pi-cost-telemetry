/**
 * Cost telemetry extension.
 *
 * POSTs per-turn token/cost usage as JSON to a configurable endpoint.
 * Only usage/cost/model metadata is sent. No prompts, completions,
 * tool args, or file contents.
 *
 * Endpoint config — global file only:
 * - ~/.pi/agent/cost-telemetry.json (or $PI_CODING_AGENT_DIR/cost-telemetry.json)
 * ```json
 * { "endpoint": "https://example.com/ingest", "timeoutMs": 5000, "apiKey": "secret" }
 * ```
 * `apiKey` is a plain literal value, sent as `Authorization: Bearer <key>`.
 * Env override: PI_COST_TELEMETRY_ENDPOINT / PI_COST_TELEMETRY_API_KEY
 *
 * Optional API key — in ~/.pi/agent/auth.json under the "cost-telemetry" key:
 * ```json
 * { "cost-telemetry": { "type": "api_key", "key": "secret" } }
 * ```
 * Env override: PI_COST_TELEMETRY_API_KEY
 * Precedence (like Pi model keys): env > auth.json > cost-telemetry.json `apiKey`.
 * Sent as `Authorization: Bearer <key>` when present.
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { getAgentDir } from "@earendil-works/pi-coding-agent";
import type { ExtensionAPI, TurnEndEvent } from "@earendil-works/pi-coding-agent";

// Basename of the global JSON config resolved under getAgentDir().
const CONFIG_FILE = "cost-telemetry.json";
// Key name for the optional API-key entry inside auth.json.
const AUTH_KEY = "cost-telemetry";

/** Shape of cost-telemetry.json: endpoint, timeout, and literal API key. */
interface CostTelemetryConfig {
	/** Ingest URL; overridable via PI_COST_TELEMETRY_ENDPOINT. */
	endpoint?: string;
	/** Fetch timeout in ms; defaults to 5000. */
	timeoutMs?: number;
	/** Literal Bearer key value. */
	apiKey?: string;
}

/** Minimal per-turn metadata POSTed to the ingest endpoint. No prompt content. */
interface TurnPayload {
	/** Pi session id owning this turn. */
	sessionId: string;
	/** Zero-based turn counter from the turn_end event. */
	turnIndex: number;
	/** Assistant message timestamp (ms epoch). */
	timestamp: number;
	/** Provider id, e.g. "anthropic". */
	provider: string;
	/** Requested model name. */
	model: string;
	/** Actual responding model, if different. */
	responseModel: string | undefined;
	/** Active thinking level, if any. */
	thinkingLevel: string | undefined;
	/** Whether the model used reasoning. */
	modelReasoning: boolean | undefined;
	/** Stop reason for the turn. */
	stopReason: string;
	/** Token counts copied from the assistant message usage. */
	usage: {
		input: number;
		output: number;
		cacheRead: number;
		cacheWrite: number;
		totalTokens: number;
		reasoning: number | undefined;
	};
	/** Cost breakdown copied from message usage. */
	cost: {
		input: number;
		output: number;
		cacheRead: number;
		cacheWrite: number;
		total: number;
	};
}

/**
 * Read and parse a JSON object file.
 *
 * @param path - Absolute file path to read.
 * @returns Parsed object, or `undefined` if missing/unparseable.
 */
function readJsonFile(path: string): Record<string, unknown> | undefined {
	try {
		// Missing file is normal (e.g. no auth.json yet).
		if (!existsSync(path)) return undefined;
		return JSON.parse(readFileSync(path, "utf-8")) as Record<string, unknown>;
	} catch {
		// Corrupt JSON is treated as absent; caller falls back to defaults.
		return undefined;
	}
}

/**
 * Resolve the ingest endpoint and timeout.
 *
 * @returns Endpoint URL (or `undefined` if unconfigured), timeoutMs, and file apiKey.
 */
function resolveEndpoint(): { endpoint: string | undefined; timeoutMs: number; apiKey: string | undefined } {
	// Global-only config: <agentDir>/cost-telemetry.json.
	const config = readJsonFile(join(getAgentDir(), CONFIG_FILE)) as CostTelemetryConfig | undefined;
	// Env var wins over the file for the endpoint.
	const endpoint = process.env.PI_COST_TELEMETRY_ENDPOINT || config?.endpoint;
	return { endpoint, timeoutMs: config?.timeoutMs ?? 5000, apiKey: config?.apiKey };
}

/**
 * Resolve the Bearer key with env > auth.json > file precedence.
 *
 * @param fileApiKey - Literal `apiKey` from cost-telemetry.json, if any.
 * @returns Key to send, or `undefined` for unauthenticated POSTs.
 */
function resolveApiKey(fileApiKey: string | undefined): string | undefined {
	// Highest precedence: explicit env override.
	if (process.env.PI_COST_TELEMETRY_API_KEY) return process.env.PI_COST_TELEMETRY_API_KEY;
	// Next: "cost-telemetry" entry in auth.json (string or {key} object).
	const auth = readJsonFile(join(getAgentDir(), "auth.json"));
	const entry = auth?.[AUTH_KEY] as string | { type?: string; key?: string } | undefined;
	const stored = typeof entry === "string" ? entry : entry?.key;
	if (typeof stored === "string" && stored) return stored;
	// Lowest: literal apiKey from cost-telemetry.json.
	if (typeof fileApiKey === "string" && fileApiKey) return fileApiKey;
	return undefined;
}

/**
 * POST one turn payload to the ingest endpoint.
 *
 * @param endpoint - Full ingest URL.
 * @param apiKey - Bearer key, if any.
 * @param timeoutMs - Abort timeout for the fetch.
 * @param payload - Turn metadata to send.
 * @throws If the response status is not 2xx.
 */
async function postTurn(
	endpoint: string,
	apiKey: string | undefined,
	timeoutMs: number,
	payload: TurnPayload,
): Promise<void> {
	// JSON body; attach Bearer auth only when a key resolved.
	const headers: Record<string, string> = { "content-type": "application/json" };
	if (apiKey) headers.authorization = `Bearer ${apiKey}`;
	const res = await fetch(endpoint, {
		method: "POST",
		headers,
		body: JSON.stringify(payload),
		signal: AbortSignal.timeout(timeoutMs),
	});
	if (!res.ok) throw new Error(`telemetry POST ${res.status}`);
}

/**
 * Register turn_end / session_shutdown hooks that ship usage metadata.
 *
 * @param pi - Extension API for subscribing to agent lifecycle events.
 */
export default function costTelemetry(pi: ExtensionAPI) {
	// In-flight POSTs drained on session_shutdown.
	const pending = new Set<Promise<void>>();

	// Per assistant turn: build the payload and fire-and-forget the POST.
	pi.on("turn_end", (event, ctx) => {
		const e = event as TurnEndEvent;
		const message = e.message;
		// Only assistant messages carry usage/cost.
		if (message.role !== "assistant") return;

		// Re-resolve each turn so env/file edits apply without restart.
		const { endpoint, timeoutMs, apiKey: fileApiKey } = resolveEndpoint();
		if (!endpoint) return;

		// Copy only metadata; never prompt/completion text or tool args.
		const payload: TurnPayload = {
			sessionId: ctx.sessionManager.getSessionId(),
			turnIndex: e.turnIndex,
			timestamp: message.timestamp,
			provider: message.provider,
			model: message.model,
			responseModel: message.responseModel,
			thinkingLevel: ctx.thinkingLevel,
			modelReasoning: ctx.model?.reasoning,
			stopReason: message.stopReason,
			usage: {
				input: message.usage.input,
				output: message.usage.output,
				cacheRead: message.usage.cacheRead,
				cacheWrite: message.usage.cacheWrite,
				totalTokens: message.usage.totalTokens,
				reasoning: message.usage.reasoning,
			},
			cost: {
				input: message.usage.cost.input,
				output: message.usage.cost.output,
				cacheRead: message.usage.cost.cacheRead,
				cacheWrite: message.usage.cost.cacheWrite,
				total: message.usage.cost.total,
			},
		};

		// Fire-and-forget: log failures, track for shutdown drain.
		const task = postTurn(endpoint, resolveApiKey(fileApiKey), timeoutMs, payload).catch((err) => {
			console.error(`[cost-telemetry] POST failed: ${err instanceof Error ? err.message : String(err)}`);
		});
		pending.add(task);
		void task.finally(() => pending.delete(task));
	});

	// Flush remaining POSTs so shutdown does not drop the last turns.
	pi.on("session_shutdown", async () => {
		if (pending.size > 0) await Promise.allSettled([...pending]);
	});
}
