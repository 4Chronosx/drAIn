# Runbook: moving the hosted Supabase project to asymmetric signing keys

For the project owner. Moves user access tokens from the legacy shared secret (HS256) to an asymmetric key (ES256). Based on the Supabase docs as fetched 2026-10-10 and on this backend's code (`app/auth.py`, `app/runs.py`). Nothing here has been run against the hosted project; see "What was not verified".

Stages 1 to 4 need no change to any API key. Stage 5 (revoking the legacy secret) is optional and is the only one that needs code and key changes.

Placeholders: `<ref>` is the Supabase project ref, `<backend>` the Render hostname.

## Time needed

About 2 hours elapsed, almost all waiting: 20 minutes before rotating, then the access-token lifetime (1 hour by default) plus 15 minutes before stage 4. Most dashboard actions on a signing key are throttled for about 5 minutes, which also slows any rollback.

## The two checks used after every stage

**Check A: the app works.** Sign out of the site, sign in again, run one simulation and wait for its result. Then open Supabase > Table Editor > `simulation_runs` and confirm a new row for it.

**Check B: the published keys.**

```sh
curl -s https://<ref>.supabase.co/auth/v1/.well-known/jwks.json
```

The docs show this endpoint called with no headers. Expected output is given per stage. The response is cached at Supabase's edge for 10 minutes, so a change can take that long to show.

**Check C (stages 3 and 4): which key signed your token.** In the browser's network tab, open the simulation request, copy the `Authorization: Bearer` value, take the part before the first dot and decode it:

```sh
echo '<first part>' | base64 -d
```

`"alg":"HS256"` is the legacy secret; `"alg":"ES256"` with a `kid` is the new key.

## Stage 0: prerequisites

1. This branch (`asymmetric-keys`) and backend PR #5 are merged to `main` and Render shows the deploy as live.
2. `curl -s https://<backend>/health` returns `{"status":"ok"}` with HTTP 200.
3. In Render > Environment, `REFUSE_HS256_TOKENS` is **unset** (or `false`). If it is `true` during stages 1 to 3, this backend refuses every signed-in user from the moment the new key is listed.
4. In Supabase > Edge Functions > `geocodeWorker`, confirm "Verify JWT" is off (it was deployed with `--no-verify-jwt`). The docs warn that functions with it on may break at rotation, and contradict themselves on whether they do.
5. Note the access-token lifetime: the JWT expiry in the project's Auth settings (default 3600 seconds). Stage 4 waits this long.
6. Run check A and check B. Check B should show `{"keys":[]}`: a project on the legacy secret publishes nothing.

## Stage 1: migrate the legacy secret and get a standby key

The docs describe one button that does both: it imports the legacy secret into the signing-keys system and creates an asymmetric standby key. There is no separate "create key" step unless the standby key is missing afterwards.

1. Supabase dashboard > the JWT signing keys page (under Project Settings > JWT Keys at the time of writing; the menu name may differ).
2. Click **Migrate JWT secret**.
3. The page should now list the legacy HS256 secret as the key **in use** and a new key as **standby**. If there is no standby key, create one on the same page and choose ES256 (the docs recommend it over RS256).

Nothing signs with the new key yet. The docs say you can stop here indefinitely.

**Check after stage 1**
- Check B: one key, with a `kid`, `"kty":"EC"` and `"alg":"ES256"`. The legacy secret is never listed. The docs do not state which algorithm the dashboard picks by default (unconfirmed); RS256 (`"kty":"RSA"`) also works with this backend. Any other algorithm does not: stop and create an ES256 key instead, and restart the stage 2 wait from when that key was created.
- Check A passes. Check C still shows HS256.
- Render logs, some minutes after a signed-in request (up to about 20, given the caches): one warning starting `The project publishes asymmetric signing keys, and tokens signed with the shared secret (HS256) are still accepted`. It is logged once per process and is expected.

## Stage 2: wait at least 20 minutes

