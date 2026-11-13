# Oracle Cloud Always Free check

`oci_free_tier_check.py` compares an OCI tenancy against the Always Free limits, so you notice before anything could cost money or get reclaimed.

- It prints a snapshot table that marks each item **OK**, **WARN** (80% or more of a limit), **OVER**, or **INFO**.
- It writes an HTML report with graphs of the last 7 and 30 days.
- It only reads from the account and never changes anything.

## Quick start: OCI Cloud Shell (no setup)

Cloud Shell has the OCI Python SDK installed and is already signed in as you, so it needs no keys or config.

1. Sign in to <https://cloud.oracle.com> and open **Developer tools → Cloud Shell** (the `>_` icon at the top right).
2. In Cloud Shell, choose **Menu → Upload** and upload `oci_free_tier_check.py`. You can also fetch it directly:
   ```bash
   curl -fsSLO https://raw.githubusercontent.com/shimonr/misc/main/oracle-free-tier/oci_free_tier_check.py
   ```
3. Run it:
   ```bash
   python3 oci_free_tier_check.py            # 30-day history
   python3 oci_free_tier_check.py --days 7   # 7-day history
   ```
4. To see the graphs, choose **Menu → Download**, enter `oci_free_tier_report.html`, and open the file in a browser.

A run takes about 30–60 seconds.

## Example output

```
Always Free check  tenancy home region eu-frankfurt-1  at 2026-11-13 18:26 UTC

OK    A1 OCPUs (all A1 instances)                    2 / 2 OCPU             shimon-oracle-host-1=2
OK    A1 memory                                      12 / 12 GB
OK    E2.1.Micro instances                           0 / 2
OK    Instances with non-free shapes                 0 / 0
OK    Tenancy service limit standard-a1-core-count   2 / 2 OCPU             Oracle's enforced limit (used / limit)
OK    Tenancy service limit standard-a1-memory-count 12 / 12 GB             Oracle's enforced limit (used / limit)
OK    Block storage (boot + block volumes)           100 / 200 GB           1 boot (100 GB), 0 block (0 GB)
OK    Volume backups (boot + block)                  1 / 5                  pre-resize-2026-11-13
OK    Object storage                                 0 / 20 GB              0 buckets
OK    Load balancers (free: 1 x 10 Mbps flexible)    0 / 1
OK    Network load balancers                         0 / 1
OK    VCNs                                           1 / 2
INFO  Reserved public IPs                            0 / -                  not in the Always Free list; ephemeral IPs are free
OK    Always Free Autonomous DBs                     0 / 2
OK    Paid Autonomous DBs                            0 / 0
WARN  Idle-reclaim check: shimon-oracle-host-1 ...                          CPU p95 6.9% | memory p95 8.2% | network p95 0.0% (last 24 h: ...)
OK    Outbound data (all VNICs, 30-day rate)         3.27 / 10240 GB/month  VNIC bytes sent; includes in-VCN traffic, so internet egress is lower
OK    Cost this month                                0 / 0 HUF              nothing billed

Result: 0 OVER, 1 WARN
HTML report with graphs: /home/you/oci_free_tier_report.html
```

The script exits with code `1` when anything is OVER and `0` otherwise, so cron or CI can alert on it.

## What it checks

| Check | Always Free limit | Source |
|---|---|---|
| A1 OCPUs / memory, summed over all `VM.Standard.A1.Flex` instances | 2 OCPU / 12 GB | Compute API |
| The tenancy's enforced A1 service limits (`standard-a1-core-count`, `standard-a1-memory-count`) | whatever Oracle enforces | Limits API |
| `VM.Standard.E2.1.Micro` instances | 2 | Compute API |
| Instances with any other shape | 0 (they are paid) | Compute API |
| Boot + block volume storage | 200 GB | Block Storage API |
| Volume backups (boot + block) | 5 | Block Storage API |
| Object storage | 20 GB | Object Storage API |
| Load balancers / network load balancers | 1 / 1 | LB and NLB APIs |
| VCNs | 2 | Networking API |
| Reserved public IPs | not in the free list (shown as INFO) | Networking API |
| Autonomous DBs: free / paid | 2 / 0 | Database API |
| Outbound data (VNIC bytes sent, projected to a month) | 10 TB / month | Monitoring `oci_vcn` |
| Idle reclamation: CPU, memory, and network p95 over 7 days | at least one ≥ 20% | Monitoring `oci_computeagent` |
| Cost this month | 0 | Usage API |

