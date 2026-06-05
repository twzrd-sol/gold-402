# scripts/harvest.py
# Gold-402: multi-source harvest pipeline
# Sources:
#   1. CDP Bazaar  -- paginates full catalog, raw endpoint data
#   2. Agentic.market -- parses /api/markdown for curated named-brand services
# Normalizes both sources to the Gold-402 schema, deduplicates by ID,
# writes to directory/services.json.
# No external deps -- pure stdlib.

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CDP_URL     = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
AGENTIC_URL = "https://agentic.market/api/markdown"
PAGE_SIZE   = 500
OUTPUT_PATH = "directory/services.json"
USER_AGENT  = "Gold-402-Harvester/1.0 (https://24klabs.ai/gold-402)"

# Canonical network map -- CDP uses EIP-155 chain IDs and Solana genesis hash
NETWORK_MAP = {
    # EIP-155 chain IDs
    "eip155:8453":  "base",
    "eip155:1":     "ethereum",
    "eip155:137":   "polygon",
    "eip155:42161": "arbitrum",
    "eip155:10":    "optimism",
    "eip155:56":    "bnb",
    "eip155:43114": "avalanche",
    "eip155:1301":  "unichain",
    # Solana / Stellar
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp": "solana",
    "stellar:pubnet": "stellar",
    # Plain canonical names (CDP sometimes returns these directly)
    "base":     "base",
    "ethereum": "ethereum",
    "polygon":  "polygon",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "solana":   "solana",
    "stellar":  "stellar",
}

# Testnet network identifiers -- these entries are excluded from the catalog
TESTNET_NETWORKS = {
    "base-sepolia", "eip155:84532", "sepolia", "eip155:11155111",
    "goerli", "eip155:5", "mumbai", "eip155:80001",
    "solana:devnet", "solana:testnet",
}

# Known USDC contract addresses (both cases) -- 6 decimals
USDC_ASSETS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v",
}

# Type inference -- order matters, first keyword match wins
TYPE_RULES = [
    ("Facilitator",            ["facilitator", "settlement", "payment rail",
                                 "payment processor", "payment facilitat"]),
    ("SDK / Library",          ["sdk", " library", "npm package", "pypi",
                                 "pip install", "framework", "middleware",
                                 "client library"]),
    ("MCP Server",             ["mcp server", "mcp tool", "model context protocol",
                                 "mcp endpoint"]),
    ("Wallet",                 ["wallet", "custody", "key management", "self-custody",
                                 "signing service"]),
    ("Directory / Aggregator", ["directory", "aggregator", "index of",
                                 "discovery service", "catalog", "registry"]),
    ("Learning Resource",      ["tutorial", "documentation", "guide", "course",
                                 "learn x402", "education", "how to use"]),
    ("Tool",                   ["cli tool", "command-line", "dashboard", "explorer",
                                 "scanner", "monitor", "analytics tool"]),
]
DEFAULT_TYPE = "API / Service"

