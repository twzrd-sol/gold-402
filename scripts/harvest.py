# scripts/harvest.py
# Gold-402: CDP Bazaar harvest pipeline
# Paginates the full CDP Bazaar catalog, normalizes to the Gold-402 schema,
# writes to directory/services.json.
# No external deps -- pure stdlib.

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CDP_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
PAGE_SIZE = 500
OUTPUT_PATH = "directory/services.json"
USER_AGENT = "Gold-402-Harvester/1.0 (https://24klabs.ai/gold-402)"

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
    url = f"{CDP_URL}?limit={PAGE_SIZE}&offset={offset}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


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

    # Sort by quality (payers desc, then calls desc) so best services bubble up
    services.sort(
        key=lambda s: (
            -(s.get("quality_payers_30d") or 0),
            -(s.get("quality_calls_30d") or 0),
        )
    )

    output = {
        "generated_at": now_iso,
        "source":       "cdp-bazaar",
        "count":        len(services),
        "services":     services,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=True)

    print(f"[harvest] Done. {len(services)} unique services written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
