# slice_services.py -- generate type/tag-sliced JSON files from directory/services.json.
# Pure stdlib. Called from harvest.yml after harvest.py. Output goes to directory/by_type/.
#
# Slicing strategy (locked by Web in REPLY_FROM_WEB_TO_NOX_PAGINATION_2.md, Option 1b):
#   1. One slice per type value (8 files: api-service.json, wallet.json, etc).
#   2. Within type "API / Service": one sub-slice per domain_tag (16 sub-slices).
#      A service with multiple tags appears in multiple sub-slices (acceptable duplication).
#   3. For any tag exceeding CHUNK_THRESHOLD services (currently only "Developer Tools"),
#      split alphabetically by service name first letter into chunks targeting CHUNK_TARGET_MB.
#   4. Write directory/by_type/manifest.json listing every slice with type, tag, count, size.
#
# Filename convention (kebab-case throughout):
#   directory/by_type/{type-slug}.json                                 (e.g. wallet.json)
#   directory/by_type/{type-slug}-{tag-slug}.json                      (e.g. api-service-ai.json)
#   directory/by_type/{type-slug}-{tag-slug}-{letter-range}.json       (e.g. api-service-developer-tools-a-c.json)
import json
import os
import re
import sys
import time
from collections import defaultdict

REPO_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES_PATH = os.path.join(REPO_ROOT, "directory", "services.json")
OUT_DIR       = os.path.join(REPO_ROOT, "directory", "by_type")

# Tags exceeding this count get alphabetical chunking instead of a single tag slice.
CHUNK_THRESHOLD = 5000
# Target chunk size in megabytes -- chunks may overshoot by ~30% to avoid tiny tail chunks.
CHUNK_TARGET_MB = 3.0


def slugify(s):
    """Convert a taxonomy label to kebab-case slug.

    Examples:
        "API / Service"        -> "api-service"
        "Finance / DeFi"       -> "finance-defi"
        "Compliance / Legal"   -> "compliance-legal"
        "Developer Tools"      -> "developer-tools"
        "MCP Server"           -> "mcp-server"
    """
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


def name_prefix(name, depth):
    """Return lowercase alphabetic prefix of `depth` letters from name. Non-alpha is skipped.
    Pads with '0' if the name has fewer than `depth` alphabetic chars.

    Examples (depth=2):
        "OpenAI-finance"  -> "op"
        "Z"               -> "z0"
        "123-foo"         -> "fo"
        ""                -> "00"
    """
    if not name:
        return "0" * depth
    result = ""
    for ch in name.lower():
        if "a" <= ch <= "z":
            result += ch
            if len(result) == depth:
                return result
    return (result + "0" * depth)[:depth]


def make_range(prefixes):
    """Format a contiguous list of prefixes as 'a-c' or 'oa-of' or just 'a'."""
    if not prefixes:
        return ""
    if len(prefixes) == 1:
        return prefixes[0]
    return "%s-%s" % (prefixes[0], prefixes[-1])


def estimate_bytes_per_service(services):
    """Estimate average JSON-encoded size of a single service in bytes."""
    if not services:
        return 1000
    sample = services[: min(200, len(services))]
    encoded = json.dumps(sample, separators=(",", ":"))
    return max(1, int(len(encoded) / len(sample)))


