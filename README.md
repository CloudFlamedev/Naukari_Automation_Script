# Naukri Skill Updater

A focused Python automation tool that manages the **Key Skills** section of your Naukri.com profile twice daily — adding one skill in the morning and removing one in the evening — using a configurable approved list.

> [!IMPORTANT]
> This tool only touches the **Key Skills** section. It never modifies your resume, headline, employment details, salary, notice period, or any other profile section.

> [!NOTE]
> Naukri's website structure changes occasionally. If a session fails unexpectedly, check the [Troubleshooting](#troubleshooting) section and inspect the page selectors in [`browser.py`](browser.py).

---

## Project Structure

```
naukri-skill-updater/
├── main.py          # Entry point and session logic
├── browser.py       # Playwright browser session management
├── skills.json      # Your skill configuration (edit this)
├── scheduler.sh     # Cron job installer/remover
├── requirements.txt # Python dependencies
├── README.md        # This file
├── logs/            # Execution logs (auto-created)
├── browser_profile/ # Persistent browser session (auto-created on first login)
└── state.json       # Round-robin skill rotation state (auto-created)
```

---

## Quick Start

### 1 — Prerequisites

```bash
# Ensure Python 3.10+ and pip are available
python3 --version
pip3 --version

# Install cron (usually pre-installed on Ubuntu)
which crontab
```

### 2 — Set Up the Virtual Environment

```bash
cd ~/path/to/naukri-skill-updater

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

# Install the Playwright Chromium browser
python -m playwright install chromium
python -m playwright install-deps chromium
```

### 3 — Configure Your Skills

Edit [`skills.json`](skills.json) to match **your actual skills**. The default values are placeholders:

```json
{
  "approved_skills_to_add": [
    "Python", "SQL", "AWS"
  ],
  "approved_skills_to_remove": [
    "LangGraph"
  ],
  "core_skills_to_preserve": [
    "Python", "SQL"
  ]
}
```

| Key | Purpose |
|---|---|
| `approved_skills_to_add` | Pool of skills the morning session may add (one per run, round-robin) |
| `approved_skills_to_remove` | Skills the evening session may remove (only if present and not core) |
| `core_skills_to_preserve` | Skills that will **never** be removed, no matter what |

> [!CAUTION]
> Only list skills you genuinely possess. Do not add skills you do not have.

### 4 — Log In Manually (First Time)

```bash
source venv/bin/activate
python main.py login
```

A Chromium browser window opens. Log in to Naukri.com as you normally would. The session is saved to `browser_profile/` and reused for future automated runs. Press **Ctrl+C** in the terminal when done.

---

## Running the Tool

All commands must be run from the project directory with the virtual environment active (`source venv/bin/activate`).

### Check Current Skills (Read-Only)

```bash
python main.py status
```

### Dry Run (Simulate Without Making Changes)

Test the morning logic:
```bash
python main.py morning --dry-run
```

Test the evening logic:
```bash
python main.py evening --dry-run
```

The dry run logs every decision but makes zero profile changes.

### Live Run (Single Session)

Add a skill (morning):
```bash
python main.py morning
```

Remove a skill (evening):
```bash
python main.py evening
```

---

## Scheduling (Cron)

> [!NOTE]
> The cron jobs run at **9:00 AM IST** (03:30 UTC) and **7:00 PM IST** (13:30 UTC). Your system clock must be set correctly; the `TZ=Asia/Kolkata` prefix ensures correct handling regardless of the system timezone.

### Install Cron Jobs

```bash
bash scheduler.sh install
```

### Verify Installation

```bash
bash scheduler.sh status
# or
crontab -l
```

### Remove Cron Jobs

```bash
bash scheduler.sh remove
```

### Monitor Execution

```bash
# Watch the cron output log in real time
tail -f logs/cron.log

# List detailed daily log files
ls -lh logs/

# View today's morning log
cat logs/$(date +%Y-%m-%d)_morning.log
```

