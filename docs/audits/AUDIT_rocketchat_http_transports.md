# Audit — Hermes Rocket.Chat adapter: HTTP/REST layer + transports

Scope reviewed: `adapter.py` (3344 lines) `RocketChatClient` (451–847), `PollingTransport` (1161–1293), `WebSocketTransport` (1457–1896), seen-id/checkpoint stores (1026–1159), adapter inbound path (2771–2898). Every claim below was verified against the code; key lines are quoted.

## P0 — broken behavior, fails in production

### P0-1. `upload_attachment()` never transmits file bytes — `rooms.media` gets JSON metadata only
`adapter.py:809-813`:
```python
upload_result = await self._request(
    "POST",
    f"/api/v1/rooms.media/{room_id}",
    json={"file_name": file_name, "file_path": str(path)},
)
```
The payload is **only** `{"file_name", "file_path"}` (the server-side path of a local file — meaningless to the server). No `multipart/form-data`, no `aiohttp.FormData`/`httpx files=` exists anywhere in the repo (grep for `multipart|FormData|FileField` matches only the docstring). Rocket.Chat's `/api/v1/rooms.media/{roomId}` expects a multipart `file` field; a JSON body is rejected — every real media send fails with `"Upload failed"`, and even if it returned 2xx no content would be stored.
The admitting docstring is explicit, `adapter.py:793-798`:
```
In a production deployment with real multipart uploads the first step sends
the file as form data. The current implementation sends file metadata as JSON
so that tests with mock HTTP sessions can exercise the full flow without
multipart machinery.
```
**Fix:** add a form-data path to `_request` (or a dedicated `_upload_file`). `_request` already normalizes both backends: it passes `json=…/params=…/headers=…` to `session.request(...)` and wraps the call in `_maybe_await` (`adapter.py:515-519`, helper at 393-397). Extend it with a `files:`/`data:` branch: for aiohttp build `aiohttp.FormData()` + `form.add_field("file", open(path,"rb"), filename=name, content_type=mime)` and pass `data=form`; for httpx pass `files={"file": (name, fh, mime)}`. Both go through the same `session.request(method, url, data=…, headers=…)` + `_maybe_await` shape, so the dual-sync/async abstraction is preserved. (Downstream `_send_media_file`, `adapter.py:2468-2515`, and `standalone_send`, `3266-3274`, call `upload_attachment` unchanged.)

## P1 — real deployment risk

### P1-1. Every REST call opens a fresh `ClientSession`/`AsyncClient` — no connection reuse, TLS handshake per request
`_get_session()` creates a brand-new client on every call, `adapter.py:483-492`:
```python
async def _get_session(self):
    import aiohttp
    return aiohttp.ClientSession()
```
and `_request` wraps it in `async with`, `adapter.py:512-514`; `download_attachment` does the same, `adapter.py:771-773`. Under aiohttp a new `ClientSession` means a new connector with no pool, so **every** HTTP call pays a fresh TCP connect + TLS handshake and tears the connection down afterward — no keep-alive. A polling cycle is `list_subscriptions()` + one `sync_messages()` per updated room (`adapter.py:1239-1265`), i.e. 1 + N TLS handshakes every `poll_interval` (default 3 s, `adapter.py:119-124`), plus one per outbound send/typing/edit. HTTPS handshakes are multi-RTT; combined with the missing timeout (P1-2) this adds latency to every send.
**Fix:** cache one session on the client (`self._session` created lazily by `_get_session`), share it between `_request` and `download_attachment`, and add `async def close()` that the adapter's `disconnect()` (`adapter.py:2123-2129`) and PollingTransport/WebSocketTransport `stop()` call.

