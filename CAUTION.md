# ⚠️ CAUTION — Google Cloud / Drive API cleanup checklist

For this project we created Google Cloud API access to your personal Drive.
When you're done with the project (or want to lock things down), here is
**exactly** what was set up and how to undo each piece.

## What was set up

| # | Thing | Where it lives |
|---|-------|----------------|
| 1 | A Google Cloud project | console.cloud.google.com (project picker, top bar) |
| 2 | Google Drive API enabled in that project | APIs & Services → Enabled APIs |
| 3 | OAuth consent screen (app in "Testing" mode, your email as test user) | APIs & Services → OAuth consent screen |
| 4 | OAuth client ID, type "Desktop app" | APIs & Services → Credentials |
| 5 | `credentials.json` — the downloaded OAuth client secret | **repo root, on this machine** (git-ignored) |
| 6 | `data/token.json` — access/refresh token minted after you click Connect | this machine (git-ignored) |
| 7 | Account-level grant: the app appears in your Google account's third-party access list once you complete the consent flow | myaccount.google.com/permissions |

Scopes: the app requests **read-only** Drive access (`drive.readonly`) by
default; full `drive` scope only if you explicitly clicked "Enable write
access" in the dashboard.

## How to reset / remove everything

Do these in order — #1 is the one that actually kills live access:

1. **Revoke the account grant** (kills all issued tokens, including refresh
   tokens): go to <https://myaccount.google.com/permissions>, find the app
   name you chose on the consent screen, → **Remove access**.
2. **Delete local secrets on this machine**:
   ```bash
   rm -f credentials.json data/token.json
   ```
3. **Delete the OAuth client**: Cloud Console → APIs & Services →
   Credentials → trash-can icon next to the Desktop client.
4. **Full cleanup (recommended if the project was created only for this)**:
   Cloud Console → IAM & Admin → Settings → **Shut down** the project.
   This deletes the consent screen, the client, and the API enablement in
   one shot (project is purged after a ~30-day grace period).
   If you keep the project, at least disable the Drive API under
   APIs & Services → Enabled APIs → Google Drive API → Disable.

## Notes

- `credentials.json` and `data/token.json` are in `.gitignore` — **never
  commit or share them**. The client secret alone can't read your Drive
  (consent + token are needed), but treat it as a secret anyway.
- Nothing billable was set up: the Drive API has no cost at this usage and
  no billing account is required.
- While the consent screen is in "Testing" mode, refresh tokens expire after
  ~7 days — if Connect stops working later, that's why (just reconnect, or
  clean up per above if you're done).
- The catalog DB (`data/catalog.db`), thumbnails, and cached files contain
  personal metadata/imagery — `rm -rf data/` wipes all of it locally.