---

## Logs

Each run creates a log file in `logs/` named `YYYY-MM-DD_morning.log` or `YYYY-MM-DD_evening.log`. Each log entry records:

- Timestamp of execution
- Session type (morning / evening)
- Skill selected for addition or removal
- Whether the operation succeeded, was skipped, or failed
- Any errors encountered

Passwords, authentication tokens, and session cookies are **never** logged.

---

## How It Works

```
Morning run
  └─ Open persistent browser profile
  └─ Check login state → wait for manual login if needed
  └─ Navigate to profile Key Skills section
  └─ Read current skills
  └─ Pick next skill from approved_skills_to_add (round-robin, skip if already present)
  └─ Type skill into input → select suggestion → Save
  └─ Re-read skills to verify addition
  └─ Log result

Evening run
  └─ Open persistent browser profile
  └─ Check login state
  └─ Navigate to profile Key Skills section
  └─ Read current skills
  └─ Pick first skill from approved_skills_to_remove that is present and not core
  └─ Safety check: abort if skill is in core_skills_to_preserve
  └─ Click delete on skill chip → Save
  └─ Re-read skills to verify removal
  └─ Log result
```

### Safety Mechanisms

| Mechanism | Description |
|---|---|
| Core skill guard | Skills in `core_skills_to_preserve` are **never** removed |
| Approved list only | Only skills explicitly in `approved_skills_to_remove` can be removed |
| Duplicate prevention | A skill is not re-added if it already exists on the profile |
| CAPTCHA / OTP detection | The session stops and asks you to complete verification manually |
| File lock | Only one instance runs at a time; concurrent runs are rejected |
| DRY_RUN mode | All logic runs but no page changes are made |
| No other sections touched | Selectors target only the Key Skills widget |

---

## Troubleshooting

### Session Expired / Not Logged In

Re-run the manual login:
```bash
python main.py login
```

### CAPTCHA or OTP Required

The tool will stop and print a message. Open the browser profile manually, solve the challenge, then re-run.

### Selectors Broken (Naukri Updated Its Page)

1. Open Chromium with the profile:
   ```bash
   python main.py login
   # Navigate to your profile page manually
   ```
2. Use browser DevTools (F12) to inspect the Key Skills section.
3. Update the `_SEL` dictionary in [`browser.py`](browser.py) with the new selectors.

### Lock File Stale After a Crash

```bash
rm naukri_updater.lock
```

### Cron Job Not Running

1. Ensure cron is running: `systemctl status cron`
2. Check the cron output log: `tail -50 logs/cron.log`
3. Verify the venv path in the cron entry: `crontab -l`
4. Test manually first: `source venv/bin/activate && python main.py morning`

### "No module named playwright" in Cron

The cron entry uses the full path to the venv Python (`venv/bin/python`), which includes all installed packages. If it still fails, verify:
```bash
./venv/bin/python -c "import playwright; print('OK')"
```

### Browser Visible During Cron Run

The browser opens in headed (visible) mode so you can see what is happening and intervene if needed. If you want headless mode (no window), change `headless=False` to `headless=True` in the `NaukriBrowser(...)` calls inside [`main.py`](main.py). Note that headless mode may trigger bot-detection on some sites.

---

## Stopping the Automation

```bash
# Remove cron jobs
bash scheduler.sh remove

# Optionally delete the lock file if a session is stuck
rm -f naukri_updater.lock
```

---

## A Note on Recruiter Visibility

Naukri's internal ranking and recruiter search algorithms are proprietary and not publicly documented. This tool does **not** claim that updating skills twice daily improves your recruiter ranking or search visibility. It is designed purely to automate the mechanical task of managing your skills list according to your own configuration.

---

## Dependencies

| Package | Purpose |
|---|---|
| `playwright` | Browser automation (Chromium) |
| `filelock` | Prevents overlapping runs |

All dependencies are open-source and free of charge.