Graphs in the HTML report:
- CPU % and memory % per instance: 7 days at 5-minute resolution and 30 days hourly, with the 20% idle line drawn in.
- Data sent per day across all VNICs.
- Daily cost.

### Notes on the limits

- **A1 was halved in June 2026.** The free allowance went from 4 OCPU / 24 GB to 2 OCPU / 12 GB (1,500 OCPU-hours and 9,000 GB-hours a month). Some console pages still show the old numbers. The *Tenancy service limit* rows show what Oracle actually enforces on your account.
- **Idle reclamation.** Oracle may reclaim an Always Free instance when, over 7 days, CPU p95 < 20% **and** network < 20% **and** memory < 20% (memory counts only for A1). Keeping any one of them above 20% avoids it. The row also shows the last 24 hours, so a fix shows up before the 7-day window catches up.
- **Outbound data** counts all bytes sent by your VNICs, including traffic inside the VCN, so real internet egress is lower.
- **Free Tier vs Pay As You Go.** A Free Tier account (not upgraded) can't be charged; over-limit resources are stopped or removed instead. On Pay As You Go, anything over these limits is billed. Either way, the goal is 0 OVER and cost 0.
- Limits come from Oracle's [Always Free Resources](https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) page as of Nov 2026. They're in the `FREE` dict at the top of the script if Oracle changes them.
- Only the tenancy's current region is checked. Always Free resources must be in the home region anyway.

## Options

```
--days N        history window for the long graphs and cost (default 30)
--html PATH     HTML report path (default oci_free_tier_report.html; '' to skip)
--auth MODE     auto | config | cloud_shell | instance_principal (default auto)
--profile NAME  profile in ~/.oci/config when using config auth (default DEFAULT)
```

`auto` uses Cloud Shell's sign-in when it runs in Cloud Shell, and `~/.oci/config` otherwise.

## Running it elsewhere

### Your own machine

```bash
pip install oci
oci setup config        # creates ~/.oci/config and an API key; upload the public key in the console
python3 oci_free_tier_check.py --profile DEFAULT
```

The user only needs read access. Policies like these should be enough; they haven't been tested, while the administrator account is:

```
Allow group FreeTierReaders to inspect all-resources in tenancy
Allow group FreeTierReaders to inspect resource-availability in tenancy
Allow group FreeTierReaders to read metrics in tenancy
Allow group FreeTierReaders to read usage-report in tenancy
Allow group FreeTierReaders to read buckets in tenancy
```

### On the VM itself, on a schedule (instance principal)

This needs no keys on the server.

1. In **Identity → Domains → Default → Dynamic groups**, create a group, for example `free-tier-check`, with this rule:
   `instance.id = '<your instance OCID>'`
2. Add the same policy statements, written for `dynamic-group free-tier-check`.
3. On the VM, run `pip install oci`, then add a cron entry:
   ```
   0 7 * * * python3 /opt/free-tier/oci_free_tier_check.py --auth instance_principal --html /opt/free-tier/report.html || echo "OCI free tier: something is OVER" | mail -s "OCI free tier alert" you@example.com
   ```

The VM setup was written from the OCI docs but hasn't been run yet. Cloud Shell and API-key runs are the tested paths.

## Notes for shimon-oracle-host-1 (eu-frankfurt-1)

- 2026-11-13: resized from 4 OCPU / 24 GB to 2 OCPU / 12 GB to fit the new A1 allowance. A full boot-volume backup, `pre-resize-2026-11-13`, was taken first.
- `keep-alive.service` stops Oracle reclaiming the server as idle. It runs this at idle CPU priority, which keeps CPU around 25% and memory around 28%:
  ```
  stress-ng --cpu 0 --cpu-load 25 --vm 1 --vm-bytes 2G --vm-hang 0 --timeout 0 --oomable
  ```
  - `--cpu 0` uses every core, so it adapts to the shape.
  - `--oomable` lets the out-of-memory killer free its 2 GB first, and the worker isn't restarted afterwards.
  - The previous unit is saved at `~ubuntu/keep-alive.service.orig`.
