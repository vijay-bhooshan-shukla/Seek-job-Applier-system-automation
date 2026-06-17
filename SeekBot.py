import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen
from urllib.parse import urljoin, urlparse
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from config import CONFIG

SEARCH_CFG = CONFIG.get("search", {})
RESUME_CFG = CONFIG.get("resume", {})
MATCHING_CFG = CONFIG.get("matching", {})
APPLY_CFG = CONFIG.get("apply", {})
LOG_CFG = CONFIG.get("logging", {})

DEBUG_HOST = SEARCH_CFG.get("debug_host", "127.0.0.1")
DEBUG_PORT = int(SEARCH_CFG.get("debug_port", 9222))
DEBUG_URL = f"http://{DEBUG_HOST}:{DEBUG_PORT}/json/version"
SEARCH_URLS = SEARCH_CFG.get("search_urls", [])
WAIT_TIMEOUT = int(SEARCH_CFG.get("wait_timeout", 12))
PAGE_LOAD_WAIT = float(SEARCH_CFG.get("page_load_wait", 5))
DETAIL_LOAD_WAIT = float(SEARCH_CFG.get("detail_load_wait", 4))
FLOW_RETRY_LIMIT = int(SEARCH_CFG.get("flow_retry_limit", 4))
CLICK_PAUSE = max(0.15, float(SEARCH_CFG.get("click_pause", 1.5)))
MAX_FLOW_STEPS = int(SEARCH_CFG.get("max_flow_steps", 20))
MAX_PAGES_PER_SEARCH = int(SEARCH_CFG.get("max_pages_per_search", 0))

SESSION_APPLY_CAP = int(APPLY_CFG.get("session_apply_cap", 25))
QUICK_APPLY_ONLY = bool(APPLY_CFG.get("quick_apply_only", True))
SKIP_EXTERNAL = bool(APPLY_CFG.get("skip_external", True))
SKIP_ALREADY_APPLIED = bool(APPLY_CFG.get("skip_already_applied", True))
AUTO_SUBMIT_ENABLED = bool(APPLY_CFG.get("auto_submit_enabled", True))
SKIP_ON_UNANSWERED_QUESTIONS = bool(APPLY_CFG.get("skip_on_unanswered_questions", True))
FORCE_RESUME_UPLOAD = bool(APPLY_CFG.get("force_resume_upload", False))
DIRECT_APPLY_URL_FALLBACK = bool(APPLY_CFG.get("direct_apply_url_fallback", True))
MAX_JOBS_PER_RUN = int(APPLY_CFG.get("max_jobs_per_run", 20))
WAIT_FOR_MANUAL_QUESTIONS = bool(APPLY_CFG.get("wait_for_manual_questions", True))
MANUAL_QUESTION_TIMEOUT = int(APPLY_CFG.get("manual_question_timeout_sec", 900))
MANUAL_QUESTION_SCAN_INTERVAL = float(APPLY_CFG.get("manual_question_scan_interval_sec", 2))
SCRIPT_EXE = APPLY_CFG.get("script_exe", "Script.exe")
SCRIPT_AU3 = APPLY_CFG.get("script_au3", "Script.au3")

SHOW_MATCH_DETAILS = bool(LOG_CFG.get("show_match_details", True))
SHOW_SKIP_REASONS = bool(LOG_CFG.get("show_skip_reasons", True))

RESUME_FILE = RESUME_CFG.get("resume_file", "")
COVER_LETTER_FILE = RESUME_CFG.get("cover_letter_file", "")
PROFILE_KEYWORDS = RESUME_CFG.get("profile_keywords", {})
MUST_HAVE_KEYWORDS = PROFILE_KEYWORDS.get("must_have", [])
PREFERRED_KEYWORDS = PROFILE_KEYWORDS.get("preferred", [])
EXCLUDE_KEYWORDS = RESUME_CFG.get("exclude_keywords", [])

MUST_HAVE_WEIGHT = int(MATCHING_CFG.get("must_have_weight", 12))
PREFERRED_WEIGHT = int(MATCHING_CFG.get("preferred_weight", 4))
EXCLUDE_PENALTY = int(MATCHING_CFG.get("exclude_penalty", 20))
MUST_HAVE_MISSING_PENALTY = int(MATCHING_CFG.get("must_have_missing_penalty", 10))
MIN_MATCH_SCORE = int(MATCHING_CFG.get("min_match_score", 20))
MATCHING_ENABLED = bool(MATCHING_CFG.get("enabled", False))
REQUIRE_RESUME_ON_STARTUP = bool(RESUME_CFG.get("require_on_startup", False))

LOG_DIR = os.path.join(os.getcwd(), "logs")
SCREENSHOT_DIR = os.path.join(LOG_DIR, "screenshots")
BEFORE_SCREENSHOT_DIR = os.path.join(SCREENSHOT_DIR, "before")
AFTER_SCREENSHOT_DIR = os.path.join(SCREENSHOT_DIR, "after")
CSV_LOG_PATH = os.path.join(LOG_DIR, "applied_jobs.csv")
LAST_HR_TEXT = ""
LAST_HR_LINK = ""
TODAY_SUBMITTED_JOB_KEYS = set()
ACTIVE_APPLY_STATE = {"job_key": "", "job_url": "", "apply_url": "", "locked": False}

BLOCKED_HR_IDENTIFIERS = [
    "agastya",
    "agastyakapoor",
    "agastyakapoorgk",
]
FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "proton.me",
    "protonmail.com",
}


