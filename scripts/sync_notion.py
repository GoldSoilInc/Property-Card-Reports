#!/usr/bin/env python3
"""
Sync Notion meeting notes to data/meetings.json for the GoldSoil dashboard.

Run by .github/workflows/sync-notion.yml every 30 minutes.
- NOTION_TOKEN comes from a GH Secret
- NOTION_DATABASE_ID and NOTION_TRANSACTION_PROPERTY come from GH Variables

Stdlib only — no pip installs needed in the workflow.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
TRANSACTION_PROPERTY = os.environ.get("NOTION_TRANSACTION_PROPERTY", "Transaction #")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "data/notion-meetings.json")

# Meeting types to extract — matched as a prefix of the toggle's plain-text label.
# Edit this list if you start tracking other meeting types in the same way.
MEETING_TYPES = ("Pricing Meeting", "Portfolio Review")

MAX_BULLETS_PER_MEETING = 8
RECURSION_DEPTH = 3  # How deep to look for meeting toggles inside containers


# ---------- Notion API helpers ----------

def notion_request(method, path, body=None):
    url = f"{NOTION_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {NOTION_TOKEN}")
    req.add_header("Notion-Version", NOTION_VERSION)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"Notion API {e.code} on {method} {path}: {body_text}", file=sys.stderr)
        raise
    except URLError as e:
        print(f"Network error on {method} {path}: {e}", file=sys.stderr)
        raise


def query_database():
    """Fetch all pages from the Notion database with pagination."""
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = notion_request("POST", f"/databases/{DATABASE_ID}/query", body)
        pages.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return pages


def fetch_blocks(block_id):
    """Fetch all child blocks of a page or block, with pagination."""
    blocks, cursor = [], None
    while True:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        resp = notion_request("GET", path)
        blocks.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return blocks


# ---------- Property extraction ----------

def get_transaction_value(page):
    """Pull the Transaction # value off a database page property."""
    prop = page.get("properties", {}).get(TRANSACTION_PROPERTY)
    if not prop:
        return None
    t = prop.get("type")
    if t == "title":
        return "".join(r.get("plain_text", "") for r in prop.get("title", [])).strip() or None
    if t == "rich_text":
        return "".join(r.get("plain_text", "") for r in prop.get("rich_text", [])).strip() or None
    if t == "number":
        n = prop.get("number")
        return str(n) if n is not None else None
    if t == "select":
        sel = prop.get("select")
        return sel.get("name") if sel else None
    if t == "formula":
        f = prop.get("formula", {})
        if f.get("type") == "string":
            return (f.get("string") or "").strip() or None
        if f.get("type") == "number" and f.get("number") is not None:
            return str(f.get("number"))
    return None


# ---------- Block parsing ----------

def rich_text_to_plain(rich_text):
    return "".join(r.get("plain_text", "") for r in rich_text or [])


def block_to_text(block):
    """Get plain-text content of a block, ignoring formatting."""
    btype = block.get("type")
    data = block.get(btype, {}) if btype else {}
    return rich_text_to_plain(data.get("rich_text", []))


def is_meeting_toggle(block):
    """Return the meeting type if this is a toggle whose label starts with one. Else None.
    Tolerant of leading emojis, whitespace, and other decorative non-letter prefixes —
    e.g. '📊 Pricing Meeting — 2026-04-28' still matches 'Pricing Meeting'.
    """
    if block.get("type") != "toggle":
        return None
    text = block_to_text(block).strip()
    # Strip any leading characters that aren't letters/digits — emojis, symbols, extra spaces.
    stripped = re.sub(r"^[^A-Za-z0-9]+", "", text)
    for mt in MEETING_TYPES:
        if stripped.startswith(mt):
            return mt
    return None


def parse_meeting_date(label, expected_type):
    """Extract a date string from 'Pricing Meeting — 2026-04-28' style labels.
    Tolerant of leading emojis/symbols/whitespace before the meeting type."""
    stripped = re.sub(r"^[^A-Za-z0-9]+", "", label)
    rest = stripped[len(expected_type):].strip(" -—:·")
    m = re.search(r"\d{4}-\d{2}-\d{2}", rest)
    if m:
        return m.group(0)
    m = re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", rest)
    if m:
        return m.group(0)
    return rest or None


