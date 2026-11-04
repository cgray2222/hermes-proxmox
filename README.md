# Proxmox VE for Hermes Agent

Six tools and one context reference that let Hermes see and operate a Proxmox VE cluster:
inventory, per-guest health, node and storage health, snapshots, backup results, and
command execution inside a VM through the QEMU guest agent.

No extra Python packages — the client is standard library only, so the plugin installs
into any Hermes environment without touching the dependency set.

## What it adds

| Tool | What it does | Write? |
|---|---|---|
| `pve_list_guests` | Every VM and LXC in the cluster: node, state, uptime, memory, CPU, disk | no |
| `pve_guest_status` | One guest: live counters, config highlights, snapshot list | no |
| `pve_node_status` | Node health: PVE version, kernel, load, CPU, memory, rootfs, storage pools | no |
| `pve_snapshot` | List, create or delete a snapshot | create/delete |
| `pve_backup_status` | Recent vzdump / Proxmox Backup Server jobs with result and duration | no |
| `pve_guest_exec` | Run a command inside a running VM via the QEMU guest agent | yes |

Plus `@pve:` context references, expanded on demand into the prompt:

```
@pve:all     cluster inventory (nodes, guests, power state)
@pve:102     one guest — status, config, snapshots
@pve:pve     one node — health and every storage pool
```

That is the difference between a tool wrapper and an integration: the agent can drag live
cluster state into a single turn instead of making five tool calls to build the picture.

## Install

```bash
hermes plugins install cgray2222/hermes-proxmox     # after it is in the catalog
# or, from a local checkout:
hermes plugins install /path/to/hermes-proxmox
```

Restart the gateway (or start a new CLI session) so the tools load.

## Configure

Create a Proxmox API token with the narrowest role that still does the job:

```bash
# read-only tools need PVEAuditor on /
pveum user token add hermes@pve hermes --privsep 1
pveum acl modify / --users hermes@pve --roles PVEAuditor
# snapshots and guest exec additionally need PVEVMAdmin on the guests involved
pveum acl modify /vms/102 --users hermes@pve --roles PVEVMAdmin
```

Then set, in `~/.hermes/.env` or through the plugin's config prompt:

```bash
PVE_HOST=192.168.6.12
PVE_API_TOKEN=hermes@pve!hermes=<secret>
# optional
PVE_PORT=8006
PVE_VERIFY_TLS=1          # set 0 only for a self-signed certificate you trust
```

`PVE_USER` + `PVE_TOKEN_NAME` + `PVE_TOKEN_VALUE` also work if you prefer the secret kept
separately. Secrets are read through `agent.secret_scope`, so the active Hermes profile
wins over a bare `os.environ` read.

## Safety model

- **Read tools are read-only.** They use the token's own permissions and nothing wider.
- **Write tools refuse without `confirm: true` in the same call.** Creating a snapshot,
  deleting one, or running a command returns a refusal that names the exact change. The
  agent has to state the change and get agreement, then call again — an accidental write
  is a refused call, not a lost disk.
- **Guest exec is QEMU-only.** Containers reject it with an explanation; there is no
  container exec API in Proxmox's stable interface to abuse.
- **No Hermes internals are touched.** Registration goes through `register_tool` and
  `register_context_reference` only.

## Verify it works

```bash
hermes plugins doctor ~/projects/hermes-proxmox       # load + registration
hermes plugins validate ~/projects/hermes-proxmox     # the same gate the catalog CI runs
python -m pytest tests/ -q                            # offline unit tests
```

## Demo script (under 90 seconds)

```
you:  @pve:all — anything unhealthy on the cluster?
      → inventory in the prompt, one node, two guests, both running

you:  why is 102 slow? @pve:102
      → live counters, 2 cores, 3.6/6.0 GB, agent enabled

you:  snapshot 102 as pre-upgrade
      → refused: needs confirm=true
you:  yes, do it
      → task qmsnapshot, exitstatus OK

you:  run df -h in 102
      → refused
you:  go ahead
      → /dev/root 220.5M 100% — HA's root partition is full, which is the whole answer
```

## Layout

```
plugin.yaml       manifest (the catalog PR pins this tree)
__init__.py       register(): six tools + the @pve: reference
pve_client.py     HTTP client, credential resolution, task polling
tools.py          tool schemas, handlers, formatting
references.py     @pve: context reference provider
tests/            offline unit tests
```

## License

MIT