def safe_input(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def normalize_path(path_value):
    if not path_value:
        return ""
    return os.path.abspath(os.path.expanduser(path_value))


def validate_config():
    if not isinstance(SEARCH_URLS, list) or not SEARCH_URLS:
        print("CONFIG_ERROR: search.search_urls must contain at least one URL")
        sys.exit(1)

    resume_path = normalize_path(RESUME_FILE)
    if not resume_path:
        if REQUIRE_RESUME_ON_STARTUP:
            print("CONFIG_ERROR: resume.resume_file is required")
            sys.exit(1)
        print("WARN: resume.resume_file missing; startup continue hoga")
    elif not os.path.exists(resume_path):
        if REQUIRE_RESUME_ON_STARTUP:
            print(f"CONFIG_ERROR: resume file not found -> {resume_path}")
            sys.exit(1)
        print(f"WARN: resume file not found -> {resume_path}")

    cover_path = normalize_path(COVER_LETTER_FILE)
    if cover_path and not os.path.exists(cover_path):
        print(f"WARN: cover letter file not found -> {cover_path}")


def get_debug_info(timeout=2):
    try:
        with urlopen(DEBUG_URL, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except Exception:
        return None


def find_chrome_binary():
    candidates = [
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def start_debug_chrome(first_url):
    chrome_binary = find_chrome_binary()
    if not chrome_binary:
        print("Chrome binary nahi mila; normal WebDriver mode use karenge.")
        return False

    profile_dir = os.path.join(os.getcwd(), ".seekbot-chrome-profile")
    os.makedirs(profile_dir, exist_ok=True)

    args = [
        chrome_binary,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-features=Crashpad",
        first_url,
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(30):
        data = get_debug_info(timeout=1)
        if data:
            print("Debug Chrome auto-start ho gaya.")
            print("Browser:", data.get("Browser"))
            return True
        time.sleep(0.5)

    print("Debug Chrome auto-start fail hua; normal WebDriver mode use karenge.")
    return False


def build_debug_driver():
    chrome_options = Options()
    chrome_options.debugger_address = f"{DEBUG_HOST}:{DEBUG_PORT}"
    return webdriver.Chrome(options=chrome_options)


class SessionReconnectRequired(RuntimeError):
    pass


def is_session_recoverable_error(exc):
    if isinstance(exc, InvalidSessionIdException):
        return True
    message = normalize_text(str(exc))
    session_markers = [
        "invalid session id",
        "session deleted",
        "disconnected",
        "unable to receive message from renderer",
        "target window already closed",
        "web view not found",
        "failed to establish a new connection",
        "actively refused it",
        "max retries exceeded",
        "forcibly closed by the remote host",
        "connectionreseterror",
        "winerror 10054",
        "httppconnectionpool",
        "httpconnectionpool",
        "newconnectionerror",
        "connection refused",
        "localhost",
    ]
    if isinstance(exc, WebDriverException):
        return any(marker in message for marker in session_markers)
    return any(marker in message for marker in session_markers)


def raise_session_reconnect(exc, context):
    if is_session_recoverable_error(exc):
        raise SessionReconnectRequired(context) from exc
    raise exc


def try_quit_driver(driver):
    if not driver:
        return
    try:
        driver.quit()
    except Exception:
        pass


def verify_driver_session(driver):
    if not driver:
        return False
    try:
        _ = driver.current_url
        return True
    except Exception as exc:
        if is_session_recoverable_error(exc):
            return False
        return False


def clear_active_apply_state():
    ACTIVE_APPLY_STATE.update({"job_key": "", "job_url": "", "apply_url": "", "locked": False})


def lock_active_apply_state(job_key="", job_url="", apply_url=""):
    ACTIVE_APPLY_STATE["job_key"] = job_key or ACTIVE_APPLY_STATE.get("job_key", "")
    ACTIVE_APPLY_STATE["job_url"] = job_url or ACTIVE_APPLY_STATE.get("job_url", "")
    ACTIVE_APPLY_STATE["apply_url"] = apply_url or ACTIVE_APPLY_STATE.get("apply_url", "")
    ACTIVE_APPLY_STATE["locked"] = True
    print("APPLY_OPEN:locked")


def refresh_active_apply_state(driver, job_key="", job_url=""):
    if not driver:
        return False
    try:
        current = (driver.current_url or "").strip()
    except Exception as exc:
        raise_session_reconnect(exc, "refresh_active_apply_state")
    if current and has_open_seek_apply_page(driver):
        ACTIVE_APPLY_STATE["job_key"] = job_key or ACTIVE_APPLY_STATE.get("job_key", "")
        ACTIVE_APPLY_STATE["job_url"] = job_url or ACTIVE_APPLY_STATE.get("job_url", "")
        ACTIVE_APPLY_STATE["apply_url"] = current
        ACTIVE_APPLY_STATE["locked"] = True
        return True
    return False


def detect_and_lock_seek_apply_page(driver, job_key="", job_url="", switch=True):
    if not driver:
        return False

    try:
        if refresh_active_apply_state(driver, job_key=job_key, job_url=job_url):
            return True
    except SessionReconnectRequired:
        raise
    except Exception:
        pass

    if not switch:
        return False

    try:
        handles = driver.window_handles
        current_handle = driver.current_window_handle
    except Exception as exc:
        raise_session_reconnect(exc, "detect_and_lock_seek_apply_page_handles")

    for handle in reversed(handles):
        try:
            driver.switch_to.window(handle)
            if refresh_active_apply_state(driver, job_key=job_key, job_url=job_url):
                return True
        except Exception as exc:
            if is_session_recoverable_error(exc):
                raise SessionReconnectRequired("detect_and_lock_seek_apply_page_state") from exc
            continue

    try:
        driver.switch_to.window(current_handle)
    except Exception:
        pass
    return False


def reattach_debug_driver(driver=None, job_url="", context="session"):
    # Do not quit the attached driver here; with debuggerAddress Chrome can close too.
    debug_data = get_debug_info(timeout=3)
    if not debug_data:
        restart_url = job_url or (SEARCH_URLS[0] if SEARCH_URLS else "")
        print(f"SESSION_RECOVER:restart_debug:{context}")
        if not restart_url or not start_debug_chrome(restart_url):
            print(f"FAILED:session_reconnect:{context}:debug_unavailable")
            return None
        debug_data = get_debug_info(timeout=3)
        if not debug_data:
            print(f"FAILED:session_reconnect:{context}:debug_unavailable")
            return None
    try:
        driver = build_debug_driver()
        _ = driver.current_url
        print("SESSION_RECOVER:reattach")
        print("Browser:", debug_data.get("Browser"))
        resume_url = job_url or (ACTIVE_APPLY_STATE.get("apply_url") if ACTIVE_APPLY_STATE.get("locked") else "")
        if resume_url:
            driver.get(resume_url)
            time.sleep(DETAIL_LOAD_WAIT)
            detect_and_lock_seek_apply_page(driver, job_key=ACTIVE_APPLY_STATE.get("job_key", ""), job_url=ACTIVE_APPLY_STATE.get("job_url", ""))
        return driver
    except Exception as exc:
        print(f"FAILED:session_reconnect:{context}:{exc}")
        return None


def init_driver():
    debug_data = get_debug_info(timeout=3)

    if debug_data:
        print("Debug Chrome running")
        print("Browser:", debug_data.get("Browser"))
        return build_debug_driver()

    print("Chrome debug mode running nahi hai; auto-start try kar rahe hain...")
    started = start_debug_chrome(SEARCH_URLS[0])
    if started and get_debug_info(timeout=2):
        return build_debug_driver()

    print("Fresh Chrome session start kiya (debug attach ke bina).")
    return webdriver.Chrome()


def normalize_text(value):
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def find_hits(haystack, keywords):
    hits = []
    for raw in keywords:
        key = normalize_text(raw)
        if key and key in haystack:
            hits.append(raw)
    return hits


def evaluate_match(title_text, detail_text):
    full_text = normalize_text(f"{title_text} {detail_text}")
    must_hits = find_hits(full_text, MUST_HAVE_KEYWORDS)
    preferred_hits = find_hits(full_text, PREFERRED_KEYWORDS)
    excluded_hits = find_hits(full_text, EXCLUDE_KEYWORDS)

    missing_must_have = [x for x in MUST_HAVE_KEYWORDS if x not in must_hits]

    score = 0
    score += len(must_hits) * MUST_HAVE_WEIGHT
    score += len(preferred_hits) * PREFERRED_WEIGHT
    score -= len(excluded_hits) * EXCLUDE_PENALTY
    score -= len(missing_must_have) * MUST_HAVE_MISSING_PENALTY

    return {
        "score": score,
        "eligible": score >= MIN_MATCH_SCORE,
        "matched_must_have": must_hits,
        "matched_preferred": preferred_hits,
        "missing_must_have": missing_must_have,
        "excluded_term_hit": excluded_hits,
    }


def safe_click(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.1)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    time.sleep(CLICK_PAUSE)


def open_jobs_page(driver, url):
    driver.get(url)
    time.sleep(PAGE_LOAD_WAIT)
    print("Jobs page opened")
    print("Title:", driver.title)
    print("URL:", driver.current_url)


def extract_job_key_from_href(href):
    href = (href or "").strip()
    if not href:
        return ""
    if "/job/" in href:
        return href.split("?")[0]
    return href


def get_job_entries(driver):
    selectors = [
        "//a[@data-automation='jobTitle' and contains(@href, '/job/')]",
        "//article//a[contains(@href, '/job/')]",
    ]

    raw = []
    for xp in selectors:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            href = (elem.get_attribute("href") or "").strip()
            if not href:
                continue
            title = (elem.text or "").strip() or "Untitled Job"
            key = extract_job_key_from_href(href)
            if not key:
                continue
            list_applied = False
            list_quick_apply = False
            try:
                card = elem.find_element(By.XPATH, "./ancestor::article[1]")
                card_text = normalize_text(card.text)
                list_applied = (
                    " applied " in f" {card_text} "
                    or "application sent" in card_text
                    or "you ve applied" in card_text
                )
                list_quick_apply = "quick apply" in card_text
            except Exception:
                list_applied = False
                list_quick_apply = False

            raw.append(
                {"key": key, "url": href, "title": title, "list_applied": list_applied, "list_quick_apply": list_quick_apply}
            )
        if raw:
            break

    dedup = {}
    for item in raw:
        dedup[item["key"]] = item
    return list(dedup.values())


def is_external_apply(driver):
    checks = [
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), \"advertiser's site\")]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'take you to the advertiser')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'external site')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply on company site')]",
    ]
    for xp in checks:
        if driver.find_elements(By.XPATH, xp):
            return True
    return False


def is_already_applied(driver):
    checks = [
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'application sent')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'already applied')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), \"you've applied\")]",
    ]
    for xp in checks:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            try:
                if elem.is_displayed():
                    return True
            except Exception:
                continue
    return False

def is_application_submitted(driver):
    checks = [
        "//*[@data-automation='application-confirmation']",
        "//*[@data-testid='application-confirmation']",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'application sent')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'application submitted')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'successfully applied')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), \"you've applied\")]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'application complete')]",
    ]
    for xp in checks:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            try:
                if elem.is_displayed():
                    return True
            except Exception:
                continue
    return False

def is_on_apply_interface(driver):
    current = (driver.current_url or "").lower()
    if "/apply" in current:
        return True
    checks = [
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'choose documents')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'review and submit')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'answer employer questions')]",
    ]
    for xp in checks:
        if driver.find_elements(By.XPATH, xp):
            return True
    return False


def is_review_submit_page(driver):
    try:
        current = (driver.current_url or "").lower()
        if "/apply/review" in current:
            return True
    except Exception as exc:
        raise_session_reconnect(exc, "is_review_submit_page_url")

    submit_checks = [
        "//button[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
        "//button[@type='submit'][.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
        "//button[.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]]",
        "//*[@data-testid='submit-application-button']",
        "//*[@data-automation='submit-application-button']",
        "//button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
        "//*[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]",
        "//button[@data-testid='submit-button']",
        "//button[@data-automation='submit-button']",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]",
        "//button[.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]]",
        "//button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit your application')]",
    ]
    continue_checks = [
        "//*[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
        "//button[@data-testid='continue-button']",
        "//button[@data-automation='continue-button']",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
        "//*[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
    ]
    review_checks = [
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'review and submit')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit your application')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'review application')]",
    ]

    def has_visible(selectors, context):
        for xp in selectors:
            try:
                elems = driver.find_elements(By.XPATH, xp)
            except Exception as exc:
                raise_session_reconnect(exc, context)
            for elem in elems:
                try:
                    if elem.is_displayed():
                        return True
                except Exception as exc:
                    if is_session_recoverable_error(exc):
                        raise SessionReconnectRequired(f"{context}_state") from exc
                    continue
        return False

    if has_visible(continue_checks, "is_review_submit_page_continue"):
        return False

    has_submit = has_visible(submit_checks, "is_review_submit_page_submit")
    if not has_submit:
        return False
    return has_visible(review_checks, "is_review_submit_page_review") or has_submit


def get_submit_application_selectors():
    return [
        "//button[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
        "//button[@type='submit'][.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
        "//button[.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]]",
        "//*[@data-testid='submit-application-button']",
        "//*[@data-automation='submit-application-button']",
        "//button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
        "//*[self::button or self::a][contains(@class, 'Button') and .//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]]",
    ]


