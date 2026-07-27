# Design Gallery — Pin-Comment Server

A zero-dependency Node.js server that hosts the PolySimulator design prototype gallery and injects a Figma-style pin-comment overlay into every HTML page.

## How it works

1. The server serves static files from `GALLERY_DIR` (the `docs/design/` HTML prototypes).
2. Every HTML response has two tags injected before `</body>`:
   - `<link rel="stylesheet" href="/__c/overlay.css">`
   - `<script src="/__c/overlay.js" defer></script>`
3. The overlay adds a small **Comment** toolbar (bottom-right). Clicking it enters comment mode; clicking anywhere on the page drops a numbered green pin and opens a composer. Pins are persisted server-side and re-rendered on every page load.

## Env vars

| Var | Default | Description |
|-----|---------|-------------|
| `GALLERY_DIR` | `./public` | Path to the directory of static gallery files |
| `DB_PATH` | `/data/comments.json` | Path to the JSON persistence file. Parent dir is created if missing. |
| `PORT` | `8080` | HTTP port to listen on |
| `OWNER_KEY` | _(empty)_ | Secret key that identifies the operator. When set, comments submitted with this key are stamped `role: "owner"`; all others receive `role: "guest"`. When empty, all comments receive `role: "guest"` and owner-marking is disabled. **Never commit this value** — set it as a Dokploy / container env var. |

## Local run

```sh
cd tools/design-gallery
GALLERY_DIR=../../docs/design DB_PATH=./comments.json PORT=8080 node server.js
# → http://localhost:8080

# With owner-key enabled:
OWNER_KEY=my-secret GALLERY_DIR=../../docs/design DB_PATH=./comments.json PORT=8080 node server.js
```

## Docker (build from repo root)

```sh
docker build -f tools/design-gallery/Dockerfile -t polysim-gallery .
docker run -p 8080:8080 -v /path/to/data:/data polysim-gallery
```

> The `/data` volume is required in production so comments survive container restarts.

## API

All endpoints live under `/__c/api/`.

### `GET /__c/api/threads?page=<pathname>`

Returns all threads for the given page path. Every comment includes a `role` field.

```json
{
  "threads": [
    {
      "id": "uuid",
      "page": "/dashboard-redesign.html",
      "xPct": 0.42,
      "yPct": 0.17,
      "resolved": false,
      "createdAt": "2026-06-20T10:00:00.000Z",
      "author": "Alice",
      "comments": [
        { "author": "Alice", "text": "Nav spacing feels tight", "createdAt": "…", "role": "owner" }
      ]
    }
  ]
}
```

`role` is `"owner"` when the comment was submitted with the correct `OWNER_KEY`, otherwise `"guest"`.

### `POST /__c/api/threads`

Create a new thread (pin + first comment). Optionally include `ownerKey` to mark the comment as owner-authoritative.

```json
{ "page": "/dashboard-redesign.html", "xPct": 0.42, "yPct": 0.17, "author": "Alice", "text": "Nav spacing feels tight", "ownerKey": "…" }
```

The `ownerKey` field (or `X-Owner-Key` request header) is compared server-side to `OWNER_KEY`. The server always derives `role` itself — a client-sent `role` field is ignored. Returns the created thread (HTTP 201).

### `POST /__c/api/threads/:id/reply`

Append a comment to an existing thread. Same `ownerKey` field applies.

```json
{ "author": "Bob", "text": "Agreed — needs 8px more", "ownerKey": "…" }
```

Returns the updated thread (HTTP 200).

### `POST /__c/api/threads/:id/resolve`

Set resolved status.

```json
{ "resolved": true }
```

Returns the updated thread (HTTP 200).

### `DELETE /__c/api/threads/:id`

Delete a thread entirely.

Returns `{ "deleted": "<id>" }` (HTTP 200).

## Storage

All comments live in a single JSON file (`DB_PATH`). Writes are atomic (write to `.tmp` then rename) and serialised so concurrent posts never corrupt the file. The in-memory store is rebuilt from disk on server startup.

## Overlay behaviour

- **Toolbar** (fixed, bottom-right): "Comment" toggle button + open-thread count badge + Settings gear.
- **Comment mode ON**: cursor becomes crosshair; clicking anywhere on the page drops a pin at that position and opens a composer popover (author name auto-saved to `localStorage` key `ds_comment_author` + comment textarea).
- **Pins**: numbered green filled circles. Resolved pins render as muted outlines.
- **Thread popover**: shows all comments with author + relative timestamp + owner badge (when `role === "owner"`) + a reply box + Resolve/Reopen + Delete controls.
- **Esc**: closes any open popover, or exits comment mode.
- **Non-obtrusive**: when comment mode is OFF the toolbar is the only addition and pointers pass through the pin layer.

### Settings panel

Click the gear icon in the toolbar to open the Settings panel. It lets you set:

- **Display name** — saved to `localStorage` key `ds_comment_author`; pre-filled in every new comment.
- **Owner key** — saved to `localStorage` key `ds_owner_key`; sent as `ownerKey` in the JSON body of every create and reply request. Helper text: "Owner key marks your comments as authoritative (the operator's key)."

The operator sets their `OWNER_KEY` once in Settings and never needs to re-enter it. Guests leave the Owner key field blank.

### Owner convention

Comments with `role: "owner"` come from the operator and are **authoritative / auto-actionable** — Claude can act on them automatically without further approval. Comments with `role: "guest"` are **suggestions only** and require operator sign-off before Claude acts. The `role` field is derived server-side from `OWNER_KEY` and cannot be spoofed by typing a name or injecting JSON.
