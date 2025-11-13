#!/usr/bin/env python3
"""
Simple IMAP-to-Gmail synchronizer.

This script connects to a source IMAP server (server A), reads new messages from a
specified folder, and appends them into a Gmail folder (label) using Gmail's IMAP.

It keeps a tiny JSON state file that stores the last synced UID and the UIDVALIDITY
so it can resume across runs and detect when the source folder has been recreated
or reset (UIDVALIDITY change).

Notes / assumptions:
- This script copies raw RFC822 bytes; it does not rewrite message IDs or headers.
- Authentication uses straight username/password (for Gmail, use an app password).
- The script does minimal error handling — it's intended as a simple utility that
  can be adapted for production use (add retries, logging, dedup checks, etc.).
"""

import os
import json
import argparse
from datetime import datetime
from imapclient import IMAPClient
import logging

# =========================
# CONFIG
# =========================

# Small helper to load a local .env file (KEY=VALUE lines). We don't add a
# dependency on python-dotenv here to keep the script self-contained. If a
# variable is already present in the environment we do not overwrite it.


def load_dotenv(path: str = ".env"):
    """Read simple KEY=VALUE lines from `path` and export them to os.environ.

    Lines starting with # are ignored. This intentionally supports the
    straightforward .env usage for local testing and deployment.
    """
    if not os.path.exists(path):
        # Config file doesn't exist - we'll read from real environment
        return False
    
    try:
        with open(path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # Don't overwrite existing environment variables
                if key not in os.environ:
                    os.environ[key] = val
        return True
    except Exception as e:
        logger.error("Failed to load config file %s: %s", path, e)
        return False


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Sync emails from source IMAP to Gmail",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use default .env file
  python3 imap_sync_to_gmail.py
  
  # Use specific config file and override Gmail label
  python3 imap_sync_to_gmail.py --config account1.conf --gmail-label "Work/ImportedMail"
  
  # Multiple accounts with different configs and labels
  python3 imap_sync_to_gmail.py --config work.conf --gmail-label "Work/Inbox"
  python3 imap_sync_to_gmail.py --config personal.conf --gmail-label "Personal/Archive"
        """,
    )
    parser.add_argument(
        "-c",
        "--config",
        default=".env",
        help="Path to config file (default: .env)",
    )
    parser.add_argument(
        "-l",
        "--gmail-label",
        help="Gmail label to store messages (overrides GMAIL_FOLDER in config). "
        "Use '/' for nested labels, e.g., 'Imported/FromServer'",
    )
    return parser.parse_args()


# Parse CLI arguments first
args = parse_args()

# Configure console logging with a timestamped format. INFO is a sensible
# default; change to DEBUG for more verbose output during troubleshooting.
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Load config file (either default .env or specified via --config)
config_loaded = load_dotenv(args.config)
if config_loaded:
    logger.info("Loaded config from: %s", args.config)
else:
    logger.info("No config file found, using environment variables")

logger = logging.getLogger(__name__)


# Source IMAP (server A)
SRC_IMAP_HOST = os.environ.get("SRC_IMAP_HOST", "imap.source-server.com")
SRC_IMAP_USER = os.environ.get("SRC_IMAP_USER", "user@source-server.com")
SRC_IMAP_PASS = os.environ.get("SRC_IMAP_PASS", "your-source-password")
SRC_FOLDER = os.environ.get("SRC_FOLDER", "INBOX")  # folder to read from on server A

# Gmail IMAP
GMAIL_IMAP_HOST = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
GMAIL_USER = os.environ.get("GMAIL_USER", "your-gmail@gmail.com")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "your-16-char-app-password")

# Gmail label (Gmail uses labels, not folders - nested labels use /)
# Can be overridden via --gmail-label CLI argument
GMAIL_FOLDER = os.environ.get("GMAIL_FOLDER", "Imported/FromServerA")
if args.gmail_label:
    GMAIL_FOLDER = args.gmail_label
    logger.info("Using Gmail label from CLI: %s", GMAIL_FOLDER)

# State file to remember the last synced UID and UIDVALIDITY. You can override
# this via the environment by setting STATE_FILE to a different path.
# If using multiple configs, make the state file unique per config
STATE_FILE = os.environ.get("STATE_FILE", None)
if STATE_FILE is None:
    # Generate a unique state file name based on config file
    config_base = os.path.splitext(os.path.basename(args.config))[0]
    STATE_FILE = f"/var/tmp/imap_sync_state_{config_base}.json"

# Optional: Only sync messages after this date. Set AFTER_DATE in format
# YYYY-MM-DD (e.g., "2024-01-01") to filter messages. If not set, all messages
# matching the UID criteria will be synced.
AFTER_DATE = os.environ.get("AFTER_DATE", None)


# =========================
# STATE HANDLING
# =========================


def load_state():
    """Load the persistent state from STATE_FILE.

    Returns a dict. If the file doesn't exist or is invalid JSON an empty dict
    is returned. This keeps the script fault-tolerant if the state file is
    accidentally removed or corrupted.
    """
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            # If the file is unreadable, ignore and start fresh.
            return {}


def save_state(state):
    """Persist the provided state dict into STATE_FILE.

    We create the directory if necessary. This function intentionally keeps
    the format minimal and human-readable (JSON) so debugging is easy.
    """
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# =========================
# MAIN SYNC LOGIC
# =========================


def main():
    """Main entrypoint: connect to source IMAP, find new messages and append to Gmail.

    Contract:
    - Inputs: global CONFIG constants above (IMAP hosts, credentials, folders)
    - Outputs: messages appended to Gmail folder/label and state file updated
    - Error modes: network/auth failures raise exceptions (you can wrap main
      with retry logic if desired)

    Edge cases considered:
    - UIDVALIDITY change on source folder (folder recreated) -> we reset last_uid
    - Corrupt/missing state file -> treated as empty state and we sync ALL
    - No new messages -> state is updated with latest UIDVALIDITY and exit
    """
    state = load_state()
    last_uid = state.get("last_uid", 0)
    last_uidvalidity = state.get("uidvalidity")

    # ----- Connect to source IMAP -----
    logger.info("Connecting to source IMAP %s...", SRC_IMAP_HOST)
    with IMAPClient(SRC_IMAP_HOST, ssl=True) as src:
        src.login(SRC_IMAP_USER, SRC_IMAP_PASS)
        # Select readonly to avoid marking messages as seen on the source
        src.select_folder(SRC_FOLDER, readonly=True)

        # Fetch the folder's UIDVALIDITY so we can detect if UIDs were reset.
        status = src.folder_status(SRC_FOLDER, [b"UIDVALIDITY"])
        uidvalidity = int(status[b"UIDVALIDITY"])

        if last_uidvalidity is None or uidvalidity != last_uidvalidity:
            # If UIDVALIDITY changed we must assume UIDs may be different now,
            # so we reset progress to avoid skipping or incorrectly mapping
            # messages.
            logger.warning("UIDVALIDITY changed or no previous state; resetting last_uid.")
            last_uid = 0

        # Build IMAP search criteria. If we have never synced before (last_uid=0)
        # we fetch ALL messages. Otherwise request messages with UID > last_uid.
        if last_uid == 0:
            uid_search_criteria = ["ALL"]
        else:
            uid_search_criteria = ["UID", f"{last_uid+1}:*"]
        
        # If AFTER_DATE is set, add a date filter to only sync messages
        # received on or after the specified date. Format: YYYY-MM-DD
        if AFTER_DATE:
            try:
                # Parse the date and format it for IMAP SINCE command
                # IMAP SINCE uses format: DD-Mon-YYYY (e.g., "01-Jan-2024")
                from datetime import datetime as dt
                after_dt = dt.strptime(AFTER_DATE, "%Y-%m-%d")
                imap_date = after_dt.strftime("%d-%b-%Y")
                
                # Combine UID criteria with date filter using AND logic
                if uid_search_criteria == ["ALL"]:
                    uid_search_criteria = ["SINCE", imap_date]
                else:
                    # For UID range + date, we need to use IMAPClient's
                    # search which accepts criteria as a list
                    uid_search_criteria.extend(["SINCE", imap_date])
                
                logger.info("Filtering messages after date: %s", AFTER_DATE)
            except ValueError as e:
                logger.error(
                    "Invalid AFTER_DATE format '%s'. Use YYYY-MM-DD. Error: %s",
                    AFTER_DATE,
                    e,
                )
                return

        # Search returns a list of UIDs matching the criteria
        uids = src.search(uid_search_criteria)
        uids = sorted(uids)
        
        # Filter out any UIDs we've already processed
        # This ensures we don't re-sync the last message
        uids = [uid for uid in uids if uid > last_uid]

        if not uids:
            # Nothing new to fetch; still update uidvalidity in case it changed
            logger.info("No new messages to sync.")
            state["uidvalidity"] = uidvalidity
            save_state(state)
            return

        logger.info("Found %d new messages to sync.", len(uids))

        # ----- Connect to Gmail -----
        logger.info("Connecting to Gmail %s...", GMAIL_IMAP_HOST)
        with IMAPClient(GMAIL_IMAP_HOST, ssl=True) as gmail:
            gmail.login(GMAIL_USER, GMAIL_APP_PASS)

            # Ensure destination label exists on Gmail. Gmail uses labels (not
            # folders), though IMAP refers to them as folders. Creating a label
            # like "Imported/FromServer" will appear as nested labels in Gmail UI.
            try:
                gmail.create_folder(GMAIL_FOLDER)
                logger.info("Created Gmail label: %s", GMAIL_FOLDER)
            except Exception as e:
                # Label probably already exists; log at debug level
                logger.debug("create_folder failed (might already exist): %s", e)

            gmail.select_folder(GMAIL_FOLDER)

            # Process messages in batches to avoid "too long argument" errors
            # when dealing with thousands of UIDs. We fetch and append in
            # chunks, saving state after each batch for resumability.
            BATCH_SIZE = 100
            total_uids = len(uids)
            
            for batch_start in range(0, total_uids, BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, total_uids)
                batch_uids = uids[batch_start:batch_end]
                
                logger.info(
                    "Processing batch %d-%d of %d messages...",
                    batch_start + 1,
                    batch_end,
                    total_uids,
                )
                
                # Fetch the full message (RFC822) and INTERNALDATE for this batch
                # which we'll use when appending to Gmail so the original date/time
                # is preserved. Optionally, you can also fetch flags (b'FLAGS')
                # if you want to preserve read/seen state.
                fetch_data = src.fetch(batch_uids, [b"RFC822", b"INTERNALDATE"])
                
                for uid in batch_uids:
                    msg_bytes = fetch_data[uid][b"RFC822"]
                    internaldate = fetch_data[uid][b"INTERNALDATE"]

                    # If you need to inspect or modify headers you can parse the
                    # message here. We keep raw bytes to preserve original headers,
                    # message-ids, MIME structure, etc.
                    # msg = BytesParser(policy=default_policy).parsebytes(msg_bytes)

                    logger.debug(
                        "Appending message UID %s (date: %s) to Gmail...",
                        uid,
                        internaldate,
                    )
                    # Append the raw message bytes into the Gmail folder. We pass
                    # an empty flags list here; add flags (e.g. ['\Seen']) if
                    # you want the messages to appear read in Gmail.
                    # The msg_time parameter preserves the original INTERNALDATE.
                    gmail.append(GMAIL_FOLDER, msg_bytes, flags=[], msg_time=internaldate)
                
                # Save state after each batch so we can resume if interrupted
                batch_last_uid = batch_uids[-1]
                state["last_uid"] = batch_last_uid
                state["uidvalidity"] = uidvalidity
                save_state(state)
                logger.info("Batch complete. Progress saved (last UID: %s).", batch_last_uid)

            # Final state update with the highest UID processed
            new_last_uid = max(uids)
            state["last_uid"] = new_last_uid
            state["uidvalidity"] = uidvalidity
            save_state(state)

            logger.info("Sync complete. Last UID now %s.", new_last_uid)


if __name__ == "__main__":
    main()
