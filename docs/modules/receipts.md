# Receipts & warranty

*Every bill, findable in seconds.* A receipt is something bought: what it was, where, when and
for how much, the bill's and the thing's numbers, and when its warranty ends. The app reminds
the user before that day. Beta since Phase 15 (ADR-0022).

## Using it

- **Add a receipt** (`/m/receipts/new`): what was bought, the shop, the day (today unless
  changed; never later), the amount, and the warranty: none, a length of one tap (6 months to 5
  years), or another end date. *Remind me this many days before it ends* starts at 30.
- **Scan the bill** on that form: a photo fills the form in (with AI, the photo is read by it;
  otherwise on the phone, as the expenses scan does). Whatever was read with doubt says *Read
  from the bill: check it* until it is changed. Nothing is kept until the user saves, and the
  photo never is.
- **The list** (`/m/receipts`): latest bought first; a search box (name, shop, numbers, note);
  and *Ending soon*, *Under warranty*, *Warranty over*, *No warranty*. Each receipt says where
  its warranty stands, in words and with an icon.
- **A reminder** comes before a warranty ends, in the notifications (and as a push where the
  user allowed it); it opens the receipt. The dashboard lists the warranties ending soon, the
  command palette finds receipts, and the Welcome tile counts them.
- **The assistant**: a bill it reads (`scan-bill`) opens a new receipt, filled in.

## API (`/v1/receipts`, the signed-in user's own)

| Route | What it does |
|---|---|
| `GET /options` | The form's defaults and limits (`data/warranty.yaml`) |
| `POST /items` | Keep a receipt (201) |
| `GET /items?warranty=&q=&sort=` | Latest bought first; `warranty` = `none`, `active`, `ending`, `ended`; `sort` = `bought_on`, `warranty_ends_on`, `text`, `created_at` (`-` before for descending) |
| `GET /items/{id}` | One receipt, with `warranty`: where it stands today |
| `PATCH /items/{id}` | Replace it: the form sends every field |
| `DELETE /items/{id}` | Delete it, and its reminder |

Refusals: `RECEIPT_DATE_INVALID` (bought after today), `RECEIPT_WARRANTY_INVALID` (a warranty
ending before the day bought, or after the longest taken), and the usual `VALIDATION_ERROR`.

## How it works

- **Warranty status** is worked out for the user's today: a warranty covers its last day, and is
  *ending* from its reminder day. Lists filter in the database through `remind_on` (stored: the
  end less the reminder days).
- **Reminders** use the platform's (`receipts.warranty_ends`, one per receipt): moved when the
  end or the days change, cancelled when the warranty is removed, ended or the receipt deleted.
- **Configuration** is data: `app/modules/receipts/data/warranty.yaml` (default and most days
  before, the longest warranty, the one-tap lengths, how many the dashboard lists).
- **Reading a bill** without AI: the platform reads the slip (`app/platform/slips`), and
  `bill.py` takes from it what a receipt needs, with a bill's own words in
  `skills/scan-bill/references/bill.yaml` (labels before a bill's or serial number, warranty
  words and units in en/ta/hi, how old a bill may be). The skill's SKILL.md tells AI the same.
- **Privacy**: receipts are the user's own (other users get 404); they are in the user's export
  and go with their account (ADR-0017). No photo of a bill is stored.
