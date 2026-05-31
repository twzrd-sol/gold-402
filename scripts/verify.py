# scripts/verify.py
# Gold-402: verification crawler
# Reads directory/services.json, probes each active service for a valid 402 response,
# updates verified/verify_status/verify_failures/last_verified fields.
# Services with 2+ consecutive failures are set to inactive.
# SSRF protection: private IPs are rejected before any network request.
# No external deps -- pure stdlib.

import ipaddress
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

INPUT_PATH  = "directory/services.json"
OUTPUT_PATH = "directory/services.json"
USER_AGENT  = "Gold-402-Verifier/1.0 (https://24klabs.ai/gold-402)"
PROBE_TIMEOUT = 10  # seconds -- timeout = pending, not failure
MAX_VERIFY_PER_RUN = 5000  # cap to keep Action runtime reasonable

# Private/reserved IP ranges to block (SSRF protection)
PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def is_private_ip(hostname):
    """Return True if hostname resolves to a private/reserved IP."""
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            ip_obj = ipaddress.ip_address(ip_str)
            for net in PRIVATE_RANGES:
                if ip_obj in net:
                    return True
        return False
    except Exception:
        # Could not resolve -- treat as unsafe
        return True


def is_safe_url(url):
    """Check URL scheme and resolve hostname for SSRF safety."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "invalid scheme"
        if not parsed.netloc:
            return False, "no hostname"
        hostname = parsed.hostname
        if not hostname:
            return False, "no hostname"
        if is_private_ip(hostname):
            return False, f"private/reserved IP for {hostname}"
        return True, "ok"
    except Exception as e:
        return False, str(e)


def probe_endpoint(url):
    """
    Probe a service endpoint. Returns (status, verify_status) where:
    - status: "verified" | "failed" | "timeout" | "ssrf_blocked" | "error"
    - detail: human-readable reason
    """
    safe, reason = is_safe_url(url)
    if not safe:
        return "failed", f"ssrf_blocked: {reason}"

    try:
        req = urllib.request.Request(
            url,
            data=b"{}",  # minimal POST body
            method="POST",
            headers={
                "User-Agent":    USER_AGENT,
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            },
        )
        # We EXPECT a 402 -- it's the pass condition.
        # urllib raises HTTPError for 4xx/5xx, so we catch it.
        try:
            urllib.request.urlopen(req, timeout=PROBE_TIMEOUT)
            # Got 200 -- service responded but not with 402
            # Could be a public endpoint. Not a verification pass.
            return "failed", "no_402_response: got 2xx"
        except urllib.error.HTTPError as e:
            if e.code == 402:
                # Check for x402 headers
                headers = dict(e.headers)
                has_x402 = any(
                    k.lower() in ("x-payment-response", "www-authenticate", "payment-required")
                    or "402" in k.lower()
                    for k in headers
                )
                # Accept any 402 as verified -- header check is best-effort
                return "verified", "http_402"
            elif e.code == 429:
                return "timeout", f"rate_limited: {e.code}"
            elif e.code in (403, 503):
                # Could be blocking our IP -- don't count as failure
                return "timeout", f"blocked: {e.code}"
            else:
                return "failed", f"http_{e.code}"
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            return "timeout", "connection_timeout"
        return "failed", f"url_error: {reason}"
    except Exception as e:
        if "timed out" in str(e).lower():
            return "timeout", "connection_timeout"
        return "failed", f"error: {e}"


def build_probe_url(service):
    """Build the probe URL from api_base_url + endpoint_path."""
    base = service.get("api_base_url", "")
    path = service.get("endpoint_path") or ""
    if path.startswith("?"):
        return base + path
    if path:
        return base.rstrip("/") + "/" + path.lstrip("/")
    return base


def main():
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[verify] Start: {now_iso}")

    try:
        with open(INPUT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[verify] FATAL: could not read {INPUT_PATH}: {e}", file=sys.stderr)
        sys.exit(1)

    services = data.get("services", [])
    active   = [s for s in services if s.get("status") == "active"]
    print(f"[verify] Total services: {len(services)} | Active: {len(active)}")

    # Prioritize: oldest last_verified first (never-verified first)
    active.sort(key=lambda s: s.get("last_verified") or "0000")
    to_probe = active[:MAX_VERIFY_PER_RUN]
    print(f"[verify] Probing {len(to_probe)} services this run")

    verified_count = 0
    failed_count   = 0
    timeout_count  = 0
    delisted_count = 0

    # Build index for fast update
    svc_index = {s["id"]: s for s in services}

    for i, svc in enumerate(to_probe):
        probe_url = build_probe_url(svc)
        result, detail = probe_endpoint(probe_url)

        s = svc_index[svc["id"]]
        s["last_verified"] = now_iso

        if result == "verified":
            s["verified"]        = 1
            s["verify_status"]   = "verified"
            s["verify_failures"] = 0
            verified_count += 1
        elif result == "timeout":
            # Timeout does not increment failure count
            s["verify_status"] = "timeout"
            timeout_count += 1
        else:
            # Failed
            s["verified"]        = 0
            s["verify_status"]   = "failed"
            s["verify_failures"] = (s.get("verify_failures") or 0) + 1
            failed_count += 1

            # 2 consecutive failures = delist
            if s["verify_failures"] >= 2:
                s["status"] = "inactive"
                delisted_count += 1
                print(f"[verify]   DELISTED {svc['id']} ({detail})")

        if (i + 1) % 100 == 0:
            print(f"[verify]   {i+1}/{len(to_probe)} -- "
                  f"ok={verified_count} fail={failed_count} "
                  f"timeout={timeout_count} delisted={delisted_count}")

    print(f"[verify] Done: verified={verified_count} failed={failed_count} "
          f"timeout={timeout_count} delisted={delisted_count}")

    # Rebuild output
    output = {
        "generated_at": data.get("generated_at"),
        "verified_at":  now_iso,
        "source":       data.get("source"),
        "count":        len([s for s in services if s.get("status") == "active"]),
        "services":     list(svc_index.values()),
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=True)

    print(f"[verify] Written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
