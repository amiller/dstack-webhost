# How people build durable autonomous coding-agent loops

*Research report — 2026-06-24. Compiled for the tee-daemon / Paseo self-running-loop design.
Backlog patterns, budgeting, introspection, human feedback, and orchestration, with real
tools, papers, URLs, and tradeoffs. Claims were flagged where unconfirmed; see caveats at the end.*

The single most useful framing up front: **an LLM run always starts with a blank context
window, so every "durable" property — the backlog, the budget meter, the memory, the human's
steering — has to live *outside* the agent and be re-read each run.** Almost every pattern below
is a different answer to "where does that external state live."

---

## 1. Backlog / task-queue patterns

Most major agents do **not** keep a first-class persistent backlog inside the agent. They
externalize it and run stateless on one task. Four representations, ordered by how close the
backlog sits to the agent's context:

**A. In-context / ephemeral list — Claude Code `TodoWrite`.** A `todos` array of
`{content, status, activeForm}`, status enum `pending`/`in_progress`/`completed`, rule "exactly
ONE in_progress at a time." Legacy TodoWrite never touches disk (dies on restart/compaction);
the newer Task tools (`TaskCreate`/`TaskUpdate`, Claude Code v2.1.142) persist to
`~/.claude/tasks/<id>/` and patch by ID (`CLAUDE_CODE_ENABLE_TASKS`).
- https://code.claude.com/docs/en/agent-sdk/todo-tracking
- Tradeoff: keeps the agent on-track within a session but is volatile and context-bounded.

**B. On-disk file re-read every loop — the "ralph wiggum" pattern (Geoffrey Huntley).** Backlog
is plain Markdown: `fix_plan.md` = "a prioritized bullet-point list of unimplemented features,
sorted by importance," re-read fresh each iteration. The loop is literally
`while :; do cat PROMPT.md | claude-code ; done`. No explicit dequeue — the model picks the top
item. Anthropic shipped an official `ralph-wiggum` Claude Code plugin (Stop hook).
- https://ghuntley.com/ralph/ · https://github.com/anthropics/claude-code (plugins/ralph-wiggum)
- Tradeoff: brutal simplicity + unlimited runtime via fresh context, bought with nondeterminism
  ("requires faith and a belief in eventual consistency"). Human re-plans by editing the file.

**C. The literal kanban-file pattern (agent reads AND writes status transitions)** — closest
prior art to a `kanban.json`, and the broader ecosystem mostly *avoids* it:
- **Task Master (`claude-task-master`)** — one `tasks.json`, each task
  `{id, title, status, dependencies, priority, subtasks}`, status enum
  `pending`/`in-progress`/`done`/`review`/`deferred`/`cancelled`. MCP server: `next` dequeues
  by dependency+status, `set-status` transitions. https://github.com/eyaltoledano/claude-task-master
- **Backlog.md** — one Markdown file per task with YAML frontmatter; `status:` *is* the column;
  agent rewrites via `backlog task edit -s "In Progress"`; git tracks it. https://github.com/MrLesk/Backlog.md
- **vibe-kanban** (SQLite backlog, one git worktree per task) https://github.com/BloopAI/vibe-kanban;
  CodeAgentSwarm, atc-claude-kanban (agents move their own cards via watcher + SSE).
- Tradeoff: Task Master's single JSON is rich but merge-conflict-prone; Backlog.md's
  one-file-per-task is git-native and conflict-friendly but enforces no state machine.

**D. External tracker, event-triggered — the production default.** Backlog = GitHub Issues /
Jira / Linear; dequeue = a human or rule nominating the next item:
- **Sweep** — backlog is GitHub Issues; trigger = `Sweep` label / `Sweep:` title prefix;
  in-flight checklist is a single PR comment edited in place. Explicitly rejects open-ended
  autonomy ("a fixed flow of search → plan → write code → validate"). https://github.com/sweepai/sweep
- **Devin** — Jira/Linear/GitHub; each ticket → a session; dispatch via assignment, `devin`
  label, `!plan`/`!implement` playbooks, or status-edge triggers. https://docs.devin.ai/integrations/jira
- **GitHub Copilot coding agent** — Issues; assign "Copilot"; in-flight plan is a markdown
  checklist inside the PR. https://github.blog/ai-and-ml/github-copilot/assigning-and-completing-issues-with-coding-agent-in-github-copilot/
- **Cursor cloud agents** — no native backlog; six push entry points (Web, Slack, GitHub
  `@cursor`, Linear assign, API). https://cursor.com/docs/cloud-agents
- **SWE-agent / Aider** — no backlog; unit of work is one problem statement / one chat message;
  multi-task = a shell loop. https://swe-agent.com · https://aider.chat/docs/scripting.html

---

## 2. Token / cost budgeting and stopping conditions

Robust systems **stack three layers**:

