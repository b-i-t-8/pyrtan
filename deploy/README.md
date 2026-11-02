# Running Pyrtan on OpenMediaVault

Two ways. Docker through OMV's Compose plugin is the one that fits the box,
and its compose file is the family hub: private, behind a token, in a
network of its own. The systemd unit runs the open demo bare on the host,
for a LAN you trust.

Replace `<nas-ip>` below with your NAS's address on the LAN. Both end with
the console at `http://<nas-ip>:8700/` (Docker, after the token) or
`http://<nas-ip>:8701/` (systemd): a sentence box, the answer, the
output, and the trace, plus the list of everything the swarm can answer.

## Docker, through the Compose plugin

The stack in [`openmediavault/docker-compose.yml`](openmediavault/docker-compose.yml)
runs four containers: a **hub** on port 8700, two example services
(**news**: Hacker News and GitHub; **weather**: Open-Meteo and site status)
that register with the hub over the compose network, and **mine**, which
serves a folder of your own trees from a shared folder and reloads them
when you edit a file. `mine` runs on the NAS's own network (port 8702),
so its trees can reach and discover the house's devices; see "The house's
devices" below.

1. **Install the plugin**, if it is not there: in OMV, *System › Plugins*,
   install `openmediavault-compose` (needs omv-extras on OMV 6/7). It
   installs Docker.
2. **Pick a shared folder** for the files. Say the share is `appdata` on
   the data disk; on the host that is a path like
   `/srv/dev-disk-by-uuid-…/appdata`. Find it under *Storage › Shared
   Folders*, or run `ls /srv` over SSH.
3. **Clone the repository there**, over SSH on the NAS:

   ```
   cd /srv/dev-disk-by-uuid-…/appdata
   git clone https://github.com/b-i-t-8/pyrtan.git
   mkdir -p pyrtan/trees
   ```

   `pyrtan/trees` is the folder **mine** watches. Copy an example in to
   start: `cp pyrtan/examples/site-status/site-status.toml pyrtan/trees/`.
4. **Add the stack**: *Services › Compose › Files › Add*. Name it `pyrtan`,
   paste the compose file, and replace every `CHANGE-ME`: both paths with
   the path from step 2 (the checkout at `…/appdata/pyrtan`, the trees
   folder at `…/appdata/pyrtan/trees`), `PYRTAN_TOKEN` with a long random
   string (`openssl rand -hex 24`), and `PYRTAN_ADVERTISE` with the NAS's
   address (see the family section below). Save. The token lives only in
   OMV's copy of the file: never commit it.
5. **Up**: select the file, press *Up*. The first time it builds the image
   on the NAS, which takes a minute or two.
6. **Open** `http://<nas-ip>:8700/` and enter the token. The page should
   say `4/4 peers up` within ten seconds (the hub polls every ten). Try
   "top 3 stories on hacker news".
7. **Make the family network**, once: `pyrtan network new family --at
   http://<nas-ip>:8700 --token <token>` (from any machine with pyrtan, or
   `docker compose -p pyrtan exec hub pyrtan network new family --at
   http://127.0.0.1:8700`). Keep the invite it prints; the family section
   below says what to do with it, and how to set up the family's phones.

To update: `git pull` in the checkout, then *Down* and *Up* with *Build*
ticked (or `docker compose -p pyrtan up -d --build` over SSH). To check a
tree you dropped into `trees/` before it is live:
`docker compose -p pyrtan exec mine pyrtan check /trees`. To see gaps the
hub has met: `http://<nas-ip>:8700/gaps` (with the token, as everything else).

The example services do not expose their own ports, and `mine`'s port
8702 is the NAS's own; everything goes through the hub on 8700, and every
request but `/health` and the console page needs
the token (every container reads it from the compose file's `x-pyrtan`
block, so the swarm shares it). The hub takes no `--cors`, so no page but
its own console talks to it: the family uses `http://<nas-ip>:8700/`. Do
not forward 8700 to the internet; for family in other houses, see the
family section below. The
`mine` service keeps its gaps in `trees/.gaps.jsonl` so they survive a
restart; the container runs as uid 10001, so `chown 10001 trees` (or
`chmod o+w trees`) on the NAS, else the gaps stay in memory and `/health`
reports `gaps_file_error`.

