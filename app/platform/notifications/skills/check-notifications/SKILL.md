---
name: check-notifications
description: Tell the user what is new in their notifications, open the notifications page, or mark everything as read. Use when they ask what's new, or about notifications, alerts or updates.
allowed-tools: notifications_list notifications_mark_all_read notifications_open
---

# Checking notifications

1. Call `notifications_list` to see what is unread. Each item has a `type` code and `params`:
   - `module.launched` with `params.module`: that module is now available to use.
   - For any other type, describe it plainly from its type and params; never invent details.
2. Summarise what is new in one or two sentences, newest first. If nothing is unread, say so.
3. If they want to see the list, call `notifications_open`.
4. If they ask to mark things as read or clear them, call `notifications_mark_all_read`. It is
   a proposal the user confirms; give it a short `summary` in their language.