**Layer 1 — USD/unit cap (hard stop on dollars).** OpenHands `max_budget_per_task` (default
0.0 = unlimited; on exceed → status ERROR). SWE-agent `per_instance_cost_limit` (default $3.0),
`total_cost_limit`; on per-instance exceed it *autosubmits the partial patch* rather than
crashing. AutoGPT `api_budget`. Commercial unit-metering: Devin ACUs (1 ACU ≈ 15 min;
sleeps after ~0.1 ACU idle); Cursor needs ≥$10 + per-task Spend Limit; Copilot premium-request
budgets. https://swe-agent.com/latest/reference/model_config/ · https://docs.devin.ai/admin/billing/usage
- Tradeoff: too tight truncates mid-task; OSS defaults of "unlimited" let a stuck agent run up
  unbounded cost — which is why iteration caps exist.

**Layer 2 — iteration / time cap.** OpenHands `max_iterations` (500); LangChain
`max_iterations` (15), `early_stopping_method` `'force'`/`'generate'`; CrewAI `max_iter` (25 in
source); LangGraph `recursion_limit` (25). **LangChain and CrewAI have no native USD cap.**

**Layer 3 — content-based no-progress detection.** OpenHands `StuckDetector` scans the last 20
events, fires on repeats (action→observation 4×, action→error 3×, monologue 3×, ping-pong 6×);
on by default. Aider `max_reflections = 3`. SWE-agent `max_requeries = 3`.
- https://github.com/OpenHands/software-agent-sdk (openhands/sdk/conversation/stuck_detector.py)
- Tradeoff: more precise than blind counters, can misfire on legitimate repetition (polling).

**Human-approval gate as a budget mechanism.** Claude Code permission modes
(`plan`/`acceptEdits`/`bypassPermissions`; auto-mode pauses after 3 consecutive / 20 total
blocks). Codex CLI separates approval (`--ask-for-approval`) from sandbox (`--sandbox`); clean
autonomous-but-contained point is `-a never -s workspace-write`.

**Folklore correction:** ralph's "loop until $X spent" is folklore — cited dollar figures are
*reported totals*, not programmatic guards. Real ralph guards are iteration counts and
completion-promise phrases. A true spend cap must be bolted on with an external cost meter. The
best tools **degrade gracefully** on exceed (autosubmit partial work; force one final answer)
rather than crash.

---

## 3. Introspection / self-evaluation

| Feedback source | Does self-eval help? | Evidence |
|---|---|---|
| None (pure introspection on reasoning) | **No — flat or worse** | Huang et al. 2310.01798 |
| Grounded/external (tests, tools, oracle) | Yes | CRITIC 2305.11738; Reflexion 2303.11366 |
| Separate trained critic | Yes (best-of-N) | OpenHands 60.6→66.4% SWE-Bench Verified |
| Generation/refinement (not reasoning) | Yes, modestly | Self-Refine 2303.17651 |
| LLM-as-judge scoring | ~Human-level but biased | MT-Bench >80% agreement |

- **Reflexion** (Shinn et al., NeurIPS 2023) — Actor/Evaluator/Self-Reflection; a sparse reward
  (ideally unit tests) becomes a *verbal* lesson in episodic memory for the retry. https://arxiv.org/abs/2303.11366
- **Self-Refine** (Madaan et al., NeurIPS 2023) — Generate→Feedback→Refine; gains concentrated
  in stronger models and on generation, not reasoning. https://arxiv.org/abs/2303.17651
- **CRITIC** (Gou et al., ICLR 2024) — self-correction works *only when critique is grounded in
  external tools* (run code, search). https://arxiv.org/abs/2305.11738
- **LLM-as-judge** (Zheng et al., MT-Bench) — GPT-4 judge >80% human agreement but has
  position/verbosity/self-enhancement biases; G-Eval adds CoT eval-steps. https://arxiv.org/abs/2306.05685
- **Skeptic:** Huang et al., *LLMs Cannot Self-Correct Reasoning Yet* (ICLR 2024) — intrinsic
  self-correction with no external feedback is flat-or-worse; earlier gains came from oracle
  labels you don't have at deployment. https://arxiv.org/abs/2310.01798
- **Verification before done (deployable):** decouple the grader from the generator and ground
  it in execution — OpenHands' separate critic model beat prompt-based reranking (60.6→66.4%);
  Claude Code's pattern runs a verification subagent in a *fresh context* that sees only the
  diff + acceptance criteria and must show evidence. https://www.openhands.dev/blog/sota-on-swe-bench-verified-with-inference-time-scaling-and-critic-model

Bottom line: an agent grading its own *reasoning* is unreliable. Make the judge be **execution**
(run the tests), a **separate** critic, or **agreement** (self-consistency voting).

---

## 4. Human-in-the-loop async feedback intake

The architectural split: **interrupt-and-abort** (discard in-flight work, take new input) vs
**queue-and-fold** (accept the message while running, inject at the next step boundary without
aborting). Five channels:

- **PR/issue comment (async, PR-granular):** Devin routes a PR comment into the live session
  (until archived); Copilot on `@copilot` (urges batching comments); Codex `@codex <x>` spins a
  cloud task. https://docs.devin.ai/integrations/gh
- **Plan-approval gate (synchronous, pre-execution):** Claude Code plan mode (`Ctrl+G` to edit
  the plan); Copilot Workspace (edit spec → regenerate plan). https://code.claude.com/docs/en/permission-modes
