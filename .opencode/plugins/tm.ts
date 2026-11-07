// Written by `tm skills install --harness opencode`; the next install rewrites
// it. A plugin for OpenCode 2 — a default export with an id and a `setup` —
// which hands every tool call to `tm hook opencode <event>` and applies the
// answer: a refusal before a tool is thrown (the model reads the reason as the
// tool's failure), rewritten arguments replace the call's, and what the hook
// says after a tool is appended to what the model reads of the result; before
// a tool, context beside an allowed call has no channel and is dropped. A hook
// that could not check the call says so to the person, and the tool runs.
//
// A server plugin has no toast. What the person is to see — what the hook
// says as a session begins, a call left unchecked — is an event of this
// plugin's RPC, which tm's TUI plugin, `tm-notices/tui.ts` beside this file,
// shows as a toast; it goes to the console of OpenCode's server too, where it
// is all there is when no TUI is attached.
//
// A call signs as the role of its harness, `opencode`, and any number of
// sessions sign as it; what a session holds is its claims. A session that
// begins is handed to the hook; a subagent's session is handed over with the
// session it was started under, which tm records, so that the subagent's
// calls count as its root's and a claim of the root speaks for its edits.
// OpenCode puts the session's id
// in every command its shell tool runs, in OPENCODE_SESSION_ID, which is what
// `tm` reads to know whose call it is. OpenCode gives its MCP servers no
// session at all, so a claim is taken with `tm` in the shell, where the
// session is; the `tm mcp` registration in opencode.json is `--readonly`.
import { accessSync, constants, readFileSync } from "node:fs"

// The binary, looked for in this order: TM_BIN, the one the machine's
// registry names, the freshly installed one, then PATH. OpenCode
// started from a desktop session may have no ~/.local/bin on its PATH, and an
// older `dart pub global` shim may sit ahead.
// pi's extension looks the same way, in the same lines from `executable` to
// `tm`: neither can load the other's, so a test holds the two to one text.
const home = process.env.HOME ?? ""
const executable = (p: string) => {
  try {
    accessSync(p, constants.X_OK)
    return true
  } catch {
    return false
  }
}
// `tm:` at the top of ~/.tm/tm.yaml, read as a line, with no parser: the one
// line of that key, a value plain or quoted, `~/` read as the home.
const registered = (() => {
  try {
    const line = /^tm:[ \t]*(.*)$/m.exec(readFileSync(`${home}/.tm/tm.yaml`, "utf8"))?.[1] ?? ""
    const quoted = /^"([^"]*)"|^'([^']*)'/.exec(line)
    const path = quoted ? (quoted[1] ?? quoted[2]) : line.replace(/\s+#.*$/, "").trim()
    return path.startsWith("~/") ? `${home}/${path.slice(2)}` : path
  } catch {
    return ""
  }
})()
const candidates = [process.env.TM_BIN, home && registered, home && `${home}/.local/bin/tm`]
const tm = candidates.find((p) => p && executable(p)) ?? "tm"

type Answer = Record<string, unknown> & { refused?: string }

// The RPC the TUI plugin listens on: no methods, one event. OpenCode checks
// what is emitted against the event's schema, a Standard Schema here, which
// takes the notice as it comes.
const notices = {
  id: "tm",
  methods: {},
  events: {
    notice: { schema: { "~standard": { version: 1, vendor: "tm", validate: (value: unknown) => ({ value }) } } },
  },
}

