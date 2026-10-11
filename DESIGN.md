# XBM design

The web UI treats bookmarks as a personal cuttings library: every saved
post is a clipping you read, file, and cross-reference. It's a working
tool used at a desk from the keyboard, so the layout is the familiar
three-pane mail/reader pattern and the identity lives in a few details.

## Palette

Defined as tokens in `frontend/style.css` (`:root`, with a dark variant
under `prefers-color-scheme: dark`).

| Role | Token | Notes |
|---|---|---|
| Rail (manila folder) | `--rail`, `--rail-ink`, `--rail-ink-2` | The library shell: views and the tag index |
| Ground / reading surface | `--ground`, `--surface` | Cool newsprint, never warm cream |
| Ink | `--ink`, `--ink-2`, `--ink-3` | Blue-black text in three strengths |
| Blue pencil | `--pencil`, `--pencil-ink`, `--pencil-wash`, `--pencil-mark` | Selection, focus, unread dots, search hits, filter chips |
| Active / primary | `--rail-active`, `--btn-primary` | Ink fill on manila (light); manila fill on ink (dark) |
| State | `--danger`, `--ok` | Errors and sync results only |

Rules: blue pencil means "you are here or this matched", nothing
decorative. Red only for failures.

## Type

- UI: the system sans stack, one family, a tight 12/13/14/16/18px scale.
- Reading: tweet text, threads, summaries and transcripts are set in a
  system serif stack (Charter, Sitka, Cambria, Georgia) at 17px / 1.6,
  capped at 62ch.
- Keys: `kbd` uses the system monospace; monospace appears nowhere else.
- Counts and dates use tabular numerals.

## Signature details

- Search hits are underlined with a blue-pencil stroke (`mark`), not boxed.
- Each tag in the rail index carries a short stroke scaled to its share
  of the current results, so the index reads as a ledger of what's on screen.
- "See also" cross-references explain themselves ("Also filed under #x").

## Interaction

- Keyboard first: `j`/`k`, `e` file, `z` undo, `w` watched, `o` open,
  `/` search, `t` tag, `g`+letter views, `Ctrl/⌘ K` palette, `?` help.
- Motion is 160–240ms ease-out and only signals a state change (filing a
  row, opening the reader on mobile, dialogs). Reduced motion is honoured.
- Every list has a loading skeleton and an empty state that says what to
  do next.

## Responsive

- ≥1100px: rail, list and reader side by side.
- 761–1100px: the rail becomes a drawer behind the menu button.
- ≤760px: the list stands alone, the reader slides over it with a back
  button, and toolbar buttons drop to icons where needed.

## Safety

All bookmark content is untrusted. The frontend builds DOM with
`textContent`/`createElement` only, never `innerHTML`. Search highlights
come back wrapped in `\x01`/`\x02` control characters that the client
turns into `<mark>` nodes. Bookmark ids are sent as strings because X
snowflake ids exceed JavaScript's safe-integer range.
