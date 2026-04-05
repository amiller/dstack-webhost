/** @jsxImportSource smithers-orchestrator */
import { createSmithers, Sequence } from "smithers-orchestrator";
import { createOpenAI } from "@ai-sdk/openai";
import { z } from "zod";

const openrouter = createOpenAI({
  baseURL: "https://openrouter.ai/api/v1",
  apiKey: process.env.OPENROUTER_API_KEY,
});

const model = openrouter("anthropic/claude-sonnet-4-20250514");

// --- Schemas ---

const codebaseMap = z.object({
  files: z.array(
    z.object({
      path: z.string(),
      purpose: z.string(),
      layer: z.enum(["proxy", "management-api", "docker", "dstack", "config", "unknown"]),
      trust_relevant: z.boolean(),
    })
  ),
  total_lines: z.number(),
  summary: z.string(),
});

const gapAnalysis = z.object({
  gaps: z.array(
    z.object({
      rfc_section: z.string(),
      current_state: z.string(),
      target_state: z.string(),
      effort: z.enum(["small", "medium", "large"]),
      dependencies: z.array(z.string()),
    })
  ),
  ready_to_implement: z.array(z.string()),
  blocked: z.array(z.string()),
});

const implementationPlan = z.object({
  phases: z.array(
    z.object({
      name: z.string(),
      description: z.string(),
      tasks: z.array(
        z.object({
          title: z.string(),
          files_to_create_or_modify: z.array(z.string()),
        })
      ),
    })
  ),
  recommended_first_task: z.string(),
  risks: z.array(z.string()),
});

const report = z.object({
  executive_summary: z.string(),
  codebase_overview: z.string(),
  gaps_summary: z.string(),
  plan_summary: z.string(),
  key_decisions: z.array(
    z.object({
      question: z.string(),
      options: z.array(z.string()),
      recommendation: z.string(),
      rationale: z.string(),
    })
  ),
});

const { Workflow, Task, smithers, outputs } = createSmithers({
  codebase: codebaseMap,
  gaps: gapAnalysis,
  plan: implementationPlan,
  report: report,
});

export default smithers((ctx) => (
  <Workflow name="tee-daemon-rfc-exploration">
    <Sequence>
      <Task
        id="map-codebase"
        output={outputs.codebase}
        agent={{
          model,
          system: `You are a senior software architect. You explore codebases methodically using bash and read tools. Categorize every file by its architectural layer.`,
        }}
      >
        {`Explore the tee-daemon project at ${process.cwd()}.

Read the RFC at RFC.md first, then explore every source file in proxy/ and the root directory.

For each file, determine:
1. path (relative to project root)
2. purpose (one sentence)
3. layer: one of proxy, management-api, docker, dstack, config, unknown
4. trust_relevant: does this file handle TEE attestation, trust modes, or verification?

Also read docker-compose.yaml and Dockerfile.

Return the complete file map with line counts and a 2-3 sentence summary of the current codebase state.`}
      </Task>

      <Task
        id="analyze-gaps"
        output={outputs.gaps}
        deps={{ codebase: outputs.codebase }}
        agent={{
          model,
          system: `You are a security architect specializing in TEE (Trusted Execution Environment) systems. You have deep knowledge of Phala dstack CVMs, Docker networking, and attestation. You identify gaps between RFCs and implementations precisely.`,
        }}
      >
        {(deps) => `Read RFC.md carefully -- every section.

Then compare against what actually exists:
Codebase: ${deps.codebase.files.length} files, ${deps.codebase.total_lines} lines. ${deps.codebase.summary}

For each section of the RFC (Hosting, Trust Modes, Verification), identify:
1. What the RFC proposes
2. What currently exists
3. The gap between them
4. How much effort to close it

Classify each gap as:
- ready_to_implement: can be done now with no blockers
- blocked: needs another gap closed first`}
      </Task>

      <Task
        id="plan-implementation"
        output={outputs.plan}
        deps={{ gaps: outputs.gaps }}
        agent={{
          model,
          system: `You are a senior backend engineer who creates realistic, phased implementation plans. You consider dependencies between tasks and sequence them correctly.`,
        }}
      >
        {(deps) => `Based on the gap analysis:
Ready to implement: ${deps.gaps.ready_to_implement.join("; ")}
Blocked: ${deps.gaps.blocked.join("; ")}
Total gaps: ${deps.gaps.gaps.length}

Create a phased plan that:
1. Starts with the foundation (network separation)
2. Builds up to promotion API
3. Then audit log
4. Then verification
5. Then developer tooling

For each task, specify exact files to create or modify and a concrete approach.`}
      </Task>

      <Task
        id="final-report"
        output={outputs.report}
        deps={{ codebase: outputs.codebase, gaps: outputs.gaps, plan: outputs.plan }}
        agent={{
          model,
          system: `You are a technical writer who produces clear, actionable reports. You synthesize complex analysis into digestible summaries.`,
        }}
      >
        {(deps) => `Write a comprehensive but concise report for the tee-daemon RFC exploration.

Codebase: ${deps.codebase.files.length} files, ${deps.codebase.total_lines} lines. ${deps.codebase.summary}
Gaps: ${deps.gaps.gaps.length} gaps identified. Ready: ${deps.gaps.ready_to_implement.join(", ")}
Plan: ${deps.plan.phases.length} phases. First task: ${deps.plan.recommended_first_task}. Risks: ${deps.plan.risks.join("; ")}

Produce:
1. Executive summary (2-3 sentences)
2. Codebase overview (what exists now)
3. Gaps summary (what's missing)
4. Plan summary (phases and first step)
5. Key decisions that need to be made before starting`}
      </Task>
    </Sequence>
  </Workflow>
));