def find_meeting_blocks(blocks, depth=0, max_depth=RECURSION_DEPTH):
    """Walk blocks (and children up to max_depth) and return [(block, type), ...] for meetings."""
    found = []
    for block in blocks:
        mt = is_meeting_toggle(block)
        if mt:
            found.append((block, mt))
            continue  # don't recurse into the meeting itself looking for nested meetings
        if depth < max_depth and block.get("has_children"):
            try:
                children = fetch_blocks(block["id"])
                found.extend(find_meeting_blocks(children, depth + 1, max_depth))
            except Exception as e:
                print(f"    Failed to fetch children of {block.get('id')}: {e}", file=sys.stderr)
    return found


def extract_bullets(blocks, depth=0, bullets=None):
    """Walk children of a meeting toggle and pull bullet/numbered/to-do items + short paragraphs."""
    if bullets is None:
        bullets = []
    for b in blocks:
        if len(bullets) >= MAX_BULLETS_PER_MEETING:
            break
        btype = b.get("type")
        text = block_to_text(b).strip()
        if not text:
            # Still recurse — sometimes content lives inside an empty wrapper
            if b.get("has_children") and depth < 2:
                try:
                    extract_bullets(fetch_blocks(b["id"]), depth + 1, bullets)
                except Exception:
                    pass
            continue
        if btype in ("bulleted_list_item", "numbered_list_item", "to_do"):
            bullets.append(text)
        elif btype in ("heading_1", "heading_2", "heading_3", "callout", "quote"):
            bullets.append(text)
        elif btype == "paragraph":
            # Short paragraph → one bullet. Long → first sentence as a preview.
            if len(text) <= 200:
                bullets.append(text)
            else:
                first = re.split(r"(?<=[.!?])\s+", text, 1)[0]
                bullets.append(first if len(first) <= 200 else (text[:197] + "…"))
        # Recurse into containers (toggles, columns, etc.) to grab nested bullets
        if b.get("has_children") and len(bullets) < MAX_BULLETS_PER_MEETING and depth < 2:
            try:
                extract_bullets(fetch_blocks(b["id"]), depth + 1, bullets)
            except Exception as e:
                print(f"      Failed to recurse into {b.get('id')}: {e}", file=sys.stderr)
    return bullets


# ---------- Page processing ----------

def page_url(page):
    pid = page["id"].replace("-", "")
    return page.get("url") or f"https://www.notion.so/{pid}"


def process_page(page):
    """Return (transaction_id, meetings_list) for a single Notion page."""
    txn = get_transaction_value(page)
    if not txn:
        return None, []

    blocks = fetch_blocks(page["id"])
    meeting_blocks = find_meeting_blocks(blocks)
    print(f"  {txn}: {len(meeting_blocks)} meeting toggle(s)", file=sys.stderr)

    meetings = []
    for block, mt in meeting_blocks:
        label = block_to_text(block).strip()
        date = parse_meeting_date(label, mt)
        try:
            children = fetch_blocks(block["id"])
            bullets = extract_bullets(children)
        except Exception as e:
            print(f"    Failed to load meeting body for {block['id']}: {e}", file=sys.stderr)
            bullets = []
        meetings.append({
            "type": mt,
            "date": date,
            "label": label,
            "bullets": bullets[:MAX_BULLETS_PER_MEETING],
            "blockId": block["id"],
        })

    # Newest first by date string (ISO sorts naturally; non-ISO falls to bottom).
    meetings.sort(key=lambda m: (m.get("date") or ""), reverse=True)
    return txn, meetings


# ---------- Main ----------

def main():
    print(f"Querying Notion database {DATABASE_ID}…", file=sys.stderr)
    pages = query_database()
    print(f"Found {len(pages)} pages.", file=sys.stderr)

    by_txn = {}
    for page in pages:
        try:
            txn, meetings = process_page(page)
        except Exception as e:
            print(f"  Failed page {page.get('id')}: {e}", file=sys.stderr)
            continue
        if not txn or not meetings:
            continue
        by_txn[txn] = {
            "notionPageId": page["id"],
            "notionUrl": page_url(page),
            "meetings": meetings,
        }

    output = {
        "lastSynced": datetime.now(timezone.utc).isoformat(),
        "transactionPropertyName": TRANSACTION_PROPERTY,
        "databaseId": DATABASE_ID,
        "byTransaction": by_txn,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(by_txn)} transactions with meetings to {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