# Domain tag inference -- multiple tags can match
DOMAIN_RULES = {
    "AI": [
        "language model", "llm", "gpt", "claude", "openai", "inference",
        "embedding", "image generation", "text generation", "speech to text",
        "text to speech", "ai model", "neural", "machine learning",
    ],
    "Data": [
        "data api", "dataset", "real-time data", "news api", "weather",
        "price feed", "market data", "query", "analytics data",
    ],
    "Finance / DeFi": [
        "defi", "swap", "yield", "lending", "trading", "dex", "amm",
        "liquidity", "staking", "vault", "derivatives", "perpetual",
    ],
    "Security": [
        "security", "vulnerability", "audit", "pentest", "firewall",
        "encryption", "threat", "malware",
    ],
    "Identity": [
        "identity", "kyc", "authentication", "credential", "did",
        "passport", "proof of", "attestation",
    ],
    "Storage": [
        "storage", "ipfs", "arweave", "file upload", "cdn", "pinning",
        "object storage", "blob",
    ],
    "Developer Tools": [
        "code review", "code generation", "github", "pull request", "ci/cd",
        "debugging", "linting", "developer tool", "api key",
    ],
    "Infrastructure": [
        "infrastructure", "rpc", "node", "indexer", "relay", "bridge",
        "cross-chain", "oracle", "validator",
    ],
    "Gaming": ["game", "gaming", "nft game", "metaverse", "play to earn"],
    "Media": ["image", "video", "audio", "music", "photo", "stream", "podcast", "media"],
    "NFT": ["nft", "mint nft", "collection", "opensea", "token uri"],
    "Compliance / Legal": [
        "compliance", "legal", "gdpr", "aml", "regulatory", "sanctions",
        "tax reporting",
    ],
    "Analytics / Monitoring": [
        "analytics", "metrics", "monitoring", "alert", "log", "tracing",
        "observability",
    ],
    "Agent / Automation": [
        "agent", "automation", "workflow", "autonomous", "agentic",
        "multi-agent", "task runner",
    ],
    "Payments / Commerce": [
        "payment", "commerce", "checkout", "shop", "invoice", "billing",
        "subscription", "e-commerce",
    ],
    "Testing / Mocking": [
        "test", "mock", "staging", "sandbox", "simulate", "fuzz",
    ],
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def normalize_network(network_raw):
    if not network_raw:
        return "other", network_raw
    canonical = NETWORK_MAP.get(network_raw)
    if canonical:
        return canonical, network_raw
    low = network_raw.lower()
    for key, name in [
        ("solana", "solana"), ("stellar", "stellar"),
        ("polygon", "polygon"), ("arbitrum", "arbitrum"),
        ("optimism", "optimism"), ("avalanche", "avalanche"),
        ("8453", "base"),
    ]:
        if key in low:
            return name, network_raw
    return "other", network_raw


def amount_to_usd(amount_str, asset):
    try:
        raw = int(amount_str)
    except (TypeError, ValueError):
        return None
    # USDC: 6 decimals
    return round(raw / 1_000_000, 6)


def infer_type(text):
    low = text.lower()
    for type_name, keywords in TYPE_RULES:
        for kw in keywords:
            if kw in low:
                return type_name
    return DEFAULT_TYPE


def infer_domain_tags(text):
    low = text.lower()
    tags = []
    for tag, keywords in DOMAIN_RULES.items():
        for kw in keywords:
            if kw in low:
                tags.append(tag)
                break
    return tags if tags else ["Developer Tools"]


def slugify(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:80]


def extract_name(description, resource_url):
    """Best-effort name from description or URL domain."""
    if description:
        # Pattern: "ServiceName . endpoint_name -- description"
        m = re.match(r"^([^·\-]{3,60}?)[\s]*[·\-]{1,3}[\s]", description)
        if m:
            candidate = m.group(1).strip(" .-")
            if 2 < len(candidate) < 60:
                return candidate
    try:
        parsed = urllib.parse.urlparse(resource_url)
        host = parsed.netloc
        host = re.sub(r"^(www|api|v1|v2)\.", "", host)
        parts = host.split(".")
        return parts[0].replace("-", " ").title()
    except Exception:
        return "Unknown Service"


def make_id(resource_url):
    try:
        parsed = urllib.parse.urlparse(resource_url)
        host = re.sub(r"^(www|api)\.", "", parsed.netloc)
        path = parsed.path.strip("/").replace("/", "-")
        combined = host + ("-" + path if path else "")
        return slugify(combined)
    except Exception:
        return slugify(resource_url[:80])


# ---------------------------------------------------------------------------
# CDP Bazaar fetch
# ---------------------------------------------------------------------------

def fetch_page(offset):
    # RT-007: retry with exponential backoff on 429 rate limit responses
    url = f"{CDP_URL}?limit={PAGE_SIZE}&offset={offset}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                print(f"[harvest] Rate limited at offset {offset}. Waiting {wait}s...")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Max retries exceeded on rate limit at offset {offset}")


# ---------------------------------------------------------------------------
# Agentic.market fetch + normalize
# ---------------------------------------------------------------------------

# Category → domain tags mapping for Agentic.market categories
AGENTIC_DOMAIN_MAP = {
    "Inference":  ["AI"],
    "Data":       ["Data"],
    "Search":     ["Developer Tools"],
    "Media":      ["Media"],
    "Social":     ["Agent / Automation"],
    "Trading":    ["Finance / DeFi"],
    "Infra":      ["Infrastructure"],
    "Storage":    ["Storage"],
    "Travel":     ["Data"],
    "Other":      ["Developer Tools"],
}

# Category → service type mapping
AGENTIC_TYPE_MAP = {
    "Inference":  "API / Service",
    "Data":       "API / Service",
    "Search":     "API / Service",
    "Media":      "API / Service",
    "Social":     "API / Service",
    "Trading":    "API / Service",
    "Infra":      "Infrastructure",
    "Storage":    "API / Service",
    "Travel":     "API / Service",
    "Other":      "API / Service",
}


