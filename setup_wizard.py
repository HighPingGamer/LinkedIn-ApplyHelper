"""First-run setup wizard for JobHunterJP.

The wizard collects user-provided profile data, creates a dedicated Chrome
profile directory, asks the user to log into LinkedIn manually, then writes
config.yaml for the agent.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path

import yaml
from patchright.sync_api import TimeoutError as PWTimeout
from patchright.sync_api import sync_playwright


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
DISCLAIMER = """
JobHunterJP automates LinkedIn job review and Easy Apply actions from your own
logged-in browser profile. You are responsible for complying with LinkedIn's
Terms of Service, job-board rules, employer instructions, and local law.

The tool does not ask for or store your LinkedIn password. It will open Chrome
so you can log in manually. V1 uses local Ollama models and does not require
OpenRouter or other cloud LLM API keys.
"""


def ask(label: str, required: bool = True, secret: bool = False) -> str:
    while True:
        value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
        value = value.strip()
        if value or not required:
            return value
        print("Required. Please enter a value.")


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def profile_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "JobHunterJP" / "profile"


def wait_for_linkedin_login(chrome_profile_dir: Path) -> None:
    chrome_profile_dir.mkdir(parents=True, exist_ok=True)
    chrome_exe = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    executable_path = str(chrome_exe) if chrome_exe.exists() else None

    print("\nOpening Chrome. Log into LinkedIn manually in the browser window.")
    print("The wizard will continue after LinkedIn's global nav is detected.")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(chrome_profile_dir),
            executable_path=executable_path,
            headless=False,
            args=["--profile-directory=Default"],
            slow_mo=120,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("nav.global-nav", timeout=300000)
        except PWTimeout as exc:
            ctx.close()
            raise SystemExit("LinkedIn login was not detected within 5 minutes.") from exc
        ctx.close()
    print("LinkedIn login detected. Chrome profile is ready.")


def build_config(answers: dict[str, str], chrome_profile_dir: Path) -> dict:
    full_name = answers["full_name"]
    name_parts = full_name.split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    locations = split_csv(answers["location_preferences"])
    keywords = split_csv(answers["job_keywords"])
    primary_location = locations[0] if locations else ""

    try:
        min_fit_score = int(answers["min_fit_score"])
    except ValueError as exc:
        raise SystemExit("Min fit score must be an integer.") from exc

    return {
        "ollama_model": "qwen2.5:7b",
        "chrome_user_data_dir": str(chrome_profile_dir).replace("\\", "/"),
        "chrome_executable_path": "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "chrome_profile": "Default",
        "search_keywords": keywords,
        "search_location": primary_location,
        "remote_ok": True,
        "target_job_urls": [],
        "target_jobs_ignore_seen": True,
        "min_salary_aed_month": None,
        "acceptable_locations": locations,
        "red_flag_keywords": [
            "commission only",
            "unpaid",
            "MLM",
            "must have own visa",
            "100% cold calling",
            "nationals only",
            "national only",
            "citizens only",
            "citizen only",
            "locals only",
            "for nationals",
            "for citizens",
            "nationalization",
        ],
        "profile_summary": (
            f"{full_name}. Location preferences: {answers['location_preferences']}. "
            f"Target keywords: {answers['job_keywords']}. "
            f"Current salary: {answers['current_salary']}. "
            f"Expected salary: {answers['expected_salary']}. "
            f"Notice period: {answers['notice_period']}."
        ),
        "min_fit_score": min_fit_score,
        "base_resume_pdf": "",
        "tailor_resume": False,
        "easy_apply_answers": {
            "first name": first_name,
            "last name": last_name,
            "location": primary_location,
            "city": primary_location.split(",")[0].strip() if primary_location else "",
            "current location": primary_location,
            "phone country code": "",
            "years of experience": "",
            "notice period": answers["notice_period"],
            "current salary": answers["current_salary"],
            "expected salary": answers["expected_salary"],
            "phone": answers["phone"],
            "email": answers["email"],
            "willing to relocate": "Yes",
            "authorized to work": "Yes",
            "require sponsorship": "No",
            "education": "",
            "visa": "",
        },
        "qc_enabled": True,
        "qc_provider": "ollama",
        "qc_ollama_model": "qwen2.5:7b",
        "qc_max_batch": 15,
        "qc_jd_excerpt_chars": 1500,
        "qc_fallback": "local_only",
        "form_ai_enabled": True,
        "form_ai_provider": "ollama",
        "form_ai_ollama_model": "qwen2.5:7b",
        "max_applications_per_session": 15,
        "min_action_delay_sec": 2,
        "max_action_delay_sec": 6,
        "seen_jobs_file": "seen_jobs.json",
        "output_xlsx": "applications_log.xlsx",
    }


def main() -> None:
    print(DISCLAIMER)
    accepted = input('Type "I ACCEPT" to continue: ').strip()
    if accepted != "I ACCEPT":
        raise SystemExit("Setup cancelled. Exact acceptance text was not entered.")

    answers = {
        "full_name": ask("Full name"),
        "email": ask("Email"),
        "phone": ask("Phone"),
        "current_salary": ask("Current salary"),
        "expected_salary": ask("Expected salary"),
        "notice_period": ask("Notice period"),
        "location_preferences": ask("Location preferences (comma-separated)"),
        "job_keywords": ask("Job keywords (comma-separated)"),
        "min_fit_score": ask("Min fit score"),
    }

    chrome_profile_dir = profile_dir()
    chrome_profile_dir.mkdir(parents=True, exist_ok=True)
    wait_for_linkedin_login(chrome_profile_dir)

    cfg = build_config(answers, chrome_profile_dir)
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False), encoding="utf-8")

    print(f"\nWrote {CONFIG_PATH}")
    print(f"Chrome profile directory: {chrome_profile_dir}")


if __name__ == "__main__":
    main()
