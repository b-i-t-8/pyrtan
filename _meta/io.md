# Design — Forms of I/O: file, shell, serial, queue, browser, memory, and the field protocols

| | |
|---|---|
| **Status** | **Built, v0.7; memory in v0.27; the field protocols (Modbus TCP, BACnet/IP, SNMP, CoAP, Matter) in v0.37; discovery in v0.38.** Five sources beside `http` (six with `memory`), each a node table with an allow-list of its own kind, the same retry rule, the same trace, the same fixtures; the examples cover each; the JavaScript runtime refuses them on a member and answers them from a fixture. |
| **Date** | 2026-10-25 |
| **Decision** | A node names a *source* and what it wants from it; the tree lists what that source may reach; the runtime refuses anything outside the list before the adapter is asked; the adapter moves the bytes and leaves a plain table in state. Files, shells and serial lines are the box itself, and a hosted service keeps them closed. |
| **Where** | `src/pyrtan/adapters.py` (the adapters, the allow-list matchers, the fixture keys), `src/pyrtan/tree.py` (the lists, `Tree.allowed`, the per-source checks), `src/pyrtan/runtime.py` (one path for every source), `src/pyrtan/transport.py` (fixtures answer for every source), `src/pyrtan/js/pyrtan.js` (the same on a member). |

## 0. The brief

"Act on all forms of I/O." v0.1 spoke HTTP and said the shape would hold
for any other form: an adapter names what it may reach, a node observes
or acts through it, a `decide` reads what came back. v0.7 is that
sentence made true five times, and nothing more: no new node kind, no
new template syntax, no way to reach anything a tree did not list.

## 1. The shape, once

An `observe` or `act` node has exactly one of `http`, `browser`, `file`,
`shell`, `serial`, `queue`, `from` or `emit`. The runtime renders the
source's table with the node's scope, asks the tree whether the rendered
request is inside its allow-list, and only then asks the adapter. An
observe retries on a transport error (not on an answer the author can
decide on); an act is attempted once; a policy refusal is never retried.
The result is a table under `into`, `extract` reads it (every source
leaves `body`, so `regex` and `css` extracts work unchanged), and the
trace line reads `source what -> brief`, the way an HTTP line reads
`GET url -> status`.

| Source | Allow-list | What it names | What comes back |
| :-- | :-- | :-- | :-- |
| `http` | `hosts` | a URL | `status`, `ok`, `headers`, `body`, `json`, `url`, `elapsed_ms` |
| `browser` | `hosts` | a URL, `wait_for`, `actions` | `url` (where it ended up), `status`, `ok`, `title`, `body` (rendered), `elapsed_ms` |
| `file` | `paths` | a path, `tail`; `write` or `append` | `path`, `exists`, `size`, `modified`, `body`, `json`, `lines`; `written` |
| `shell` | `commands` | a program and `args` | `command`, `args`, `status` (exit code), `ok`, `stdout`, `stderr`, `body`, `json`, `lines` |
| `serial` | `devices` | a device, `baud`, `write`, `read_until` or `read` | `device`, `baud`, `sent`, `body`, `bytes`, `complete`, `json`, `lines` |
| `queue` | `hosts` and `topics` | a broker URL and a topic; `publish` | `received`, `message` (`topic`, `body`, `json`), `body`, `json`; `published` |
| `memory` | `keys` | a key; `write`, `add` or `delete`, and `ttl` | `key`, `exists`, `value`, `json`, `body`, `written`, `expires`; `previous`, `existed` |
| `modbus` | `hosts` | `modbus://host`, `unit`, a table to `read` from `address` (`count`, `type`, `scale`); or `write` and `value` | `ok`, `raw`, `values`, `value`, `exception`, `body`, `json`; `written` |
| `bacnet` | `hosts` | `bacnet://host`, `object`, `property`, `index`; or `write` and `priority` | `ok`, `value`, `values`, `refused`, `body`, `json`; `written` |
| `snmp` | `hosts` | `snmp://host`, `community`, `version`; `get`, `walk` (and `max`) or `set` | `ok`, `status`, `values`, `value`, `varbinds`, `body`, `json`; `written` |
| `coap` | `hosts` | `coap://host/path`, `method`, `payload`, `format`, `accept` | `status` (`"2.05"`), `code`, `ok`, `format`, `body`, `json`, `elapsed_ms` |
| `discover` | `networks` | `mdns`, `ssdp`, `bacnet = "who-is"` or `scan` (and `ports`), `wait` | `method`, `query`, `ok`, `found`, `devices`, `body`, `json` |
| `matter` | `hosts` | a controller's `ws://…/ws`, `node`; `read`, `write` and `value`, or `command`, `cluster`, `endpoint`, `payload` | `ok`, `path`, `value`, `result`, `refused`, `body`, `json`; `written` |

## 2. The allow-lists

Each list is checked twice, like `hosts`: at load time when the value is
literal (`pyrtan check` refuses `shell = { command = "rm" }` in a tree
whose `commands` is `["df"]`), and at run time when it is templated
(`paths = ["/var/log"]` refuses `/etc/passwd` from a sentence, and
`/var/log/../etc/passwd`, because the path is made absolute first). A
tree that uses a source without its list does not load. The matchers:

