# Privacy policy template

This is a starting point for the privacy policy of a deployment. It is not legal advice. Fill in
every `{{PLACEHOLDER}}` and delete what your deployment doesn't use. Then check the result against
the law where you and your users are (for example GDPR, or India's DPDP Act 2023) and against
Google Play's current policies.

The facts below match the code as shipped. If you change a retention setting, change the matching
number here too. The setting names are given in brackets.

---

## Privacy policy for {{APP_NAME}}

_Last updated: {{DATE}}_

{{APP_NAME}} ("the app") is run by {{OPERATOR_NAME}} ("we"). This policy explains what the app
keeps about you, why, for how long, and how to get it or erase it.

### What we keep

| What | Why | How long |
|---|---|---|
| Your email address and name | To sign you in and show your account | Until you delete your account |
| Your password, as an Argon2id hash (never the password itself) | To check it when you sign in | Until you delete your account |
| Your Google account ID, if you sign in with Google | To recognise you on your next Google sign-in | Until you delete your account |
| Your settings: language, theme, currency, time zone, the modules you turn on | To set the app up the way you chose | Until you delete your account |
| What you enter in the modules you use, for example {{MODULE_DATA_EXAMPLES}}, and the edit history of those records | To provide those features | Until you delete it, or delete your account |
| Your signed-in devices: browser or app name (user agent), IP address, and the times of sign-in and last use | To keep you signed in, and so you can review your devices and sign them out | Up to {{SESSION_MAX_DAYS}} days (`AUTH_REFRESH_ABSOLUTE_TTL_SECONDS`), plus {{SESSION_GRACE_DAYS}} days after a session ends (`AUTH_SESSION_PURGE_GRACE_DAYS`), then removed automatically |
| Two-step sign-in: your authenticator secret (encrypted), and your recovery codes (as one-way hashes only) | To check your codes when you sign in | Until you turn two-step sign-in off, or delete your account |
| A security record of important account events: sign-in changes, role changes, data exports and account deletion. Each entry has your account ID and IP address, but none of your content | To protect your account and investigate misuse | {{AUDIT_RETENTION_DAYS}} days (`AUDIT_RETENTION_DAYS`) |
| If registration is by invitation and an administrator invited you: your email address on the list of who may register | To let you register | Until you delete your account, or an administrator removes it. The security record that it was added or removed keeps the email address for {{AUDIT_RETENTION_DAYS}} days |
| Your recent in-app activity and notifications | To show your activity feed and your inbox | {{ACTIVITY_DAYS}} days (`DASHBOARD_ACTIVITY_RETENTION_DAYS`) and {{NOTIFICATION_DAYS}} days (`NOTIFICATIONS_RETENTION_DAYS`) |
| Assistant requests and answers, if you use the assistant | To answer you, and to enforce daily limits | {{AGENT_RUN_DAYS}} days (`AGENT_RUN_RETENTION_DAYS`) |
| A count of errors per day, by error type. These counts contain nothing about you | To find and fix problems | {{ERROR_TALLY_DAYS}} days (`ADMIN_ERROR_TALLY_DAYS`) |

We do not collect your location, contacts, photos, or advertising IDs. We do not sell your data,
we do not show ads, and we do not use your data to build advertising profiles.

### AI features (only with your consent)

The assistant and smart-capture features only use an AI service after you turn on AI in your
settings. You can turn it off at any time. When AI is on, two things are passed to
{{AI_PROVIDERS}}, which process them to produce an answer: the text you send to those features,
and the records the assistant looks up to answer you (for example, the expenses that match your
question). Before your text is sent, the app removes email addresses, phone numbers and similar
identifiers it can recognise (`AGENT_REDACT_PATTERNS`). When AI is off, or a provider is
unavailable, the app uses its own built-in rules and nothing leaves our server.

{{AI_PROVIDER_TERMS_NOTE}}: for example, "Our AI provider's free tier may use submitted text to
improve its services: see {{PROVIDER_TERMS_URL}}."

### Cookies

The app uses a single cookie. It is a sign-in cookie that keeps you signed in. It is httpOnly
(scripts can't read it), sent only to the sign-in endpoints, and only over HTTPS. There are no
analytics or advertising cookies.

### Where your data is kept, and who runs the servers

Your data is stored in {{DATABASE_PROVIDER_AND_REGION}}. The app runs on {{HOSTING_PROVIDERS}}.
These companies store and serve data on our behalf. They don't use it for their own purposes.
Data travels between your device and our servers over HTTPS (TLS).

### Your choices and rights

In the app, under **Security** (the link beside your name at the top), you can do the following:

- **See your data**: download everything the app keeps about you as one JSON file (Your data →
  "Export my data"). This includes your security record. Where an administrator acted on your
  account, it says what they did, but not who they are or where they acted from.
- **Correct it**: change your name, language, currency and time zone on your profile, and edit
  or delete any record.
- **Delete your account** (Your data → "Delete account"): this removes your account and everything
  in it, permanently. You'll be asked to type your email address and your password, or to sign in
  with Google again, plus a code if two-step sign-in is on. The security record of the deletion is kept for the period
  above, with your account ID but no content, and is then removed automatically.
- **Protect it**: turn on two-step sign-in, and review or sign out your devices.

If you can't sign in, email {{CONTACT_EMAIL}} to ask for an export or a deletion. We may ask you
to prove the account is yours. We answer within {{RESPONSE_DAYS}} days.

### Security

- Passwords are hashed with Argon2id.
- Repeated failed sign-ins lock the account for a growing period.
- Authenticator secrets are encrypted at rest.
- Recovery codes and sign-in tokens are kept only as one-way hashes.
- Administrators can see account details such as email, role and status. They can't see your
  password or your authenticator secret, and every administrative change is recorded.

### Children

The app is not meant for children under {{MINIMUM_AGE}}, and we don't knowingly keep data about
them.

### Changes to this policy

We'll post any change here and update the date above. If a change is significant, we'll also
tell you in the app.

### Contact

{{OPERATOR_NAME}}, {{CONTACT_EMAIL}}, {{POSTAL_ADDRESS_IF_REQUIRED}}

---

## Google Play Data safety form: how this deployment answers it

Use this as a checklist when you fill in Play Console → App content → Data safety. Answer for
your own deployment: these answers assume the shipped defaults.

**Overview questions:**

| Question | Answer |
|---|---|
| Does your app collect or share any of the required user data types? | Yes |
| Is all of the user data collected by your app encrypted in transit? | Yes (HTTPS only) |
| Do you provide a way for users to request that their data is deleted? | Yes: in the app (Security → Your data → Delete account). Play also asks for a **web link** where people can request deletion without the app: give {{DELETION_REQUEST_URL}} (a page that explains the in-app steps and the {{CONTACT_EMAIL}} route). |

**Data types:**

| Play data type | Collected? | Shared? | Required or optional | Purposes |
|---|---|---|---|---|
| Personal info → Name | Yes | No | Required | Account management, App functionality |
| Personal info → Email address | Yes | No | Required | Account management, App functionality |
| Personal info → User IDs | Yes (the account ID, and the Google ID with Google sign-in) | No | Required | Account management |
| Financial info → Other financial info (if the Expenses module is on) | Yes (amounts, merchants and notes you enter) | No | Optional | App functionality |
| Health and fitness → Health info (only if you turn on a health module such as Medicine) | Yes | No | Optional | App functionality |
| App activity → Other user-generated content (notes, journal and similar modules) | Yes | No | Optional | App functionality |
| App activity → App interactions (activity feed) | Yes | No | Optional | App functionality |
| App info and performance → Diagnostics | No (only anonymous daily error counts, not linked to anyone) | No | — | — |
| Location, Contacts, Photos and videos, Audio, Files and docs, Calendar, Web browsing, Device or other IDs | No | No | — | — |

**Notes on these answers:**

- **Shared:** Play does not count transfers to service providers that process data on your
  behalf (hosting, database, and AI providers under their API terms) as "sharing". If your AI
  provider's terms let it use the submitted data for its own purposes, for example to improve its
  models, answer **Shared: Yes** for the data types the AI features receive, and give the purpose.
- **IP address:** it is kept for security on the session and audit records, and is not used to
  work out anyone's location. Check Play's current guidance on whether your use needs declaring
  under Location → Approximate location.
- **Ephemeral processing:** don't mark data as processed ephemerally. Records are stored until the
  user deletes them.
- Re-check this table whenever a module ships that collects a new kind of data.