def hard_submit_application(driver):
    selectors = get_submit_application_selectors()
    if not any_visible_selector(driver, selectors):
        return False
    if click_first_match(driver, selectors):
        return True

    try:
        submitted = driver.execute_script(
            r"""
            const selectors = [
              'button[type="submit"]',
              '[data-testid="submit-application-button"]',
              '[data-automation="submit-application-button"]',
              'button'
            ];
            const norm = value => (value || '').toLowerCase().replace(/\s+/g, ' ').trim();
            for (const selector of selectors) {
              for (const el of document.querySelectorAll(selector)) {
                const text = norm((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || ''));
                if (!text.includes('submit application')) continue;
                el.scrollIntoView({block: 'center', inline: 'nearest'});
                try { el.click(); } catch (e) {}
                try { ['mousedown','mouseup','click'].forEach(name => el.dispatchEvent(new MouseEvent(name, {bubbles:true,cancelable:true,view:window}))); } catch (e) {}
                const form = el.form || el.closest('form');
                if (form) {
                  try { if (form.requestSubmit) { form.requestSubmit(el); } else { form.submit(); } } catch (e) {}
                }
                return true;
              }
            }
            return false;
            """
        )
        return bool(submitted)
    except Exception as exc:
        raise_session_reconnect(exc, "hard_submit_application")


def is_seek_domain(url):
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return host.endswith("seek.com.au") or host.endswith("www.seek.com.au")


def classify_apply_target(target_url, attrs_text=""):
    url = (target_url or "").strip()
    attrs = normalize_text(attrs_text)
    if any(marker in attrs for marker in ["advertiser s site", "apply on company site", "external site", "apply with seek"]):
        return "external_handoff"
    if not url:
        if "quick apply" in attrs:
            return "seek_in_site"
        if "apply with seek" in attrs:
            return "external_handoff"
        return "unknown"
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if host and not is_seek_domain(url):
        return "external_handoff"
    if is_seek_domain(url) and "/job/" in path and "/apply" in path:
        return "seek_in_site"
    if is_seek_domain(url) and "/job/" in path:
        return "seek_job"
    return "external_handoff" if host else "unknown"


def build_apply_url(job_url):
    url = (job_url or "").strip()
    if not url or not is_seek_domain(url):
        return ""

    base = url.split("?")[0]
    match = re.search(r"(https?://[^/]+/job/\d+)", base)
    if match:
        return f"{match.group(1)}/apply"

    if "/job/" in base and not base.endswith("/apply"):
        return f"{base.rstrip('/')}/apply"

    return ""


def wait_for_apply_interface(driver, timeout=6):
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            if is_on_apply_interface(driver):
                return True
        except Exception as exc:
            raise_session_reconnect(exc, "wait_for_apply_interface")
        time.sleep(0.1)
    return False


def has_open_seek_apply_page(driver):
    try:
        current = (driver.current_url or "").strip()
    except Exception as exc:
        raise_session_reconnect(exc, "has_open_seek_apply_page")

    if classify_apply_target(current, current) == "seek_in_site":
        return True
    return is_on_apply_interface(driver)


def wait_for_apply_transition(driver, original_url, timeout=12):
    end_time = time.time() + timeout
    baseline_url = (original_url or "").lower()
    while time.time() < end_time:
        try:
            current = (driver.current_url or "").lower()
            if has_open_seek_apply_page(driver):
                return True
            if current != baseline_url and "/apply" in current and is_seek_domain(current):
                return True
        except Exception as exc:
            raise_session_reconnect(exc, "wait_for_apply_transition")
        time.sleep(0.1)
    return False


def classify_current_location(driver):
    try:
        current = (driver.current_url or "").strip()
    except Exception as exc:
        raise_session_reconnect(exc, "classify_current_location")
    if not current:
        return "unknown"
    return classify_apply_target(current, current)


def find_seek_window_handle(driver):
    try:
        handles = driver.window_handles
        current_handle = driver.current_window_handle
    except Exception as exc:
        raise_session_reconnect(exc, "find_seek_window_handle")
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            current_url = (driver.current_url or "").strip()
            if is_seek_domain(current_url) or current_url.lower().startswith("chrome://"):
                return handle
        except Exception as exc:
            if is_session_recoverable_error(exc):
                raise SessionReconnectRequired("find_seek_window_handle_state") from exc
            continue
    try:
        driver.switch_to.window(current_handle)
    except Exception:
        pass
    return ""


def close_external_target_and_return(driver, original_handle=None):
    host = ""
    try:
        current = (driver.current_url or "").strip()
        host = (urlparse(current).netloc or "").lower()
    except Exception:
        current = ""
    try:
        handles = driver.window_handles
    except Exception as exc:
        raise_session_reconnect(exc, "close_external_target_handles")

    if original_handle and len(handles) > 1:
        try:
            current_handle = driver.current_window_handle
            if current_handle != original_handle:
                driver.close()
                remaining_handles = driver.window_handles
                if original_handle in remaining_handles:
                    driver.switch_to.window(original_handle)
                else:
                    seek_handle = find_seek_window_handle(driver)
                    if seek_handle:
                        driver.switch_to.window(seek_handle)
                return True, host
        except Exception as exc:
            if is_session_recoverable_error(exc):
                return False, host
            raise_session_reconnect(exc, "close_external_target_close_tab")

    try:
        if driver.execute_script("return window.history.length"):
            driver.back()
            time.sleep(CLICK_PAUSE)
            return False, host
    except Exception as exc:
        if is_session_recoverable_error(exc):
            return False, host
    return False, host


def switch_to_new_tab_if_any(driver):
    try:
        handles = driver.window_handles
        if len(handles) <= 1:
            return
        driver.switch_to.window(handles[-1])
    except Exception as exc:
        raise_session_reconnect(exc, "switch_to_new_tab")


def click_apply(driver, job_url):
    base_selectors = [
        "//*[@data-automation='job-detail-apply']",
        "//*[@data-testid='job-detail-apply']",
        "//main//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'quick apply')]",
        "//main//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'quick apply')]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'quick apply')]",
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'quick apply')]",
    ]
    if QUICK_APPLY_ONLY:
        possible = base_selectors
    else:
        possible = base_selectors + [
            "//main//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply')]",
            "//main//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply')]",
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply')]",
            "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply')]",
        ]

    saw_candidate = False
    saw_quick_candidate = False
    try:
        origin_url = driver.current_url
        origin_handle = driver.current_window_handle
    except Exception as exc:
        raise_session_reconnect(exc, "click_apply_origin")

    for xp in possible:
        try:
            elems = driver.find_elements(By.XPATH, xp)
        except Exception as exc:
            raise_session_reconnect(exc, "click_apply_find")
        for btn in elems:
            try:
                if not btn.is_displayed() or not btn.is_enabled():
                    continue
            except Exception as exc:
                if is_session_recoverable_error(exc):
                    raise SessionReconnectRequired("click_apply_button_state") from exc
                continue

            try:
                attrs = " ".join(
                    [
                        btn.text or "",
                        btn.get_attribute("aria-label") or "",
                        btn.get_attribute("title") or "",
                        btn.get_attribute("data-automation") or "",
                        btn.get_attribute("data-testid") or "",
                        btn.get_attribute("href") or "",
                    ]
                )
            except Exception as exc:
                raise_session_reconnect(exc, "click_apply_button_attrs")
            text_btn = normalize_text(attrs)
            is_quick_signal = "quick apply" in text_btn
            if QUICK_APPLY_ONLY and not is_quick_signal:
                continue

            saw_candidate = True
            if is_quick_signal:
                saw_quick_candidate = True
            try:
                btn_href = (btn.get_attribute("href") or "").strip()
            except Exception as exc:
                raise_session_reconnect(exc, "click_apply_button_href")

            target_kind = classify_apply_target(btn_href, attrs)
            if target_kind == "external_handoff":
                return "external_target"

            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                btn.click()
                print("APPLY_CLICK:normal")
                time.sleep(CLICK_PAUSE)
                switch_to_new_tab_if_any(driver)
                if detect_and_lock_seek_apply_page(driver, job_url=job_url):
                    print("APPLY_OPEN:detected_interface")
                    return "opened"
                current_kind = classify_current_location(driver)
                if current_kind == "external_handoff" or is_external_apply(driver):
                    closed_tab, host = close_external_target_and_return(driver, origin_handle)
                    print(f"SKIP_EXTERNAL_HOST:{host or 'unknown'}")
                    return "external_target_closed_tab" if closed_tab else "external_target"
                if wait_for_apply_transition(driver, origin_url, timeout=12) and detect_and_lock_seek_apply_page(driver, job_url=job_url):
                    print("APPLY_OPEN:detected_interface")
                    return "opened"
            except Exception as exc:
                if detect_and_lock_seek_apply_page(driver, job_url=job_url):
                    print("APPLY_OPEN:detected_interface")
                    return "opened"
                if is_session_recoverable_error(exc):
                    raise SessionReconnectRequired("apply_click_normal") from exc

            try:
                driver.execute_script("arguments[0].click();", btn)
                print("APPLY_CLICK:js")
                time.sleep(CLICK_PAUSE)
                switch_to_new_tab_if_any(driver)
                if detect_and_lock_seek_apply_page(driver, job_url=job_url):
                    print("APPLY_OPEN:detected_interface")
                    return "opened"
                current_kind = classify_current_location(driver)
                if current_kind == "external_handoff" or is_external_apply(driver):
                    closed_tab, host = close_external_target_and_return(driver, origin_handle)
                    print(f"SKIP_EXTERNAL_HOST:{host or 'unknown'}")
                    return "external_target_closed_tab" if closed_tab else "external_target"
                if wait_for_apply_transition(driver, origin_url, timeout=12) and detect_and_lock_seek_apply_page(driver, job_url=job_url):
                    print("APPLY_OPEN:detected_interface")
                    return "opened"
            except Exception as exc:
                if detect_and_lock_seek_apply_page(driver, job_url=job_url):
                    print("APPLY_OPEN:detected_interface")
                    return "opened"
                if is_session_recoverable_error(exc):
                    raise SessionReconnectRequired("apply_click_js") from exc

            if detect_and_lock_seek_apply_page(driver, job_url=job_url):
                print("APPLY_OPEN:detected_interface")
                return "opened"

            if btn_href and "/apply" in btn_href and classify_apply_target(btn_href, attrs) == "seek_in_site" and not detect_and_lock_seek_apply_page(driver, job_url=job_url, switch=False):
                try:
                    driver.get(btn_href)
                    print("APPLY_CLICK:href")
                    current_kind = classify_current_location(driver)
                    if current_kind == "external_handoff" or is_external_apply(driver):
                        closed_tab, host = close_external_target_and_return(driver, origin_handle)
                        print(f"SKIP_EXTERNAL_HOST:{host or 'unknown'}")
                        return "external_target_closed_tab" if closed_tab else "external_target"
                    if detect_and_lock_seek_apply_page(driver, job_url=job_url) or wait_for_apply_transition(driver, origin_url, timeout=10):
                        detect_and_lock_seek_apply_page(driver, job_url=job_url)
                        print(f"APPLY_BUTTON_HREF:{btn_href}")
                        return "opened"
                except Exception as exc:
                    if detect_and_lock_seek_apply_page(driver, job_url=job_url):
                        print("APPLY_OPEN:detected_interface")
                        return "opened"
                    if is_session_recoverable_error(exc):
                        raise SessionReconnectRequired("apply_click_href") from exc

    allow_fallback = DIRECT_APPLY_URL_FALLBACK and (not QUICK_APPLY_ONLY or saw_quick_candidate) and not detect_and_lock_seek_apply_page(driver, job_url=job_url, switch=False)
    if allow_fallback:
        apply_url = build_apply_url(job_url)
        if apply_url and classify_apply_target(apply_url, "quick apply") == "seek_in_site":
            try:
                driver.get(apply_url)
                print("APPLY_CLICK:fallback_url")
                current_kind = classify_current_location(driver)
                if current_kind == "external_handoff" or is_external_apply(driver):
                    closed_tab, host = close_external_target_and_return(driver, origin_handle)
                    print(f"SKIP_EXTERNAL_HOST:{host or 'unknown'}")
                    return "external_target_closed_tab" if closed_tab else "external_precheck"
                if detect_and_lock_seek_apply_page(driver, job_url=job_url) or wait_for_apply_transition(driver, origin_url, timeout=10):
                    detect_and_lock_seek_apply_page(driver, job_url=job_url)
                    print(f"APPLY_FALLBACK_URL:{apply_url}")
                    return "opened"
            except Exception as exc:
                if detect_and_lock_seek_apply_page(driver, job_url=job_url):
                    print("APPLY_OPEN:detected_interface")
                    return "opened"
                if is_session_recoverable_error(exc):
                    raise SessionReconnectRequired("apply_click_fallback") from exc

    if QUICK_APPLY_ONLY and not saw_quick_candidate:
        non_quick_selectors = [
            "//main//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply')]",
            "//main//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply')]",
            "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply')]",
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply')]",
        ]
        for xp in non_quick_selectors:
            try:
                elems = driver.find_elements(By.XPATH, xp)
            except Exception as exc:
                raise_session_reconnect(exc, "click_apply_non_quick_find")
            for elem in elems:
                try:
                    text = normalize_text(elem.text)
                    if elem.is_displayed() and "apply" in text and "quick apply" not in text:
                        return "not_quick_apply"
                except Exception as exc:
                    if is_session_recoverable_error(exc):
                        raise SessionReconnectRequired("click_apply_non_quick_state") from exc
                    continue

    if saw_candidate:
        return "visible_but_not_opened"
    return "not_found"


