---
name: exchange-assistant
description: >
  Generic example of composing the ews-mcp 4.5 tool surface into a daily
  mailbox routine: morning overview, triage, and a reply drafted through
  the two-phase confirm flow. Use as a template — real assistants add
  their own judgment, tone and priorities on top of these calls.
---

# Exchange assistant (example skill)

This skill demonstrates the intended division of labor: the MCP server is
a fast, safe DATA PLANE; everything that needs judgment (what matters,
what to say, when to escalate) lives here, in the calling assistant.

## Morning overview

1. Call `get_mailbox_overview` once. It returns `unread_total`, the ten
   most recent unread message cards and today's calendar in a single
   compact payload (`source: "cache"` when the mirror is warm).
2. Summarize for the user in their language. Lead with meetings that
   start soon, then unread mail grouped by sender. Card ids (`m12`,
   `e3`) are stable handles — keep them for follow-up calls.
3. If the user tracks follow-ups, add `waiting_on` (sent threads with no
   reply) and `list_tasks` to the brief.

## Triage

- To act on a specific message: `get_message` with the card id
  (`format: "full"` returns the cleaned body — quoted history is already
  stripped; use `get_thread` when context from the conversation is
  needed).
- Searching: `search_messages` with either structured filters
  (`sender`, `subject`, `since: "+0d"`-style dates) or a free-text
  `query`. Arabic queries match across spelling variants automatically.
- Bulk hygiene: `update_messages` (mark read, categorize) and
  `move_messages` accept up to 50 ids per call.

## Replying (draft-first, always)

1. Compose the reply text yourself — tone and content are your job.
2. `create_draft {mode: "reply", reply_to: "m12", body: "..."}` — the
   server saves a draft and returns `draft_id` plus a preview. Nothing
   has been sent.
3. Show the preview to the user. Only after they approve:
4. `send_draft {draft_id: "d1"}` — phase 1 returns the draft's REAL
   recipients/subject/body snippet and a `confirm_token`.
5. Re-check that preview (it is the server's ground truth, not your
   memory), then `send_draft {draft_id: "d1", confirm_token: "..."}`.
   If anyone edited the draft in between, the token is rejected and you
   must re-preview — that is the tamper protection working.

## Scheduling

- `list_events {start: "today", end: "+7d"}` for the week;
  `check_availability {attendees: [...], start, end}` proposes concrete
  free slots for everyone.
- `create_event` with `send_invitations: false` (the default) saves a
  placeholder without emailing anyone; flipping it to `true` triggers
  the same two-phase confirm as sending mail.

## Ground rules baked into the server (do not fight them)

- Sends are refused entirely unless the operator enabled them
  (`kill_switch` errors are expected on read-only deployments).
- `tier_blocked` means this deployment intentionally does not allow that
  class of action — tell the user, do not retry.
- A `not_found` with a re-search hint means the id went stale (items
  move); re-run the search and use the fresh id.
- Every error carries a `hint` written for you — read it before retrying.
