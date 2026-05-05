#!/usr/bin/env python3
"""
Ingest a single Fireflies meeting into the Notion 'Fireflies Meetings' inbox database.

Triggered by .github/workflows/ingest-fireflies-meeting.yml when Make.com sends a
repository_dispatch with a Fireflies meeting ID. The script:
  1. Fetches the full transcript from Fireflies (sentences → joined text)
  2. Checks the Notion inbox for an existing row with the same meeting ID — skip if found
  3. Creates a new row with title, date, duration, host, transcript URL, and full transcript
The Notion Agent fires automatically when the row appears, fanning out to per-transaction pages.

Stdlib only — no pip installs.
"""

import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------- Config ----------

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
FIREFLIES_BASE = "https://api.fireflies.ai/graphql"

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
FIREFLIES_API_KEY = os.environ["FIREFLIES_API_KEY"]
INBOX_DATABASE_ID = os.environ["FIREFLIES_INBOX_DATABASE_ID"]
MEETING_ID = os.environ.get("MEETING_ID", "").strip()

# Property names on the Notion inbox database. Override via env if your column names differ.
PROP_TITLE       = os.environ.get("PROP_TITLE",       "Name")
PROP_TRANSCRIPT  = os.environ.get("PROP_TRANSCRIPT",  "Full Transcript")
PROP_MEETING_ID  = os.environ.get("PROP_MEETING_ID",  "Fireflies ID")
PROP_DATE        = os.environ.get("PROP_DATE",        "Meeting Date")
PROP_DURATION    = os.environ.get("PROP_DURATION",    "Duration")
PROP_HOST        = os.environ.get("PROP_HOST",        "Host")
PROP_URL         = os.environ.get("PROP_URL",         "Fireflies URL")

# Notion has a hard limit of 2000 characters per rich_text element. We split longer
# transcripts into multiple chunks and pass them as a list of rich_text elements.
NOTION_RICH_TEXT_CHUNK = 2000


# ---------- HTTP helpers ----------

def http_request(method, url, headers, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} on {method} {url}: {body_text}", file=sys.stderr)
        raise
    except URLError as e:
        print(f"Network error on {method} {url}: {e}", file=sys.stderr)
        raise


# ---------- Fireflies ----------

def fireflies_query(query, variables=None):
    headers = {
        "Authorization": f"Bearer {FIREFLIES_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {"query": query, "variables": variables or {}}
    resp = http_request("POST", FIREFLIES_BASE, headers, body)
    if "errors" in resp:
        raise RuntimeError(f"Fireflies GraphQL errors: {json.dumps(resp['errors'])}")
    return resp.get("data", {})


def fetch_meeting(meeting_id):
    """Fetch a Fireflies transcript including sentences, attendees, host, date, etc."""
    query = """
    query Transcript($id: String!) {
      transcript(id: $id) {
        id
        title
        date
        duration
        transcript_url
        host_email
        organizer_email
        participants
        meeting_attendees { displayName email }
        sentences { text speaker_name }
      }
    }
    """
    data = fireflies_query(query, {"id": meeting_id})
    t = data.get("transcript")
    if not t:
        raise RuntimeError(f"Fireflies returned no transcript for id={meeting_id}")
    return t


def assemble_transcript(transcript):
    """Join all sentences into one big block of text, with speaker labels."""
    sentences = transcript.get("sentences") or []
    if not sentences:
        return ""
    lines, last_speaker = [], None
    for s in sentences:
        speaker = (s.get("speaker_name") or "").strip()
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if speaker and speaker != last_speaker:
            lines.append(f"\n{speaker}: {text}")
            last_speaker = speaker
        else:
            lines.append(text)
    return " ".join(lines).strip()


def fireflies_date_to_iso(ts):
    """Fireflies returns date as a unix epoch in milliseconds. Convert to ISO."""
    if ts is None:
        return None
    try:
        ts = int(ts)
        # Heuristic: if value looks like ms, divide
        if ts > 10**12:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


# ---------- Notion ----------

def notion_request(method, path, body=None):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    return http_request(method, f"{NOTION_BASE}{path}", headers, body)


def find_existing_row(meeting_id):
    """Return the page object if a row with this Fireflies ID already exists. Else None."""
    body = {
        "filter": {
            "property": PROP_MEETING_ID,
            "rich_text": {"equals": meeting_id},
        },
        "page_size": 1,
    }
    try:
        resp = notion_request("POST", f"/databases/{INBOX_DATABASE_ID}/query", body)
        results = resp.get("results", [])
        return results[0] if results else None
    except HTTPError as e:
        # If the meeting-id property doesn't exist or has the wrong type, fall through —
        # we'd rather risk a duplicate than fail to ingest.
        print(f"  WARNING: dedupe query failed ({e}); proceeding without dedupe", file=sys.stderr)
        return None


# Window for near-duplicate detection. When the same meeting has multiple Fireflies
# notetakers attending, you get two transcripts with DIFFERENT IDs but the SAME title
# and near-identical timestamps. We catch those by looking for rows with the same
# title whose date is within NEAR_DUPLICATE_WINDOW_MIN minutes of this meeting's start.
NEAR_DUPLICATE_WINDOW_MIN = 5

def find_near_duplicate(title, iso_date):
    """Return an existing page if there's a row with the same Title within the ±5 min
    window of `iso_date`. Used to skip duplicates from multiple Fireflies notetakers
    in the same meeting."""
    if not title or not iso_date:
        return None
    try:
        # Build the window: meeting time ± NEAR_DUPLICATE_WINDOW_MIN minutes.
        center = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        from datetime import timedelta
        before = (center + timedelta(minutes=NEAR_DUPLICATE_WINDOW_MIN)).isoformat()
        after = (center - timedelta(minutes=NEAR_DUPLICATE_WINDOW_MIN)).isoformat()
    except Exception as e:
        print(f"  WARNING: couldn't parse date for near-dup check ({e})", file=sys.stderr)
        return None

    body = {
        "filter": {
            "and": [
                {"property": PROP_TITLE, "title": {"equals": title}},
                {"property": PROP_DATE, "date": {"on_or_after": after}},
                {"property": PROP_DATE, "date": {"on_or_before": before}},
            ]
        },
        "page_size": 1,
    }
    try:
        resp = notion_request("POST", f"/databases/{INBOX_DATABASE_ID}/query", body)
        results = resp.get("results", [])
        return results[0] if results else None
    except HTTPError as e:
        print(f"  WARNING: near-duplicate query failed ({e}); proceeding without that check", file=sys.stderr)
        return None


def chunk_text(text, size=NOTION_RICH_TEXT_CHUNK):
    """Split text into <=size character chunks for Notion rich_text array."""
    if not text:
        return [""]
    return [text[i:i + size] for i in range(0, len(text), size)]


def rich_text_array(text):
    """Convert long text into a list of {type:'text', text:{content:...}} chunks."""
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunk_text(text)]


