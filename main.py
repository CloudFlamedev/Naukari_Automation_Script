"""
main.py — Entry point for Naukri Skill Updater.

Usage:
    python main.py morning              # Run morning (add) session
    python main.py evening              # Run evening (remove) session
    python main.py morning --dry-run    # Simulate morning session, no changes
    python main.py evening --dry-run    # Simulate evening session, no changes
    python main.py login                # Open browser for manual login only
    python main.py status               # Show current skills on profile (read-only)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from filelock import FileLock, Timeout

from browser import NaukriBrowser, get_profile_dir

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.resolve()
SKILLS_FILE = BASE_DIR / "skills.json"
LOG_DIR = BASE_DIR / "logs"
LOCK_FILE = BASE_DIR / "naukri_updater.lock"
STATE_FILE = BASE_DIR / "state.json"  # tracks which skill was last added
PROFILE_DIR = BASE_DIR / "browser_profile"

LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(session: str) -> None:
    """Configure file + console logging for the current session."""
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{today}_{session}.log"

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Suppress playwright internal noise below WARNING.
    logging.getLogger("playwright").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load and validate skills.json."""
    if not SKILLS_FILE.exists():
        sys.exit(f"ERROR: Configuration file not found: {SKILLS_FILE}")
    with SKILLS_FILE.open(encoding="utf-8") as f:
        cfg = json.load(f)

    required_keys = [
        "approved_skills_to_add",
        "approved_skills_to_remove",
        "core_skills_to_preserve",
    ]
    for key in required_keys:
        if key not in cfg:
            sys.exit(f"ERROR: Missing required key '{key}' in skills.json")
        if not isinstance(cfg[key], list):
            sys.exit(f"ERROR: '{key}' must be a list in skills.json")

    return cfg


