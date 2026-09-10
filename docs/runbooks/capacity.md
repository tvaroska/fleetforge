# Capacity Check Runbook

**Tool:** `scripts/capacity_snapshot.py`  
**Purpose:** Measure host + container memory/swap/disk; detect capacity problems before they cause failures.

---

## Quick Start

```bash
# One sample, this box
just capacity-check

# 15-minute window with 30s sampling
just capacity-check --watch 900 --interval 30

# Same script, piped to prod over ssh
just capacity-check-prod

# Filter to specific containers
just capacity-check --match fleetforge
```

**Critical:** Measure in the **production shape** (`just up-prod`). `just up` runs the Vite dev server in the frontend container (~48 M against a 64 M limit); prod runs nginx (~6 M). Measuring the dev shape produces a false "frontend needs more memory" alarm.

---

## What It Measures

### Host Metrics

| Metric | Source | What It Means |
|---|---|---|
| **MemAvailable floor** | `/proc/meminfo`, min over window | Real headroom — the lowest it got during the run |
| **pswpin/pswpout/pgmajfault deltas** | `/proc/vmstat`, first vs last | Is it *thrashing* (pswpin > 0) or just parking cold pages (pswpout only) |
| **Swap used** | `/proc/meminfo` | Stock, not flow — 1 GB of cold pages in swap costs nothing; the *rate* costs |

### Container Metrics (cgroup v2)

| Metric | Source | What It Means |
|---|---|---|
| **memory.peak** | `memory.peak` | Highest usage since container start |
| **memory.events max** | `memory.events` | **The definitive under-provisioning signal** — counts forced reclaims *at* the limit, which happen long before an OOM kill and are otherwise invisible |
| **memory.events oom_kill** | `memory.events` | Hard OOM kills — the limit was hit and nothing could be reclaimed |
| **memory.swap.current** | `memory.swap.current` | *Which* container's pages are in swap |

---

## Verdict Rules

Encoded in `compute_verdict()` — these are testable, not prose:

- **FAIL** if:
  - Any container has `oom_kill > 0`, **or**
  - `pswpin` delta > 1000 pages over the window (thrashing), **or**
  - `MemAvailable` floor < 256 MiB
- **TIGHT** if:
  - Any container's `memory.peak` ≥ 85% of its limit, **or**
  - Any container has `memory.events max > 0` (reclaim at the limit), **or**
  - `MemAvailable` floor < 512 MiB, **or**
  - Σ declared limits > `MemTotal` (over-committed on paper)
- **OK** otherwise

Exit code: `0` for OK/TIGHT, `1` for FAIL.

---

## When to Run It

1. **Before deploying a new service** — does the box have headroom?
2. **After a capacity alarm** — which container, which signal?
3. **Before resizing the VM** — is the pressure real, or a false alarm from a point sample?
4. **After a config change** — did raising a limit fix the `memory.events max`, or just move the problem?

---

## Interpreting the Output

### Good

```
VERDICT: OK
  headroom  MemAvailable floor 2231 MiB
  reclaim   0 containers hit their limit (memory.events max)
  thrash    pswpin delta 0 over 900 s
```

### Needs Attention

```
VERDICT: TIGHT
  - fleetforge-api peak 89% of limit
  - memory.events max > 0: fleetforge-ingestor
```

**Action:** Watch it. The api is close to its limit; the ingestor has hit its limit and forced a reclaim (but didn't OOM). If load is expected to grow, resize or raise limits.

### Failing

```
VERDICT: FAIL
  - OOM kill: fleetforge-api
```

**Action:** The api was OOM-killed. Either the limit is too low for the workload, or there's a memory leak. Raise the limit **and** investigate.

---

## Resizing the VM

**The agent cannot do this.** Neither `devserver@btvaroska` nor `mainsite@sites-470716` has `compute.instances.*` on `sites-470716`. Run from **Cloud Shell** as the owner:

### 1. Check the External IP (Critical)

A stop/start releases an **ephemeral** external IP. All four production domains resolve to this VM's address. Confirm it is a **reserved static address** before stopping anything:

```bash
gcloud compute addresses list --project sites-470716

gcloud compute instances describe main --zone us-central1-c --project sites-470716 \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP,networkInterfaces[0].accessConfigs[0].name)'
```

If the address is **not** listed in `gcloud compute addresses list`, it is ephemeral and will change. Reserve it first:

```bash
# Promote ephemeral to static
gcloud compute addresses create main-ip --region us-central1 --addresses <CURRENT_IP> --project sites-470716
```

### 2. Resize

Machine-type changes require the instance to be **STOPPED**. This is real downtime for boris, content, downloader **and** the fleetforge broker.

```bash
gcloud compute instances stop  main --zone us-central1-c --project sites-470716

gcloud compute instances set-machine-type main --zone us-central1-c --project sites-470716 \
  --machine-type e2-custom-2-6144      # 2 vCPU / 6 GB

# Or: e2-standard-2 = 2 vCPU / 8 GB
# Or: e2-custom-2-8192 = 2 vCPU / 8 GB (e2-custom requires memory in 256 MB increments, ≥ 512 MB/vCPU)

gcloud compute instances start main --zone us-central1-c --project sites-470716
```

### 3. Verify

`restart: unless-stopped` brings every container back on boot. Verify:

```bash
ssh prod 'docker ps --format "{{.Names}} {{.Status}}"'
curl -sS -o /dev/null -w '%{http_code}\n' https://update.tvaroska.sk/ https://download.tvaroska.sk/ https://boris.tvaroska.sk/ https://bingo.tvaroska.sk/
ssh prod 'docker compose -f /opt/fleetforge/docker-compose.yml ps fleetforge-mosquitto --format "{{.Status}}"'
```

All containers `Up`, all domains 200 (or their normal auth code), mosquitto `(healthy)`.

### Gotchas

1. **The ephemeral-IP trap above** — it takes all four sites down until DNS is re-pointed.
2. **This project has no `default` network** — firewall rules need `--network=sites` (seen in R0-infra-3).
3. **e2-custom** requires memory in 256 MB increments and ≥ 512 MB/vCPU.

---

## Non-Resize Mitigations (Considered and Deferred)

- **`docker image prune`** reclaims 1.15 GB of disk (not memory). Safe to run on prod, but disk is not the bottleneck today.
- **Enlarging `/swapfile` beyond 2 GiB** buys tolerance for cold pages parked in swap, but not for a hot working set under pressure.
- **Shaving container limits** was explicitly rejected by the task instruction — size the VM up rather than tune limits down.

---

## Technical Notes

- **Swap *used* is a stock, not a flow.** A gigabyte of cold anonymous pages parked in swap and never read back costs nothing; what costs is the *rate* of `pswpin` / `pgmajfault`. A verdict built on "swap used is 1 G, therefore resize" would be wrong. A verdict built on the window deltas is defensible.
- **`memory.events max` is the signal that matters.** It counts forced reclaims at the limit — which happen long before an OOM kill and are otherwise invisible. A container with `max > 0` is under-provisioned even if it never crashes.
- **`docker stats` is not used.** It's slow (~1 s/container even with `--no-stream`), gives no peak and no `memory.events`. This script reads cgroup v2 directly.
- **The script imports stdlib only** — no `uv`, no project venv, so the same script runs here and on prod over ssh. It must stay that way (prod has `python3 3.11` and nothing else).

---

## Related

- **Capacity measurement:** `design/production.md` → *Capacity*
- **Decision log:** `DECISIONS.md` 2026-09-10 (R0-infra-4)
- **Acceptance tests:** `.claude/plans/R0-infra-4-prod-capacity-check.md` §8
