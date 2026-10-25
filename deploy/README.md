# Running the demo on OpenMediaVault

Two ways. Docker through OMV's Compose plugin is the one that fits the box;
the systemd unit is for running it bare on the host.

Replace `<nas-ip>` below with your NAS's address on the LAN. Both end with
the demo page at `http://<nas-ip>:8700/` (Docker) or `http://<nas-ip>:8701/`
(systemd): a sentence box, the answer, the
output, and the trace, plus the list of everything the swarm can answer.

## Docker, through the Compose plugin

The stack in [`openmediavault/docker-compose.yml`](openmediavault/docker-compose.yml)
runs four containers: a **hub** on port 8700, two example services
(**news**: Hacker News and GitHub; **weather**: Open-Meteo and site status)
that register with the hub over the compose network, and **mine**, which
serves a folder of your own trees from a shared folder and reloads them
when you edit a file.

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
   paste the compose file, and replace both `CHANGE-ME` paths with the path
   from step 2 (the checkout at `…/appdata/pyrtan`, the trees folder at
   `…/appdata/pyrtan/trees`). Save.
5. **Up**: select the file, press *Up*. The first time it builds the image
   on the NAS, which takes a minute or two.
6. **Open** `http://<nas-ip>:8700/`. The page should say `4/4 peers
   up` within ten seconds (the hub polls every ten). Try "top 3 stories on
   hacker news".

To update: `git pull` in the checkout, then *Down* and *Up* with *Build*
ticked (or `docker compose -p pyrtan up -d --build` over SSH). To check a
tree you dropped into `trees/` before it is live:
`docker compose -p pyrtan exec mine pyrtan check /trees`. To see gaps the
demo has met: `http://<nas-ip>:8700/gaps`.

The services do not expose their own ports; everything goes through the
hub on 8700. As shipped nothing authenticates: this is a demo for your
LAN. Before forwarding 8700 anywhere, set a token: uncomment
`PYRTAN_TOKEN` in the compose file's `x-pyrtan` block (every container
reads it, so the swarm shares it), put TLS in front, and open
`https://pyrtan.com/console/` with the hub's URL and the token; the
compose file already passes `--cors https://pyrtan.com` to the hub. The
`mine` service keeps its gaps in `trees/.gaps.jsonl` so they survive a
restart; the container runs as uid 10001, so `chown 10001 trees` (or
`chmod o+w trees`) on the NAS, else the gaps stay in memory and `/health`
reports `gaps_file_error`.

To let this swarm answer for another (a friend's hub, or a hosted one),
add `--federate --advertise https://<your-public-url> --state
/trees/.federation.json` to the hub's command once it is behind TLS, then
`pyrtan follow https://other-hub --at https://<your-public-url> --token …`.
Following is one-directional trust: your hub routes to the hubs you
follow, and hubs that follow yours may ask it. See
[`../_meta/federation.md`](../_meta/federation.md).

### Without the plugin

Over SSH, from the checkout:

```
cd /srv/dev-disk-by-uuid-…/appdata/pyrtan
cp deploy/openmediavault/docker-compose.yml .
sed -i "s#/srv/dev-disk-by-uuid-CHANGE-ME/pyrtan#$PWD#g" docker-compose.yml
docker compose up -d --build
```

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