def click_first_match(driver, selectors):
    candidates = []
    seen_ids = set()
    for xp in selectors:
        try:
            elems = driver.find_elements(By.XPATH, xp)
        except Exception as exc:
            raise_session_reconnect(exc, "click_first_match_find")
        for elem in elems:
            try:
                candidate = elem
                try:
                    resolved = driver.execute_script(
                        'const el=arguments[0]; return el && el.closest ? el.closest("button, a, [role=\"button\"], input[type=\"submit\"], input[type=\"button\"]") : el;',
                        elem,
                    )
                    if resolved is not None:
                        candidate = resolved
                except Exception:
                    candidate = elem

                if not candidate.is_displayed() or not candidate.is_enabled():
                    continue
                elem_id = getattr(candidate, "id", None) or id(candidate)
                if elem_id in seen_ids:
                    continue
                seen_ids.add(elem_id)
                tag = (candidate.tag_name or "").lower()
                elem_type = (candidate.get_attribute("type") or "").lower()
                text_blob = normalize_text(" ".join([
                    candidate.text or "",
                    candidate.get_attribute("aria-label") or "",
                    candidate.get_attribute("title") or "",
                    candidate.get_attribute("data-testid") or "",
                    candidate.get_attribute("data-automation") or "",
                ]))
                y = 0
                x = 0
                width = 0
                height = 0
                try:
                    location = candidate.location_once_scrolled_into_view or {}
                    size = candidate.size or {}
                    y = int(location.get("y", 0))
                    x = int(location.get("x", 0))
                    width = int(size.get("width", 0))
                    height = int(size.get("height", 0))
                except Exception:
                    pass
                priority = 0
                if tag == "button":
                    priority += 25
                if elem_type == "submit":
                    priority += 35
                if tag == "a":
                    priority -= 10
                if width >= 80 and height >= 28:
                    priority += 10
                if text_blob == "submit application":
                    priority += 60
                elif "submit application" in text_blob:
                    priority += 45
                elif text_blob == "submit":
                    priority += 35
                elif "submit" in text_blob:
                    priority += 25
                elif text_blob == "continue":
                    priority += 30
                elif "continue" in text_blob:
                    priority += 20
                elif "next" in text_blob:
                    priority += 15
                candidates.append((priority, y, width * height, candidate, text_blob))
            except Exception as exc:
                if is_session_recoverable_error(exc):
                    raise SessionReconnectRequired("click_first_match_collect") from exc
                continue

    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    for _priority, _y, _area, elem, text_blob in candidates:
        try:
            block = "end" if any(token in text_blob for token in ("continue", "submit", "next", "review")) else "center"
            driver.execute_script("arguments[0].scrollIntoView({block: arguments[1], inline: 'nearest'});", elem, block)
            try:
                elem.click()
            except Exception:
                try:
                    ActionChains(driver).move_to_element(elem).pause(0.1).click(elem).perform()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", elem)
                    except Exception:
                        try:
                            driver.execute_script(
                                "const el=arguments[0]; ['mousedown','mouseup','click'].forEach(name => el.dispatchEvent(new MouseEvent(name, {bubbles:true,cancelable:true,view:window})));",
                                elem,
                            )
                        except Exception:
                            try:
                                elem.send_keys(Keys.ENTER)
                            except Exception:
                                submitted = driver.execute_script(
                                    "const el=arguments[0]; const form=el.form || el.closest('form'); if(form){ if(form.requestSubmit){ form.requestSubmit(el); } else { form.submit(); } return true; } return false;",
                                    elem,
                                )
                                if not submitted:
                                    raise
            time.sleep(CLICK_PAUSE)
            return True
        except Exception as exc:
            if is_session_recoverable_error(exc):
                raise SessionReconnectRequired("click_first_match_click") from exc
            continue
    return False


def get_job_text_snapshot(driver):
    title = ""
    for xp in ["//h1", "//*[@data-automation='job-detail-title']"]:
        elems = driver.find_elements(By.XPATH, xp)
        if elems:
            title = (elems[0].text or "").strip()
            if title:
                break

    blocks = []
    for xp in [
        "//*[@data-automation='jobAdDetails']",
        "//*[contains(@data-automation, 'job-detail')]",
        "//main",
    ]:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            text = (elem.text or "").strip()
            if text:
                blocks.append(text)
        if blocks:
            break

    return title, "\n".join(blocks).strip()


def select_resume_if_present(driver, target_name="Agastya Resume.pdf"):
    page_text = normalize_text(driver.page_source)
    if target_name.lower() in page_text:
        selectors = [
            f"//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{normalize_text(target_name)}')]",
            f"//option[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{normalize_text(target_name)}')]",
        ]
        if click_first_match(driver, selectors):
            print(f"RESUME_SELECT:{target_name}")
            return True
    print("RESUME_SELECT:keep_current")
    return False


def get_field_context_text(driver, elem):
    try:
        text = driver.execute_script(
            """
            const el = arguments[0];
            const container = el.closest('fieldset, section, form, div');
            return (container ? container.innerText : (el.innerText || el.textContent || '')) || '';
            """,
            elem,
        )
    except Exception as exc:
        raise_session_reconnect(exc, "get_field_context_text")
    return normalize_text(text)


def select_first_matching_option(select_elem, match_tokens):
    try:
        sel = Select(select_elem)
        options = sel.options
    except Exception:
        return False

    for option in options:
        text = normalize_text(option.text)
        value = normalize_text(option.get_attribute("value") or "")
        if not text or text in ("select", "select one", "please select"):
            continue
        if any(token in text or token in value for token in match_tokens):
            try:
                sel.select_by_visible_text(option.text)
                return True
            except Exception:
                try:
                    sel.select_by_value(option.get_attribute("value") or "")
                    return True
                except Exception:
                    continue
    return False


