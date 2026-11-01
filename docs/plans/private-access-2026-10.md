# Private access to the library from anywhere: wiki.mattinees.com behind Cloudflare Access (2026-10-30)

The owner wants to reach the library from outside the house, privately: only the owner gets in, and
Cloudflare Access checks who you are before any request reaches this computer (owner, 2026-10-30).
This plan can be carried out at any time. Everything it needs was checked on 2026-10-30 against this
computer, the code at `6b24c36`, and Cloudflare's current documentation. Nothing in it has been done yet.

**Hostname.** `wiki.mattinees.com`, in the zone this account already serves, next to
`jellyfin.mattinees.com` and `immich.mattinees.com` (confirmed by the owner, 2026-10-30).

## What the result looks like

- Opening `https://wiki.mattinees.com` on any device shows Cloudflare's sign-in page. After a one-time
  code sent to the owner's email, the Navigator opens as it does at home. Nobody else gets a page from
  this computer, a crawler included.
- Search, documents, the Atlas, weather, the reader and translation all work. Actions that change the
  library stay home-only, as they are today: starting a weather pull (the Navigator says "a refresh can
  be started from this computer only"), gap reports (dropped silently, as designed) and the memory trim.
- The library at home is unchanged: `http://127.0.0.1:8737/`, the health check, the scrub and parity
  yield all work as before.
- Halting the site (`tools/wsp-shutdown`) also takes the remote address offline.

## Design, and why

```
phone ─https─▶ Cloudflare edge ──▶ Access: owner's email? ──▶ tunnel "whisipedia"
                                                                   │ (outbound from this computer)
                     WSL:  cloudflared (whisipedia-tunnel.service) │ checks the Access token
                                   │ http://127.0.0.1:8738         ◀┘
                     WSL:  wsp serve: 127.0.0.1:8737 (home, as today)
                                       127.0.0.1:8738 (remote: own rules)
```

### A small connector inside WSL, not the existing tunnel container

The owner first asked about adding the library to the existing `cloudflared` container
(`~/docker/cloudflared`). Tested on 2026-10-30 with a throwaway listener and a container on the
`proxy` network:

| From a `proxy` container to WSL | Result |
|---|---|
| `host.docker.internal` (it resolves to IPv6 `fdc4:…::254`) | fails, on loopback and on all addresses |
| `gateway.docker.internal`, `172.18.0.1` | fails |
| WSL's `eth0` address (`172.21.54.252` that day), listener on `0.0.0.0` | works; caller seen as `172.21.48.1` |
| WSL's `eth0` address, listener on `127.0.0.1` | fails |

So the container could reach the library only if the API listened beyond loopback, on an address WSL
hands out anew at every start (NAT mode). That needs a rewritten route after each restart, plus a
systemd IP filter widened past `localhost`. Nor can a second copy of the existing tunnel run in WSL:
Cloudflare spreads one tunnel's requests over all its connectors, so a WSL copy would get Jellyfin and
Immich requests it cannot reach, and those services would fail at random.

The plan therefore uses a **second, separate tunnel** ("whisipedia"), whose `cloudflared` runs in WSL
as a user service and talks to the API on loopback only. The API's offline guarantee (PLAN §10.5:
"the API makes no outbound connections") is kept. `cloudflared` is a separate process, like the one
serving Jellyfin, and the API's own unit keeps `IPAddressAllow=localhost`. Cost: one more small
service (~30 MB) and one more token. The Docker setup stays untouched. The hosting plan that put
everything in Docker remains on hold (2026-10-12).

### A second listener: "remote" is decided by the port, never the address

Today `wsp_api.browse.is_loopback()` decides that a caller is "this computer" from its peer address.
Through a tunnel on the same machine, every visitor arrives from `127.0.0.1`, so the check would let
the internet start weather pulls. `wsp serve` gets a second loopback socket, `127.0.0.1:8738`. A
request that arrived on it is remote whatever its address. uvicorn records the accepting socket in
`scope["server"]` per connection (`uvicorn.protocols.utils.get_local_addr`, checked in uvicorn 0.54),
and `uvicorn.Server.run(sockets=[...])` serves several sockets from one process. `proxy_headers` stays
off (it is today), so no header can make a caller look local.

### Five layers, each enough to stop a stranger

1. **Cloudflare Access** at the edge: deny by default. One Allow rule matches the owner's email and
   needs a one-time PIN (or Google sign-in). The free Zero Trust plan covers up to 50 users.