- **Interrupt-and-redirect (real-time, mid-turn):** Claude Code `Esc` (keeps work so far);
  Aider/Goose `Ctrl+C`. https://code.claude.com/docs/en/interactive-mode
- **Queue-and-fold (clean async):** OpenHands `Conversation.send_message()` from another thread
  while the agent runs; Linear's `prompted` AgentSessionEvent (per-message webhook, 5-s ack).
  https://docs.openhands.dev/sdk/guides/convo-send-message-while-running · https://linear.app/developers/agent-interaction
- **Steering file (async, between loops):** ralph — human edits `fix_plan.md`; next iteration
  re-reads. Dead simple, no synchronous gate.
- **HITL frameworks:** LangGraph `interrupt()` + `Command(resume=...)` (gotcha: resume re-runs
  the whole node, side effects re-fire); HumanLayer `@hl.require_approval()` over Slack/email.

Tradeoff: comment/file channels are durable+async but redirect at coarse (commit/loop)
granularity; interrupt is immediate but can leave partially-applied edits; queue-and-fold never
discards work but can't stop an action already underway.

---

## 5. Orchestration / scheduling

- **Cron / scheduled:** GitHub Actions `on: schedule: cron:` → `anthropics/claude-code-action@v1`.
  Gotcha: schedule-fired runs use the `github-actions[bot]` actor → OIDC token exchange 401s;
  supply your own token via `actions/create-github-app-token@v2`. Codex Automations (cron, but
  the local machine must be powered on); Devin Scheduled Sessions (cron, UTC, cloud).
  https://code.claude.com/docs/en/github-actions
- **Daemon vs ephemeral:** ralph keeps the *process* alive but discards *context* each loop. The
  ephemeral-VM camp (Codex cloud, Cursor cloud, Devin) discards the *whole machine* per task —
  **per-task sandbox maps cleanly onto a per-task attestation boundary.** https://developers.openai.com/codex/cloud/environments
- **Event-triggered vs polling, edge vs level:** Devin uses **edge detection** ("only fire when
  a ticket transitions from not matching to matching… not for tickets that already match").
  Edge/webhook = instant + cheap but loses events during downtime and ignores the existing
  backlog; polling/level = latency + wasted reads but self-healing.
- **State/memory across runs:** git history (ralph's journal: task → test → commit → exit, bad
  trajectories `git reset --hard`); instruction files re-injected each run (`CLAUDE.md`,
  `AGENTS.md` open standard); LangGraph checkpointers keyed by `thread_id`. https://agents.md/
- **MCP as shared state:** a memory MCP server (knowledge graph persisted to `memory.jsonl`;
  mem0/OpenMemory) the next run re-handshakes against. https://modelcontextprotocol.io/introduction

---

## Synthesis: a minimal, robust setup for a solo dev driving remote agents

1. **Backlog = a tracked store the agent reads *and* writes, with an enforced status enum.**
   The production default is an external tracker (GitHub Issues) + event trigger; the
   self-writing-kanban file (Task Master / Backlog.md) is viable solo but conflict-prone.
   Enforce the enum in the control plane so the agent can't write garbage states.
2. **Budget = three stacked guards.** USD/unit cap with **graceful degradation** (commit partial
   + mark `review`, never crash); an iteration backstop; no-progress detection. Don't rely on
   the agent self-reporting "done."
3. **Introspection = grounded, decoupled verification — never self-graded reasoning.** Gate
   "done" on execution (tests pass) + a separate reviewer in a fresh context seeing only the
   diff + acceptance criteria. The skeptical literature is unambiguous here.
4. **Feedback = a re-read file + an async fold-in queue**, plus a synchronous plan-approval gate
   for high-cost tasks.
5. **Orchestration = ephemeral-per-task sandboxes, edge-triggered with a polling safety net, git
   as durable state.** A fresh sandbox per card gives clean context *and* a clean
   attestation/isolation boundary — directly relevant to tee-daemon.

One line: **tracked backlog the agent dequeues and updates → ephemeral sandbox per card → three
stacked budget guards with graceful partial-commit → execution-grounded + decoupled verification
gate → human steers via a re-read file or a fold-in queue → edge-triggered with a polling
backstop.** Every durable property lives outside the agent's context window.

---

## Caveats surfaced by the research (relayed honestly)

- ralph's "loop until $X spent" stop condition is folklore; real guards are iteration /
  completion-phrase, not dollars.
- LangChain and CrewAI have **no native USD budget** (iterations/time/rate only).
- Some exact paper figures (Huang GSM8K 75.9→74.7; OpenHands 60.6→66.4; MT-Bench >80%) come from
  consistent secondary summaries; confirm against arXiv HTML before citing in published work.
- Unconfirmed from primary docs: Devin's interrupt-vs-queue mechanism; Trae SOLO plan-edit
  granularity; webhook-vs-polling transport for GitHub comment integrations.
- Doc/source drift: CrewAI `max_iter` 25 (source) vs 20 (docs); LangGraph `recursion_limit`
  default moved 25→10007 in `main`.
- Vendor drift: Sweep pivoted to a JetBrains plugin; Cursor "Memories" removed ~v2.1.x.