def answer_common_select_questions(driver):
    changed = False
    try:
        selects = driver.find_elements(By.XPATH, "//select[not(@disabled)]")
    except Exception as exc:
        raise_session_reconnect(exc, "answer_common_select_questions_find")

    for select_elem in selects:
        try:
            if not select_elem.is_displayed() or not select_elem.is_enabled():
                continue
            current_value = normalize_text(select_elem.get_attribute("value") or "")
            selected_text = ""
            try:
                selected_text = normalize_text(Select(select_elem).first_selected_option.text)
            except Exception:
                selected_text = ""
            if current_value and selected_text not in ("", "select", "select one", "please select"):
                continue

            context = get_field_context_text(driver, select_elem)
            if not context:
                continue

            if any(token in context for token in ["right to work", "work rights", "work in australia", "visa"]):
                if select_first_matching_option(select_elem, ["temporary visa", "student visa", "restrictions on work hours"]):
                    print("EMPLOYER_Q:work_rights=temp_visa")
                    changed = True
                    continue

            if any(token in context for token in ["years experience", "how many years", "experience do you have"]):
                if select_first_matching_option(select_elem, ["0", "0-1", "less than 1", "under 1", "1 year", "1-2"]):
                    print("EMPLOYER_Q:experience=conservative")
                    changed = True
                    continue
        except Exception as exc:
            if is_session_recoverable_error(exc):
                raise SessionReconnectRequired("answer_common_select_questions_state") from exc
            continue
    return changed


def click_visible_label_choice(driver, label_text):
    xpath = f"//label[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{label_text}')]"
    try:
        elems = driver.find_elements(By.XPATH, xpath)
    except Exception as exc:
        raise_session_reconnect(exc, "click_visible_label_choice_find")

    for elem in elems:
        try:
            if not elem.is_displayed():
                continue
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
            try:
                elem.click()
            except Exception:
                driver.execute_script("arguments[0].click();", elem)
            time.sleep(0.2)
            return True
        except Exception as exc:
            if is_session_recoverable_error(exc):
                raise SessionReconnectRequired("click_visible_label_choice_state") from exc
            continue
    return False


def answer_known_employer_questions(driver):
    text = normalize_text(driver.page_source)
    changed = False

    changed = answer_common_select_questions(driver) or changed

    if "rsa" in text or "responsible service of alcohol" in text:
        if click_visible_label_choice(driver, "no"):
            print("EMPLOYER_Q:rsa=no")
            changed = True

    yes_selectors = [
        "//label[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'yes')]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'yes')]",
    ]
    keywords = ["driver", "driver's licence", "right to work", "work rights", "australia"]
    matched = any(k in text for k in keywords)
    if not matched:
        return changed

    for xp in yes_selectors:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            try:
                if not elem.is_displayed() or not elem.is_enabled():
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
                try:
                    elem.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", elem)
                print("EMPLOYER_Q:yes")
                time.sleep(0.2)
                return True
            except Exception:
                continue
    return changed


def has_unanswered_required_questions(driver):
    # Strict blockers only: invalid required controls and visible error messages.
    strict_markers = [
        "//*[@aria-invalid='true' and (self::input or self::textarea or self::select)]",
        "//*[@aria-required='true' and (self::input or self::textarea or self::select) and normalize-space(@value)='']",
        "//input[@required and not(@disabled) and normalize-space(@value)='']",
        "//textarea[@required and not(@disabled) and normalize-space(.)='']",
        "//select[@required and not(@disabled) and (not(@value) or @value='')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'please make a selection')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'please answer')]",
    ]

    for xp in strict_markers:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            try:
                if elem.is_displayed():
                    return True
            except Exception:
                continue

    radio_group_script = """
    const groups = new Map();
    const radios = Array.from(document.querySelectorAll('input[type="radio"]:not([disabled])'));
    for (const radio of radios) {
      const name = radio.name || radio.id;
      if (!name) continue;
      const required = radio.required || radio.getAttribute('aria-required') === 'true';
      if (!required) continue;
      const visible = !!(radio.offsetWidth || radio.offsetHeight || radio.getClientRects().length);
      if (!visible) continue;
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(radio);
    }
    for (const items of groups.values()) {
      if (!items.some(r => r.checked)) return true;
    }
    return false;
    """
    try:
        if driver.execute_script(radio_group_script):
            return True
    except Exception as exc:
        raise_session_reconnect(exc, "has_unanswered_required_questions_radios")
    return False



def prepare_active_application(driver):
    select_resume_if_present(driver, "Agastya Resume.pdf")
    answer_known_employer_questions(driver)
    return handle_resume_upload(driver)


def get_apply_page_signature(driver, phase=None):
    try:
        current = (driver.current_url or "").lower().strip()
        action = get_primary_action_name(driver, phase or get_current_flow_phase(driver))
        main_text = ""
        for xp in ["//main", "//*[@data-automation='application-form']", "//*[@data-testid='application-form']"]:
            elems = driver.find_elements(By.XPATH, xp)
            for elem in elems:
                try:
                    if not elem.is_displayed():
                        continue
                    main_text = normalize_text(elem.text)[:800]
                    if main_text:
                        break
                except Exception as exc:
                    if is_session_recoverable_error(exc):
                        raise SessionReconnectRequired("get_apply_page_signature_state") from exc
                    continue
            if main_text:
                break
        return "|".join([
            current,
            phase or "",
            action,
            "questions" if is_employer_questions_step(driver) else "no_questions",
            main_text,
        ])
    except Exception as exc:
        raise_session_reconnect(exc, "get_apply_page_signature")