The rest of this section is for a **public** hub instead, which this
compose file is not. To let a swarm answer for another (a friend's hub,
or any hub in the directory), give the hub a public address, with a tunnel that opens no
port (`cloudflared tunnel --url http://127.0.0.1:8700`, or `tailscale
funnel 8700`) or a port forward behind TLS, then set in the compose
file's environment `PYRTAN_ADVERTISE` (that address), `PYRTAN_JOIN:
default` and `PYRTAN_LISTED: "true"`, with `PYRTAN_TOKEN` set and
`PYRTAN_EGRESS: public` (hubs that follow yours may ask it to run trees,
and `any` would let them point a tree at your LAN). The hub then joins
the default network (built in), follows the first hub in it that
accepts (one it already knows, else from pyrtan.com's hub list, then
its snapshot),
routes to it, and is listed in the directory every hub gossips; `pyrtan
follow URL --at <your hub> --token …` follows any other. With other hubs
in the same house (a second NAS, a laptop running `pyrtan hub`), set
`PYRTAN_LAN: "true"` too, with `network_mode: host` on the hub service
and `PYRTAN_ADVERTISE` at the box's LAN address: the hubs find each
other by multicast, and join through each other before asking
pyrtan.com anything. For a network
of your own, `pyrtan network new home --at <your hub> --token …` prints
an invite; a friend's hub joins with `pyrtan join <invite>`, or by
adding it to its `PYRTAN_JOIN`. Following is one-directional trust: your
hub routes to the hubs you follow, and hubs that follow yours may ask it.
See [`../_meta/federation.md`](../_meta/federation.md).

Your phone is part of it too: open the hub's console on the phone, enter
the token, and in the Member tab join; the phone then keeps the hub's
blobs and runs the hub's trees when asked. Trees meant for the phones
rather than the NAS (they need the internet the NAS is kept from, or the
network the person is on) go in a `handout` directory next to `trees`,
with `PYRTAN_HANDOUT: /handout` in the compose file.

Trees that read the box (a `file` node over `/var/log`, a `shell` node
running `df`, a `serial` node on a USB modem) see the container, not the
NAS: mount the paths and pass the devices through in the compose file
(there are commented lines for it), and the tree's own `paths`,
`commands` and `devices` lists decide what it may touch. Trees that
drive a browser need Playwright, which the image does not carry; run
those from a checkout with `uv sync --extra browser`.

### A family intranet: a private network

This is how [`openmediavault/docker-compose.yml`](openmediavault/docker-compose.yml)
is set up. For a hub that serves a family (or a team) and nobody else,
keep it off everything public: no `PYRTAN_LISTED`, no `PYRTAN_JOIN:
default`, nothing added to pyrtan.com's `hubs.json`, no
`PYRTAN_CHAT_PUBLIC` and no `PYRTAN_CHAT_REACH`, no `--cors`. Make a
private network instead, and share its invite only with the family. In
the hub's environment:

```yaml
PYRTAN_TOKEN: "<long random string>"          # openssl rand -hex 24; the family's way in
PYRTAN_EGRESS: public                          # hubs that follow this one may ask it to run trees
PYRTAN_ADVERTISE: "http://<nas-ip>:8700"       # an address every family hub can reach (below)
PYRTAN_CHAT: "true"                            # the lobby, behind the token: family only
PYRTAN_CLOSED: "true"                          # follows only within its networks, members only with the token
```

Then, once, from any machine that reaches the hub:

```
pyrtan network new family --at http://<nas-ip>:8700 --token <token>
```

It prints an invite, `family:SECRET@http://<nas-ip>:8700`. Send it the
way you would send a house key. Another family hub (a second NAS, a
laptop running `pyrtan hub`) joins with `pyrtan join <invite> --at <that
hub> --token <its token>`, or with the invite in its `PYRTAN_JOIN`. A
private network's hubs never reach the public directory or the snapshot,
and its secret signs every follow made in it. Following is one-way:
the hub that joined follows the NAS, so its people can meet the NAS's
in the lobby, and for the NAS's people to meet theirs, the NAS follows
it back: `pyrtan follow http://<that hub>:8700 --network family --at
http://<nas-ip>:8700 --token <token>`.

Phones and browsers do not need a hub of their own: the NAS's console is
the app.

#### Setting up a phone

1. **Reach the hub.** At home, on the house Wi-Fi, open
   `http://<nas-ip>:8700/` in the phone's browser. Away from home, install
   the Tailscale app, sign in to the family's tailnet (the NAS is on it
   too: see below), and open the NAS's address there,
   `http://<nas-name>.<tailnet>.ts.net:8700/`.
2. **Enter the token.** The page says *This Pyrtan needs a token*: type it
   in and press *Connect*. It is kept in that browser only, for that
   address; a different address (Wi-Fi, then Tailscale) asks once more.
3. **Chat.** Open the *Chat* tab and press *Start*. Whoever else in the
   family presses *Start* is paired with you; with nobody waiting, *a
   stranger, or a bot* gives you a bot after a few seconds, and *people
   only* waits. Untick *Typing indicator* if you would rather not show it.
