/**
 * PI-MIG-02 deterministic offline provider and terminating test tool.
 *
 * This extension performs no I/O.  It registers one model-visible tool and a
 * local streamSimple provider that deterministically requests that tool.
 */

import {
  type AssistantMessage,
  type Context,
  type Model,
  type SimpleStreamOptions,
  StringEnum,
  createAssistantMessageEventStream,
} from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const PROVIDER = "biomind-poc-fake";
const MODEL = "biomind-poc-model";
const TOOL = "biomind_finish_step_poc";
const TERMINAL_PROTOCOL = "biomind.pi-rpc-poc.terminal-outcome";
const OUTCOME_TYPES = [
  "completion_claimed",
  "needs_replan",
  "blocked",
  "harness_error",
  "cancelled",
] as const;

type OutcomeType = (typeof OUTCOME_TYPES)[number];

interface PocToolInput {
  type: OutcomeType;
  summary: string;
  blocker?: { code: string; detail?: string };
}

function configuredOutcomeType(): OutcomeType {
  const configured = process.env.BIOMIND_PI_POC_OUTCOME_TYPE ?? "completion_claimed";
  if ((OUTCOME_TYPES as readonly string[]).includes(configured)) {
    return configured as OutcomeType;
  }
  return "harness_error";
}

function deterministicToolArguments(): PocToolInput {
  const type = configuredOutcomeType();
  const result: PocToolInput = {
    type,
    summary: `Pi RPC POC deterministically emitted ${type}`,
  };
  if (type === "blocked") {
    result.blocker = { code: "PI_RPC_POC_BLOCKED" };
  } else if (type === "harness_error") {
    result.blocker = {
      code: "PI_RPC_POC_CONFIG_INVALID",
      detail: "The requested deterministic POC outcome was invalid or harness_error",
    };
  }
  return result;
}

const finishTool = defineTool({
  name: TOOL,
  label: "Finish BioMind Step POC",
  description: "Return the deterministic PI-MIG-02 terminal test payload.",
  parameters: Type.Object(
    {
      type: StringEnum([...OUTCOME_TYPES]),
      summary: Type.String(),
      blocker: Type.Optional(
        Type.Object(
          {
            code: Type.String(),
            detail: Type.Optional(Type.String()),
          },
          { additionalProperties: false },
        ),
      ),
    },
    { additionalProperties: false },
  ),
  async execute(_toolCallId, params) {
    const outcome = {
      schema_version: 1,
      type: params.type,
      summary: params.summary,
      evidence_refs: [],
      artifact_refs: [],
      binding_refs: [],
      blocker: params.blocker ?? null,
    };
    return {
      content: [{ type: "text" as const, text: "BioMind PI-MIG-02 terminal payload accepted" }],
      details: {
        protocol: TERMINAL_PROTOCOL,
        schema_version: 1,
        outcome,
      },
      terminate: true,
    };
  },
});

export default function registerBioMindPiRpcPoc(pi: ExtensionAPI): void {
  let callSequence = 0;

  function streamSimple(
    model: Model<any>,
    _context: Context,
    options?: SimpleStreamOptions,
  ) {
    const stream = createAssistantMessageEventStream();
    void (async () => {
      const output: AssistantMessage = {
        role: "assistant",
        content: [],
        api: model.api,
        provider: model.provider,
        model: model.id,
        usage: {
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
        },
        stopReason: "pending",
        timestamp: Date.now(),
      };
      try {
        options?.signal?.throwIfAborted();
        stream.push({ type: "start", partial: output });
        callSequence += 1;
        const argumentsObject = deterministicToolArguments();
        const toolCall = {
          type: "toolCall" as const,
          id: `biomind-poc-tool-${callSequence}`,
          name: TOOL,
          arguments: argumentsObject,
        };
        const contentIndex = output.content.length;
        output.content.push(toolCall);
        stream.push({ type: "toolcall_start", contentIndex, partial: output });
        stream.push({
          type: "toolcall_delta",
          contentIndex,
          delta: JSON.stringify(argumentsObject),
          partial: output,
        });
        stream.push({
          type: "toolcall_end",
          contentIndex,
          toolCall,
          partial: output,
        });
        output.stopReason = "toolUse";
        stream.push({ type: "done", reason: "toolUse", message: output });
        stream.end();
      } catch (error) {
        output.stopReason = options?.signal?.aborted ? "aborted" : "error";
        output.errorMessage = error instanceof Error ? error.message : String(error);
        stream.push({ type: "error", reason: output.stopReason, error: output });
        stream.end();
      }
    })();
    return stream;
  }

  pi.registerTool(finishTool);
  pi.registerProvider(PROVIDER, {
    name: "BioMind deterministic offline POC",
    baseUrl: "http://127.0.0.1:9/biomind-offline-poc",
    apiKey: "offline-no-credential",
    api: "biomind-poc-fake-api" as any,
    models: [
      {
        id: MODEL,
        name: "BioMind deterministic offline POC model",
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 8192,
        maxTokens: 512,
      },
    ],
    streamSimple,
  });
}