def find_autoit_binary():
    candidates = [
        shutil.which("AutoIt3"),
        shutil.which("AutoIt3.exe"),
        r"C:\Program Files (x86)\AutoIt3\AutoIt3.exe",
        r"C:\Program Files\AutoIt3\AutoIt3.exe",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def run_upload_script(file_path):
    target = normalize_path(file_path)
    if not target or not os.path.exists(target):
        print(f"UPLOAD_FAIL:file_missing:{target}")
        return False

    script_exe_path = normalize_path(SCRIPT_EXE)
    script_au3_path = normalize_path(SCRIPT_AU3)

    if script_au3_path and os.path.exists(script_au3_path):
        autoit_bin = find_autoit_binary()
        if autoit_bin:
            try:
                completed = subprocess.run([autoit_bin, script_au3_path, target], timeout=20)
                return completed.returncode == 0
            except Exception as e:
                print(f"UPLOAD_FAIL:script_au3:{e}")

    if script_exe_path and os.path.exists(script_exe_path):
        try:
            completed = subprocess.run([script_exe_path, target], timeout=20)
            return completed.returncode == 0
        except Exception as e:
            print(f"UPLOAD_FAIL:script_exe:{e}")

    print("UPLOAD_FAIL:no_executable_upload_runner")
    return False


def click_upload_trigger(driver, label):
    needle = normalize_text(label)
    selectors = [
        f"//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{needle}')]",
        f"//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{needle}')]",
        f"//label[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{needle}')]",
    ]
    return click_first_match(driver, selectors)


def handle_resume_upload(driver):
    if not FORCE_RESUME_UPLOAD:
        print("UPLOAD:skip_force_resume_upload=False")
        return True

    resume_path = normalize_path(RESUME_FILE)
    cover_path = normalize_path(COVER_LETTER_FILE)

    resume_triggered = click_upload_trigger(driver, "upload a resume") or click_upload_trigger(driver, "resume")
    cover_triggered = click_upload_trigger(driver, "cover letter")

    if not resume_triggered and not cover_triggered:
        print("UPLOAD:skipped:not_requested")
        return True

    if resume_triggered and not run_upload_script(resume_path):
        return False

    if cover_triggered and cover_path and not run_upload_script(cover_path):
        return False

    print("UPLOAD:forced:ok")
    return True


def ensure_log_paths():
    os.makedirs(BEFORE_SCREENSHOT_DIR, exist_ok=True)
    os.makedirs(AFTER_SCREENSHOT_DIR, exist_ok=True)


def safe_filename(value):
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", value or "")
    return clean.strip("_") or "job"


def capture_job_screenshot(driver, job_key, status, phase="after"):
    ensure_log_paths()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{stamp}_{safe_filename(job_key)}_{safe_filename(status)}.png"
    target_dir = BEFORE_SCREENSHOT_DIR if phase == "before" else AFTER_SCREENSHOT_DIR
    out_path = os.path.join(target_dir, fname)
    try:
        driver.save_screenshot(out_path)
        return out_path
    except Exception:
        return ""


def extract_company_and_position(driver, fallback_title):
    position = (fallback_title or "").strip()
    company = ""

    title_selectors = [
        "//h1",
        "//*[@data-automation='job-detail-title']",
        "//*[@data-testid='job-title']",
    ]
    for xp in title_selectors:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            t = (elem.text or "").strip()
            if t:
                position = t
                break
        if position:
            break

    company_selectors = [
        "//*[@data-automation='advertiser-name']",
        "//*[@data-testid='advertiser-name']",
        "//a[contains(@href, '/companies/') and normalize-space(.)!='']",
        "//span[contains(@data-automation, 'advertiser') and normalize-space(.)!='']",
    ]
    for xp in company_selectors:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            c = (elem.text or "").strip()
            if c:
                company = c
                break
        if company:
            break

    if not company:
        text_blob = (driver.page_source or "")[:2000]
        m = re.search(r"by\s+([A-Za-z0-9 &.,'-]{2,60})", text_blob)
        if m:
            company = m.group(1).strip()

    return company or "Unknown", position or "Unknown"


def _normalize_spaces(value):
    return re.sub(r"\s+", " ", (value or "")).strip()


def _has_blocked_identifier(value):
    lowered = (value or "").lower()
    return any(token in lowered for token in BLOCKED_HR_IDENTIFIERS)


def build_hr_context_text(driver, title_text, detail_text):
    parts = []
    for chunk in [title_text or "", detail_text or ""]:
        if chunk and chunk not in parts:
            parts.append(chunk)

    selectors = [
        "//*[@data-automation='jobAdDetails']",
        "//*[@data-automation='advertiser-name']/ancestor::*[1]",
        "//main//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'recruit')]",
        "//main//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'hiring manager')]",
        "//main//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'contact')]",
        "//main//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'talent acquisition')]",
    ]
    for xp in selectors:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            try:
                if not elem.is_displayed():
                    continue
                txt = _normalize_spaces(elem.text)
                if txt and txt not in parts:
                    parts.append(txt)
            except Exception:
                continue

    return "\n".join(parts)


def extract_hr_profile_link(driver):
    links = []
    for xp in ["//main//a[@href]", "//a[contains(@href, '/companies/')]"]:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            try:
                href = (elem.get_attribute("href") or "").strip()
                text = _normalize_spaces(elem.text).lower()
                if not href:
                    continue
                links.append((urljoin(driver.current_url, href), text))
            except Exception:
                continue

    for href, text in links:
        h = href.lower()
        if any(k in text for k in ["recruit", "hiring", "talent", "contact"]):
            return href
        if any(k in h for k in ["linkedin.com", "/recruit", "/contact"]):
            return href

    for href, _text in links:
        if "/companies/" in href.lower():
            return href

    return ""


def extract_hr_details(text_blob):
    text = text_blob or ""
    hr_name = ""
    hr_email = ""
    hr_contact = ""

    windows = []
    for token in ["recruiter", "hiring manager", "talent acquisition", "contact"]:
        idx = text.lower().find(token)
        while idx != -1:
            start = max(0, idx - 120)
            end = min(len(text), idx + 320)
            windows.append(text[start:end])
            idx = text.lower().find(token, idx + 1)
    if not windows:
        windows = [text]

    emails = []
    for chunk in windows:
        emails.extend(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", chunk))
    for email in emails:
        e = email.strip()
        domain = e.split("@")[-1].lower() if "@" in e else ""
        if _has_blocked_identifier(e):
            continue
        if domain in FREE_EMAIL_DOMAINS:
            continue
        hr_email = e
        break

    phones = []
    for chunk in windows:
        phones.extend(re.findall(r"(?:\+?\d[\d\s()\-]{7,}\d)", chunk))
    for phone in phones:
        p = _normalize_spaces(phone)
        if _has_blocked_identifier(p):
            continue
        digits = re.sub(r"\D", "", p)
        if len(digits) < 8:
            continue
        hr_contact = p
        break

    name_patterns = [
        r"(?:recruiter|hiring manager|contact|talent acquisition)\s*[:\-]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})",
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*(?:\(|-)\s*(?:recruiter|hiring manager|talent acquisition|contact)",
    ]
    for chunk in windows:
        for pat in name_patterns:
            m = re.search(pat, chunk, flags=re.IGNORECASE)
            if m:
                candidate = _normalize_spaces(m.group(1))
                if _has_blocked_identifier(candidate):
                    continue
                hr_name = candidate
                break
        if hr_name:
            break

    if _has_blocked_identifier(hr_name):
        hr_name = ""
    if _has_blocked_identifier(hr_email):
        hr_email = ""
    if _has_blocked_identifier(hr_contact):
        hr_contact = ""

    return hr_name, hr_email, hr_contact


def load_today_submitted_job_keys():
    today = datetime.now().strftime("%d-%m-%Y")
    submitted = set()
    if not os.path.exists(CSV_LOG_PATH):
        return submitted
    try:
        with open(CSV_LOG_PATH, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row:
                    continue
                if (row.get("status") or "").strip().lower() != "submitted":
                    continue
                if (row.get("date") or "").strip() != today:
                    continue
                job_link = (row.get("job_link") or "").strip()
                key = extract_job_key_from_href(job_link)
                if key:
                    submitted.add(key)
    except Exception:
        return submitted
    return submitted


def append_apply_log(
    company_name,
    position,
    job_link,
    status,
    screenshot_path="",
    before_screenshot_path="",
    hr_name="",
    hr_email="",
    hr_contact="",
    hr_profile_link="",
):
    if status != "submitted":
        return

    ensure_log_paths()
    header = [
        "date",
        "company_name",
        "position",
        "job_link",
        "status",
        "hr_name",
        "hr_email",
        "hr_contact",
        "hr_profile_link",
    ]

    rewrite_header = False
    if os.path.exists(CSV_LOG_PATH):
        try:
            with open(CSV_LOG_PATH, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            if not rows or rows[0] != header:
                rewrite_header = True
        except Exception:
            rewrite_header = True
    else:
        rewrite_header = True

    if rewrite_header:
        with open(CSV_LOG_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)

    if not any([hr_name, hr_email, hr_contact]):
        hr_name, hr_email, hr_contact = extract_hr_details(LAST_HR_TEXT)
    if not hr_profile_link:
        hr_profile_link = LAST_HR_LINK

    with open(CSV_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%d-%m-%Y"),
            company_name,
            position,
            job_link,
            "submitted",
            hr_name,
            hr_email,
            hr_contact,
            hr_profile_link,
        ])


def is_employer_questions_step(driver):
    current = (driver.current_url or "").lower()
    if "role-requirements" in current or "employer-questions" in current:
        return True
    checks = [
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'answer employer questions')]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'before you can continue with the application')]",
    ]
    for xp in checks:
        if driver.find_elements(By.XPATH, xp):
            return True
    return False


def wait_for_manual_required_answers(driver):
    if not WAIT_FOR_MANUAL_QUESTIONS:
        return "blocked_questions"

    interval = max(0.5, MANUAL_QUESTION_SCAN_INTERVAL)
    print("MANUAL_WAIT:start mode=infinite")
    last_ping = time.time()
    while True:
        if is_application_submitted(driver):
            return "submitted"
        if not has_unanswered_required_questions(driver):
            print("MANUAL_WAIT:resolved")
            return "resolved"

        now = time.time()
        if now - last_ping >= 30:
            print("MANUAL_WAIT:still_waiting")
            last_ping = now
        time.sleep(interval)

def get_quick_apply_step_selectors():
    pre_review_steps = [
        (
            "CONTINUE",
            [
                "//button[@type='submit'][.//text()[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]]",
                "//*[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
                "//button[@data-testid='continue-button']",
                "//button[@data-automation='continue-button']",
                "//button[contains(@aria-label, 'Continue')]",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
                "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
            ],
        ),
        (
            "NEXT",
            [
                "//*[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
                "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
            ],
        ),
        (
            "REVIEW",
            [
                "//*[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'review')]",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'review')]",
                "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'review')]",
            ],
        ),
    ]
    review_submit_steps = [
        (
            "SUBMIT_APPLICATION",
            [
                "//button[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
                "//button[@type='submit'][.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]]",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
                "//button[.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]]",
                "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
                "//*[@data-testid='submit-application-button']",
                "//*[@data-automation='submit-application-button']",
                "//button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit application')]",
            ],
        ),
        (
            "SUBMIT",
            [
                "//button[@type='submit' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]",
                "//button[@type='submit'][.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]]",
                "//*[@data-testid='submit-button']",
                "//*[@data-automation='submit-button']",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]",
                "//button[.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]]",
                "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]",
                "//button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]",
            ],
        ),
        (
            "YES",
            [
                "//button[normalize-space(.)='Yes']",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'yes')]",
                "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'yes')]",
            ],
        ),
    ]
    return {"pre_review": pre_review_steps, "review_submit": review_submit_steps}


def get_current_flow_phase(driver):
    if is_review_submit_page(driver):
        return "review_submit"
    return "pre_review"


def any_visible_selector(driver, selectors):
    for xp in selectors:
        try:
            elems = driver.find_elements(By.XPATH, xp)
        except Exception as exc:
            raise_session_reconnect(exc, "any_visible_selector")
        for elem in elems:
            try:
                if elem.is_displayed() and elem.is_enabled():
                    return True
            except Exception as exc:
                if is_session_recoverable_error(exc):
                    raise SessionReconnectRequired("any_visible_selector_state") from exc
                continue
    return False


def get_primary_cta_sequence():
    step_groups = get_quick_apply_step_selectors()
    selector_map = {}
    for group_steps in step_groups.values():
        for step_name, selectors in group_steps:
            selector_map[step_name] = selectors
    ordered_steps = ["SUBMIT_APPLICATION", "SUBMIT", "CONTINUE", "NEXT", "REVIEW", "YES"]
    return [(step_name, selector_map.get(step_name, [])) for step_name in ordered_steps if selector_map.get(step_name)]


def get_primary_action_name(driver, phase=None):
    phase = phase or get_current_flow_phase(driver)
    step_groups = get_quick_apply_step_selectors()
    pre_review_steps = step_groups.get("pre_review", [])
    submit_steps = step_groups.get("review_submit", [])

    for step_name, selectors in pre_review_steps:
        if any_visible_selector(driver, selectors):
            return step_name

    if phase == "review_submit":
        for step_name, selectors in submit_steps:
            if any_visible_selector(driver, selectors):
                return step_name
    else:
        for step_name, selectors in submit_steps:
            if any_visible_selector(driver, selectors):
                return step_name
    return ""


def get_primary_action_selectors(step_name):
    for name, selectors in get_primary_cta_sequence():
        if name == step_name:
            return selectors
    return []


def should_prepare_active_application(driver):
    return not is_review_submit_page(driver)