The docs recommend waiting at least 20 minutes after a standby key is created, so every cache of the key list has it. While this backend holds no published keys, a token signed with a key it has not seen is sent to Supabase instead of being refused. Once it holds any key, a token naming a different one is refused here until the new key is fetched, and that fetch can be served the old list from Supabase's edge cache for up to 10 minutes. So the wait is required whenever a standby key is created or replaced after the first one, and on any later rotation between asymmetric keys. Count the 20 minutes from the creation of the key you are about to rotate in. Do not rotate earlier.

## Stage 3: rotate

1. On the JWT signing keys page, click **Rotate keys** and confirm.
2. The ES256 key should now be **in use** and the legacy secret under **Previously used**. Leave the legacy secret there; do not revoke it.

New and refreshed tokens are now ES256. Tokens already issued stay valid until they expire, so nobody is signed out. The legacy `anon` and `service_role` API keys keep working, because the legacy secret is still trusted.

**Check after stage 3**
- Check B: unchanged from stage 1 (the same key is listed).
- Check A passes after a fresh sign-in, and check C on that session shows `"alg":"ES256"` with the `kid` from check B.
- Render logs show no `Fetching the project's signing keys answered HTTP ...` warnings.
- `geocodeWorker` still works: trigger whatever normally calls it and check its logs for a 200.

## Stage 4: set `REFUSE_HS256_TOKENS=true` on Render

**Wait first:** the access-token lifetime from stage 0, counted from the rotation, plus 15 minutes (1 h 15 min on the default). By then every HS256 token Supabase issued has expired. Setting it sooner signs out of simulations anyone still holding one, until their browser refreshes its token.

1. Render > the backend service > Environment > add `REFUSE_HS256_TOKENS` = `true`. Save; Render redeploys. A misspelt value (`ture`) stops the server starting, by design: fix it and redeploy.
2. Wait for the deploy to go live and `/health` to return 200.

**Check after stage 4**
- Sign out, sign in, run a simulation (check A). It works, and check C shows ES256.
- The `still accepted` warning from stage 1 no longer appears in the new process's logs.

From here a forged HS256 token is refused by this backend without a call to Supabase. The migration can end here.

## Stage 5 (optional): revoke the legacy secret

Not needed for anything above. Its only gain is that Supabase itself stops trusting HS256 tokens. Supabase plans to retire the legacy `anon` and `service_role` keys by the end of 2026, so this work comes due eventually, but it is a separate change with code in it.

