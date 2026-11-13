#!/usr/bin/env python3
"""Check an Oracle Cloud tenancy against the Always Free limits.

Prints a snapshot table (OK / WARN / OVER) and writes an HTML report with 7- and 30-day graphs of
CPU, memory, network, egress, and cost.

Run it in OCI Cloud Shell with no setup:   python3 oci_free_tier_check.py
Elsewhere it uses ~/.oci/config (--profile) or --auth instance_principal.
Needs the `oci` Python SDK (preinstalled in Cloud Shell; otherwise `pip install oci`).

Limits follow https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
as of Nov 2026 (A1 halved to 2 OCPU / 12 GB in June 2026). The A1 limit is also read live from the
tenancy's service limits, which is what Oracle enforces.
"""
import argparse
import datetime as dt
import html
import os
import sys

import oci

FREE = {
    "a1_ocpu": 2, "a1_mem_gb": 12, "e2_micro": 2, "block_gb": 200, "backups": 5,
    "object_gb": 20, "egress_gb_month": 10240, "lb": 1, "nlb": 1, "vcn": 2, "adb": 2,
}
IDLE_PCT = 20  # Oracle reclaims an Always Free instance when CPU p95, network, and memory are all below this for 7 days
FREE_SHAPES = {"VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro"}
UTC = dt.timezone.utc