def chunk_alphabetically(services, target_mb):
    """Split services into alphabetical chunks targeting target_mb each.

    Recursive: starts grouping by first letter. If any single-letter group exceeds
    target_count * 1.3, that group is recursively sub-chunked by 2-letter prefix.
    Continues to deeper prefix lengths if a 2-letter group is still too big.
    Returns list of (range_str, chunk_services) in alphabetical order.
    """
    if not services:
        return []
    sorted_services = sorted(services, key=lambda s: (s.get("name") or "").lower())
    bps = estimate_bytes_per_service(sorted_services)
    target_bytes = int(target_mb * 1024 * 1024)
    target_count = max(1, target_bytes // bps)
    overshoot_cap = int(target_count * 1.3)
    return _group_by_prefix(sorted_services, depth=1, overshoot_cap=overshoot_cap)


def _group_by_prefix(services, depth, overshoot_cap, max_depth=6):
    """Group services by name prefix of `depth` letters. Sub-split oversized groups.
    If still oversized at max_depth (e.g. same name prefix repeats thousands of times),
    fall back to index-based splitting with -1, -2, -N suffixes.
    """
    by_prefix = defaultdict(list)
    for s in services:
        by_prefix[name_prefix(s.get("name") or "", depth)].append(s)
    prefixes = sorted(by_prefix.keys())

    chunks = []
    current_prefixes = []
    current_services = []

    def flush():
        if current_services:
            chunks.append((make_range(current_prefixes), list(current_services)))
            current_prefixes[:] = []
            current_services[:] = []

    for prefix in prefixes:
        group = by_prefix[prefix]
        if len(group) > overshoot_cap:
            if depth < max_depth:
                # Try deeper prefix split first
                flush()
                sub_chunks = _group_by_prefix(group, depth + 1, overshoot_cap, max_depth)
                chunks.extend(sub_chunks)
                continue
            # Reached max depth and still too large -- index-based fallback
            flush()
            n_chunks = (len(group) + overshoot_cap - 1) // overshoot_cap
            chunk_size = (len(group) + n_chunks - 1) // n_chunks
            for i in range(n_chunks):
                slice_part = group[i * chunk_size:(i + 1) * chunk_size]
                if not slice_part:
                    break
                label = "%s-%d" % (prefix, i + 1)
                chunks.append((label, slice_part))
            continue
        # Accumulate into current chunk
        if current_services and len(current_services) + len(group) > overshoot_cap:
            flush()
        current_prefixes.append(prefix)
        current_services.extend(group)

    flush()
    return chunks


def write_slice(path, envelope):
    """Write the slice JSON. Returns size in bytes."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, separators=(",", ":"))
    return os.path.getsize(path)


def main():
    print("Reading %s ..." % SERVICES_PATH)
    with open(SERVICES_PATH, "r", encoding="utf-8") as f:
        envelope = json.load(f)
    services = envelope.get("services") or []
    print("  loaded %d services" % len(services))

    os.makedirs(OUT_DIR, exist_ok=True)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Clear any stale slice files so removed types/tags don't linger.
    removed = 0
    for fn in os.listdir(OUT_DIR):
        if fn.endswith(".json"):
            try:
                os.remove(os.path.join(OUT_DIR, fn))
                removed += 1
            except Exception:
                pass
    if removed:
        print("  cleared %d stale slice files" % removed)

    manifest_slices = []

    # Group services by type
    by_type = defaultdict(list)
    no_type = 0
    for s in services:
        t = s.get("type")
        if t:
            by_type[t].append(s)
        else:
            no_type += 1
    if no_type:
        print("  WARNING: %d services have no type and were skipped" % no_type)

    # --- Pass 1: write type slices for the 7 small types ---
    # API / Service is intentionally skipped here -- its full slice would be 28MB.
    # Frontend uses the per-tag sub-slices for API/Service (Pass 2) instead.
    print("\nType slices:")
    for type_name in sorted(by_type.keys()):
        if type_name == "API / Service":
            print("  (api-service.json skipped -- use per-tag sub-slices below)")
            continue
        type_services = by_type[type_name]
        type_slug = slugify(type_name)
        filename = "%s.json" % type_slug
        path = os.path.join(OUT_DIR, filename)
        out = {
            "generated_at": now_iso,
            "type": type_name,
            "tag": None,
            "letter_range": None,
            "count": len(type_services),
            "services": type_services,
        }
        size = write_slice(path, out)
        size_mb = size / (1024.0 * 1024.0)
        print("  %-50s %6d services  %6.2f MB" % (filename, len(type_services), size_mb))
        manifest_slices.append({
            "filename": filename,
            "type": type_name,
            "tag": None,
            "letter_range": None,
            "count": len(type_services),
            "size_bytes": size,
        })

    # --- Pass 2: API/Service tag sub-slices ---
    api_services = by_type.get("API / Service", [])
    if api_services:
        print("\nAPI/Service tag sub-slices:")
        by_tag = defaultdict(list)
        for s in api_services:
            for tag in (s.get("domain_tags") or []):
                by_tag[tag].append(s)

        for tag_name in sorted(by_tag.keys()):
            tag_services = by_tag[tag_name]
            tag_slug = slugify(tag_name)

            if len(tag_services) >= CHUNK_THRESHOLD:
                # Alphabetical chunking
                chunks = chunk_alphabetically(tag_services, CHUNK_TARGET_MB)
                for letter_range, chunk_services in chunks:
                    filename = "api-service-%s-%s.json" % (tag_slug, letter_range)
                    path = os.path.join(OUT_DIR, filename)
                    out = {
                        "generated_at": now_iso,
                        "type": "API / Service",
                        "tag": tag_name,
                        "letter_range": letter_range,
                        "count": len(chunk_services),
                        "services": chunk_services,
                    }
                    size = write_slice(path, out)
                    size_mb = size / (1024.0 * 1024.0)
                    print("  %-50s %6d services  %6.2f MB" % (filename, len(chunk_services), size_mb))
                    manifest_slices.append({
                        "filename": filename,
                        "type": "API / Service",
                        "tag": tag_name,
                        "letter_range": letter_range,
                        "count": len(chunk_services),
                        "size_bytes": size,
                    })
            else:
                filename = "api-service-%s.json" % tag_slug
                path = os.path.join(OUT_DIR, filename)
                out = {
                    "generated_at": now_iso,
                    "type": "API / Service",
                    "tag": tag_name,
                    "letter_range": None,
                    "count": len(tag_services),
                    "services": tag_services,
                }
                size = write_slice(path, out)
                size_mb = size / (1024.0 * 1024.0)
                print("  %-50s %6d services  %6.2f MB" % (filename, len(tag_services), size_mb))
                manifest_slices.append({
                    "filename": filename,
                    "type": "API / Service",
                    "tag": tag_name,
                    "letter_range": None,
                    "count": len(tag_services),
                    "size_bytes": size,
                })

    # --- Pass 3: featured slice (top N by quality_payers_30d, fallback quality_calls_30d) ---
    print("\nFeatured slice (top 100 by quality):")

    def quality_key(s):
        return (s.get("quality_payers_30d") or 0, s.get("quality_calls_30d") or 0)

    ranked = [s for s in services if quality_key(s) != (0, 0)]
    ranked.sort(key=quality_key, reverse=True)
    featured = ranked[:100]
    featured_out = {
        "generated_at": now_iso,
        "type": None,
        "tag": None,
        "letter_range": None,
        "count": len(featured),
        "services": featured,
    }
    featured_path = os.path.join(OUT_DIR, "featured.json")
    featured_size = write_slice(featured_path, featured_out)
    featured_size_kb = featured_size / 1024.0
    print("  featured.json                                     %6d services  %6.1f KB" % (len(featured), featured_size_kb))
    manifest_slices.append({
        "filename": "featured.json",
        "type": None,
        "tag": None,
        "letter_range": None,
        "count": len(featured),
        "size_bytes": featured_size,
        "kind": "featured",
    })

    # --- Pass 4: manifest ---
    active_count = sum(1 for s in services if s.get("status") == "active")
    total_size = sum(s["size_bytes"] for s in manifest_slices)
    manifest = {
        "generated_at": now_iso,
        "total_services": len(services),
        "active_count": active_count,
        "slice_count": len(manifest_slices),
        "total_slice_size_bytes": total_size,
        "slices": manifest_slices,
    }
    manifest_path = os.path.join(OUT_DIR, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print("\nWrote manifest.json with %d slice entries" % len(manifest_slices))
    print("Total slice payload: %.1f MB across %d files" % (total_size / (1024.0 * 1024.0), len(manifest_slices)))


if __name__ == "__main__":
    main()