2. **"Protect with Access"** on the tunnel route: `cloudflared` checks the Access token (a JWT signed
   by the team's keys) before passing a request on. It does the cryptography, so the API still never
   connects out to fetch keys.
3. **Loopback only**: nothing listens beyond `127.0.0.1`. The only way in from outside is the
   outbound tunnel.
4. **The remote listener's own rules** (in the API):
   - the `Host` must be the remote hostname, and only that;
   - the request must carry `Cf-Access-Jwt-Assertion`. This checks only that the header is there, as a
     backstop: if the Access app is ever deleted or no longer covers the hostname, the site closes
     rather than opens.
   - `GET`/`HEAD` are allowed everywhere. `POST` is allowed only to `/v1/translate` and
     `/v1/links/missing`, the two reads the Navigator sends as POST. Anything else gets
     `403 local_only`.
5. **Home-only actions stay home-only.** `POST /v1/weather/refresh`, `POST /v1/gaps` and
   `POST /v1/system/memory/trim` are refused on the remote listener. The first two already refuse
   non-local callers; the trim gains the same guard.

## Step 0: owner decisions (defaults in bold)

| Question | Default |
|---|---|
| Sign-in method | **One-time PIN to the owner's email** (built in; no Google setup). Google login is an option. |
| How often to sign in again (Access session duration) | **1 week** (Cloudflare's default is 24 h) |
| Who may sign in | **the owner only** (more emails can be added later; free up to 50) |

## Step 1: code (Claude, in the worktree, one gated commit)

All in `~/dev/wsp-extra` on `gazrank`. Gate as always: `ruff format --check`, `ruff check`, `pyright`,
then pytest under `systemd-run --user --scope -p MemoryMax=8G`. Commit only on exit code 0, then
`git merge --ff-only gazrank` in the live checkout and push.

1. **Config** (`config/default.toml`, `[api]`):
   - `remote_port = 0`: 0 means no remote listener (today's behaviour);
   - `remote_hosts = []`: the hostnames the remote listener answers to.

   `config/local.toml` (not in git) then gets `remote_port = 8738` and
   `remote_hosts = ["wiki.mattinees.com"]`. `bootstrap.serve_settings` reads them. They are refused if
   `remote_port` equals the home port, or if `remote_hosts` is empty while `remote_port` is set.
2. **`wsp serve`** (`wsp_cli/main.py:cmd_serve`):
   - with a remote port set, bind two loopback sockets and run `uvicorn.Server(config).run(sockets=[home, remote])`;
   - keep `proxy_headers=False`, `server_header=False`, `access_log=False`;
   - print both addresses at start;
   - refuse a remote port when `--host` is not loopback.
3. **`ApiSettings`** (`wsp_api/services.py`): `remote_port: int | None`, `remote_hosts: tuple[str, ...]`.
4. **`SecurityMiddleware`** (`wsp_api/security.py`). It keys on `scope["server"][1]`:
   - On the home port: today's `LOOPBACK_HOSTS` allow-list, unchanged. A remote hostname arriving on
     the home port is refused (400), so the two listeners cannot be mixed up.
   - On the remote port, in order:
     - `Host` in `remote_hosts`, else 400;
     - `cf-access-jwt-assertion` present, else 403 `access_required`;
     - method rule, else 403 `local_only`;
     - then mark the request remote (`scope["wsp.remote"] = True`).
   - The security headers apply to both, as today.
5. **One "is this computer" test**:
   - `browse.is_loopback(request)` becomes `is_local(request)`: the peer is loopback and the request is
     not marked remote.
   - It is used by `/v1/gaps`, `/v1/weather/refresh`, and newly `/v1/system/memory/trim`.
   - Grep for other `client.host` uses first (pattern 8: helpers drifting apart).
6. **Unit files**:
   - `deploy/systemd/whisipedia-tunnel.service` (template, installed by `wsp service install`):
     `ExecStart=%h/.local/bin/cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:20241 run --token-file %h/.config/whisipedia/tunnel.token`,
     `Restart=always`, `RestartSec=10`, `NoNewPrivileges=yes`, `PrivateTmp=yes`, `MemoryMax=256M`,
     `After=whisipedia-api.service`, `WantedBy=default.target`.
   - The unit is installed **disabled**; step 4 enables it.
7. **`tools/wsp-shutdown`**: stop `whisipedia-tunnel.service` first, before the API, so a halt takes
   the remote address down with the site. Resuming after a halt starts it with the API.
8. **Tests**:
   - `test_api_security`:
     - remote listener: wrong Host is 400; missing JWT header is 403; `POST /v1/system/memory/trim`,
       `/v1/gaps` and `/v1/weather/refresh` are 403 although the peer is 127.0.0.1; `GET /v1/search`,
       `POST /v1/translate` and `POST /v1/links/missing` are 200;
     - home listener: a remote Host is 400; everything else as today.
   - `test_cli_commands`: two sockets bound, both loopback; remote port refused with a non-loopback
     host or equal ports; `proxy_headers` still off.
   - Config tests for the refusals.
9. **Docs**:
   - `docs/runbooks/remote-access.md`: steps 2–5 below, with the kill switch;
   - ADR-0014, "Private remote access through a separate tunnel and Cloudflare Access", which amends
     PLAN §10.5 (the API still makes no outbound connection; a separate connector carries requests in);
   - `docs/components/api.md`: the remote listener.

Estimate: half a day with tests. This step changes nothing on its own: with `remote_port = 0` the
server is exactly today's.

## Step 2: install `cloudflared` in WSL (Claude; no sudo)

- Download a **pinned** release, not `latest`: the newest at the time, and at least 2025.4.0, which
  `--token-file` needs. Take `cloudflared-linux-amd64` from `github.com/cloudflare/cloudflared/releases`
  into a new empty directory.
- Check its SHA-256 against the value in that release's notes, then move it to `~/.local/bin/cloudflared`.
- Fallback with no download: copy the binary out of the image this computer already runs
  (`docker create cloudflare/cloudflared:latest` then `docker cp <id>:/usr/local/bin/cloudflared …`),
  then `docker rm`.
- Updating later is the same step, then `systemctl --user restart whisipedia-tunnel`. Cloudflare
  supports versions up to a year old.

## Step 3: Cloudflare dashboard (owner, ~15 minutes; in this order)

The order matters. Cloudflare: "We recommend creating an Access application before setting up the
tunnel route"; without one "the published application will be available to anyone on the Internet".

1. **Access application first.** Zero Trust → Access controls → Applications → Create new
   application → Self-hosted and private:
   - Add public hostname: subdomain `wiki`, domain `mattinees.com`.
   - Access policy: action **Allow**, include **Emails**: the owner's address. Access is deny by default.
   - Identity providers: **One-time PIN** only. With a single provider, turn on **Apply instant
     authentication** to skip the chooser page.
   - Session Duration: per step 0.
   - Create. Then open the application and copy its **Application Audience (AUD) Tag**.
   - Note the **team name** (Zero Trust → Settings; the `<team>` in `<team>.cloudflareaccess.com`).
2. **New tunnel.** Networking → Tunnels → Create a tunnel → Cloudflared → name `whisipedia`:
   - Copy the token: the long string after `--token` in the install command shown.
   - Do **not** run the install command shown; step 4 does it.
   - Leave the existing tunnel alone.
3. **Route.** On the `whisipedia` tunnel → Routes → Add route → Published application:
   - hostname `wiki` + the domain; service **HTTP**, URL `127.0.0.1:8738`.
   - Under Additional application settings → Access, turn on **Protect with Access**, with the team
     name and the AUD tag from 1.
   - Leave **HTTP Host Header** empty, so the API sees the real hostname.
   - Save. With the zone on Cloudflare DNS, the `wiki` CNAME to the tunnel is created automatically.
4. **Zone settings for this hostname** (Rules → Configuration Rules, matching hostname equals
   `wiki.mattinees.com`):
   - turn **Rocket Loader**, **Email Obfuscation** and automatic **Web Analytics** injection off.
     They rewrite pages or add scripts, which the Navigator's strict Content-Security-Policy blocks or
     which break its scripts.
   - Add a **Cache Rule** "Bypass cache" for the hostname. Library pages and files then stay off
     Cloudflare's cache, and large originals do not count against its CDN terms.

## Step 4: switch on (Claude, after the owner pastes the token into a file)

1. The owner saves the token, without echoing it into any log:
   `install -m 600 /dev/null ~/.config/whisipedia/tunnel.token`, then paste it in with an editor.
   Claude never prints it.
2. `config/local.toml`: `remote_port = 8738`, `remote_hosts = ["wiki.mattinees.com"]`.
3. `systemctl --user restart whisipedia-api`, and check that both listeners are up:
   `ss -ltn | grep -E '127.0.0.1:(8737|8738)'`. Nothing else may show on `0.0.0.0`.
4. `systemctl --user enable --now whisipedia-tunnel`. In `journalctl --user -u whisipedia-tunnel`,
   look for "Registered tunnel connection" (×4).

## Step 5: verify (Claude, then the owner)

- From this computer, through the edge:
  - `curl -sI https://wiki.mattinees.com/` gives a 302 to `<team>.cloudflareaccess.com`, not a library page;
  - `curl -s https://wiki.mattinees.com/v1/health` gives the same redirect, not JSON.
- Remote listener directly, as a forged request: `curl -s -H 'Host: wiki.mattinees.com' 127.0.0.1:8738/v1/health`
  gives 403 `access_required`; with a dummy JWT header and `-X POST …/v1/system/memory/trim` it gives
  403 `local_only`.
- Home listener: `curl -s 127.0.0.1:8737/v1/health` still gives 200, and
  `curl -s -H 'Host: wiki.mattinees.com' 127.0.0.1:8737/` gives 400.
- `tools/wsp-api-health` and the stall watch stay quiet. A remote page view touches the activity
  marker, so scrub and parity yield to remote reading as they do to home reading.
- Owner, on the phone off home Wi-Fi: sign in with the code. Then:
  - search and open a document;
  - open the Atlas and the weather (Refresh shows the home-only message);
  - open a large PDF original, which streams.
  - Then on another device, or in a private window, try an address that is not the owner's: Access
    must refuse it.

## Known behaviour and limits

- **Session expiry mid-visit:** when the Access session ends, the Navigator's background requests are
  sent to the sign-in page and fail as network errors until the page is reloaded. Optional follow-up:
  on such a failure, show "Sign-in expired — reload".
- **Speed:** a cold search still takes 12–20 s, as at home. Cloudflare waits 100 s for a first byte, so
  it does not time out. Large originals stream at the home upload speed.
- **When the library is halted** (games, a reboot, `wsp-shutdown`), the address shows Cloudflare's
  "tunnel unavailable" page. That is expected.
- **What stays home-only:** weather pulls, gap reports and the memory trim. Gap reports from away
  could be allowed later, by deciding the remote marker is the owner too. Not now.
- **No change to what the library fetches:** this is inbound only. Sources, robots and the
  User-Agent are untouched.

## Kill switch and undo

- **Instant, no Cloudflare login:** `systemctl --user disable --now whisipedia-tunnel`. The address
  goes dark at once; the library at home is unaffected.
- **Close the API side too:** set `remote_port = 0` in `local.toml` and restart the API.
- **Remove entirely:** delete the tunnel route and the `whisipedia` tunnel in the dashboard (this
  removes the DNS record), then the Access application; then
  `rm ~/.config/whisipedia/tunnel.token ~/.local/bin/cloudflared`.
- **If the token leaks:** in the dashboard, the tunnel's page → Refresh token (or delete and recreate
  the tunnel); save the new token to the file; restart the unit. With the Access application in place,
  a leaked token cannot let anyone past sign-in. It could only let someone attach a connector in our
  place, which the logs would show.

## Sources checked (2026-10-30)

- Cloudflare: [origin parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/cloudflared-parameters/origin-parameters/)
  ("Protect with Access": `cloudflared` validates the Access JWT; HTTP Host Header; editing a route under
  Networking → Tunnels → Routes).
- [run parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/cloudflared-parameters/run-parameters/)
  (`--token-file` needs 2025.4.0+; `--no-autoupdate`; `--metrics`; `--protocol auto` uses QUIC and
  falls back to HTTP/2).
- [self-hosted public application](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
  (the steps; Access before the route; deny by default; validate the token at the origin).
- [create a remotely managed tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/).
- [downloads](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/)
  (Linux amd64 binary; supported for a year).
- [Zero Trust plans](https://www.cloudflare.com/plans/zero-trust-services/) (free up to 50 users).
- This computer: Docker Desktop 29.9.2; the `proxy` network is `172.18.0.0/16`. Its reachability was
  tested 2026-10-30 (table above). WSL networking is NAT (no `networkingMode` in `.wslconfig`).
  uvicorn is 0.54.0.