export default {
  id: "tm",
  async setup(api: any) {
    const directory: string = api.location.directory

    // The guard does not depend on the notices: where OpenCode will not
    // register the RPC, the console is left, and the tool hooks are hooked
    // as they would be.
    const channel = await Promise.resolve()
      .then(() => api.rpc.register(notices, {}))
      .catch((e: unknown) => console.error(`tm could not register its notices to the TUI: ${e}`))
    const tell = (text: string, variant: "info" | "warning", sessionID?: string) => {
      console.error(text)
      void Promise.resolve()
        .then(() => channel?.events.emit("notice", { text, variant, ...(sessionID ? { sessionID } : {}) }))
        .catch(() => {})
    }
    const warn = (text: string, sessionID?: string) => tell(text, "warning", sessionID)

    // A call the hook could not check: said, and the tool runs.
    const failOpen = (what: string, sessionID?: string): Answer => {
      warn(`${what}; the call was not checked`, sessionID)
      return {}
    }

    const hook = async (event: string, payload: Record<string, unknown>): Promise<Answer> => {
      const sessionID = typeof payload.session_id === "string" ? payload.session_id : undefined
      const json = JSON.stringify(payload)
      const r = await Bun.$`${tm} hook opencode ${event} < ${new Response(json)}`
        .cwd(directory)
        .quiet()
        .nothrow()
      const stderr = r.stderr.toString().trim()
      const text = r.stdout.toString().trim()
      if (r.exitCode === 2) {
        // A refusal carries its answer on stdout as well. Exit 2 with nothing
        // there is a tm that could not answer — an older one says so of a
        // harness or an event it does not know — and is no refusal.
        if (!text) {
          return failOpen(`${stderr ? `${stderr}\n` : ""}tm hook exited 2 on ${event} with no answer`, sessionID)
        }
        return { refused: stderr || "tm hook refused the call and gave no reason" }
      }
      // One notice for a hook that failed, with what it said: a toast shown
      // takes the place of the one before it.
      if (r.exitCode !== 0) {
        return failOpen(`${stderr ? `${stderr}\n` : ""}tm hook exited ${r.exitCode} on ${event}`, sessionID)
      }
      if (stderr) {
        warn(stderr, sessionID)
      }
      if (!text) {
        return {}
      }
      // Not JSON is the hook failing open, like any other answer it should not
      // give: thrown from here it would be a refusal with a parse error as the
      // reason.
      let answer: { hookSpecificOutput?: Answer } | null
      try {
        answer = JSON.parse(text)
      } catch {
        return failOpen(`tm hook answered ${event} with something that is not JSON`, sessionID)
      }
      return answer?.hookSpecificOutput ?? {}
    }

    // Each session's start, told to the hook once: from the bus event where it
    // arrives, else before the session's first tool call — a subscription can
    // miss an event, and a session never told would be unknown to the hook. A
    // subagent is told with its parent, after the parent itself.
    const started = new Map<string, Promise<void>>()
    const start = (sessionID: string, info?: { parentID?: string; location?: { directory?: string } }) => {
      let told = started.get(sessionID)
      if (!told) {
        told = (async () => {
          const known = info ?? (await api.session.get({ sessionID }).catch(() => undefined))
          const parent = known?.parentID
          if (parent) {
            await start(parent)
          }
          const answer = await hook("session-start", {
            hook_event_name: "session.created",
            session_id: sessionID,
            ...(parent ? { parent_session_id: parent } : {}),
            cwd: known?.location?.directory || directory,
            source: "startup",
          })
          // Nothing the model reads comes of a bus event, so what the hook
          // says as the session begins is the person's, beside the warnings.
          const context = answer.additionalContext
          if (typeof context === "string" && context.trim()) {
            tell(context, "info", sessionID)
          }
        })().catch((e) => warn(`tm hook could not be told session ${sessionID} began: ${e}`, sessionID))
        started.set(sessionID, told)
      }
      return told
    }

    const events = api.event.subscribe()[Symbol.asyncIterator]()
    void (async () => {
      for (;;) {
        const next = await events.next().catch(() => ({ done: true }) as const)
        if (next.done) {
          return
        }
        const event = next.value
        if (event?.type === "session.created" && typeof event.data?.sessionID === "string") {
          void start(event.data.sessionID, event.data)
        }
      }
    })()

    // What setup registered, handed back when OpenCode unloads the plugin —
    // which it does on every edit of this file, before loading it again.
    const hooks: { dispose(): unknown }[] = channel ? [channel] : []

    hooks.push(await api.tool.hook("execute.before", async (call: any) => {
      await start(call.sessionID)
      const answer = await hook("pre-tool-use", {
        hook_event_name: "tool.execute.before",
        session_id: call.sessionID,
        cwd: directory,
        tool_name: call.tool,
        tool_input: call.input,
      })
      if (typeof answer.refused === "string") {
        throw new Error(answer.refused)
      }
      const updated = answer.updatedInput
      if (updated !== null && typeof updated === "object") {
        call.input = updated
      }
    }))

    // A failed call too: a command that wrote and then exited non-zero is
    // still checked, and its snapshot taken. It has no result to append to,
    // and its error is OpenCode's to word.
    hooks.push(await api.tool.hook("execute.after", async (call: any) => {
      const result = call.result
      const answer = await hook("post-tool-use", {
        hook_event_name: "tool.execute.after",
        session_id: call.sessionID,
        cwd: directory,
        tool_name: call.tool,
        tool_input: call.input,
        tool_response: result,
      })
      const note = typeof answer.refused === "string" ? answer.refused : answer.additionalContext
      if (typeof note !== "string" || call.status !== "completed") {
        return
      }
      // What the model reads is `content`, or the output where there is none —
      // the order OpenCode reads them in; the note is a part after it.
      const parts =
        typeof result.content === "string"
          ? [{ type: "text", text: result.content }]
          : Array.isArray(result.content) && result.content.length > 0
            ? [...result.content]
            : [{ type: "text", text: typeof result.output === "string" ? result.output : JSON.stringify(result.output) ?? "" }]
      result.content = [...parts, { type: "text", text: note }]
    }))

    // The session a command runs in is OpenCode's to set, after this hook.
    // Claude Code's variable is removed: OpenCode started from Claude Code's
    // shell inherits that session's id, which `tm` reads first, and the
    // command would carry that session.
    hooks.push(await api.shell.hook("create.before", (shell: any) => {
      delete shell.env.CLAUDE_CODE_SESSION_ID
    }))

    return () => {
      void Promise.resolve(events.return?.()).catch(() => {})
      for (const h of hooks) {
        void Promise.resolve()
          .then(() => h.dispose())
          .catch(() => {})
      }
    }
  },
}
