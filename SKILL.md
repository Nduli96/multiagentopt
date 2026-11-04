---
name: multiagentopt
description: Runs a list of tasks with parallel agents while keeping token spend down. Every task, and every piece of a task, goes to the cheapest model that can do it - Opus for decisions and debugging, Sonnet for clear implementation and verification, Haiku for reads and closed searches, a script with no model for mechanical edits. Decides the shared contract up front, has agents work in the same working tree with locked byte-level edits through patch.py, then integrates and builds once. Invoke with /multiagentopt followed by the task list.
---

# The cheapest model that can do the job

Invoking the skill is the go-ahead: no confirmation, start.

A subagent inherits its parent's model unless told otherwise, so an Opus agent that spawns a helper to read three files pays Opus prices for the reading. This skill makes the model an explicit choice every time an agent is started.

## 1. Routing

Choose from this table for every task and every subagent:

| Work | Who |
|---|---|
| Understanding why something happens, debugging, design decisions | **Opus** |
| Implementing something already specified, adversarial verification, review | **Sonnet** |
| Reading files, broad searches with a closed and checkable answer, simple mechanical tasks | **Haiku** |
| Mechanical transformations across N files (N lines, N languages, N ids) | **No model**: a script in the scratchpad |

Rules that go with it:

- **Always pass `model` explicitly**, on every Agent call and every `agent()` call in a Workflow.
- **Opus never spawns Opus to read, search or do something simple.** Opus under Opus is only for a part that needs design or debugging judgment of its own.
- **Prefer a script to a model** for mechanical work. It is cheaper, repeatable and does not make encoding mistakes.
- **Use a cheap model only when the question is closed and a wrong answer is obvious at a glance.** Otherwise the savings get spent on fixing the result.
- **Do not delegate what you can do yourself in a few tool calls.** An agent starts cold, and the brief that keeps it from going wrong can cost more than the work.
- **Do not hand scripts that edit files at byte level to a cheap model.** That is where encoding corruption starts.

## 2. Tracking file and memory

First create `<scratchpad>/multiagentopt-<slug>.md` in the session scratchpad, not in the repo:

- the numbered tasks, with the user's wording almost verbatim;
- a table `# | task | model | status | files touched | what is left`, with status one of `todo`, `running`, `done`, `to test by hand`, `blocked`;
- the **Contract** section (step 3);
- at the bottom, **To do by hand**: server builds, restarts, manual tests.

Update it at every step: after launching, on every result and at the end. It holds the state of this batch and dies with the session.

Anything that must outlive the session goes only into the project's existing memory or docs, never a parallel folder:

- constraints, traps, magic numbers, assigned id ranges: project memory;
- what is left to test by hand, manual cleanups, unverified suspicions: the project's list of pending work;
- flow, files touched, how to test: project docs.

## 3. Contract, decided here and not by the agents

Two parallel agents that have to agree on a value will invent different ones. Asking them to reconcile in their reports does not work, because reports are written when the work is already done.

Everything shared between tasks is assigned **in the main session**, written in the tracking file and copied into **every** brief:

- **shared data structures and wire formats**: field by field, with types and the computed total size;
- **names called across a boundary**: exact name and signature of each function one side exposes and the other calls;
- **ids, enum values and constants**: a separate free range for each task, plus a reserve;
- **shared files**: which tasks touch the same file.

If an agent really has to decide one of these, the work is not parallel. Split it into two phases, where the second reads the first's result instead of assuming it.

Inside a single task the agent of that task decides its own contract. If it splits the task into parallel subagents, it decides the contract in their briefs.

## 4. Launch

- **One agent per task**, with the model chosen from the table in step 1. Use type **general-purpose** for any agent that may spawn subagents.
- **With a Workflow** (ultracode, or the user asked for one), run each task through implement, verify and fix. The fix runs only if verification confirms defects:

  ```js
  await pipeline(tasks,
    t => agent(brief(t), { model: t.model, agentType: 'general-purpose', label: t.name }),
    (result, t) => agent(verifyPrompt(t, result), { model: 'sonnet', schema: VERDICT, label: 'verify: ' + t.name }),
    (verdict, t) => verdict.defects.length
      ? agent(fixPrompt(t, verdict), { model: t.model, agentType: 'general-purpose', label: 'fix: ' + t.name })
      : verdict)
  ```

- **Without a Workflow**: all Agent calls in **a single message**, in the background.
- **Subagents**: an agent splits its task only when the parts are independent, and routes each part with the same table.

## 5. Brief for each agent

It contains the task text, the contract, the paths already known, the rules below and the report format. Copy the rules **verbatim**:

1. The working tree is **shared with other agents working right now**. Never `git checkout`/`restore`/`stash`/`reset`/`clean`, never `add`/`commit`/`push`. To see the original of a file: `git show HEAD:<path>`.
2. **Existing** files are changed **only** with `.claude/skills/multiagentopt/patch.py`. From a script in the scratchpad: `sys.path.insert(0, r"<repo>/.claude/skills/multiagentopt")`, then `from patch import replace, insert_after, insert_before, append, new_file`. Never use Edit or Write on an existing file: they take no lock, and they can damage files that are not UTF-8.
3. Pass `enc=` with **the encoding of the file being changed**, such as `enc="utf-8"`. The default is `latin-1`, which is only safe on a UTF-8 file when the text is pure ASCII.
4. Keep changes **targeted**. Never rewrite a shared file in full, and reread the area right before changing it, because another agent may have changed it.
5. New files: `new_file(...)`, which fails if the file already exists, or Write with pure ASCII content. Pass `crlf=False` to `new_file` unless the project uses CRLF.
6. **Do not run the shared build, packaging or deployment.** The main session does those once at the end, because two builds on the same tree collide.
7. Stay within the task. Declare every file you touch outside it.
8. Comments are one line. Write docs only for a new system, or update the existing doc if there is one. Write memory only for constraints that are not obvious, and append to a shared memory index with `patch.py`.
9. Final report: status; files touched; the contract actually used (ids, names, sizes); checks run; what is left (build, deployment, manual test); assumptions and doubts. Say so if something could not really be compiled or run.

## 6. Integration (main session)

1. Run `python .claude/skills/multiagentopt/patch.py --verify`: no more U+FFFD than HEAD, no mixed line endings.
2. Check the contract was honored: ids and names unique, and each shared structure, name and signature matching on both sides.
3. **Build once.** Outside a Workflow, send errors back to the agent of that task with `SendMessage`, or fix them here. In a Workflow run, fix them here or start a new agent (see Traps).
4. Package or deploy **once**, if the project has such a step and the user asked for it.
5. Write whatever cannot be done from here under **To do by hand**.
6. Update the tracking file, move what is pending into the project's list of pending work, and report it. No commit unless the user asks.

## Traps

- **Never `SendMessage` an agent running inside a Workflow.** The message does not reach it. It wakes a **second copy** from the same transcript while the original keeps going, and the two collide on the same files. Put a common fix in a shared file or stop the workflow. If it has already happened, `TaskStop` on that agent's id stops the woken copy and leaves the workflow's copy running.
- **Checking against stubs is not verification.** Stubs written by the agent only confirm what the agent assumed. If the real build is not available, the task counts as not compiled, and the report must say so.