4. **Join as a member** (optional). In the *Member* tab, give the phone a
   name ("Mum's phone") and press *Join this hub*; leave the federation
   secret empty. Joined with the token, the phone is *trusted*: it keeps
   copies of the hub's blobs (so a dead NAS loses nothing) and runs the
   hub's web trees when the hub asks it to. While the page is open it
   checks in every four seconds, which costs some battery; *Leave* undoes
   it.
5. **Put it on the home screen.** In the browser's menu, *Add to Home
   Screen* (Safari: the share button; Chrome: the ⋮ menu). On an `http://`
   address that is a shortcut that needs the network. For the full app,
   which opens offline, the page must come over `https://`: turn on
   *HTTPS Certificates* in the Tailscale admin console (once, for the
   tailnet), then on the NAS run `tailscale serve --bg 8700`, which serves
   the console at
   `https://<nas-name>.<tailnet>.ts.net/` to the tailnet and nobody else,
   open that address on the phone, enter the token, and add it to the home
   screen from there (and join again in step 4, since it is a new
   address).

#### If a phone is lost

Changing the token alone does not cut it off: a phone that joined as a
member signs with a key of its own, and a trusted one can still ask the
hub to run its trees and receives what the hub hands it. Do all three:

1. **Take it off the tailnet.** In the Tailscale admin console,
   *Machines*, the phone, *Remove*. Away from home it can no longer reach
   the NAS at all.
2. **Let it go on the hub.** `pyrtan peers --at http://<nas-ip>:8700
   --token <token>` lists each phone as `member  <name>  <id>  (trusted)`;
   then `pyrtan drop <id> --at http://<nas-ip>:8700 --token <token>`. Or,
   from another phone's console, the *Peers* tab, *Federation*, *drop* on
   its row. The hub forgets it, its mailbox, and what it handed it or kept
   for it, and refuses anything it signs after that.
3. **Change the token.** Put a new `PYRTAN_TOKEN` in OMV's copy of the
   compose file and press *Down*, then *Up*. The lost phone's browser held
   the old one. Every other phone asks for the new token once; the ones
   that joined as members stay members.

If the phone comes back within range of the house Wi-Fi it can reach the
hub again, but with `PYRTAN_CLOSED` (as the compose file sets it) and
without the new token it can neither ask, chat nor run anything, nor join
again as a member of any kind. On a hub that is not closed it could join
again as an untrusted member and be handed copies of the hub's directory,
gaps and unanswered sentences; there, change the Wi-Fi password too.

`PYRTAN_ADVERTISE` is the address the other hubs reach this one at. If
every family hub is in one house, that is the NAS's LAN address. If
they are in different houses, keep it off the public internet and put
every hub and phone on one private overlay network instead, such as
[Tailscale](https://tailscale.com) (`tailscale up` on each machine, not
`tailscale funnel`, which is public), and advertise the NAS's address
there (`http://<nas-name>.<tailnet>.ts.net:8700`). An internal hostname
under a domain you own works too, if every family network resolves it.
`PYRTAN_LAN` adds nothing here: hubs on the LAN only announce public
networks, and the invite already carries the NAS's address, so leave
it off and the hub needs no `network_mode: host`.

### The house's devices: the `mine` service

The hub and the example services keep to public addresses
(`PYRTAN_EGRESS: public`); `mine` does not (`PYRTAN_EGRESS: any`), so a
tree in `trees/` may reach the house: a PLC over Modbus, a building
controller over BACnet, a UPS or a switch over SNMP, a sensor over CoAP,
a Matter controller, any web page on the LAN, within the `hosts` (and,
for discovery, the `networks`) it lists. Every hub in the family network
may ask `mine` to run its trees, so put a tree there only if the family
may use it.