def wait_for_step_progress(driver, before_url, before_phase, before_action, before_signature="", before_question_state=False, timeout=4):
    end_time = time.time() + timeout
    baseline = (before_url or "").lower()
    while time.time() < end_time:
        try:
            if is_application_submitted(driver):
                return True
            current = (driver.current_url or "").lower()
            current_phase = get_current_flow_phase(driver)
            current_action = get_primary_action_name(driver, current_phase)
            current_question_state = is_employer_questions_step(driver)
            current_signature = get_apply_page_signature(driver, current_phase)
            if current != baseline:
                return True
            if current_phase != before_phase:
                return True
            if current_question_state != before_question_state:
                return True
            if current_action and current_action != before_action:
                return True
            if before_signature and current_signature != before_signature:
                return True
        except Exception as exc:
            raise_session_reconnect(exc, "wait_for_step_progress")
        time.sleep(0.15)
    return False


def run_quick_apply_flow(driver):
    idle_cycles = 0
    last_wait_log = 0
    same_page_count = 0
    same_review_page_count = 0
    prepared_signatures = set()
    while True:
        try:
            refresh_active_apply_state(driver)
            if is_external_apply(driver):
                return "external"

            if is_application_submitted(driver):
                return "submitted"

            phase = get_current_flow_phase(driver)
            current_signature = get_apply_page_signature(driver, phase)
            if phase == "review_submit":
                print("FLOW_PHASE:review_submit")
            if should_prepare_active_application(driver) and current_signature not in prepared_signatures:
                if not prepare_active_application(driver):
                    return "resume_upload_failed"
                prepared_signatures.add(current_signature)
                current_signature = get_apply_page_signature(driver, phase)

            if is_employer_questions_step(driver) and has_unanswered_required_questions(driver):
                print("APPLY_WAIT:manual_questions")
                manual_state = wait_for_manual_required_answers(driver)
                if manual_state == "submitted":
                    return "submitted"
                if manual_state == "resolved":
                    idle_cycles = 0
                    continue

            progressed = False
            clicked_step = False
            step_name = get_primary_action_name(driver, phase)
            if not step_name and detect_and_lock_seek_apply_page(driver, switch=False):
                phase = get_current_flow_phase(driver)
                step_name = get_primary_action_name(driver, phase)
            selectors = get_primary_action_selectors(step_name)
            if phase == "review_submit" and any_visible_selector(driver, get_submit_application_selectors()):
                step_name = "SUBMIT_APPLICATION"
                selectors = get_submit_application_selectors()
            if step_name:
                before_url = driver.current_url
                before_action = step_name
                before_question_state = is_employer_questions_step(driver)
                before_signature = current_signature
                if step_name in ("CONTINUE", "NEXT", "REVIEW") and not has_unanswered_required_questions(driver):
                    print(f"FLOW_ADVANCE:primary_cta={step_name}")
                elif step_name in ("SUBMIT_APPLICATION", "SUBMIT"):
                    print(f"FLOW_ADVANCE:primary_cta={step_name}")
                clicked = False
                if step_name == "SUBMIT_APPLICATION":
                    clicked = hard_submit_application(driver)
                else:
                    clicked = click_first_match(driver, selectors)
                if clicked:
                    clicked_step = True
                    print(f"FLOW_STEP:{step_name}")
                    advanced = wait_for_step_progress(
                        driver,
                        before_url,
                        before_phase=phase,
                        before_action=before_action,
                        before_signature=before_signature,
                        before_question_state=before_question_state,
                        timeout=max(2.5, DETAIL_LOAD_WAIT * 5),
                    )
                    if advanced:
                        progressed = True
                        idle_cycles = 0
                        same_page_count = 0
                        same_review_page_count = 0
                    elif step_name in ("SUBMIT_APPLICATION", "SUBMIT", "YES") or phase == "review_submit":
                        same_review_page_count += 1
                        print(f"FLOW_WAIT:same_review_page:{same_review_page_count}")
                    else:
                        same_page_count += 1
                        print(f"FLOW_WAIT:same_page:{same_page_count}")
                    if is_application_submitted(driver):
                        return "submitted"

            if progressed:
                time.sleep(CLICK_PAUSE)
                if is_application_submitted(driver):
                    return "submitted"
                continue

            if clicked_step:
                time.sleep(CLICK_PAUSE)

            if SKIP_ON_UNANSWERED_QUESTIONS and has_unanswered_required_questions(driver):
                print("APPLY_WAIT:required_questions")
                manual_state = wait_for_manual_required_answers(driver)
                if manual_state == "submitted":
                    return "submitted"
                if manual_state == "resolved":
                    idle_cycles = 0
                    continue
                return "blocked_questions"

            idle_cycles += 1
            now = time.time()
            current = driver.current_url
            if idle_cycles == 1 or now - last_wait_log >= 15:
                if is_on_apply_interface(driver):
                    print(f"FLOW_WAIT:in_progress:idle={idle_cycles}:url={current}")
                else:
                    print(f"FLOW_WAIT:awaiting_apply_state:idle={idle_cycles}:url={current}")
                last_wait_log = now
            if MAX_FLOW_STEPS > 0 and idle_cycles >= MAX_FLOW_STEPS:
                return "blocked"
            time.sleep(max(0.5, DETAIL_LOAD_WAIT))
        except Exception as exc:
            raise_session_reconnect(exc, "run_quick_apply_flow")


def log_match_result(job_key, title, match_result):
    if not SHOW_MATCH_DETAILS:
        return
    print(
        "MATCH:"
        f"key={job_key} "
        f"score={match_result['score']} "
        f"eligible={match_result['eligible']}"
    )
    print(f"MATCH_TITLE:{title}")
    print(f"MATCH_MUST:{match_result['matched_must_have']}")
    print(f"MATCH_PREF:{match_result['matched_preferred']}")
    print(f"MATCH_MISSING:{match_result['missing_must_have']}")
    print(f"MATCH_EXCLUDED:{match_result['excluded_term_hit']}")