### P1-2. No timeout on any REST call — hung server stalls poll loop, WS handshake/bootstrap, sends
`grep timeout` in the file shows timeouts only for config fields (49-50, 153-161) and the WS receive/ping `asyncio.wait_for` (`adapter.py:1675, 1682`). `_request` (`512-546`) and `download_attachment` (`765-777`) pass **no** `timeout=`. With aiohttp the only bound is its 5-minute default total timeout; with httpx (the fallback, `490-492`) a 5 s default applies — behavior depends on which library got installed, and neither is surfaced to the transports. Consequences: a hung `subscriptions.get` inside `_poll_loop` freezes message delivery for up to 5 min (`adapter.py:1211-1231`); a hung `list_subscriptions()` in `_bootstrap_subscriptions` stalls the WS reconnect loop (`adapter.py:1823`); `initialize`, `post_message`, `update_message`, `upload_attachment`, and `standalone_send` all block the awaiting caller.
**Fix:** pass an explicit `timeout` (e.g. `aiohttp.ClientTimeout(total=…)` / `httpx.Timeout(…)`) into `_request` and `download_attachment`, and wrap the WS handshake recv calls in `asyncio.wait_for` (P1-5).

### P1-3. Inbound attachment download is broken: `_file_url_from_rc` returns a *relative* URL that is never joined to `server_url`
`adapter.py:913-919`:
```python
def _file_url_from_rc(file_obj: dict, message: dict) -> str:
    rid = message.get("rid", "")
    fid = file_obj.get("_id", "")
    if fid and rid:
        return f"/file-upload/{rid}/{fid}/{file_obj.get('name', 'file')}"
    return ""
```
`download_attachment` then does `session.get(url, headers=headers)` (`adapter.py:774`) with that relative path — aiohttp raises (`Only absolute URLs are supported`), httpx raises `InvalidURL`. The exception is swallowed by `resolve_message_media`'s `except Exception` → warning + `continue` (`adapter.py:1001-1007`), so **every file/file[] attachment is silently dropped** in inbound media handling. (The `attachments[].image_url` branch, `876-885`, is absolute and works.) When no `media_cache_dir` is configured, the relative URL is passed straight through as `media_urls` (`adapter.py:1008-1009`), equally unusable downstream.
**Fix:** join with the origin — e.g. `urljoin(client.server_url, candidate.url)` before download (Rocket.Chat's `/file-upload/...` serves with `X-User-Id`/`X-Auth-Token` headers, which `download_attachment` already sends).

### P1-4. DDP login `result.error` is ignored — silent dead WebSocket, no re-auth, no auth-failure callback
`_handshake` waits for any result with id "1" and breaks, without inspecting `error`, `adapter.py:1807-1817`:
```python
if msg.get("msg") == "result" and msg.get("id") == "1":
    break
```
A login failure (invalid/expired resume token, e.g. token rotated or server restart) is therefore swallowed: the transport proceeds to subscribe with a dead session, the server keeps sending pings, the heartbeat sees "any frame" and never fires, and the bot is permanently blind until restart. The re-auth path (`_handle_connection_error` → `_is_auth_error` → `_reauthenticate`, `adapter.py:1702-1704, 1735-1744, 1746-1763`) only triggers on exceptions whose text contains auth markers — a DDP error frame never raises, so `_on_auth_failure` (→ `_mark_auth_fatal`) is never invoked for the most common auth failure mode. (Test `test_auth_error_triggers_reauthentication`, `tests/test_websocket.py:560-594`, only calls `_handle_connection_error` manually with an exception — it does not exercise the DDP frame path.)
**Fix:** in the login-result loop, if `msg.get("error")` is present, raise a `RocketChatClientError` (carrying the error text) so the normal `_is_auth_error`/reconnect plumbing runs; treat a `nosub`/`failed` result the same way.

### P1-5. WS handshake has no timeout — a server that never sends `connected`/`result` hangs `_receive_loop` forever
The handshake recv loops, `adapter.py:1783-1784` and `1808-1809`, call `_ws_recv_text(ws)` with no `asyncio.wait_for` (unlike `_read_loop`, `1674-1676`). DDP version-mismatch frames (`{msg:"failed", version}`) are also unhandled (`1789-1792` handles only `connected`/`ping`), so the loop just blocks on the next recv. The heartbeat never starts (it is only in `_read_loop`). A socket that accepts but never speaks leaves the transport stuck in the handshake with no backoff, and `stop()` can't interrupt a blocked `recv()` on a real aiohttp ws until the socket closes.
**Fix:** wrap each handshake `_ws_recv_text` in `asyncio.wait_for(…, timeout)` (reuse `_receive_timeout`) and handle `msg == "failed"` by raising/negotiating.

### P1-6. `list_subscriptions(updated_since=…)` builds the param but never sends it; polling ignores server-side delta and truncates pagination
`adapter.py:703-709`:
```python
async def list_subscriptions(self, updated_since=None) -> list[dict]:
    params = {}
    if updated_since:
        params["updatedSince"] = updated_since
    data = await self._request("GET", "/api/v1/subscriptions.get")   # params never passed
    return data.get("update", data.get("subscriptions", []))
```
`params` is dead code. Both transports call it with no delta (`poll_once` `adapter.py:1239`, `_bootstrap_subscriptions` `1823`), so polling re-fetches the full subscription list every cycle (client-side filter at `1244-1247`) and Rocket.Chat's default 50-item pagination can silently truncate the room list for bots in many rooms (rooms beyond the first page are never polled/subscribed).
**Fix:** pass `params=params` in the `_request` call, and have `poll_once` pass its oldest checkpoint as `updated_since`.

### P1-7. `PersistentSeenIdStore.flush()` runs a synchronous full-file rewrite on **every** inbound message
`_on_inbound` marks and flushes per message, `adapter.py:2823-2825`:
```python
if msg_id:
    self._seen_id_store.mark(msg_id)
    self._seen_id_store.flush()
```
`flush()` (`adapter.py:1132-1158`) is **synchronous** file I/O: prune + `tempfile.mkstemp` + `json.dump` of the entire store + `os.replace` — executed inline on the event loop before `handle_message`. Store size ≈ rate × `dedup_ttl_hours` (default 168 h, `adapter.py:187-191`): at ~1 msg/s that is ~600k ids, a multi-MB JSON rewritten per message. Cost per message is O(store size), and it blocks every other coroutine (including the WS receive loop's `change` handling, since `_handle_changed` awaits the same callback chain).
**Fix:** make `mark`/`flush` async-aware (run in `asyncio.to_thread` / `loop.run_in_executor`) and debounce flushes (flush at most once per N seconds or on `stop()`/reconnect), keeping `contains` fast.

### P1-8. No mid-session token refresh; 429 is honored only inside the poll loop, and outbound 429s are misclassified
- Polling: an expired token makes every `_request` raise `RocketChatClientError("…HTTP 401…")`; `_poll_loop` logs and sleeps forever (`adapter.py:1227-1231`) with no re-auth (`initialize()` runs only in `connect`, `2067-2078` — and there is no refresh logic in `_verify_token`/`_login_password` at all).
- Rate limits: `_request` parses 429 + `Retry-After` into `RocketChatRateLimitError(retry_after=…)` (`adapter.py:533-541`) and `_sleep_after_error` honors it (`adapter.py:1198-1205` — verified: returns `max(poll_interval, retry_after)`, and `_poll_loop` sleeps it, `1217-1226`). **But** on all outbound paths (post/update/upload/typing/standalone), the 429 propagates as a raw error: `_is_transient_client_error` (`adapter.py:3094-3115`) matches `timeout|connection|closed|eof…` — `"HTTP 429"`/`"Too many requests"` matches none, so sends return a hard failure instead of the replayable `send_path_degraded` code (`adapter.py:2313-2318, 2501-2510`), and no sleep/retry is ever attempted.
**Fix:** catch `RocketChatRateLimitError` in `send`/`_send_media_file` and map it to `send_path_degraded` (or sleep `retry_after` once); add token-expiry (401) detection in the poll loop that re-runs `initialize()` once before giving up.

## P2 — quality / performance cleanup

- **P2-1** `adapter.py:583-584` — `_login_password` passes `headers={}` expecting "no auth headers yet", but `_request` merges into default headers that always carry `X-User-Id`/`X-Auth-Token` (`505-510`), so empty (or stale, if both modes configured) auth headers are sent on the login request. Harmless today; either strip them or drop the misleading comment.
- **P2-2** `adapter.py:1679-1689` — the heartbeat ping probe consumes whatever frame arrives within `ping_timeout` and `continue`s, so a real `changed` frame that lands during the probe is silently discarded (missed message). Dispatch it to `_handle_frame` instead.
- **P2-3** `adapter.py:1789-1792` / `1808-1817` — DDP `failed` (version negotiation) and `nosub` frames are unhandled during handshake (see P1-5); no version re-negotiation per DDP spec.
- **P2-4** Unbounded in-memory growth in the transports: `PollingTransport._seen_ids` (`1172`), `WebSocketTransport._seen_ids` (`1522`), `_room_types` (`1525`), `_sub_ids` (`1523`) all grow without eviction on a long-running gateway (the adapter layer bounds its dicts with `BoundedDict`, but the transports don't). Bound with `BoundedDict` or TTL-based pruning.
- **P2-5** `adapter.py:721-732` — `sync_messages` falls back to `history_messages` only on `RocketChatNotFoundError`; older servers that reply 400 to `chat.syncMessages` never fall back.
- **P2-6** `adapter.py:751-761` — `history_messages` fetches one `count=100` page with no offset/pagination; fast rooms can drop messages between polls.
- **P2-7** `adapter.py:1448-1454` — `_ws_url` mishandles non-`http(s)` server URLs (`"host:3000"` → `"host:3000/websocket"`, invalid ws URL).
- **P2-8** `adapter.py:564` — `_verify_token` accepts `success:false` responses as long as `_id` is present (`if not success and not _id`), weakening the auth check.
- **P2-9** `adapter.py:546` — `resp.json()` on a 2xx with non-JSON body raises an unwrapped `ContentTypeError`/`JSONDecodeError` instead of a `RocketChatClientError`.
- **P2-10** `adapter.py:1244-1246, 1288-1291` — poll checkpoint advances to the subscription `_updatedAt` (string-compared) rather than message timestamps; benign in practice but can skip/duplicate near checkpoint boundaries.
- **P2-11** WS `_handshake` reads `self._client._access_token` (`1801`) — works after `_login_password` persists the token (`593-594`), but the "password mode doesn't persist the token" hypothesis is **not** confirmed: `_login_password` does persist `userId`/`authToken` into `_user_id`/`_access_token` and every later request uses them. Good.

## Verified-correct items (checked, no issue found)
- `server_url` trailing-slash handling: `self._server_url = server_url.rstrip("/")` (`466`) — no double-slash bug in `f"{self._server_url}{path}"` (`504`).
- WS receive-timeout timer restarts on **any** frame: each loop iteration wraps a fresh `asyncio.wait_for(_ws_recv_text(ws), …)` (`1672-1690`) — correct heartbeat behavior.
- Reconnect backoff: exponential with cap + jitter, attempts reset on success (`1589-1596, 1645-1646`); `ws.close()` + `_close_http_session()` in `finally` prevent fd leaks (`1655-1662`).
- `RocketChatRateLimitError`/`Retry-After` parsing (`357-380`) and `_sleep_after_error` (`1198-1205`) are correct for numeric Retry-After (HTTP-date Retry-After values are not parsed — minor).

## Fix-first list (max 5, only confirmed defects)
1. **Real multipart upload** in `upload_attachment` (P0-1) — add a form-data path to `_request` (aiohttp `FormData` as `data=`, httpx `files=`) preserving the `_maybe_await` dual shape.
2. **Join `server_url` for inbound file URLs** (P1-3) — `urljoin(server_url, candidate.url)` in `resolve_message_media`/`download_attachment` so `file`/`files` attachments download.
3. **Session reuse + explicit timeouts** (P1-1/P1-2/P1-5) — cache one `ClientSession`/`AsyncClient` on `RocketChatClient` with `close()` on disconnect; pass timeout to `_request` and `download_attachment`; wrap WS handshake reads in `wait_for`.
4. **Inspect the DDP login `result.error`** (P1-4) — raise on error frames so re-auth + `_on_auth_failure` triggers instead of a silent dead socket.
5. **Fix `list_subscriptions` to pass `params`** + make `poll_once` pass `updated_since` (P1-6), and debounce/throttle `PersistentSeenIdStore.flush()` (P1-7).

---

*Evidence notes: all line numbers refer to the current `adapter.py` (3344 lines). Claims were verified by reading the code; the tests only reproduce the JSON-only upload and non-frame-based auth paths, which is consistent with the findings above.*