`mine` runs on the NAS's own network (`network_mode: host`), because the
broadcast and multicast that discovery uses (mDNS, SSDP, BACnet Who-Is) do
not cross Docker's NAT. It listens on the NAS's port 8702 (8701 is the
bare demo's, below), behind the token like everything else; it registers
with the hub at the port the hub publishes (`http://127.0.0.1:8700`), and
the hub reaches it through the Docker host's gateway
(`host.docker.internal`, which the compose file gives the hub). If the
hub's page says 3/4 peers up, check that nothing else holds port 8702 and
that the NAS's firewall lets the Docker network reach it.

To find what to put in a tree, ask the network from the NAS:

```
docker compose -p pyrtan exec mine pyrtan discover scan 192.168.1.0/24 --ports 22,80,443,502,1883,5540,47808 --names
docker compose -p pyrtan exec mine pyrtan discover mdns _matter._tcp
docker compose -p pyrtan exec mine pyrtan discover mdns          # every service type on the network
docker compose -p pyrtan exec mine pyrtan discover ssdp
docker compose -p pyrtan exec mine pyrtan discover bacnet
```

A BACnet Who-Is listens on UDP 47808 for the I-Ams that come back; if
another BACnet program on the NAS holds that port, only devices that
answer by unicast are found.

**An alert when a device goes offline.** `mine` runs the watches of the
trees in `trees/` (`PYRTAN_WATCH`) and keeps what they remember in
`trees/.memory.json` (`PYRTAN_MEMORY`). To have your phone told when a
device stops answering, and when it is back:

1. Install the ntfy app (or run your own ntfy server) and subscribe to a
   long random topic: it is the only thing between your alerts and anyone
   else, so treat it as a password.
2. In the compose file, uncomment `PYRTAN_ENV_NTFY_TOPIC` on `mine` and
   set it to that topic.
3. `cp pyrtan/examples/device-offline/device-offline.toml pyrtan/trees/`,
   then in the copy uncomment the `[[watches]]` block and set the device's
   address and name (one block per device, each with its own `name`).
4. *Down* and *Up*. `docker compose -p pyrtan exec mine pyrtan check /trees`
   should pass, and the hub's console answers "is 192.168.1.50 online".

It alerts once when a device stops answering and once when it comes back,
not every minute in between. A device is online when any of a dozen common
TCP ports answers, even with a refusal; one that drops everything (a
phone asleep, a strict firewall) looks offline.

### Without the plugin

Over SSH, from the checkout:

```
cd /srv/dev-disk-by-uuid-…/appdata/pyrtan
cp deploy/openmediavault/docker-compose.yml .
sed -i "s#/srv/dev-disk-by-uuid-CHANGE-ME/pyrtan#$PWD#g" docker-compose.yml
sed -i "s#CHANGE-ME-to-a-long-random-string#$(openssl rand -hex 24)#" docker-compose.yml
sed -i "s#CHANGE-ME-nas-ip#<nas-ip>#" docker-compose.yml   # the NAS's LAN address, or its Tailscale name
grep PYRTAN_TOKEN docker-compose.yml                       # the token: keep it, the family needs it
docker compose up -d --build
```

This copy holds the token. The repository's `.gitignore` keeps a
`docker-compose.yml` at the root of the checkout out of git, and
`deploy/openmediavault/docker-compose.yml` stays with its `CHANGE-ME`s.

### One container only

If you want a single process and no swarm:

```
docker build -t pyrtan:local .
docker run -d --name pyrtan -p 8701:8701 --restart unless-stopped pyrtan:local
```

It serves the examples at `http://<nas-ip>:8701/`.

## Bare, with systemd

For a host with no Docker. Pyrtan needs Python 3.12+ and OMV 7 ships 3.11,
so `uv` fetches an interpreter. Everything is installed under `/opt/pyrtan`
and run by a system user with no home directory.

```
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
sudo env UV_TOOL_DIR=/opt/pyrtan/tools UV_TOOL_BIN_DIR=/opt/pyrtan/bin \
  UV_PYTHON_INSTALL_DIR=/opt/pyrtan/python \
  uv tool install --python 3.12 git+https://github.com/b-i-t-8/pyrtan
sudo useradd --system --no-create-home --shell /usr/sbin/nologin pyrtan
sudo mkdir -p /var/lib/pyrtan/trees
sudo cp -r /path/to/pyrtan/examples/* /var/lib/pyrtan/trees/
sudo chown -R pyrtan:pyrtan /var/lib/pyrtan
sudo cp deploy/systemd/pyrtan-demo.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pyrtan-demo
curl http://127.0.0.1:8701/health
```

The unit runs one process holding every tree in `/var/lib/pyrtan/trees` on
port 8701, hardened to read its trees and reach the network and nothing
else. Logs: `journalctl -u pyrtan-demo -f`. To update, run the
same `uv tool install` line with `--force`, then
`sudo systemctl restart pyrtan-demo`.

## What the demo shows

- A sentence routed by the hub to the service whose tree answers it, and
  the trace of every observation and decision on the way back.
- A tree in one service delegating to a tree in another: ask
  "top 3 stories on hacker news" and watch `hacker-news` on **news** call
  `hacker-news-item` three times.
- Hot reload: edit a file in `trees/` from the share and ask again.
- Gaps: a decision that met a value no branch covers, at `/gaps`.
- Unanswered: ask something no tree answers and the page shows the tree
  that would, with the command that writes it; the list is at
  `/unanswered`.
- Health as one field: `/peers` on the hub shows each service's `link`.
