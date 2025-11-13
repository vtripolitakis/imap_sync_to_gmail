# IMAP to Gmail Sync Tool
## Vangelis Tripolitakis - vtripolitakis@__NOSPAM__gmail.com

Simple utility to sync emails from any IMAP server to Gmail.

## Features

- Incremental sync (remembers last synced position)
- Batch processing (handles thousands of messages)
- Date filtering (sync only messages after a specific date)
- Multiple account support (use different config files)
- Preserves original message dates
- Resumable (can continue after interruption)

## Setup

1. Install dependencies:
```bash
pip install imapclient python-dotenv
```

2. Create a config file from the example:
```bash
cp .env.example .env
# Edit .env with your credentials
```

For Gmail, you need an App Password:
- Go to Google Account > Security > 2-Step Verification
- Scroll to "App passwords" and create one for "Mail"

## Usage

### Single account (default .env)
```bash
python3 imap_sync_to_gmail.py
```

### Multiple accounts with different configs
```bash
# Create config files for each account
cp account1.conf.example account1.conf
cp account2.conf.example account2.conf

# Edit each config file with credentials

# Run sync for each account
python3 imap_sync_to_gmail.py --config account1.conf
python3 imap_sync_to_gmail.py --config account2.conf
```

### With date filtering
```bash
# In your config file, uncomment and set:
# AFTER_DATE=2024-01-01

# Or pass via environment:
AFTER_DATE=2024-01-01 python3 imap_sync_to_gmail.py --config account1.conf
```

### Override Gmail label via CLI
```bash
# Override the label without editing config file
python3 imap_sync_to_gmail.py --config work.conf --gmail-label "Work/2024"

# Use nested labels (creates hierarchy in Gmail)
python3 imap_sync_to_gmail.py --config personal.conf --gmail-label "Archive/OldMail"
```

### Run as a cron job
```bash
# Edit crontab
crontab -e

# Add line to sync every hour:
0 * * * * cd /path/to/mailsync && python3 imap_sync_to_gmail.py --config account1.conf >> /var/log/mailsync.log 2>&1
```

## Configuration

All settings are via environment variables or config files:

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `SRC_IMAP_HOST` | Yes | Source IMAP server | `imap.example.com` |
| `SRC_IMAP_USER` | Yes | Source username | `user@example.com` |
| `SRC_IMAP_PASS` | Yes | Source password | `password123` |
| `SRC_FOLDER` | No | Source folder | `INBOX` (default) |
| `GMAIL_IMAP_HOST` | No | Gmail IMAP server | `imap.gmail.com` (default) |
| `GMAIL_USER` | Yes | Gmail address | `you@gmail.com` |
| `GMAIL_APP_PASS` | Yes | Gmail app password | `16-char-password` |
| `GMAIL_FOLDER` | No | Gmail label (use `/` for nested) | `Imported/FromServer` |
| `STATE_FILE` | No | State file path | `/var/tmp/state.json` |
| `AFTER_DATE` | No | Filter by date | `2024-01-01` |

## Troubleshooting

### Enable debug logging
Change in the script or set environment:
```bash
# In script: change level=logging.INFO to level=logging.DEBUG
# Or set via env:
LOG_LEVEL=DEBUG python3 imap_sync_to_gmail.py
```

### Common issues

**"Too long argument" error**: Fixed in current version with batching

**Authentication failed**: 
- For Gmail, use an App Password, not your regular password
- Check 2FA is enabled on Google Account

**State file issues**: Delete the state file to start fresh:
```bash
rm /var/tmp/imap_sync_state*.json
```

## Examples

### Sync work email to Gmail
```bash
# work.conf
SRC_IMAP_HOST=imap.company.com
SRC_IMAP_USER=john@company.com
SRC_IMAP_PASS=work-password
GMAIL_USER=john.personal@gmail.com
GMAIL_APP_PASS=app-password-here
GMAIL_FOLDER=Work/Company
STATE_FILE=/var/tmp/sync_work.json

# Run
python3 imap_sync_to_gmail.py --config work.conf
```

### Sync only recent messages
```bash
# recent.conf - include AFTER_DATE
AFTER_DATE=2024-06-01
# ... other settings ...

python3 imap_sync_to_gmail.py --config recent.conf
```

## License

MIT