- `paths`: absolute paths or globs; a directory covers everything under
  it; `~` is the service's home; `*` alone is everything.
- `commands`: program names (`df` matches `df` and `/usr/bin/df`) or full
  paths (`/usr/bin/df` matches only that). No shell ever reads the line:
  `args` is a list handed to the program as given, so `$HOME`, `;` and
  backticks in an argument are letters.
- `devices`: paths or globs (`/dev/ttyUSB*`).
- `topics`: MQTT filters, `+` (or `*`) one level, `#` the rest; the
  broker's host must be in `hosts` as well.
- `keys`: memory keys or globs (`disk-space/*`); `*` alone is every key.
  Two trees that list the same key share it; a tree that lists `*` says
  out loud that it reads everything the box remembers.

## 3. What this design refuses

- **A general `exec`.** `shell` runs one program by name with a list of
  arguments, through `subprocess` with `shell=False`. There is no
  `script`, no pipe, no `sh -c` unless the author lists `sh` and writes
  the `-c` out loud, which the checker lets through because the list is
  the author's word and `pyrtan check` shows it.
- **Files beyond what a tree reads at once.** A file over 8 MB is refused
  without `tail`; a tail reads the last megabyte and keeps the last N
  lines. A tree is not a log shipper.
- **Making directories.** `write` and `append` need the directory to
  exist. A tree that wants a place to write is given one by its
  operator.
- **A queue library.** MQTT 3.1.1 over a socket is six packets: connect,
  connack, subscribe, suback, publish, disconnect, at QoS 0. That is what
  a tree needs to read one reading or flip one switch; retained sessions,
  QoS 2 and wildcards in a publish are not a tree's business. `mqtts://`
  uses the standard library's TLS.
- **A browser as a requirement.** Playwright is the one optional
  dependency (`pip install 'pyrtan[browser]'`), and a service without it
  answers a `browser` node with a transport error that says so. The
  browser lives on one worker thread (Playwright's sync API is bound to
  the thread that started it) and the service's request threads queue
  for it; each run gets a fresh context. A page may fetch what it likes,
  as pages do; a top-level navigation to a host outside `hosts`, by
  redirect or by a click in `actions`, is aborted and the node fails.
  `PYRTAN_BROWSER` points at a Chromium when Playwright's own is not
  installed.
- **Memory that travels.** What a tree remembers is the box's own: a
  JSON file a service's `--memory` names (or the process, without one).
  No endpoint serves it, it is never a blob, never gossiped and never
  handed to a member; `pyrtan memory FILE` is how a person reads it. A
  delegated tree remembers where it runs. Memory is small on purpose
  (a key up to 200 characters, a value up to 64 KB, 10,000 keys): a
  tree's notes, not a database.
- **Lending the box.** `--egress public` was "reach only public
  addresses". It now also means "no files, no programs, no serial lines,
  no memory":
  an operator hosting other people's trees does not hand them the host.
  A queue or a browser under that policy goes through the same address
  check an HTTP request does.
- **Members running these.** A phone has no shell. The hub hands a
  trusted member only trees whose sources are `http`; the JavaScript
  runtime answers any other source with `source-unsupported` unless a
  recorded fixture stands in for it, which is how the conformance
  vectors cover all six.

## 4. Fixtures

One fixture file answers for every source. An HTTP record is keyed `GET
url`; the others by `adapter_key`: `shell df -P /`, `file /var/log/syslog`
(or `file append …`), `serial /dev/ttyUSB0 "AT\r\n"`, `queue mqtt://broker
home/kitchen/temperature` (or `queue publish …`), `browser https://…`,
`memory disk-space/root` (or `memory write …`, `memory add …`, `memory
delete …`). A memory read is an observation like the others, so a run
that remembers is still deterministic given what it observed, and its
tests replay it.
The record is the table the node would observe, or `{"error": "…"}` for
a transport failure. `pyrtan run --record` writes them from real runs;
the JavaScript runtime builds the same keys, so `tests/fixtures/
examples.json` serves both runtimes and the vectors in `tests/conformance`
cover a shell, a file, a queue, a browser and a serial line without any
of them being present.

## 5. Failure modes, honestly

- A program that hangs holds a service thread for the node's `timeout`
  and is then killed; the node fails with `no answer within`. An author
  who sets `timeout = 600` on a shell node gets what they asked for.
- A serial device that is open elsewhere, or answers nothing, is a
  transport error after the timeout; an observe tries once more. A
  device that answers slowly past the terminator returns `complete =
  false` and what it had.
- A broker that never delivers makes `received = false` after the
  timeout; the tree decides what silence means (the example says "is the
  sensor publishing?"). A broker that needs a client certificate is not
  reachable; username and password are.
- A page that keeps loading past `timeout` fails the node; `wait_until =
  "domcontentloaded"` is the author's lever. A browser that cannot start
  (no Chromium, no display libraries) fails every browser node the same
  way until it can.
- `tail` on a file whose last megabyte is one line gives one line.

## 6. Sequencing

- **v0.7, this:** the five sources, their lists, their fixtures, the
  examples, the JavaScript runtime's own HTML reader (so css extracts
  agree on a service, in a browser and in node).