def auth(args):
    if args.auth == "instance_principal":
        return {}, oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    if args.auth == "cloud_shell" or (args.auth == "auto" and os.environ.get("OCI_CLI_CLOUD_SHELL")):
        config = oci.config.from_file(os.environ["OCI_CONFIG_FILE"], os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT"))
        with open(os.environ["OCI_DELEGATION_TOKEN_FILE"]) as f:
            signer = oci.auth.signers.InstancePrincipalsDelegationTokenSigner(delegation_token=f.read().strip())
        return config, signer
    config = oci.config.from_file(profile_name=args.profile)
    return config, oci.signer.Signer(config["tenancy"], config["user"], config["fingerprint"],
                                     config["key_file"], config.get("pass_phrase"))


def client(cls, config, signer, region=None):
    cfg = dict(config)
    if region:
        cfg["region"] = region
    return cls(cfg, signer=signer)


def tenancy_id(config, signer):
    if config.get("tenancy"):
        return config["tenancy"]
    return signer.tenancy_id


def paged(fn, *a, **kw):
    data = oci.pagination.list_call_get_all_results(fn, *a, **kw).data
    return getattr(data, "items", data)


def status(used, limit, warn=0.8):
    """warn=None for allowances you allocate up front, such as A1 OCPUs, where using all of it is normal."""
    if limit is None:
        return "INFO"
    if used > limit:
        return "OVER"
    return "WARN" if warn and limit and used >= warn * limit else "OK"


def series(mon, comp, ns, query, start, end, resolution):
    d = oci.monitoring.models.SummarizeMetricsDataDetails(
        namespace=ns, query=query, start_time=start, end_time=end, resolution=resolution)
    out = mon.summarize_metrics_data(comp, d, compartment_id_in_subtree=True).data
    pts = []
    for m in out:
        pts += [(p.timestamp, p.value) for p in m.aggregated_datapoints]
    return sorted(pts)


def pctl(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def gather(args):
    config, signer = auth(args)
    ten = tenancy_id(config, signer)
    ident = client(oci.identity.IdentityClient, config, signer)
    home = next(r.region_name for r in ident.list_region_subscriptions(ten).data if r.is_home_region)
    region = config.get("region") or home
    comps = [ten] + [c.id for c in paged(ident.list_compartments, ten, compartment_id_in_subtree=True,
                                         access_level="ANY", lifecycle_state="ACTIVE")]
    ads = [a.name for a in ident.list_availability_domains(ten).data]
    now = dt.datetime.now(UTC).replace(second=0, microsecond=0)
    r = {"tenancy": ten, "home": home, "region": region, "time": now, "rows": [], "charts": [], "notes": []}

    def row(name, used, limit, unit="", detail="", warn=0.8):
        r["rows"].append({"name": name, "used": used, "limit": limit, "unit": unit,
                          "status": status(used, limit, warn), "detail": detail})

    # Compute
    comp = client(oci.core.ComputeClient, config, signer)
    insts = [i for c in comps for i in paged(comp.list_instances, c) if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
    a1 = [i for i in insts if i.shape == "VM.Standard.A1.Flex"]
    a1_ocpu = sum(i.shape_config.ocpus for i in a1)
    a1_mem = sum(i.shape_config.memory_in_gbs for i in a1)
    row("A1 OCPUs (all A1 instances)", a1_ocpu, FREE["a1_ocpu"], "OCPU",
        ", ".join(f"{i.display_name}={i.shape_config.ocpus:g}" for i in a1), warn=None)
    row("A1 memory", a1_mem, FREE["a1_mem_gb"], "GB", warn=None)
    row("E2.1.Micro instances", sum(i.shape == "VM.Standard.E2.1.Micro" for i in insts), FREE["e2_micro"])
    paid = [i for i in insts if i.shape not in FREE_SHAPES]
    row("Instances with non-free shapes", len(paid), 0, "", ", ".join(f"{i.display_name} ({i.shape})" for i in paid))
    lim = client(oci.limits.LimitsClient, config, signer)
    for name, unit in (("standard-a1-core-count", "OCPU"), ("standard-a1-memory-count", "GB")):
        used, svc_limit = 0, 0
        for ad in ads:
            try:
                a = lim.get_resource_availability("compute", name, ten, availability_domain=ad).data
            except oci.exceptions.ServiceError as e:
                r["notes"].append(f"limits {name} {ad}: {e.message}")
                continue
            used += a.used or 0
            svc_limit = max(svc_limit, (a.used or 0) + (a.available or 0))
        row(f"Tenancy service limit {name}", used, svc_limit, unit, "Oracle's enforced limit (used / limit)", warn=None)

    # Block storage
    bs = client(oci.core.BlockstorageClient, config, signer)
    boots = [b for c in comps for ad in ads for b in paged(bs.list_boot_volumes, availability_domain=ad, compartment_id=c)
             if b.lifecycle_state != "TERMINATED"]
    vols = [v for c in comps for v in paged(bs.list_volumes, compartment_id=c) if v.lifecycle_state != "TERMINATED"]
    total_gb = sum(b.size_in_gbs for b in boots) + sum(v.size_in_gbs for v in vols)
    row("Block storage (boot + block volumes)", total_gb, FREE["block_gb"], "GB",
        f"{len(boots)} boot ({sum(b.size_in_gbs for b in boots)} GB), {len(vols)} block ({sum(v.size_in_gbs for v in vols)} GB)")
    bb = [b for c in comps for b in paged(bs.list_boot_volume_backups, c) if b.lifecycle_state != "TERMINATED"]
    vb = [b for c in comps for b in paged(bs.list_volume_backups, c) if b.lifecycle_state != "TERMINATED"]
    row("Volume backups (boot + block)", len(bb) + len(vb), FREE["backups"], "",
        ", ".join(b.display_name for b in bb + vb))

    # Object storage
    osc = client(oci.object_storage.ObjectStorageClient, config, signer)
    ns = osc.get_namespace().data
    obj_bytes, nb = 0, 0
    for c in comps:
        for b in paged(osc.list_buckets, ns, c):
            nb += 1
            obj_bytes += osc.get_bucket(ns, b.name, fields=["approximateSize"]).data.approximate_size or 0
    row("Object storage", round(obj_bytes / 1e9, 2), FREE["object_gb"], "GB", f"{nb} buckets")

    # Networking
    lbc = client(oci.load_balancer.LoadBalancerClient, config, signer)
    lbs = [x for c in comps for x in paged(lbc.list_load_balancers, c) if x.lifecycle_state != "DELETED"]
    row("Load balancers (free: 1 x 10 Mbps flexible)", len(lbs), FREE["lb"], "", ", ".join(f"{x.display_name} ({x.shape_name})" for x in lbs))
    nlbc = client(oci.network_load_balancer.NetworkLoadBalancerClient, config, signer)
    nlbs = [x for c in comps for x in paged(nlbc.list_network_load_balancers, c) if x.lifecycle_state != "DELETED"]
    row("Network load balancers", len(nlbs), FREE["nlb"])
    vn = client(oci.core.VirtualNetworkClient, config, signer)
    vcns = [x for c in comps for x in paged(vn.list_vcns, c) if x.lifecycle_state != "TERMINATED"]
    row("VCNs", len(vcns), FREE["vcn"])
    rips = [x for c in comps for x in paged(vn.list_public_ips, "REGION", c, lifetime="RESERVED")]
    row("Reserved public IPs", len(rips), None, "", "not in the Always Free list; ephemeral IPs are free")

    # Databases
    db = client(oci.database.DatabaseClient, config, signer)
    adbs = [x for c in comps for x in paged(db.list_autonomous_databases, c) if x.lifecycle_state != "TERMINATED"]
    row("Always Free Autonomous DBs", sum(bool(x.is_free_tier) for x in adbs), FREE["adb"])
    row("Paid Autonomous DBs", sum(not x.is_free_tier for x in adbs), 0)

    # Metrics: idle reclamation and graphs, per running instance
    mon = client(oci.monitoring.MonitoringClient, config, signer)
    d7, d30 = now - dt.timedelta(days=7), now - dt.timedelta(days=args.days)
    for i in insts:
        if i.lifecycle_state != "RUNNING":
            continue
        f = f'{{resourceId = "{i.id}"}}'
        cpu7 = series(mon, ten, "oci_computeagent", f"CpuUtilization[5m]{f}.mean()", d7, now, "5m")
        mem7 = series(mon, ten, "oci_computeagent", f"MemoryUtilization[5m]{f}.mean()", d7, now, "5m")
        net7 = []
        for va in paged(comp.list_vnic_attachments, i.compartment_id, instance_id=i.id):
            net7 += series(mon, ten, "oci_vcn", f'VnicToNetworkBytes[5m]{{resourceId = "{va.vnic_id}"}}.sum()', d7, now, "5m")
        cpu30 = series(mon, ten, "oci_computeagent", f"CpuUtilization[1h]{f}.mean()", d30, now, "1h")
        mem30 = series(mon, ten, "oci_computeagent", f"MemoryUtilization[1h]{f}.mean()", d30, now, "1h")
        bw = (getattr(i.shape_config, "networking_bandwidth_in_gbps", None) or 1) * 1e9 / 8
        cpu_p95 = pctl([v for _, v in cpu7], 0.95)
        mem_p95 = pctl([v for _, v in mem7], 0.95)
        net_p95 = pctl([v / 300 / bw * 100 for _, v in net7], 0.95)
        busy = [x for x in (cpu_p95, mem_p95, net_p95) if x is not None and x >= IDLE_PCT]
        d1 = now - dt.timedelta(days=1)
        cpu_24h = pctl([v for t, v in cpu7 if t >= d1], 0.95)
        mem_24h = pctl([v for t, v in mem7 if t >= d1], 0.95)
        r["rows"].append({"name": f"Idle-reclaim check: {i.display_name} (7 days, needs one metric >= {IDLE_PCT}%)",
                          "used": None, "limit": None, "unit": "",
                          "status": "OK" if busy else ("WARN" if cpu7 else "INFO"),
                          "detail": "CPU p95 {} | memory p95 {} | network p95 {} (last 24 h: CPU {}, memory {})".format(*(
                              "n/a" if x is None else f"{x:.1f}%" for x in (cpu_p95, mem_p95, net_p95, cpu_24h, mem_24h)))})
        r["charts"] += [
            (f"{i.display_name}: CPU % (7 days, 5-min mean)", cpu7, IDLE_PCT, "%"),
            (f"{i.display_name}: memory % (7 days, 5-min mean)", mem7, IDLE_PCT, "%"),
            (f"{i.display_name}: CPU % ({args.days} days, hourly mean)", cpu30, IDLE_PCT, "%"),
            (f"{i.display_name}: memory % ({args.days} days, hourly mean)", mem30, IDLE_PCT, "%"),
        ]
    egr = {}
    for t, v in series(mon, ten, "oci_vcn", "VnicToNetworkBytes[1d].sum()", d30, now, "1d"):
        egr[t] = egr.get(t, 0) + v
    month_gb = sum(egr.values()) / 1e9 * 30 / max(args.days, 1)
    row("Outbound data (all VNICs, 30-day rate)", round(month_gb, 2), FREE["egress_gb_month"], "GB/month",
        "VNIC bytes sent; includes in-VCN traffic, so internet egress is lower")
    r["charts"].append((f"Data sent per day, all VNICs ({args.days} days, GB)", sorted((t, v / 1e9) for t, v in egr.items()), None, "GB"))

    # Cost (Usage API lives in the home region)
    us = client(oci.usage_api.UsageapiClient, config, signer, region=home)
    end = (now + dt.timedelta(days=1)).replace(hour=0, minute=0)
    start = (now - dt.timedelta(days=args.days)).replace(hour=0, minute=0)
    try:
        items = us.request_summarized_usages(oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=ten, time_usage_started=start, time_usage_ended=end, granularity="DAILY",
            query_type="COST", group_by=["service"])).data.items
        by_day, by_svc, cur = {}, {}, "billing currency"
        for it in items:
            amt = float(it.computed_amount or 0)
            cur = (it.currency or "").strip() or cur
            by_day[it.time_usage_started] = by_day.get(it.time_usage_started, 0) + amt
            by_svc[it.service or "?"] = by_svc.get(it.service or "?", 0) + amt
        month_start = now.replace(day=1, hour=0, minute=0)
        mtd = sum(v for t, v in by_day.items() if t >= month_start)
        row("Cost this month", round(mtd, 2), 0, "" if cur == "billing currency" else cur,
            ", ".join(f"{k} {v:.2f}" for k, v in sorted(by_svc.items(), key=lambda kv: -kv[1]) if v) or "nothing billed")
        r["charts"].append((f"Daily cost ({args.days} days, {cur})", sorted(by_day.items()), None, cur))
    except oci.exceptions.ServiceError as e:
        r["notes"].append(f"usage API: {e.message}")
    return r


def fmt(x):
    if x is None:
        return "-"
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def print_report(r):
    print(f"Always Free check  tenancy home region {r['home']}  at {r['time']:%Y-%m-%d %H:%M} UTC\n")
    w = max(len(x["name"]) for x in r["rows"])
    for x in r["rows"]:
        val = "" if x["used"] is None else f"{fmt(x['used'])} / {fmt(x['limit'])} {x['unit']}".rstrip()
        print(f"{x['status']:<5} {x['name']:<{w}}  {val:<22} {x['detail']}")
    for n in r["notes"]:
        print(f"note: {n}")
    bad = [x for x in r["rows"] if x["status"] in ("OVER", "WARN")]
    print("\nResult:", "everything within Always Free" if not bad else
          f"{sum(x['status'] == 'OVER' for x in bad)} OVER, {sum(x['status'] == 'WARN' for x in bad)} WARN")


def svg_chart(title, pts, threshold, unit, w=760, h=180):
    if not pts:
        return f"<h3>{html.escape(title)}</h3><p>No data.</p>"
    xs = [p[0].timestamp() for p in pts]
    ys = [p[1] for p in pts]
    top = max(ys + ([threshold * 1.5] if threshold else [])) or 1
    x0, x1 = min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1
    px = lambda t: 40 + (t - x0) / (x1 - x0) * (w - 50)
    py = lambda v: h - 20 - v / top * (h - 35)
    line = " ".join(f"{px(t):.1f},{py(v):.1f}" for t, v in zip(xs, ys))
    th = (f'<line x1="40" x2="{w - 10}" y1="{py(threshold):.1f}" y2="{py(threshold):.1f}" stroke="#c0392b" stroke-dasharray="4 3"/>'
          f'<text x="{w - 12}" y="{py(threshold) - 4:.1f}" text-anchor="end" fill="#c0392b">{threshold}{unit} threshold</text>') if threshold else ""
    first, last = pts[0][0].strftime("%b %d"), pts[-1][0].strftime("%b %d %H:%M")
    return (f"<h3>{html.escape(title)}</h3><svg viewBox='0 0 {w} {h}' width='100%'>"
            f"<line x1='40' x2='40' y1='10' y2='{h - 20}' stroke='#999'/><line x1='40' x2='{w - 10}' y1='{h - 20}' y2='{h - 20}' stroke='#999'/>"
            f"<text x='36' y='{py(top) + 10:.1f}' text-anchor='end'>{top:.3g}</text><text x='36' y='{h - 20}' text-anchor='end'>0</text>"
            f"<text x='40' y='{h - 4}'>{first}</text><text x='{w - 10}' y='{h - 4}' text-anchor='end'>{last}</text>{th}"
            f"<polyline fill='none' stroke='#2c6fbb' stroke-width='1.5' points='{line}'/></svg>"
            f"<p class='s'>min {min(ys):.3g} · mean {sum(ys) / len(ys):.3g} · max {max(ys):.3g} {html.escape(unit)}</p>")


def write_html(r, path):
    color = {"OK": "#1e8449", "WARN": "#b9770e", "OVER": "#c0392b", "INFO": "#555"}
    rows = ""
    for x in r["rows"]:
        val = "" if x["used"] is None else f"{fmt(x['used'])} / {fmt(x['limit'])} {x['unit']}"
        rows += (f"<tr><td style='color:{color[x['status']]};font-weight:600'>{x['status']}</td>"
                 f"<td>{html.escape(x['name'])}</td><td>{html.escape(val)}</td><td>{html.escape(x['detail'])}</td></tr>")
    charts = "".join(svg_chart(*c) for c in r["charts"])
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in r["notes"])
    doc = f"""<!doctype html><meta charset="utf-8"><title>OCI Always Free check</title>
<style>body{{font:14px system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 12px;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}
svg text{{font-size:11px;fill:#555}}h3{{margin:24px 0 4px;font-size:14px}}.s{{color:#666;margin:2px 0}}</style>
<h1>OCI Always Free check</h1><p>Home region {html.escape(r['home'])} · generated {r['time']:%Y-%m-%d %H:%M} UTC</p>
<table><tr><th>Status</th><th>Resource</th><th>Used / free limit</th><th>Detail</th></tr>{rows}</table>
{('<ul>' + notes + '</ul>') if notes else ''}{charts}"""
    with open(path, "w") as f:
        f.write(doc)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=30, help="history window for the long graphs and cost (default 30)")
    p.add_argument("--html", default="oci_free_tier_report.html", help="HTML report path ('' to skip)")
    p.add_argument("--auth", choices=["auto", "config", "cloud_shell", "instance_principal"], default="auto")
    p.add_argument("--profile", default=os.environ.get("OCI_CLI_PROFILE", "DEFAULT"))
    args = p.parse_args()
    r = gather(args)
    print_report(r)
    if args.html:
        write_html(r, args.html)
        print(f"HTML report with graphs: {os.path.abspath(args.html)}")
    sys.exit(1 if any(x["status"] == "OVER" for x in r["rows"]) else 0)


if __name__ == "__main__":
    main()
