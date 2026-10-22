# Sharing a public demo

This guide puts Knowledge Copilot on a public https link, so anyone (a recruiter, an interviewer)
can try it from their browser. It runs on your own machine and goes out through a
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/),
which is free, needs no open ports on your router and no cloud server.

The link works only while your machine is on and the copilot is running. If you want a link that
works around the clock, the same settings work on any cloud VM; only the tunnel step changes.

- [1. What public demo mode does](#1-what-public-demo-mode-does)
- [2. Settings](#2-settings)
- [3. Start the copilot](#3-start-the-copilot)
- [4. Open the tunnel](#4-open-the-tunnel)
- [5. Before you share the link](#5-before-you-share-the-link)
- [6. Turning the demo off](#6-turning-the-demo-off)
- [7. Troubleshooting](#7-troubleshooting)

## 1. What public demo mode does

With `COPILOT_PUBLIC_DEMO=true`:

- **Shared demo logins.** The sign-in page shows one button per demo account: alice (employee),
  bob (manager) and carol (admin). Visitors click one and are signed in, so they can compare what
  each role is allowed to see. No password change is asked for.
- **Read-only.** The server allows only three kinds of change: signing in, switching workspace
  and asking a question. Everything else is refused with a 403, for every account including
  carol: uploading or deleting documents, ingest, model downloads, evaluations, password changes
  and every admin change to users and tenants. Pages that only read (the KPIs, the Admin tabs, the
  document list) still work, so visitors can look around.
- **Demo accounts are reset at every start.** alice, bob and carol get back their role and
  `COPILOT_DEMO_PASSWORD`, and are created again if they were deleted. Other accounts you made
  are not touched.
- **Limits on questions.** Each visitor (by IP address) may ask
  `COPILOT_DEMO_ASKS_PER_MINUTE` questions a minute and `COPILOT_DEMO_ASKS_PER_DAY` a day.
  Only `COPILOT_DEMO_MAX_CONCURRENT` questions are answered at once; the rest wait up to
  `COPILOT_DEMO_QUEUE_WAIT_S` seconds and then get "the demo is busy". A visitor over a limit
  gets a 429 with the time to wait.
- **Failed logins lock out the visitor, not the account**, so nobody can lock alice out for
  everyone by guessing wrong five times.
- **The copilot won't start with unsafe settings**: `COPILOT_DEV_TOKEN_ENDPOINT=true`, or the
  default `COPILOT_JWT_SECRET`.

Answers in the demo are as private as anywhere else in the copilot: the KPI records never store the
question text (see [monitoring.md](monitoring.md)).

## 2. Settings

Add these to `.env` (`python scripts/manage.py setup` creates it):

```ini
# Public demo
COPILOT_PUBLIC_DEMO=true
COPILOT_DEMO_PASSWORD=TryTheDemo2026     # shown on the sign-in page, so make it nothing you use elsewhere
COPILOT_JWT_SECRET=<a long random string> # python -c "import secrets; print(secrets.token_urlsafe(48))"
COPILOT_DEV_TOKEN_ENDPOINT=false

# Only the tunnel should reach the copilot, so the visitor IP header can't be faked
COPILOT_HOST=127.0.0.1
COPILOT_CLIENT_IP_HEADER=CF-Connecting-IP

# Cloudflare gives up on a request after 100 seconds, so keep an answer below that
COPILOT_LLM_TIMEOUT_S=90
COPILOT_LLM_MAX_TOKENS=384

# Optional: the limits (these are the defaults)
# COPILOT_DEMO_ASKS_PER_MINUTE=4
# COPILOT_DEMO_ASKS_PER_DAY=40
# COPILOT_DEMO_MAX_CONCURRENT=1
# COPILOT_DEMO_QUEUE_WAIT_S=30
```

`COPILOT_CLIENT_IP_HEADER` matters: without it every visitor arrives from the tunnel's address and
shares one set of limits. Set it only together with `COPILOT_HOST=127.0.0.1`. If the copilot is
also reachable directly, anyone could send that header and pick their own IP.

## 3. Start the copilot

Do the admin work first, while demo mode is still off, because the demo refuses changes: download
the model, upload the documents you want to show and run ingest (see [admin-console.md](admin-console.md)).
Then set the settings above and start it again:

```bash
python scripts/manage.py up --gpu     # or without --gpu
python scripts/manage.py status
```

Open http://127.0.0.1:8000 yourself: the sign-in page should show the "Public demo" box with the
three account buttons. Ask one question as alice. The first answer loads the model and is the slow
one, so it's better that it's yours and not a visitor's.

Use the GPU if you can. On the CPU a 7B model takes around 25 seconds per answer, which is about
the limit of a visitor's patience. Check with `ollama ps` (native) or
`docker compose exec ollama ollama ps` (Docker): the PROCESSOR column should say `100% GPU`.

Running natively (`copilot serve`, no Docker) works the same way with the same `.env`.

## 4. Open the tunnel

Install `cloudflared`:

- Windows: `winget install --id Cloudflare.cloudflared`
- Linux (Debian/Ubuntu): download the `.deb` from
  https://github.com/cloudflare/cloudflared/releases and `sudo dpkg -i cloudflared-linux-amd64.deb`
- macOS: `brew install cloudflared`

### Quick tunnel: no account, a new link every time

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

It prints a link like `https://random-words-here.trycloudflare.com`. Share that link. It works
until you stop the command (Ctrl+C), and the next run gives a different one. Quick tunnels are
meant for trying things out, and Cloudflare doesn't promise they stay up, which is fine for a
demo you start before an interview.

### Named tunnel: the same link every time

For a link you can put on your CV, you need a free Cloudflare account and a domain whose DNS is on
Cloudflare (a cheap domain costs about $10 a year).

```bash
cloudflared tunnel login                                 # opens the browser, pick your domain
cloudflared tunnel create copilot
cloudflared tunnel route dns copilot copilot.example.com
cloudflared tunnel run --url http://127.0.0.1:8000 copilot
```

The link is now `https://copilot.example.com` whenever the last command is running. You can also
add [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/) in front
to let in only email addresses you choose.

## 5. Before you share the link

- Open the link on your phone (off your Wi-Fi), sign in as each account and ask one question.
- Check that a change is refused: as carol, open **Admin > Users** and try to delete a user. You
  should see "This is a read-only public demo".
- Set your machine not to sleep while plugged in (Windows: Settings > System > Power), or the
  link stops working.
- Remember that while the tunnel runs, your machine is serving the internet. Stop the tunnel when
  you don't need it.
- Many visitors won't wait for an answer. A one or two minute screen recording in the README,
  next to the link, shows the role differences even when the demo is offline.

## 6. Turning the demo off

Stop the tunnel (Ctrl+C), set `COPILOT_PUBLIC_DEMO=false` in `.env`, and run
`python scripts/manage.py up` again. Everything works as before. The demo accounts keep
`COPILOT_DEMO_PASSWORD` and no longer have to change it, so if you use them yourself outside the
demo, give them a new password from **Admin > Users**.

## 7. Troubleshooting

| What you see | What to do |
|---|---|
| Cloudflare error 524 | The answer took longer than 100 seconds. Lower `COPILOT_LLM_TIMEOUT_S` and `COPILOT_LLM_MAX_TOKENS`, and check that the model runs on the GPU. |
| Cloudflare error 502 / "Bad gateway" | The copilot isn't running on 127.0.0.1:8000. Run `python scripts/manage.py status`. |
| "The demo is busy answering other visitors" | Several people asked at once. Raise `COPILOT_DEMO_QUEUE_WAIT_S` (keep it plus the answer time under 100 seconds), or `COPILOT_DEMO_MAX_CONCURRENT` if you have a GPU. |
| Every visitor hits the limit together | `COPILOT_CLIENT_IP_HEADER=CF-Connecting-IP` is missing. |
| The copilot won't start: "COPILOT_PUBLIC_DEMO needs ..." | Set the setting the message names (section 2). |
| No "Public demo" box on the sign-in page | `COPILOT_PUBLIC_DEMO` isn't `true` in `.env`, or the copilot wasn't restarted with `manage.py up`. Reload the page. |
