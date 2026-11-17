### Changed

- **`EVENT_BUS_BACKEND=redis` is refused with one sentence** (idk-low7-kernel). The message names the removed driver, the release that removed it, the new name `broadcast`, and both spellings to write: `EVENT_BUS_BACKEND=broadcast`, or `{"eventbus": {"default": {"driver": "broadcast"}}}` in `INFRA`. It no longer repeats the `INFRA` refusal with its "other capabilities" tail. A configuration that still writes `redis` is refused at startup as before.
