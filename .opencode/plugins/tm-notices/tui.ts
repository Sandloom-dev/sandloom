// Written by `tm skills install --harness opencode`; the next install rewrites
// it. The half of tm's plugin for OpenCode 2 that runs in its TUI: what the
// server half, `../tm.ts`, has for the person — what the hook says as a
// session begins, a call the hook could not check — arrives as an event of
// that plugin's RPC and is shown here as a toast. A server plugin has no toast of its own.
//
// OpenCode 2 loads a TUI plugin that is not a package from a directory under
// `.opencode/plugins/` holding a `tui` module; the server passes such a
// directory over, as it loads `../tm.ts` alone.

// The RPC of `../tm.ts`, as a client names it: by its id and the names of its
// events.
const notices = { id: "tm", methods: {}, events: { notice: {} } }

export default {
  id: "tm-notices",
  async setup(api: any) {
    // What `on` hands back stops the subscription; returned, OpenCode calls it
    // when it unloads the plugin.
    return api.client.rpc(notices).events.on("notice", (event: any) => {
      // One background server serves every directory OpenCode is open in,
      // and each TUI attached to it is sent every directory's events.
      if (event?.location?.directory !== api.location?.directory) {
        return
      }
      const { text, variant, sessionID } = event.data ?? {}
      if (typeof text !== "string" || !text.trim()) {
        return
      }
      // Given the session it is about, OpenCode shows a toast from a session
      // other than the one on screen with a way to open that session.
      api.ui.toast.show({
        title: "tm",
        message: text,
        variant: variant === "warning" ? "warning" : "info",
        ...(typeof sessionID === "string" ? { sessionID } : {}),
      })
    })
  },
}