def fetch_agentic_markdown():
    """Fetch the Agentic.market /api/markdown endpoint."""
    req = urllib.request.Request(AGENTIC_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"[harvest] WARNING: could not reach Agentic.market: {e}", file=sys.stderr)
        return None


def parse_agentic_markdown(md, now_iso):
    """
    Parse the Agentic.market markdown table into Gold-402 service records.

    Table format:
      ### Category
      | Service | Description | Price | Networks |
      |---------|-------------|-------|----------|
      | **Name** | description | $X USDC, $Y USDC | Base, Solana |
    """
    import re as _re

    cat_pat = _re.compile(r"^### (.+)$")
    row_pat = _re.compile(r"^\| \*\*(.+?)\*\* \| (.+?) \| (.+?) \| (.+?) \|")

    current_cat = "Other"
    services = []
    seen_ids = set()

    for line in md.split("\n"):
        cm = cat_pat.match(line)
        if cm:
            current_cat = cm.group(1).strip()
            continue

        rm = row_pat.match(line)
        if not rm:
            continue

        name        = rm.group(1).strip()
        description = rm.group(2).strip()
        prices_raw  = rm.group(3).strip()
        networks_raw = rm.group(4).strip()

        # Parse prices: match "$0.001" patterns (may be escaped as \$)
        price_strs = _re.findall(r"\$(\d+\.?\d*)", prices_raw)
        prices = []
        for p in price_strs:
            try:
                prices.append(float(p))
            except ValueError:
                pass

        price_min = min(prices) if prices else None
        price_max = max(prices) if len(prices) > 1 else None

        # Network: prefer base
        nets_lower = networks_raw.lower()
        if "base" in nets_lower:
            network = "base"
        elif "solana" in nets_lower:
            network = "solana"
        else:
            network = "other"

        # Build service ID: "agentic-" + slugified name
        svc_id = "agentic-" + slugify(name)
        if svc_id in seen_ids:
            continue
        seen_ids.add(svc_id)

        domain_tags = AGENTIC_DOMAIN_MAP.get(current_cat, ["Developer Tools"])
        svc_type    = AGENTIC_TYPE_MAP.get(current_cat, "API / Service")

        services.append({
            "id":               svc_id,
            "name":             name,
            "short_desc":       description[:160],
            "full_desc":        None,
            "website_url":      None,
            "api_base_url":     None,
            "endpoint_path":    None,
            "type":             svc_type,
            "domain_tags":      domain_tags,
            "logo_url":         None,
            "price_min":        price_min,
            "price_max":        price_max,
            "network":          network,
            "network_raw":      networks_raw,
            "token":            "USDC",
            "facilitator":      "cdp",
            "pay_to":           None,
            "verified":         0,
            "last_verified":    None,
            "verify_status":    "pending",
            "verify_failures":  0,
            "source":           "agentic-market",
            "submitted_by":     None,
            "submitted_at":     now_iso,
            "status":           "active",
            "quality_calls_30d":  None,
            "quality_payers_30d": None,
        })

    return services





# ---------------------------------------------------------------------------
# Service normalization
# ---------------------------------------------------------------------------