def load_state() -> dict:
    """Load rotation state (which skill was last added)."""
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open(encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_added_index": -1}


def save_state(state: dict) -> None:
    """Persist rotation state to disk."""
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def pick_skill_to_add(cfg: dict, current_skills: list[str]) -> str | None:
    """
    Pick the next skill from approved_skills_to_add that is not already
    on the profile, using round-robin rotation.
    Returns None if all approved skills are already present.
    """
    strategy = cfg.get("settings", {}).get("skill_add_strategy", "round_robin")
    candidates = [s for s in cfg["approved_skills_to_add"] if s not in current_skills]

    if not candidates:
        logger.info("All approved skills are already on the profile.")
        return None

    if strategy == "random":
        return random.choice(candidates)

    # Round-robin: pick the candidate whose index in approved_skills_to_add comes
    # after the last added index.
    state = load_state()
    approved = cfg["approved_skills_to_add"]
    last_idx = state.get("last_added_index", -1)

    # Walk forward from last_idx + 1 through the approved list.
    for offset in range(1, len(approved) + 1):
        idx = (last_idx + offset) % len(approved)
        skill = approved[idx]
        if skill in candidates:
            state["last_added_index"] = idx
            save_state(state)
            return skill

    return random.choice(candidates)  # fallback


def pick_skill_to_remove(cfg: dict, current_skills: list[str]) -> str | None:
    """
    Pick a skill from approved_skills_to_remove that IS on the profile
    and is NOT in core_skills_to_preserve.
    Returns None if nothing safe to remove.
    """
    core = set(cfg["core_skills_to_preserve"])
    removable = [
        s for s in cfg["approved_skills_to_remove"]
        if s in current_skills and s not in core
    ]
    if not removable:
        logger.info("No approved-for-removal skills found on profile; skipping.")
        return None
    return removable[0]  # take the first safe candidate


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------


def safety_check_remove(skill: str, cfg: dict) -> None:
    """Raise ValueError if `skill` is a core skill or not in the remove list."""
    core = set(cfg["core_skills_to_preserve"])
    approved_remove = set(cfg["approved_skills_to_remove"])

    if skill in core:
        raise ValueError(
            f"SAFETY ABORT: '{skill}' is in core_skills_to_preserve. "
            "It will never be removed."
        )
    if skill not in approved_remove:
        raise ValueError(
            f"SAFETY ABORT: '{skill}' is not in approved_skills_to_remove. "
            "Only explicitly approved skills can be removed."
        )


# ---------------------------------------------------------------------------
# Session runners
# ---------------------------------------------------------------------------


def run_morning(cfg: dict, dry_run: bool, headless: bool = False) -> None:
    """Morning session: add one skill from the approved list."""
    logger.info("=== MORNING SESSION%s ===", " (DRY RUN)" if dry_run else "")

    timeout_ms = cfg.get("settings", {}).get("page_load_timeout_ms", 30_000)
    profile_dir = get_profile_dir(BASE_DIR)

    with NaukriBrowser(profile_dir, headless=headless, timeout_ms=timeout_ms) as nb:
        nb.ensure_logged_in(strict=True)
        nb.go_to_profile()

        current_skills = nb.get_current_skills()
        logger.info("Skills before operation: %s", current_skills)

        skill = pick_skill_to_add(cfg, current_skills)
        if skill is None:
            logger.info("RESULT: SKIPPED — all approved skills already present.")
            return

        logger.info("Selected skill to add: %s", skill)
        success = nb.add_skill(skill, dry_run=dry_run)

        if dry_run:
            logger.info("RESULT: DRY RUN — no actual changes made.")
            return

        if success:
            # Verify: re-read and confirm the skill is now present.
            nb.go_to_profile()
            updated_skills = nb.get_current_skills()
            if skill in updated_skills:
                logger.info("RESULT: SUCCESS — '%s' added and verified on profile.", skill)
            else:
                logger.warning(
                    "RESULT: UNCERTAIN — operation reported success but '%s' "
                    "not found in re-read skills. Please verify manually.",
                    skill,
                )
        else:
            logger.error("RESULT: FAILED — could not add '%s'.", skill)


def run_evening(cfg: dict, dry_run: bool, headless: bool = False) -> None:
    """Evening session: remove one approved skill from the profile."""
    logger.info("=== EVENING SESSION%s ===", " (DRY RUN)" if dry_run else "")

    timeout_ms = cfg.get("settings", {}).get("page_load_timeout_ms", 30_000)
    profile_dir = get_profile_dir(BASE_DIR)

    with NaukriBrowser(profile_dir, headless=headless, timeout_ms=timeout_ms) as nb:
        nb.ensure_logged_in(strict=True)
        nb.go_to_profile()

        current_skills = nb.get_current_skills()
        logger.info("Skills before operation: %s", current_skills)

        skill = pick_skill_to_remove(cfg, current_skills)
        if skill is None:
            logger.info("RESULT: SKIPPED — nothing to remove.")
            return

        # Hard safety check before proceeding.
        safety_check_remove(skill, cfg)

        logger.info("Selected skill to remove: %s", skill)
        success = nb.remove_skill(skill, dry_run=dry_run)

        if dry_run:
            logger.info("RESULT: DRY RUN — no actual changes made.")
            return

        if success:
            # Verify removal.
            nb.go_to_profile()
            updated_skills = nb.get_current_skills()
            if skill not in updated_skills:
                logger.info("RESULT: SUCCESS — '%s' removed and verified.", skill)
            else:
                logger.warning(
                    "RESULT: UNCERTAIN — operation reported success but '%s' "
                    "still appears in re-read skills. Please verify manually.",
                    skill,
                )
        else:
            logger.info("RESULT: SKIPPED — '%s' was not on the profile.", skill)


def run_login_only() -> None:
    """
    Open the browser so the user can log in manually.
    CAPTCHA and OTP challenges are allowed — the browser stays open
    until the user completes authentication or 5 minutes elapse.
    """
    logger.info("Opening browser for manual login …")
    logger.info(
        "The browser will remain open throughout. "
        "Complete any CAPTCHA or OTP in the browser window."
    )
    profile_dir = get_profile_dir(BASE_DIR)
    with NaukriBrowser(profile_dir, headless=False) as nb:
        # strict=False: CAPTCHA/OTP prompts are logged, not raised.
        nb.ensure_logged_in(strict=False)
        logger.info(
            "✅  Login complete. Session saved to browser_profile/. "
            "You may close the browser window or press Ctrl+C to exit."
        )
        # Keep the browser open so the user can visually confirm their profile.
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Exiting login session (Ctrl+C received).")



def run_status(headless: bool = False) -> None:
    """Read and print the current skills without making any changes."""
    logger.info("=== STATUS (read-only) ===")
    profile_dir = get_profile_dir(BASE_DIR)
    with NaukriBrowser(profile_dir, headless=headless) as nb:
        nb.ensure_logged_in()
        nb.go_to_profile()
        skills = nb.get_current_skills()
        print("\nCurrent skills on profile:")
        for s in skills:
            print(f"  • {s}")


# ---------------------------------------------------------------------------
# Overlap prevention (file lock)
# ---------------------------------------------------------------------------


def acquire_lock(cfg: dict) -> FileLock:
    """Acquire a file lock to prevent concurrent runs. Raises SystemExit on failure."""
    timeout = cfg.get("settings", {}).get("lock_timeout_seconds", 300)
    lock = FileLock(str(LOCK_FILE))
    try:
        lock.acquire(timeout=1)  # fail immediately if already locked
        return lock
    except Timeout:
        sys.exit(
            "ERROR: Another instance of naukri-skill-updater is already running "
            f"(lock file: {LOCK_FILE}). "
            "If this is stale, delete the lock file and retry."
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Naukri Skill Updater — automates Key Skills management.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "session",
        choices=["morning", "evening", "login", "status"],
        help="Which session to run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate the session without making any profile changes.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run browser in headless mode (recommended for cron or background jobs).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.session)

    logger.info(
        "Naukri Skill Updater starting | session=%s | dry_run=%s | headless=%s",
        args.session,
        args.dry_run,
        args.headless,
    )

    if args.session in ("login", "status"):
        # These do not need the config or the lock.
        if args.session == "login":
            run_login_only()
        else:
            run_status(headless=args.headless)
        return

    cfg = load_config()
    lock = acquire_lock(cfg)

    try:
        if args.session == "morning":
            run_morning(cfg, dry_run=args.dry_run, headless=args.headless)
        elif args.session == "evening":
            run_evening(cfg, dry_run=args.dry_run, headless=args.headless)
    except RuntimeError as exc:
        logger.error("Session aborted: %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("Session interrupted by user.")
        sys.exit(130)
    finally:
        lock.release()
        logger.info("Session complete.")


if __name__ == "__main__":
    main()