> **WARNING: revoking the legacy secret retires the legacy API keys.**
> The docs are explicit: `anon` and `service_role` are JWTs signed by the legacy secret, and "before you revoke the legacy JWT secret, you must disable the `anon` and `service_role`" keys. Every place below must be moved to a new `sb_publishable_...` / `sb_secret_...` key first (create them under Project Settings > API Keys; both key systems work side by side until you disable the old one).
>
> | Where | Value to replace | With | Notes |
> | --- | --- | --- | --- |
> | Render env | `SUPABASE_ANON_KEY` | publishable key | Sent only as the `apikey` header (`app/auth.py`), the documented form. No code change. |
> | Render env | `SUPABASE_SERVICE_ROLE_KEY` | secret key | **Needs a code change first.** `app/runs.py` sends it as `apikey` *and* `Authorization: Bearer`. The docs say a secret key must not go on `Authorization`; whether it is tolerated there is unconfirmed. |
> | Vercel env | `NEXT_PUBLIC_SUPABASE_ANON_KEY` | publishable key | Built into the bundle: redeploy after changing it. |
> | Edge function `geocodeWorker` | auto-injected `SUPABASE_SERVICE_ROLE_KEY` | `JSON.parse(Deno.env.get('SUPABASE_SECRET_KEYS')!)['default']` | Code change and redeploy. Whether the legacy variable is removed or left holding a dead key is unconfirmed; assume it stops working. Confirm `SUPABASE_SECRET_KEYS` exists under Edge Functions > Secrets first. Also check what its caller sends as `Authorization`. |
> | Frontend repo, `.env.hosted.local` | `SUPABASE_SERVICE_ROLE_KEY` (used by `scripts/scrub-storage-photos.mjs`) | secret key | supabase-js sends the key on both headers by default; same unconfirmed point as `app/runs.py`. |
> | This repo, your shell | `SUPABASE_KEY` for `scripts/validate_against_reports.py` | secret key | The script sends it on both headers; same point. |
> | Any other local `.env` pointing at the hosted project | anon or service-role key | matching new key | |
>
> **How a missed one looks.** A dead `SUPABASE_ANON_KEY` on Render does not produce an obvious error: this backend reads Supabase's 401 as "this token is nobody", so every user gets `401` on simulations, as if their sign-in were bad. The logs may also show `Fetching the project's signing keys answered HTTP 401`. A dead service-role key shows as `Could not count recent runs` warnings and no new rows in `simulation_runs`.

Order, if you go ahead:

1. Make and deploy the code changes (`app/runs.py`, `geocodeWorker`), then replace every value in the table. Run check A after each one, while the legacy keys still work.
2. Project Settings > API Keys: **disable** the legacy `anon` and `service_role` keys. This is reversible. Run check A, trigger `geocodeWorker`, and leave it a day or so in case something was missed. The docs disagree on whether the page shows a "last used" indicator; do not rely on one.
3. JWT signing keys page: **Revoke** the legacy secret under Previously used. Wait at least 20 minutes, then run checks A and B. Check B is unchanged.
4. Never click **Delete** on a key you may want back. Delete is the one irreversible action. The legacy secret itself cannot be deleted.

## Rollback

Every key action except Delete is reversible, each subject to the 5-minute throttle. The docs say none of these steps causes downtime or signs users out.

| After | To back out |
| --- | --- |
| Stage 1 | Nothing to undo. The standby key signs nothing and can stay. Do not delete it unless you are abandoning the move. |
| Stage 3 | **First** make sure `REFUSE_HS256_TOKENS` is unset on Render and the deploy is live; otherwise the next step locks everyone out of simulations, because the ES256 key stays listed. Then on the JWT signing keys page move the legacy secret from Previously used to **standby**, wait out the throttle, and click **Rotate keys**. New tokens are HS256 again; ES256 tokens already issued stay valid until they expire. |
| Stage 4 | Render > Environment: delete `REFUSE_HS256_TOKENS` (or set `false`) and let it redeploy. Takes effect when the deploy is live. |
| Stage 5, legacy keys disabled | Project Settings > API Keys: re-enable the legacy keys. Old env values work again at once. |
| Stage 5, legacy secret revoked | Move the legacy secret from Revoked to **standby** (the docs say a revoked key in standby is trusted again), re-enable the legacy API keys, and put back any env values you changed. To sign with it again, follow with a rotation as for stage 3. |

If users report `401` on simulations at any stage: check `REFUSE_HS256_TOKENS` first, then the Render logs for `Fetching the project's signing keys answered HTTP`, then whether `SUPABASE_ANON_KEY` is still an enabled key.

## What was not verified

- None of this was run against the hosted project. Button names and menu paths come from the docs and may have moved.
- That the hosted JWKS endpoint accepts a request carrying an `apikey` header (this backend sends its anon key there). The docs show the endpoint called without one and do not say.
- Which algorithm the dashboard gives the auto-created standby key. ES256 is the documented recommendation, not a stated default.
- Whether `Authorization: Bearer <sb_secret_...>` is accepted by the Data API when it matches `apikey`. The docs say not to send it; `app/runs.py`, `scripts/validate_against_reports.py` and supabase-js all do today.
- Whether the edge runtime's `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_ANON_KEY` are removed or left with dead values once the legacy keys are disabled.
- What status Supabase returns for a disabled legacy `apikey`. The "looks like bad tokens" hazard in stage 5 assumes 401.
- Whether Edge Functions with "Verify JWT" on accept ES256 tokens: the docs say both yes and no. It does not affect `geocodeWorker` while Verify JWT stays off.
- Whether the secret key's browser block (matched on `User-Agent`) could ever catch this backend's `Python-urllib` requests. Not expected, not tested.
- `geocodeWorker` and `scrub-storage-photos.mjs` live in the frontend repo and were not read for this runbook; their rows in the stage 5 table come from the deployment notes.
