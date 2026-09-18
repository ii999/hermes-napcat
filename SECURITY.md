# Security boundaries

A QQ channel gives remote people a route to an agent. Deploy a dedicated account and Hermes profile. Start with user and group allowlists and empty toolsets; grant only the capabilities needed for your use case. A prompt, role name or administrator label is not an OS sandbox. Tools executing code need their own container/user/filesystem restrictions.

The adapter checks self_id, user authorization and group authorization before processing messages or attachments. Hermes performs a second gateway authorization pass. `NAPCAT_ALLOWED_USERS` is required for a consistent gate at both layers. `admins` enables gateway control; it does not grant model tool access or bypass upstream policy. Group sessions override model toolsets with an empty list by default.

Transport tokens go in Authorization headers, never URL query strings. Non-loopback plaintext links require operator opt-in. Use private networking or certificate-verified TLS, keep OneBot and NapCat management ports off the public Internet, and do not log authorization headers. Treat OneBot itself as a trusted service: a stolen token or compromised NapCat process can forge events for that bot account.

The media downloader allowlists hosts, checks actual DNS results, validates each redirect, ignores proxy environment settings, enforces transfer/cache bounds and refuses encoded bodies. Configure trusted_private_origins only for dedicated read-only media servers; a URL path can invoke a sensitive GET endpoint on a privileged service. Image header checks are not a full decoder sandbox. Keep media parsers patched. Local outbound file access is restricted to explicitly configured roots and resolved paths; concurrent malicious modification of the local filesystem is outside this process-level check.

Recent-ID deduplication and queues live in process memory. A crash, overload or OneBot disconnect can lose events. The plugin does not guarantee exactly-once delivery. If a message action times out after a write, inspect the QQ conversation before retrying. Partial sends expose known message IDs; never treat a successful prefix as full delivery.

QQ participants can supply prompt injections through text, quotes, images and documents. The plugin preserves source identity and limits gateway control but cannot guarantee model-level instruction isolation. Review the full Hermes tool and memory configuration before sharing a group session.

Report a vulnerability privately to the repository owner. Do not include working tokens, complete private message histories or personal account details in a public issue. The source release has not undergone an independent security audit or a live QQ acceptance test.