def process_job_url(driver, job_entry, idx, stats):
    global LAST_HR_TEXT, LAST_HR_LINK
    job_url = job_entry["url"]
    job_key = job_entry["key"]
    job_title = job_entry["title"]

    attempts = 2
    for attempt in range(attempts):
        try:
            if attempt > 0:
                print(f"SESSION_RECOVER:retry_job:{job_key}")

            if not verify_driver_session(driver):
                raise SessionReconnectRequired("pre_job_check")

            active_apply_url = ""
            if ACTIVE_APPLY_STATE.get("locked") and ACTIVE_APPLY_STATE.get("job_key") == job_key:
                active_apply_url = ACTIVE_APPLY_STATE.get("apply_url") or ""
            target_url = active_apply_url or job_url

            print(f"OPEN:{idx}:{job_title}")
            driver.get(target_url)
            time.sleep(DETAIL_LOAD_WAIT)

            company_name, position = extract_company_and_position(driver, job_title)

            if SKIP_EXTERNAL and is_external_apply(driver):
                if SHOW_SKIP_REASONS:
                    print(f"SKIP_EXTERNAL:{job_key}")
                stats["skipped_external"] += 1
                append_apply_log(company_name, position, job_url, "skipped_external", "", "")
                clear_active_apply_state()
                return job_key, driver

            title_text, detail_text = get_job_text_snapshot(driver)
            LAST_HR_TEXT = build_hr_context_text(driver, title_text, detail_text)
            LAST_HR_LINK = extract_hr_profile_link(driver)
            match_result = evaluate_match(title_text, detail_text)
            log_match_result(job_key, title_text, match_result)

            if MATCHING_ENABLED and not match_result["eligible"]:
                if SHOW_SKIP_REASONS:
                    print(
                        "SKIP_LOW_MATCH:"
                        f"score={match_result['score']} "
                        f"min={MIN_MATCH_SCORE} "
                        f"missing={match_result['missing_must_have']} "
                        f"excluded={match_result['excluded_term_hit']}"
                    )
                stats["skipped_low_match"] += 1
                append_apply_log(company_name, position, job_url, "skipped_low_match", "", "")
                return job_key, driver

            if not MATCHING_ENABLED and SHOW_MATCH_DETAILS:
                print("MATCH_BYPASS:matching.enabled=False")

            before_shot = capture_job_screenshot(driver, job_key, "before_apply", phase="before")

            apply_state = click_apply(driver, job_url)
            if apply_state == "external_precheck":
                print(f"SKIP_EXTERNAL_PRECHECK:{job_key}")
                stats["skipped_external"] += 1
                append_apply_log(company_name, position, job_url, "skipped_external_precheck", "", "")
                return job_key, driver

            if apply_state == "external_target":
                print(f"SKIP_EXTERNAL_TARGET:{job_key}")
                stats["skipped_external"] += 1
                append_apply_log(company_name, position, job_url, "skipped_external_target", "", "")
                return job_key, driver

            if apply_state == "external_target_closed_tab":
                print(f"SKIP_EXTERNAL_TARGET:{job_key}:closed_tab")
                stats["skipped_external"] += 1
                append_apply_log(company_name, position, job_url, "skipped_external_target_closed_tab", "", "")
                return job_key, driver

            if apply_state in ("not_found", "not_quick_apply"):
                print(f"SKIP_NO_QUICK_APPLY:{job_key}")
                stats["skipped_no_quick_apply"] += 1
                append_apply_log(company_name, position, job_url, "skipped_no_quick_apply", "", "")
                return job_key, driver

            if apply_state == "visible_but_not_opened":
                print(f"FAILED:{job_key}:quick_apply_transition")
                stats["failed"] += 1
                append_apply_log(company_name, position, job_url, "failed_quick_apply_transition", "", "")
                return job_key, driver

            if not is_on_apply_interface(driver) and not wait_for_apply_interface(driver, timeout=max(6, WAIT_TIMEOUT)):
                print(f"FAILED:{job_key}:quick_apply_interface_not_opened")
                stats["failed"] += 1
                clear_active_apply_state()
                append_apply_log(company_name, position, job_url, "failed_quick_apply_interface", "", "")
                return job_key, driver

            refresh_active_apply_state(driver, job_key=job_key, job_url=job_url)

            if not AUTO_SUBMIT_ENABLED:
                print("AUTO_SUBMIT_DISABLED")
                append_apply_log(company_name, position, job_url, "auto_submit_disabled", "", "")
                return job_key, driver

            result = run_quick_apply_flow(driver)
            if result == "submitted":
                print(f"SUBMITTED:{job_key}")
                stats["applied"] += 1
                TODAY_SUBMITTED_JOB_KEYS.add(job_key)
                confirm_deadline = time.time() + 1.0
                while time.time() < confirm_deadline:
                    if is_application_submitted(driver):
                        break
                    time.sleep(0.2)
                shot = capture_job_screenshot(driver, job_key, "submitted", phase="after")
                hr_name, hr_email, hr_contact = extract_hr_details(LAST_HR_TEXT)
                append_apply_log(
                    company_name,
                    position,
                    job_url,
                    "submitted",
                    shot,
                    before_shot,
                    hr_name,
                    hr_email,
                    hr_contact,
                    LAST_HR_LINK,
                )
                clear_active_apply_state()
            elif result == "external":
                print(f"SKIP_EXTERNAL:{job_key}")
                stats["skipped_external"] += 1
                append_apply_log(company_name, position, job_url, "skipped_external", "", "")
                clear_active_apply_state()
            elif result == "blocked_questions":
                print(f"FAILED:{job_key}:blocked_questions")
                stats["failed"] += 1
                append_apply_log(company_name, position, job_url, "failed_blocked_questions", "", "")
                clear_active_apply_state()
            elif result == "resume_upload_failed":
                print(f"FAILED:{job_key}:resume_upload")
                stats["failed"] += 1
                append_apply_log(company_name, position, job_url, "failed_resume_upload", "", "")
                clear_active_apply_state()
            else:
                print(f"FAILED:{job_key}:blocked_or_incomplete")
                stats["failed"] += 1
                append_apply_log(company_name, position, job_url, "failed_blocked_or_incomplete", "", "")
                clear_active_apply_state()

            return job_key, driver
        except SessionReconnectRequired as exc:
            context = str(exc) or "session_reconnect"
        except Exception as exc:
            if is_session_recoverable_error(exc):
                context = "unexpected_session_loss"
            else:
                print(f"FAILED:{job_key}:unexpected:{exc}")
                stats["failed"] += 1
                append_apply_log("Unknown", job_title, job_url, "failed_unexpected", "", "")
                clear_active_apply_state()
                return job_key, driver

        if attempt + 1 >= attempts:
            print(f"FAILED:session_reconnect:{job_key}:{context}")
            stats["failed"] += 1
            append_apply_log("Unknown", job_title, job_url, "failed_session_reconnect", "", "")
            clear_active_apply_state()
            return job_key, driver

        resume_url = ACTIVE_APPLY_STATE.get("apply_url") if ACTIVE_APPLY_STATE.get("locked") and ACTIVE_APPLY_STATE.get("job_key") == job_key else ""
        if resume_url:
            print("SESSION_RECOVER:resume_active_apply")
        recovered_driver = reattach_debug_driver(driver, job_url=resume_url or job_url, context=f"{job_key}:{context}")
        if recovered_driver is None:
            print(f"FAILED:session_reconnect:{job_key}:{context}")
            stats["failed"] += 1
            append_apply_log("Unknown", job_title, job_url, "failed_session_reconnect", "", "")
            clear_active_apply_state()
            return job_key, driver
        driver = recovered_driver

    return job_key, driver


def go_to_next_results_page(driver):
    selectors = [
        "//a[@aria-label='Next']",
        "//button[@aria-label='Next']",
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
    ]
    for xp in selectors:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            try:
                if not elem.is_displayed() or not elem.is_enabled():
                    continue
                safe_click(driver, elem)
                time.sleep(PAGE_LOAD_WAIT)
                print("NEXT_PAGE")
                return True
            except Exception:
                continue
    return False


def apply_cap_reached(stats):
    return SESSION_APPLY_CAP > 0 and stats["applied"] >= SESSION_APPLY_CAP


def run_continuous(driver):
    global TODAY_SUBMITTED_JOB_KEYS
    TODAY_SUBMITTED_JOB_KEYS = load_today_submitted_job_keys()

    stats = {
        "pages": 0,
        "scanned": 0,
        "applied": 0,
        "skipped_external": 0,
        "skipped_applied": 0,
        "skipped_no_quick_apply": 0,
        "skipped_low_match": 0,
        "failed": 0,
    }

    processed_global = set()

    for search_url in SEARCH_URLS:
        if apply_cap_reached(stats):
            print("STOP:apply_cap_reached")
            break

        per_url_start = dict(stats)
        scanned_this_search = 0
        dedup_skipped_this_search = 0

        print(f"SEARCH_START:{search_url}")
        open_jobs_page(driver, search_url)
        pages_in_this_search = 0

        while True:
            if apply_cap_reached(stats):
                print("STOP:apply_cap_reached")
                break

            if MAX_JOBS_PER_RUN > 0 and scanned_this_search >= MAX_JOBS_PER_RUN:
                print("STOP:max_jobs_per_url_reached")
                break

            stats["pages"] += 1
            pages_in_this_search += 1
            results_page_url = driver.current_url

            entries = get_job_entries(driver)
            print(f"PAGE:{stats['pages']}:jobs={len(entries)}")
            if not entries:
                break

            page_processed = 0
            for idx, entry in enumerate(entries, start=1):
                if apply_cap_reached(stats):
                    print("STOP:apply_cap_reached")
                    break

                if MAX_JOBS_PER_RUN > 0 and scanned_this_search >= MAX_JOBS_PER_RUN:
                    print("STOP:max_jobs_per_url_reached")
                    break

                key = entry["key"]
                if not key:
                    continue

                if key in processed_global:
                    dedup_skipped_this_search += 1
                    print(f"SKIP_DUPLICATE:{key}")
                    continue

                if key in TODAY_SUBMITTED_JOB_KEYS:
                    print(f"SKIP_APPLIED_TODAY:{key}")
                    stats["scanned"] += 1
                    scanned_this_search += 1
                    stats["skipped_applied"] += 1
                    processed_global.add(key)
                    page_processed += 1
                    continue

                stats["scanned"] += 1
                scanned_this_search += 1
                result_key, driver = process_job_url(driver, entry, idx, stats)
                processed_global.add(result_key or key)
                page_processed += 1

                if ACTIVE_APPLY_STATE.get("locked"):
                    continue

                try:
                    driver.get(results_page_url)
                    time.sleep(PAGE_LOAD_WAIT)
                except Exception as exc:
                    if is_session_recoverable_error(exc):
                        resume_url = ACTIVE_APPLY_STATE.get("apply_url") if ACTIVE_APPLY_STATE.get("locked") else ""
                        if resume_url:
                            print("SESSION_RECOVER:resume_active_apply")
                        recovered_driver = reattach_debug_driver(driver, job_url=resume_url or results_page_url, context="results_page")
                        if recovered_driver is None:
                            print("STOP:results_page_session_lost")
                            return
                        driver = recovered_driver
                    else:
                        raise

            if apply_cap_reached(stats):
                break

            if MAX_JOBS_PER_RUN > 0 and scanned_this_search >= MAX_JOBS_PER_RUN:
                break

            if page_processed == 0:
                break

            if MAX_PAGES_PER_SEARCH > 0 and pages_in_this_search >= MAX_PAGES_PER_SEARCH:
                print("STOP:max_pages_per_search_reached")
                break

            if not go_to_next_results_page(driver):
                break

        per_url_end = dict(stats)
        print(
            "SEARCH_DONE:"
            f"url={search_url} "
            f"scanned={per_url_end['scanned'] - per_url_start['scanned']} "
            f"applied={per_url_end['applied'] - per_url_start['applied']} "
            f"skip_applied={per_url_end['skipped_applied'] - per_url_start['skipped_applied']} "
            f"skip_no_quick_apply={per_url_end['skipped_no_quick_apply'] - per_url_start['skipped_no_quick_apply']} "
            f"failed={per_url_end['failed'] - per_url_start['failed']} "
            f"dedup_skipped={dedup_skipped_this_search}"
        )

    print(
        "DONE:"
        f"pages={stats['pages']} "
        f"scanned={stats['scanned']} "
        f"applied={stats['applied']} "
        f"skip_external={stats['skipped_external']} "
        f"skip_applied={stats['skipped_applied']} "
        f"skip_no_quick_apply={stats['skipped_no_quick_apply']} "
        f"skip_low_match={stats['skipped_low_match']} "
        f"failed={stats['failed']}"
    )

def main():
    validate_config()
    driver = init_driver()

    try:
        if QUICK_APPLY_ONLY:
            print("QUICK_ONLY_MODE:on")
        print("Connected successfully")
        print("Current title:", driver.title)
        print("Current URL:", driver.current_url)

        safe_input("Agar login already ho chuka hai to Enter dabao... ")
        run_continuous(driver)
        safe_input("Script finished. Enter dabao...")
    except Exception as e:
        print("ERROR:", e)
        safe_input("Enter dabao...")


if __name__ == "__main__":
    main()

























