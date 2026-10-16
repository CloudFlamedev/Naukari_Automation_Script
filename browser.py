"""
browser.py — Playwright browser session management for Naukri Skill Updater.

Responsibilities:
  - Launch a persistent browser profile so the session survives restarts.
  - Detect login state; prompt the user to log in manually when needed.
  - Detect CAPTCHA / OTP walls and halt gracefully instead of bypassing them.
  - Navigate to the Key Skills section of the Naukri profile.
  - Read, add, and remove skills using stable, accessible selectors.
  - Never touch any profile section other than Key Skills.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NAUKRI_HOME = "https://www.naukri.com"
NAUKRI_LOGIN = "https://www.naukri.com/nlogin/login"
NAUKRI_PROFILE = "https://www.naukri.com/mnjuser/profile"

# Selectors — ordered from most-stable to most-brittle.
# Update these if Naukri changes its markup; always prefer aria labels.
_SEL = {
    # NOTE: Login detection is handled by _is_logged_in() using URL + DOM
    # signals rather than a single CSS selector. See that function for details.

    # ------------------------------------------------------------------ #
    # Key Skills — read-only profile view.                                #
    # id="lazyKeySkills" is the stable, unique identifier Naukri uses for #
    # the Key Skills card on the profile page.                            #
    # ------------------------------------------------------------------ #
    "skills_section": "#lazyKeySkills, [class='keySkills']",

    # ------------------------------------------------------------------ #
    # Key Skills — edit drawer / modal that opens after clicking Edit.    #
    # ------------------------------------------------------------------ #
    "skills_editor": ".keySkillsEdit, [class*='keySkill'][class*='Edit'], "
                     "[class*='keySkill'][class*='lightbox']",

    # Edit / pencil icon inside the Key Skills card.
    # Naukri frequently changes class names; we try every known pattern.
    "skills_edit_btn": (
        # Scoped to the skills card first ──────────────────────────────
        "#lazyKeySkills [class*='edit' i], "
        "#lazyKeySkills [class*='Edit'], "
        "#lazyKeySkills [class*='pencil' i], "
        "#lazyKeySkills span[title='Edit'], "
        "#lazyKeySkills button[title='Edit'], "
        "#lazyKeySkills i[title='Edit'], "
        "#lazyKeySkills a[title='Edit'], "
        # Unscoped fallbacks ───────────────────────────────────────────
        "span[title='Edit Key Skills'], "
        "[aria-label='Edit Key Skills'], "
        "[aria-label='Edit key skills'], "
        # Generic edit icons that only appear inside profile sections
        ".edit-icon, .editIcon, .pencil-icon, .icon-edit"
    ),


    # Text input inside the skills edit modal.
    # Naukri uses placeholder "Add skill" or "Type skill".
    "skills_input": (
        "input[placeholder*='skill' i], "
        "input[placeholder*='Add skill' i], "
        "input[placeholder*='Type skill' i], "
        "input[aria-label*='skill' i]"
    ),

    # Dropdown suggestion list item that appears while typing.
    "skill_suggestion": (
        ".suggesstedValues li, "
        ".autocomplete-dropdown li, "
        "[class*='suggestion'] li, "
        "[class*='dropdown'] li, "
        "[role='option']"
    ),

    # Individual skill chip/tag on the profile card or inside the editor.
    # Naukri renders chips as <li class="chip"> or <span class="chip">,
    # sometimes inside a <ul class="chips"> or <div class="chipWrapper">.
    # We list patterns from most-specific to broadest fallback.
    "skill_chip": (
        "#lazyKeySkills li.chip, "
        "#lazyKeySkills span.chip, "
        "#lazyKeySkills .chip, "
        ".keySkillsEdit li.chip, "
        ".keySkillsEdit span.chip, "
        ".keySkillsEdit .chip, "
        "li.chip, span.chip, .chip"
    ),

    # The ✕ / delete button on a skill chip inside the editor.
    "skill_chip_delete": (
        "i[class*='close' i], "
        "span[class*='close' i], "
        "button[aria-label*='remove' i], "
        "button[aria-label*='delete' i], "
        "span[class*='delete' i], "
        "[class*='chipDelete'], "
        "[class*='chip-delete'], "
        "[class*='cross' i], "
        "i, a, svg"
    ),

    # Save / Done button inside the skills editor.
    # Naukri uses various button styles; try many patterns.
    "skills_save_btn": (
        # Direct ID matches first (most specific) ──────────────────────
        "#saveKeySkills, "
        "#saveKeySkillsBtn, "
        "button#saveKeySkills, "
        "input#saveKeySkills, "
        # Scoped to the editor drawer ──────────────────────────────────
        ".keySkillsEdit #saveKeySkills, "
        ".keySkillsEdit button#saveKeySkills, "
        ".keySkillsEdit input#saveKeySkills, "
        ".keySkillsEdit button[type='submit'], "
        ".keySkillsEdit button.btn-dark, "
        ".keySkillsEdit button.btn-dark-ot, "
        ".keySkillsEdit button.btn-primary, "
        ".keySkillsEdit button[class*='save' i], "
        ".keySkillsEdit button[class*='submit' i], "
        ".keySkillsEdit button:has-text('Save'), "
        ".keySkillsEdit button:has-text('save'), "
        ".keySkillsEdit input[value*='Save' i], "
        # Lightbox / drawer / modal scoped ─────────────────────────────
        ".lightbox #saveKeySkills, "
        ".lightbox button[type='submit'], "
        ".lightbox button.btn-dark, "
        ".lightbox button.btn-dark-ot, "
        ".lightbox button.btn-primary, "
        ".lightbox button:has-text('Save'), "
        ".lightbox input[value*='Save' i], "
        # Unscoped fallbacks ───────────────────────────────────────────
        "button:has-text('Save'), "
        "button:has-text('Done')"
    ),

    # Success toast / confirmation after saving.
    "success_toast": ".toast-success, [class*='success-msg'], [role='alert']",

    # CAPTCHA indicators.
    "captcha_indicators": "iframe[src*='captcha'], #captchaV2, .g-recaptcha, [class*='captcha']",

    # OTP indicators.
    "otp_indicators": "input[placeholder*='OTP'], input[name*='otp'], [class*='otpModal']",
}


# How long (ms) to wait for elements before giving up.
DEFAULT_TIMEOUT = 30_000  # 30 s


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def get_profile_dir(base_dir: Path) -> Path:
    """Return the persistent browser profile directory, creating it if needed."""
    profile = base_dir / "browser_profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _is_logged_in(page: Page) -> bool:
    """
    Return True when the browser is on an authenticated Naukri session.

    Uses three independent signals so the function stays correct even if
    Naukri renames one CSS class or changes one URL path:

      Signal 1 — Authenticated URL path
          The path starts with /mnjuser/ (homepage, profile, jobs, etc.)
          but is NOT /nlogin/ or similar.

      Signal 2 — Authenticated nav text
          A link or button whose visible text is "My home" or "View profile"
          is present in the DOM.  These elements only appear after login.

      Signal 3 — Login form absent
          The mobile-number / email input that is the first field on
          /nlogin/login is not in the DOM.

    Any TWO of the three signals returning True is treated as logged-in.
    A single signal is not enough to avoid false positives.
    Each Playwright call is individually guarded so a timeout or missing
    element never propagates as an exception.
    """
    signals: list[bool] = []

    # ------------------------------------------------------------------
    # Signal 1: URL path indicates an authenticated section.
    # ------------------------------------------------------------------
    try:
        url = page.url
        auth_paths = ("/mnjuser/", "/jobseeker/")
        not_auth_paths = ("/nlogin/", "/login", "login.naukri")
        on_auth_path = (
            any(p in url for p in auth_paths)
            and not any(p in url for p in not_auth_paths)
        )
        signals.append(on_auth_path)
        if on_auth_path:
            logger.debug("Login signal 1 (URL): authenticated path detected — %s", url)
        else:
            logger.debug("Login signal 1 (URL): not an authenticated path — %s", url)
    except Exception as exc:
        logger.debug("Login signal 1 (URL): error — %s", exc)
        signals.append(False)

    # ------------------------------------------------------------------
    # Signal 2: Authenticated-only nav elements are visible.
    # Naukri shows "My home" and "View profile" links only after login.
    # We check for any of several known authenticated nav texts/selectors.
    # ------------------------------------------------------------------
    try:
        auth_nav_selectors = [
            # Text-based: works regardless of class-name changes.
            "a:has-text('My home')",
            "a:has-text('View profile')",
            # Candidate-name data attribute (may be present on some page versions).
            "[data-ga-track='candidateName']",
            # User avatar / drawer icon.
            ".nI-gNb-drawer__icon--user",
        ]
        found_nav = False
        for sel in auth_nav_selectors:
            try:
                count = page.locator(sel).count()
                if count > 0:
                    found_nav = True
                    logger.debug(
                        "Login signal 2 (nav): authenticated element found — %s", sel
                    )
                    break
            except Exception:
                continue
        signals.append(found_nav)
        if not found_nav:
            logger.debug("Login signal 2 (nav): no authenticated nav elements found.")
    except Exception as exc:
        logger.debug("Login signal 2 (nav): error — %s", exc)
        signals.append(False)

    # ------------------------------------------------------------------
    # Signal 3: Login form input is absent.
    # The login page has an <input> for mobile number / email.
    # If it is missing, we are probably on a post-login page.
    # ------------------------------------------------------------------
    try:
        login_form_selectors = [
            "input[placeholder*='mobile']",
            "input[placeholder*='email']",
            "input[name='username']",
            "#usernameField",
        ]
        login_form_present = False
        for sel in login_form_selectors:
            try:
                if page.locator(sel).count() > 0:
                    login_form_present = True
                    break
            except Exception:
                continue
        signals.append(not login_form_present)
        logger.debug(
            "Login signal 3 (no login form): login form %s.",
            "absent ✓" if not login_form_present else "present ✗",
        )
    except Exception as exc:
        logger.debug("Login signal 3 (no login form): error — %s", exc)
        signals.append(False)

    # ------------------------------------------------------------------
    # Decision: require at least 2 of 3 signals to be True.
    # ------------------------------------------------------------------
    true_count = sum(signals)
    result = true_count >= 2
    logger.debug(
        "Login detection: %d/3 signals positive → %s",
        true_count,
        "LOGGED IN" if result else "NOT logged in",
    )
    return result



def _has_captcha(page: Page) -> bool:
    try:
        return page.locator(_SEL["captcha_indicators"]).count() > 0
    except Exception:
        return False


def _has_otp(page: Page) -> bool:
    try:
        return page.locator(_SEL["otp_indicators"]).count() > 0
    except Exception:
        return False


def _check_auth_wall_strict(page: Page) -> None:
    """
    Raise RuntimeError immediately if a CAPTCHA or OTP wall is detected.
    Used by automated morning/evening runs — they must not proceed past an
    auth challenge, so they stop and ask the user to log in manually first.
    """
    if _has_captcha(page):
        raise RuntimeError(
            "CAPTCHA detected during an automated run. "
            "Please run  'python main.py login'  first to complete "
            "authentication manually, then retry the scheduled session."
        )
    if _has_otp(page):
        raise RuntimeError(
            "OTP / verification required during an automated run. "
            "Please run  'python main.py login'  first to complete "
            "authentication manually, then retry the scheduled session."
        )


def _log_auth_wall_prompt(page: Page) -> bool:
    """
    During manual login: if a CAPTCHA or OTP challenge is present, log a
    clear prompt and return True.  Does NOT raise — the browser stays open
    so the user can complete the challenge themselves.
    Returns False when no auth challenge is visible.
    """
    if _has_captcha(page):
        logger.warning(
            "⚠  CAPTCHA detected. Please solve it in the browser window. "
            "The script will keep waiting (up to 5 minutes total)."
        )
        return True
    if _has_otp(page):
        logger.warning(
            "⚠  OTP / verification required. Please enter it in the browser "
            "window. The script will keep waiting (up to 5 minutes total)."
        )
        return True
    return False



# ---------------------------------------------------------------------------
# Public browser context factory
# ---------------------------------------------------------------------------


class NaukriBrowser:
    """
    Manages a persistent Chromium browser context for Naukri automation.

    Usage::

        with NaukriBrowser(profile_dir) as nb:
            nb.ensure_logged_in()
            skills = nb.get_current_skills()
    """

    def __init__(self, profile_dir: Path, headless: bool = False, timeout_ms: int = DEFAULT_TIMEOUT):
        self._profile_dir = profile_dir
        # Auto-detect headless mode if no GUI display is attached (e.g. running from cron or daemon)
        import os
        if not headless and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            logger.info("No DISPLAY or WAYLAND_DISPLAY detected; switching to headless mode.")
            headless = True
        self._headless = headless
        self._timeout = timeout_ms
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    # Path to the installed Google Chrome binary.
    # Playwright's bundled Chromium crashes with SIGSEGV on this machine;
    # the system Chrome is stable and reuses the same persistent profile dir.
    CHROME_EXECUTABLE = "/usr/bin/google-chrome"

    def __enter__(self) -> "NaukriBrowser":
        self._playwright = sync_playwright().start()

        logger.info("Launching Google Chrome from: %s", self.CHROME_EXECUTABLE)
        logger.info("Browser profile directory   : %s", self._profile_dir)

        try:
            self._browser = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                executable_path=self.CHROME_EXECUTABLE,
                headless=self._headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    # Keeps the browser stable on Linux with integrated/discrete GPUs.
                    "--disable-gpu",
                ],
                viewport={"width": 1280, "height": 800},
            )
        except Exception as exc:
            # Stop the Playwright driver before re-raising so we don't leak it.
            try:
                self._playwright.stop()
            except Exception:
                pass
            raise RuntimeError(
                f"Failed to launch Google Chrome at '{self.CHROME_EXECUTABLE}'. "
                "Verify that Google Chrome is installed at that path and is "
                "executable. Original error: " + str(exc)
            ) from exc

        # launch_persistent_context returns a BrowserContext, not a Browser.
        self._context = self._browser  # type: ignore[assignment]
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._page.set_default_timeout(self._timeout)
        logger.info("Browser launched successfully.")
        return self

    def __exit__(self, *_) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            if self._playwright:
                self._playwright.stop()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def ensure_logged_in(self, strict: bool = False) -> None:
        """
        Verify the browser has an active Naukri session.

        Strategy
        --------
        1. If the browser is already on a Naukri page, check login state
           right there — do NOT navigate away first.
        2. If the browser is on about:blank or a non-Naukri page (fresh
           launch), navigate to NAUKRI_PROFILE before checking.
        3. If already logged in → return immediately.
        4. If not logged in → navigate to the login page and wait up to
           5 minutes for the user to authenticate manually.

        This prevents the script from clobbering an already-authenticated
        page (e.g. /mnjuser/homepage) with a redirect to /mnjuser/profile
        before the login check runs.

        Parameters
        ----------
        strict : bool
            True  → automated run: raise RuntimeError on CAPTCHA / OTP.
            False → manual login: log a prompt, keep the browser open.
        """
        page = self._page

        # ------------------------------------------------------------------
        # Step 1: Decide whether we need to navigate before checking.
        # ------------------------------------------------------------------
        current_url = page.url
        on_naukri = bool(current_url) and "naukri.com" in current_url

        if on_naukri:
            logger.info(
                "Browser is already on Naukri (%s). "
                "Checking login state without navigating away …",
                current_url,
            )
        else:
            logger.info(
                "Browser is not on Naukri yet (%s). "
                "Navigating to profile page to check login status …",
                current_url or "blank",
            )
            try:
                page.goto(NAUKRI_PROFILE, wait_until="domcontentloaded")
            except Exception as exc:
                error_msg = str(exc).lower()
                if "net::err_internet_disconnected" in error_msg or \
                   "net::err_name_not_resolved" in error_msg or \
                   "net::err_connection" in error_msg or \
                   "net::err_network" in error_msg:
                    raise RuntimeError(
                        "No internet connection. Cannot reach Naukri.com. "
                        "Please check your network and try again."
                    ) from exc
                raise

        if strict:
            _check_auth_wall_strict(page)

        if _is_logged_in(page):
            logger.info("✅  Already logged in. Session is active.")
            return

        # ------------------------------------------------------------------
        # Step 2: Not logged in — open the login page and wait.
        # ------------------------------------------------------------------
        logger.warning(
            "Not logged in. Opening Naukri login page. "
            "Please enter your credentials and complete any verification "
            "in the browser window. The script will wait up to 5 minutes."
        )
        page.goto(NAUKRI_LOGIN, wait_until="domcontentloaded")

        deadline = time.time() + 300       # 5-minute total window
        next_reminder = time.time() + 60   # remind every 60 s
        last_auth_wall_logged = False

        while time.time() < deadline:
            remaining = int(deadline - time.time())

            if strict:
                _check_auth_wall_strict(page)
            else:
                auth_wall_present = _log_auth_wall_prompt(page)
                if auth_wall_present:
                    last_auth_wall_logged = True
                elif last_auth_wall_logged:
                    logger.info(
                        "Verification challenge cleared. Checking login status …"
                    )
                    last_auth_wall_logged = False

            if _is_logged_in(page):
                logger.info(
                    "✅  Login detected successfully. "
                    "Session saved to browser_profile/ and available for future runs."
                )
                return

            if time.time() >= next_reminder:
                logger.info(
                    "Still waiting for login … (%d seconds remaining). "
                    "Please complete sign-in in the browser window.",
                    remaining,
                )
                next_reminder = time.time() + 60

            time.sleep(3)

        raise RuntimeError(
            "Timed out waiting for manual login (5 minutes elapsed). "
            "Please re-run 'python main.py login' and complete sign-in promptly."
        )


    # ------------------------------------------------------------------
    # Navigate to profile
    # ------------------------------------------------------------------

    def go_to_profile(self) -> None:
        """Navigate to the Naukri profile page (automated runs only)."""
        page = self._page
        logger.info("Navigating to profile page …")
        try:
            page.goto(NAUKRI_PROFILE, wait_until="domcontentloaded")
        except Exception as exc:
            error_msg = str(exc).lower()
            if "net::err_internet_disconnected" in error_msg or \
               "net::err_name_not_resolved" in error_msg or \
               "net::err_connection" in error_msg or \
               "net::err_network" in error_msg:
                raise RuntimeError(
                    "No internet connection. Cannot reach Naukri.com. "
                    "Please check your network and try again."
                ) from exc
            raise
        _check_auth_wall_strict(page)
        page.wait_for_load_state("networkidle", timeout=self._timeout)

    # ------------------------------------------------------------------
    # Read current skills
    # ------------------------------------------------------------------

    def get_current_skills(self) -> list[str]:
        """
        Return the list of skill names currently shown on the profile page.

        Uses #lazyKeySkills (the unique, stable id Naukri assigns to the
        Key Skills card) to scope the search.  Waits for networkidle so that
        lazy-loaded content has time to render.

        Raises RuntimeError if the section cannot be found at all.
        """
        page = self._page
        logger.info("Reading current skills from profile …")

        # Wait for the page to finish lazy-loading.
        try:
            page.wait_for_load_state("networkidle", timeout=self._timeout)
        except PlaywrightTimeout:
            logger.debug("networkidle timed out; proceeding anyway.")

        # Locate the skills section.  Use :first-of-type via .first to avoid
        # a strict-mode violation if a second matching element exists.
        section = page.locator(_SEL["skills_section"]).first
        try:
            section.wait_for(state="visible", timeout=self._timeout)
        except PlaywrightTimeout:
            raise RuntimeError(
                "Could not find the Key Skills section (#lazyKeySkills) on the page. "
                "The page structure may have changed — please inspect manually."
            )

        # Read chip elements scoped inside the section.
        chips = section.locator(_SEL["skill_chip"])
        count = chips.count()
        skills: list[str] = []
        for i in range(count):
            try:
                raw = chips.nth(i).inner_text().strip()
                # Naukri chips sometimes include a hidden delete icon whose
                # text leaks into inner_text().  Strip trailing "×" or "x".
                text = raw.rstrip("×x \u00d7").strip()
                if text:
                    skills.append(text)
            except Exception:
                continue

        logger.info("Current skills (%d): %s", len(skills), skills)
        return skills


    # ------------------------------------------------------------------
    # Open the skills editor
    # ------------------------------------------------------------------

    def _open_skills_editor(self) -> None:
        """
        Hover over the Key Skills section to reveal the Edit button,
        click it, then wait until the edit drawer/modal is visible.

        Strategy (in order):
          1. Try the CSS selectors in _SEL['skills_edit_btn'].
          2. Fall back to a JavaScript scan of the section's DOM tree
             for any element whose class, title, or aria-label contains
             'edit' (case-insensitive).
          3. If both fail, dump the section's outer HTML to the log file
             so the user can share it for debugging.
        """
        page = self._page

        # ── Locate the Key Skills card ──────────────────────────────────
        section = page.locator(_SEL["skills_section"]).first
        try:
            section.wait_for(state="visible", timeout=self._timeout)
        except PlaywrightTimeout:
            raise RuntimeError(
                "Key Skills section (#lazyKeySkills) not found. "
                "Cannot open editor."
            )

        # ── Hover to reveal the Edit button ─────────────────────────────
        section.hover()
        page.wait_for_timeout(800)

        # ── Strategy 1: CSS selectors ───────────────────────────────────
        edit_btn = page.locator(_SEL["skills_edit_btn"]).first
        clicked = False
        try:
            edit_btn.wait_for(state="visible", timeout=6_000)
            edit_btn.click()
            clicked = True
            logger.info("Clicked edit button via CSS selector.")
        except (PlaywrightTimeout, Exception) as exc:
            logger.debug("CSS selector strategy failed: %s", exc)

        # ── Strategy 2: JavaScript DOM scan ─────────────────────────────
        if not clicked:
            logger.info(
                "CSS selectors did not find the edit button. "
                "Trying JavaScript DOM scan …"
            )
            js_click_result = page.evaluate("""() => {
                // Search inside #lazyKeySkills for any clickable element
                // whose attributes suggest it is an "edit" control.
                const root = document.querySelector('#lazyKeySkills')
                    || document.querySelector('.keySkills');
                if (!root) return {found: false, reason: 'skills section not in DOM'};

                const candidates = root.querySelectorAll('span, i, button, a, div, svg');
                for (const el of candidates) {
                    const cls = (el.className || '').toString().toLowerCase();
                    const title = (el.getAttribute('title') || '').toLowerCase();
                    const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                    const text = (el.textContent || '').trim().toLowerCase();

                    const isEdit = (
                        cls.includes('edit') ||
                        cls.includes('pencil') ||
                        title.includes('edit') ||
                        ariaLabel.includes('edit') ||
                        text === 'edit'
                    );
                    if (isEdit) {
                        el.click();
                        return {
                            found: true,
                            tag: el.tagName,
                            className: el.className.toString().substring(0, 120),
                            title: el.getAttribute('title') || '',
                        };
                    }
                }
                return {found: false, reason: 'no edit element found in scan'};
            }""")

            if js_click_result.get("found"):
                clicked = True
                logger.info(
                    "Clicked edit button via JS scan — tag=%s, class=%s, title=%s",
                    js_click_result.get("tag"),
                    js_click_result.get("className"),
                    js_click_result.get("title"),
                )
            else:
                logger.warning("JS scan result: %s", js_click_result.get("reason"))

        # ── If nothing worked: dump HTML for diagnostics ────────────────
        if not clicked:
            self._dump_skills_section_html()
            raise RuntimeError(
                "Edit button for Key Skills not found by any strategy. "
                "The section's HTML has been logged for debugging. "
                "Run 'python main.py status' to see the dump in the log file, "
                "or share it so the selector can be updated."
            )

        # ── Wait for the edit drawer to appear ──────────────────────────
        page.wait_for_timeout(1_200)
        editor = page.locator(_SEL["skills_editor"]).first
        try:
            editor.wait_for(state="visible", timeout=8_000)
            logger.debug("Skills editor drawer is now visible.")
        except PlaywrightTimeout:
            logger.warning(
                "Skills editor drawer did not appear in 8 s. "
                "Proceeding anyway — the modal may use a different selector."
            )

        page.wait_for_timeout(500)  # let animations settle

    def _dump_skills_section_html(self) -> None:
        """Log the first 3000 chars of #lazyKeySkills innerHTML for debugging."""
        page = self._page
        try:
            html = page.evaluate("""() => {
                const el = document.querySelector('#lazyKeySkills')
                    || document.querySelector('.keySkills');
                return el ? el.innerHTML.substring(0, 3000) : '(element not found)';
            }""")
            logger.error(
                "=== Key Skills section HTML dump (for debugging) ===\n%s\n"
                "=== end of dump ===",
                html,
            )
        except Exception as exc:
            logger.error("Could not dump skills section HTML: %s", exc)



    # ------------------------------------------------------------------
    # Add a skill
    # ------------------------------------------------------------------

    def add_skill(self, skill: str, dry_run: bool = False) -> bool:
        """
        Type `skill` into the skills input, select it from the suggestions,
        and save. Returns True on success, False if unable to confirm.
        """
        page = self._page
        logger.info("%sAdding skill: %s", "[DRY RUN] " if dry_run else "", skill)

        self._open_skills_editor()

        # Locate the skills input.
        try:
            inp = page.locator(_SEL["skills_input"]).first
            inp.wait_for(state="visible", timeout=self._timeout)
        except PlaywrightTimeout:
            raise RuntimeError("Skills input field not found in the editor.")

        if dry_run:
            logger.info("[DRY RUN] Would type '%s' and save (no changes made).", skill)
            page.keyboard.press("Escape")
            return True

        inp.fill("")
        inp.type(skill, delay=80)  # human-like typing
        page.wait_for_timeout(1_200)  # wait for suggestions

        # Try to click a matching suggestion.
        suggestions = page.locator(_SEL["skill_suggestion"])
        matched = False
        for i in range(suggestions.count()):
            s = suggestions.nth(i)
            if skill.lower() in s.inner_text().lower():
                s.click()
                matched = True
                logger.info("Selected suggestion for: %s", skill)
                break

        if not matched:
            # Press Enter as fallback (some UIs accept free-text).
            logger.warning("No dropdown suggestion matched '%s'; pressing Enter.", skill)
            inp.press("Enter")

        page.wait_for_timeout(800)

        # Save.
        return self._save_skills_editor()

    # ------------------------------------------------------------------
    # Remove a skill
    # ------------------------------------------------------------------

    def remove_skill(self, skill: str, dry_run: bool = False) -> bool:
        """
        Find the skill chip with `skill` text inside the edit drawer and
        click its delete button, then save.
        Returns True on success, False if the skill was not found.
        """
        page = self._page
        logger.info("%sRemoving skill: %s", "[DRY RUN] " if dry_run else "", skill)

        self._open_skills_editor()

        # Scope chip search to the editor drawer so we don't accidentally
        # match chips in the read-only card that may still be in the DOM.
        editor = page.locator(_SEL["skills_editor"]).first
        chips = editor.locator(_SEL["skill_chip"])

        target_chip = None
        for i in range(chips.count()):
            chip = chips.nth(i)
            try:
                raw = chip.inner_text().strip().rstrip("×x \u00d7").strip()
            except Exception:
                raw = ""
            if skill.lower() in raw.lower():
                target_chip = chip
                break

        if target_chip is None:
            logger.info("Skill '%s' not found in editor; skipping removal.", skill)
            page.keyboard.press("Escape")
            return False

        if dry_run:
            logger.info("[DRY RUN] Would remove '%s' (no changes made).", skill)
            page.keyboard.press("Escape")
            return True

        # Click the delete button inside the chip.
        deleted = False
        try:
            delete_btn = target_chip.locator(_SEL["skill_chip_delete"]).first
            delete_btn.wait_for(state="visible", timeout=3_000)
            delete_btn.click()
            deleted = True
        except Exception:
            try:
                target_chip.hover()
                page.wait_for_timeout(400)
                delete_btn = target_chip.locator(_SEL["skill_chip_delete"]).first
                delete_btn.click()
                deleted = True
            except Exception:
                # Fallback: JavaScript click on delete icon inside chip
                deleted = page.evaluate("""(skillName) => {
                    const drawer = document.querySelector('.keySkillsEdit')
                        || document.querySelector('.lightbox.profileEditDrawer')
                        || document.querySelector('[class*="keySkill"][class*="Edit"]');
                    if (!drawer) return false;
                    const chips = drawer.querySelectorAll('.chip, li.chip, span.chip');
                    for (const chip of chips) {
                        if (chip.textContent.toLowerCase().includes(skillName.toLowerCase())) {
                            const icon = chip.querySelector('i, a, span, svg, button');
                            if (icon) { icon.click(); return true; }
                            chip.click();
                            return true;
                        }
                    }
                    return false;
                }""", skill)
                if deleted:
                    logger.info("Deleted skill chip via JS fallback.")

        if not deleted:
            logger.warning("Could not click delete icon on chip for '%s'.", skill)
            page.keyboard.press("Escape")
            return False

        page.wait_for_timeout(800)
        return self._save_skills_editor()

    # ------------------------------------------------------------------
    # Save helper
    # ------------------------------------------------------------------

    def _save_skills_editor(self) -> bool:
        """
        Click Save/Done in the skills editor and wait for confirmation.

        Strategy:
          1. Scroll the editor drawer to make the Save button visible.
          2. Check candidate locators scoped to the drawer to find
             and click the visible Save button.
          3. Fall back to JavaScript scan if locator matching fails.
          4. Wait for confirmation (drawer closing or success toast).
        """
        page = self._page

        # ── Step 1: Scroll the drawer so the save button is in view ─────
        page.evaluate("""() => {
            const drawer = document.querySelector('.keySkillsEdit')
                || document.querySelector('.lightbox.profileEditDrawer')
                || document.querySelector('[class*="keySkill"][class*="Edit"]');
            if (drawer) drawer.scrollTo(0, drawer.scrollHeight);
        }""")
        page.wait_for_timeout(600)

        # ── Step 2: Search for visible save button via Playwright locators
        clicked = False
        drawer = page.locator(".keySkillsEdit, .lightbox.profileEditDrawer, [class*='keySkill'][class*='Edit']").first

        save_candidates = [
            drawer.locator("#saveKeySkills"),
            drawer.locator("button#saveKeySkills"),
            drawer.locator("input#saveKeySkills"),
            drawer.locator("button:has-text('Save')"),
            drawer.locator("button:has-text('save')"),
            drawer.locator("input[value*='Save' i]"),
            drawer.locator("button.btn-dark-ot"),
            drawer.locator("button.btn-dark"),
            drawer.locator("button[type='submit']"),
            drawer.locator("button[class*='save' i]"),
            drawer.locator("[id*='save' i]"),
            drawer.locator("button:has-text('Done')"),
            page.locator("#saveKeySkills"),
            page.locator("button#saveKeySkills"),
            page.locator(_SEL["skills_save_btn"]),
        ]

        for cand in save_candidates:
            try:
                count = cand.count()
                for i in range(count):
                    btn = cand.nth(i)
                    if btn.is_visible():
                        btn.scroll_into_view_if_needed()
                        btn.click()
                        clicked = True
                        logger.info("Clicked save button via locator.")
                        break
                if clicked:
                    break
            except Exception as exc:
                logger.debug("Candidate check failed: %s", exc)

        # ── Step 3: JavaScript DOM scan fallback ────────────────────────
        if not clicked:
            logger.info(
                "Locators did not find the save button. "
                "Trying JavaScript DOM scan …"
            )
            js_result = page.evaluate("""() => {
                const drawer = document.querySelector('.keySkillsEdit')
                    || document.querySelector('.lightbox.profileEditDrawer')
                    || document.querySelector('[class*="keySkill"][class*="Edit"]');
                const roots = drawer ? [drawer, document.body] : [document.body];

                for (const root of roots) {
                    const elements = root.querySelectorAll('button, input[type="button"], input[type="submit"], a, div[role="button"]');
                    for (const el of elements) {
                        const id = (el.id || '').toLowerCase();
                        const txt = (el.textContent || '').trim().toLowerCase();
                        const val = (el.value || '').trim().toLowerCase();
                        const cls = (el.className || '').toString().toLowerCase();
                        const type = (el.getAttribute('type') || '').toLowerCase();

                        const isCancel = (txt === 'cancel' || val === 'cancel' || cls.includes('cancel'));
                        if (isCancel) continue;

                        const isSave = (
                            id.includes('save') ||
                            txt === 'save' || txt === 'done' ||
                            txt.startsWith('save') ||
                            val === 'save' || val === 'done' ||
                            val.startsWith('save') ||
                            cls.includes('save') ||
                            cls.includes('btn-dark') ||
                            type === 'submit'
                        );
                        if (isSave) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return {
                                found: true,
                                tag: el.tagName,
                                id: el.id,
                                text: txt.substring(0, 50),
                                value: val.substring(0, 50),
                                className: cls.substring(0, 120),
                            };
                        }
                    }
                }
                return {found: false};
            }""")

            if js_result.get("found"):
                clicked = True
                logger.info(
                    "Clicked save button via JS scan — tag=%s, id=%s, text='%s', value='%s'",
                    js_result.get("tag"),
                    js_result.get("id"),
                    js_result.get("text"),
                    js_result.get("value"),
                )
            else:
                logger.warning("JS save-button scan found nothing.")

        # ── If nothing worked: dump HTML for diagnostics ────────────────
        if not clicked:
            self._dump_editor_html()
            raise RuntimeError(
                "Save button not found by any strategy. "
                "The editor's HTML has been logged for debugging. "
                "Check today's log in logs/ and share the dump."
            )

        # ── Step 4: Wait for confirmation ───────────────────────────────
        page.wait_for_timeout(1_000)

        # Check if drawer closed (strong indicator of save success)
        drawer_closed = False
        try:
            drawer_elem = page.locator(".keySkillsEdit, .lightbox.profileEditDrawer, [class*='keySkill'][class*='Edit']").first
            drawer_elem.wait_for(state="hidden", timeout=5_000)
            drawer_closed = True
            logger.info("Skills editor drawer closed (save confirmed).")
        except (PlaywrightTimeout, Exception):
            pass

        try:
            page.locator(_SEL["success_toast"]).first.wait_for(
                state="visible", timeout=5_000
            )
            logger.info("Save confirmed via success indicator.")
            return True
        except (PlaywrightTimeout, Exception):
            if drawer_closed:
                return True
            logger.warning("No explicit success indicator; waiting for page to settle.")
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            return True  # optimistic — the caller will re-read skills to verify

    def _dump_editor_html(self) -> None:
        """Log the first 3000 chars of the editor drawer HTML for debugging."""
        page = self._page
        try:
            html = page.evaluate("""() => {
                const el = document.querySelector('.keySkillsEdit')
                    || document.querySelector('.lightbox.profileEditDrawer')
                    || document.querySelector('[class*="keySkill"][class*="Edit"]');
                return el ? el.innerHTML.substring(0, 3000) : '(editor element not found)';
            }""")
            logger.error(
                "=== Skills editor HTML dump (for debugging) ===\n%s\n"
                "=== end of dump ===",
                html,
            )
        except Exception as exc:
            logger.error("Could not dump editor HTML: %s", exc)