- **v0.9, built:** `[[watches]]`, a tree that runs when a file, a topic
  or a serial line speaks, or on a clock (`watch.py`; the file is
  polled, the queue and the serial line are held open as streams,
  `adapters.mqtt_stream` and `adapters.serial_lines`).
- **v0.38, built:** discovery (section 8).
- **v0.37, built:** the field protocols (section 7).
- **v0.27, built:** `memory`, what a tree keeps across runs, with `keys`
  (`memory.py`): the last reading, a count of checks in a row, whether
  it already said so. With `[[watches]]`, that is debouncing and
  alerting once, decided by nodes a reader can follow.
- **Later:** a serial line held open across the nodes of one run, for
  devices with a session.

## 7. The field protocols

Most of what a building, a plant floor or a home is made of does not
speak HTTP. Five protocols cover most of it, and each arrives the way the
others did: a node table, an allow-list, a plain table in state, a
fixture key, and the standard library only (`modbus.py`, `bacnet.py`,
`snmp.py`, `coap.py`, `matter.py`).

- **The allow-list is `hosts`.** Each names its device by a URL
  (`modbus://plc.local`, `bacnet://bms.local`, `snmp://ups.local`,
  `coap://garden.local/…`, `ws://matter.local:5580/ws`), and its host is
  held to `hosts` at load time and run time, and to the egress policy,
  exactly as an HTTP request's is. A finer list (registers, objects,
  OIDs) was considered and left out: a tree that may reach a PLC may read
  what the PLC serves, as a tree that may reach an API may call its
  paths; what changes things is an `act`, which `confirm` and a dry run
  already hold.
- **A device's refusal is an answer.** A Modbus exception, a BACnet
  Error, Reject or Abort, an SNMP error-status, a CoAP 4.xx or 5.xx and a
  Matter controller's error are tables a decide reads (`exception`,
  `refused`, `status`, `ok`), so `unknown-object` can mean "no such zone"
  in the tree's own words. Only silence, a closed connection or a reply
  that is not the protocol is a transport error, retried by an observe.
  (`error` is the one key a fixture keeps for a transport failure, so no
  source leaves it in state.)
- **Matter goes through a controller.** A Matter device lives on a
  fabric: commissioning, operational certificates, CASE sessions. That is
  a controller's job, and python-matter-server (the one Home Assistant
  runs) already does it and serves a WebSocket API; `matter` speaks that
  API (`read_attribute`, `write_attribute`, `device_command`) with a
  small RFC 6455 client of its own. The controller's host is the one in
  `hosts`.
- **What is not here.** Modbus RTU and Modbus over TLS; BACnet
  segmentation (a property too large for one packet is read by `index`),
  Who-Is, COV and BBMD registration; SNMPv3 (its privacy needs ciphers
  the standard library does not carry); CoAP over DTLS and Observe;
  Matter without a controller. Watches over these (a BACnet COV, a CoAP
  observe, a Matter subscription) are the natural next step; until then
  a clock watch polls them.
- **Members do not reach devices.** A browser or a phone has no socket
  to a PLC: the JavaScript runtime answers these sources from a fixture
  and otherwise says `source-unsupported`, as for a serial line. It
  builds the same fixture keys, so the vectors cover every example.

## 8. Discovery

A tree that talks to a device names it; a person writing that tree first
has to find it. `discover` answers "what is on the network" the ways the
network itself answers it (`discover.py`, standard library only):

- **mDNS / DNS-SD** (RFC 6762, 6763): a PTR query for a service type
  (`_matter._tcp`, `_hap._tcp`, `_ipp._tcp`), or for the service types
  themselves. It is sent from an ephemeral port, so responders answer by
  unicast (RFC 6762 6.7) and the box need not join the group on 5353.
- **SSDP / UPnP**: an M-SEARCH, then each answer's description, for the
  name, maker and model a person recognises.
- **BACnet Who-Is**: broadcast to every network, through routers; the
  I-Ams name each device's instance, address and vendor, and a routed
  device's network and MAC, the `network` and `mac` a `bacnet` node takes.
- **A TCP scan**: which hosts of a subnet are up and which ports accept a
  connection. A refusal means up too. There is no ICMP: a ping needs a
  raw socket, which a container does not have, and a connection says as
  much for the ports that matter.

The allow-list is `networks`, CIDRs: a scan's target must lie inside
them, at load time and at run time, and a device answering from outside
them is left out of what the node observes. Discovery is the box's own
network, so `--egress public` refuses it, as it refuses files and
programs. Limits keep a tree a tree, not a network scanner: 1,024 hosts
(a /22) and 64 ports per scan, within the node's timeout; 30 seconds of
listening; 32 UPnP descriptions.

`pyrtan discover mdns|ssdp|bacnet|scan` asks the same from the command
line, with no allow-list (the person at the command is the operator). In a
container, multicast and broadcast stay inside Docker's network: the
mDNS, SSDP and BACnet kinds need `network_mode: host` (the deploy README
says how for the NAS); a scan does not.