def build_properties(transcript, full_text):
    """Assemble the Notion property payload. Empty values are omitted, not nulled."""
    props = {}

    title = transcript.get("title") or f"Meeting {transcript.get('id')}"
    props[PROP_TITLE] = {"title": [{"type": "text", "text": {"content": title[:2000]}}]}

    if full_text:
        props[PROP_TRANSCRIPT] = {"rich_text": rich_text_array(full_text)}

    props[PROP_MEETING_ID] = {"rich_text": [{"type": "text", "text": {"content": transcript["id"]}}]}

    iso_date = fireflies_date_to_iso(transcript.get("date"))
    if iso_date:
        props[PROP_DATE] = {"date": {"start": iso_date}}

    duration = transcript.get("duration")
    if PROP_DURATION and isinstance(duration, (int, float)):
        props[PROP_DURATION] = {"number": float(duration)}

    host = transcript.get("organizer_email") or transcript.get("host_email")
    if host:
        props[PROP_HOST] = {"rich_text": [{"type": "text", "text": {"content": host}}]}

    url = transcript.get("transcript_url")
    if url:
        props[PROP_URL] = {"url": url}

    return props


def create_row(transcript, full_text):
    body = {
        "parent": {"database_id": INBOX_DATABASE_ID},
        "properties": build_properties(transcript, full_text),
    }
    resp = notion_request("POST", "/pages", body)
    return resp


# ---------- Main ----------

def main():
    if not MEETING_ID:
        print("ERROR: MEETING_ID env var is empty. Make.com should pass it via repository_dispatch payload.", file=sys.stderr)
        sys.exit(1)

    print(f"Ingesting Fireflies meeting {MEETING_ID}", file=sys.stderr)

    # Layer 1: exact Fireflies ID match — catches retries of the same notetaker
    existing = find_existing_row(MEETING_ID)
    if existing:
        print(f"  Already in Notion (page {existing['id']}) — skipping (Fireflies ID match).", file=sys.stderr)
        return

    transcript = fetch_meeting(MEETING_ID)
    full_text = assemble_transcript(transcript)
    title = transcript.get("title") or f"Meeting {transcript['id']}"
    iso_date = fireflies_date_to_iso(transcript.get("date"))
    print(f"  Title: {title!r}", file=sys.stderr)
    print(f"  Date: {iso_date}", file=sys.stderr)
    print(f"  Sentences: {len(transcript.get('sentences') or [])}, transcript chars: {len(full_text)}", file=sys.stderr)

    # Layer 2: same title + date within ±5 min — catches duplicate notetakers in the same meeting
    near_dup = find_near_duplicate(title, iso_date)
    if near_dup:
        print(f"  Near-duplicate found (page {near_dup['id']}) — same title within ±{NEAR_DUPLICATE_WINDOW_MIN} min. Skipping.", file=sys.stderr)
        return

    page = create_row(transcript, full_text)
    print(f"  Created Notion page {page['id']} — agent should fire shortly.", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