def normalize_service(item, now_iso):
    resource    = item.get("resource", "")
    description = item.get("description", "") or ""
    accepts     = item.get("accepts") or []
    quality     = item.get("quality") or {}
    last_updated = item.get("lastUpdated") or now_iso

    prices = []
    all_networks_raw = []
    pay_to_list = []

    is_testnet_only = True
    for a in accepts:
        network_raw = a.get("network", "")
        asset       = a.get("asset", "")
        amount      = a.get("amount", "")
        pay_to      = a.get("payTo", "")

        # Track whether any accept is on mainnet
        if network_raw and network_raw not in TESTNET_NETWORKS:
            is_testnet_only = False

        all_networks_raw.append(network_raw)
        usd = amount_to_usd(amount, asset)
        if usd is not None:
            prices.append(usd)
        if pay_to:
            pay_to_list.append(pay_to)

    # Skip testnet-only services
    if is_testnet_only and accepts:
        return None

    # Primary network -- prefer base, then first
    network      = "other"
    network_raw_val = ""
    for nr in all_networks_raw:
        n, _ = normalize_network(nr)
        if n == "base":
            network, network_raw_val = "base", nr
            break
    if network == "other" and all_networks_raw:
        network, network_raw_val = normalize_network(all_networks_raw[0])

    price_min = min(prices) if prices else None
    price_max = max(prices) if len(prices) > 1 else None

    try:
        parsed       = urllib.parse.urlparse(resource)
        api_base_url = f"{parsed.scheme}://{parsed.netloc}"
        endpoint_path = parsed.path if parsed.path and parsed.path != "/" else None
        if parsed.query:
            endpoint_path = (endpoint_path or "") + "?" + parsed.query
    except Exception:
        api_base_url   = resource
        endpoint_path  = None

    return {
        "id":               make_id(resource),
        "name":             extract_name(description, resource),
        "short_desc":       description[:160],
        "full_desc":        None,
        "website_url":      api_base_url,
        "api_base_url":     api_base_url,
        "endpoint_path":    endpoint_path,
        "type":             infer_type(description),
        "domain_tags":      infer_domain_tags(description),
        "logo_url":         None,
        "price_min":        price_min,
        "price_max":        price_max,
        "network":          network,
        "network_raw":      network_raw_val,
        "token":            "USDC",
        "facilitator":      "cdp",
        "pay_to":           pay_to_list[0] if pay_to_list else None,
        "verified":         0,
        "last_verified":    None,
        "verify_status":    "pending",
        "verify_failures":  0,
        "source":           "cdp-bazaar",
        "submitted_by":     None,
        "submitted_at":     last_updated,
        "status":           "active",
        "quality_calls_30d":  quality.get("l30DaysTotalCalls"),
        "quality_payers_30d": quality.get("l30DaysUniquePayers"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[harvest] Start: {now_iso}")

    # First page -- discover total
    try:
        data = fetch_page(0)
    except Exception as e:
        print(f"[harvest] FATAL: could not reach CDP Bazaar: {e}", file=sys.stderr)
        sys.exit(1)

    pagination = data.get("pagination", {})
    total      = pagination.get("total", 0)
    print(f"[harvest] CDP Bazaar total: {total}")

    services  = []
    seen_ids  = set()

    def add_items(items):
        for item in items:
            svc = normalize_service(item, now_iso)
            if svc is None:
                continue  # testnet-only, skip
            if svc["id"] not in seen_ids:
                seen_ids.add(svc["id"])
                services.append(svc)

    add_items(data.get("items", []))

    offset = PAGE_SIZE
    while offset < total:
        print(f"[harvest] Page offset={offset} ({len(services)} so far)...")
        try:
            page = fetch_page(offset)
            add_items(page.get("items", []))
        except Exception as e:
            print(f"[harvest] WARNING: offset={offset} failed: {e}", file=sys.stderr)
        offset += PAGE_SIZE

    cdp_count = len(services)
    print(f"[harvest] CDP Bazaar: {cdp_count} unique services")

    # ------------------------------------------------------------------
    # Agentic.market: fetch + merge curated brand services
    # ------------------------------------------------------------------
    print("[harvest] Fetching Agentic.market catalog...")
    agentic_md = fetch_agentic_markdown()
    agentic_added = 0
    if agentic_md:
        agentic_services = parse_agentic_markdown(agentic_md, now_iso)
        for svc in agentic_services:
            if svc["id"] not in seen_ids:
                seen_ids.add(svc["id"])
                services.append(svc)
                agentic_added += 1
        print(f"[harvest] Agentic.market: {len(agentic_services)} parsed, {agentic_added} new (non-duplicate)")
    else:
        print("[harvest] Agentic.market: skipped (fetch failed)")

    # Sort by quality (payers desc, then calls desc) so best services bubble up
    # Agentic entries have None quality so they sort to the end, behind CDP entries
    services.sort(
        key=lambda s: (
            -(s.get("quality_payers_30d") or 0),
            -(s.get("quality_calls_30d") or 0),
        )
    )

    output = {
        "generated_at": now_iso,
        "sources":      ["cdp-bazaar", "agentic-market"],
        "source":       "cdp-bazaar",  # kept for backwards compat
        "count":        len(services),
        "services":     services,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=True)

    print(f"[harvest] Done. {len(services)} unique services written to {OUTPUT_PATH}")
    print(f"[harvest]   CDP Bazaar:      {cdp_count}")
    print(f"[harvest]   Agentic.market:  {agentic_added}")


if __name__ == "__main__":
    main()
