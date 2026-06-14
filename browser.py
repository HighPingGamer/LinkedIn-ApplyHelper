"""Patchright wrapper around LinkedIn job search + Easy Apply.

Design choices that matter:
  * Uses YOUR existing Chrome profile (launch_persistent_context) so you are already
    logged in — the script never sees or stores your password.
  * Human pace: randomised delays between every action.
  * Easy Apply only. External-redirect applications are logged as 'skipped (external)'.
  * If a required question can't be answered from config, the application is ABANDONED
    (modal dismissed, nothing submitted) and logged as 'skipped (manual needed)'.
"""

import random
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout

import form_ai


class LinkedInBrowser:
    ANSWER_SYNONYMS = {
        "years of experience": ["years of experience", "how many years", "total experience"],
        "notice period": ["notice period", "notice", "availability"],
        "current salary": ["current salary", "current ctc", "present salary"],
        "expected salary": ["expected salary", "desired salary", "salary expectation"],
        "authorized to work": [
            "work authorization", "are you authorized", "legally authorized",
            "authorized to work", "right to work",
        ],
        "require sponsorship": ["require sponsorship", "need sponsorship", "visa sponsorship", "sponsorship"],
        "willing to relocate": ["relocation", "willing to relocate", "relocate"],
        "education": ["education", "highest degree", "qualification"],
        "location": ["location", "city", "current location", "location city", "location (city)"],
    }

    def __init__(self, cfg):
        self.cfg = cfg
        self._pw = None
        self.ctx = None
        self.page = None
        self._unanswered_logged = set()
        self._ai_answer_cache = {}

    def __enter__(self):
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.cfg["chrome_user_data_dir"],
            executable_path=self.cfg.get("chrome_executable_path"),
            headless=False,                      # watch it work; also lowers detection
            args=[f'--profile-directory={self.cfg.get("chrome_profile", "Default")}'],
            slow_mo=120,
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *a):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    # ── pacing ────────────────────────────────────────────────
    def _wait(self):
        time.sleep(random.uniform(self.cfg["min_action_delay_sec"],
                                  self.cfg["max_action_delay_sec"]))

    # ── search & iterate ──────────────────────────────────────
    def search(self, keyword, location):
        url = (f"https://www.linkedin.com/jobs/search/"
               f"?keywords={quote_plus(keyword)}&location={quote_plus(location)}"
               f"&f_AL=true&sortBy=DD")          # f_AL=true => Easy Apply only; DD => most recent
        self.page.goto(url, wait_until="domcontentloaded")
        self._wait()

    def job_cards(self):
        """Yields (job_id, card_handle) for each visible job card."""
        self.page.wait_for_selector("li[data-occludable-job-id]", timeout=15000)
        cards = self.page.query_selector_all("li[data-occludable-job-id]")
        for c in cards:
            jid = c.get_attribute("data-occludable-job-id")
            if jid:
                yield jid, c

    def open_job(self, card):
        card_title, card_company = self._read_card_title_company(card)
        card.click()
        self._wait()
        jd, loc, url, detail_title, detail_company = self._read_detail_pane()
        return jd, loc, url, detail_title or card_title, detail_company or card_company

    def open_job_url(self, url):
        """Re-open a job by URL in the apply phase (card handles go stale after nav)."""
        self.page.goto(url, wait_until="domcontentloaded")
        self._wait()
        jd, loc, page_url, _, _ = self._read_detail_pane()
        return jd, loc, page_url

    def inspect_job_url(self, url):
        """Open a job URL for discovery/scoring and return detail metadata."""
        self.page.goto(url, wait_until="domcontentloaded")
        self._wait()
        return self._read_detail_pane()

    def _read_detail_pane(self):
        # JD text lives in the right-hand detail pane
        try:
            self.page.wait_for_selector(
                "div#job-details, div.jobs-description__content, div.jobs-description",
                timeout=10000)
        except PWTimeout:
            pass
        pane = self.page.query_selector(
            "div#job-details, div.jobs-description__content, div.jobs-description")
        jd = pane.inner_text() if pane else ""
        loc_el = self.page.query_selector(
            ".job-details-jobs-unified-top-card__primary-description-container, "
            ".job-details-jobs-unified-top-card__primary-description, "
            ".jobs-unified-top-card__bullet, "
            ".jobs-unified-top-card__primary-description")
        loc = loc_el.inner_text() if loc_el else ""
        title_el = self.page.query_selector(
            "h1.job-details-jobs-unified-top-card__job-title, "
            "h1.jobs-unified-top-card__job-title, "
            ".job-details-jobs-unified-top-card__job-title")
        company_el = self.page.query_selector(
            ".job-details-jobs-unified-top-card__company-name, "
            ".jobs-unified-top-card__company-name")
        title = self._clean_text(title_el.inner_text()) if title_el else ""
        company = self._clean_text(company_el.inner_text()) if company_el else ""
        return jd, loc, self.page.url, title, company

    def _read_card_title_company(self, card):
        title_el = card.query_selector(
            ".job-card-list__title, .job-card-container__link, "
            "a[href*='/jobs/view/']")
        company_el = card.query_selector(
            ".artdeco-entity-lockup__subtitle, .job-card-container__company-name")
        title = self._clean_text(title_el.inner_text()) if title_el else ""
        company = self._clean_text(company_el.inner_text()) if company_el else ""
        return title, company

    @staticmethod
    def _clean_text(text):
        return " ".join((text or "").split())

    # ── Easy Apply ────────────────────────────────────────────
    def easy_apply(self, answers: dict, resume_pdf: str | None) -> tuple:
        """Attempts the Easy Apply modal. Returns (status, note).
        status in {'submitted','skipped','failed'}."""
        if not self._click_first_visible([
                'button[aria-label^="Easy Apply to"]',
                'button[aria-label^="Easy Apply"]',
                'button.jobs-apply-button:has-text("Easy Apply")',
                'button:has-text("Easy Apply")',
        ], timeout=10000):
            return "skipped", "no Easy Apply button (likely external application)"
        self._wait()

        for _ in range(20):                       # cap modal steps to avoid loops
            self._fill_visible_fields(answers, resume_pdf)
            self._untick_follow_company()

            if self._click_first_visible([
                    'button[aria-label*="Submit application"]',
                    'button:has-text("Submit application")',
            ], timeout=1500):
                self._wait()
                self._dismiss_post_submit()
                return "submitted", "submitted via Easy Apply"

            if not self._has_visible([
                    'button[aria-label*="Review your application"]',
                    'button[aria-label*="Review"]',
                    'button:has-text("Review")',
                    'button[aria-label*="Continue to next step"]',
                    'button[aria-label*="Continue"]',
                    'button[aria-label*="Next"]',
                    'button:has-text("Continue")',
                    'button:has-text("Next")',
            ]):
                self._abandon()
                return "skipped", "manual needed (unknown step / required field)"

            # Before advancing, make sure no required field was left empty.
            if self._has_unanswered_required():
                self._abandon()
                return "skipped", "manual needed (required question not in config)"
            self._click_first_visible([
                'button[aria-label*="Review your application"]',
                'button[aria-label*="Review"]',
                'button:has-text("Review")',
                'button[aria-label*="Continue to next step"]',
                'button[aria-label*="Continue"]',
                'button[aria-label*="Next"]',
                'button:has-text("Continue")',
                'button:has-text("Next")',
            ], timeout=10000)
            self._wait()

        self._abandon()
        self._log_modal_snapshot("too_many_steps")
        return "skipped", "manual needed (too many steps)"

    def _untick_follow_company(self):
        modal = self.page.query_selector("div.jobs-easy-apply-modal") or self.page
        follow_terms = ("follow", "following", "company page", "company")
        for checkbox in modal.query_selector_all("input[type='checkbox']"):
            try:
                if not checkbox.is_checked():
                    continue
                label = self._label_text(checkbox).lower()
                if not label:
                    label = self._clean_text(checkbox.evaluate("""el => {
                        const row = el.closest("label, div, section");
                        return row && row.innerText ? row.innerText : "";
                    }""")).lower()
                if "follow" in label and any(term in label for term in follow_terms):
                    checkbox.click(force=True)
                    self._log_form_event("unchecked_follow_company", label)
            except Exception:
                continue

    def _log_form_event(self, event, detail):
        line = (
            f"{datetime.now().isoformat(timespec='seconds')}\t"
            f"event={event}\t"
            f"url={self.page.url}\t"
            f"detail={self._clean_text(detail)[:500]}\n"
        )
        try:
            with Path("form_events.log").open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _first_visible_locator(self, selectors):
        for selector in selectors:
            loc = self.page.locator(selector)
            try:
                count = min(loc.count(), 20)
            except Exception:
                continue
            for i in range(count):
                item = loc.nth(i)
                try:
                    if item.is_visible():
                        return item
                except Exception:
                    continue
        return None

    def _has_visible(self, selectors):
        return self._first_visible_locator(selectors) is not None

    def _click_first_visible(self, selectors, timeout=10000):
        loc = self._first_visible_locator(selectors)
        if not loc:
            return False
        try:
            loc.scroll_into_view_if_needed(timeout=timeout)
            loc.click(timeout=timeout)
            return True
        except Exception:
            return False

    # ── form helpers ──────────────────────────────────────────
    def _fill_visible_fields(self, answers, resume_pdf):
        modal = self.page.query_selector("div.jobs-easy-apply-modal") or self.page
        # resume upload, if a file input is present
        if resume_pdf:
            file_input = modal.query_selector('input[type="file"]')
            if file_input:
                try:
                    file_input.set_input_files(resume_pdf)
                    self._wait()
                except Exception:
                    pass
        # text inputs / textareas matched loosely by their label text
        for el in modal.query_selector_all(
                "input[type='text'], input[type='number'], input[type='tel'], input[type='email'], textarea"):
            input_type = (el.get_attribute("type") or "").lower()
            answer = self._answer_for_element(el, answers, input_type or "text")
            if answer is not None:
                if el.input_value() and input_type not in {"text", "search"}:
                    continue
                el.fill(str(answer))

        # Native dropdowns. Match configured answers to option text or value.
        for select in modal.query_selector_all("select"):
            try:
                if select.input_value():
                    continue
            except Exception:
                pass
            answer = self._answer_for_element(select, answers, "select", self._select_options(select))
            if answer is not None:
                self._select_dropdown_option(select, str(answer))

        # Radio buttons are usually grouped by fieldset/radiogroup, but LinkedIn
        # sometimes wraps them in generic form-section containers.
        seen = set()
        groups = modal.query_selector_all(
            "fieldset, div[role='radiogroup'], .jobs-easy-apply-form-section__grouping")
        for group in groups:
            radios = group.query_selector_all("input[type='radio']")
            if not radios:
                continue
            group_key = tuple(sorted(r.get_attribute("name") or r.get_attribute("id") or "" for r in radios))
            if group_key in seen:
                continue
            seen.add(group_key)
            answer = self._answer_for_element(group, answers, "radio", self._radio_options(group))
            if answer is not None:
                self._select_radio_option(group, str(answer))

    def _answer_for_element(self, element, answers, field_type="text", options=None):
        raw_label = self._label_text(element)
        label = raw_label.lower()
        for key, val in answers.items():
            if key.lower() in label and self._safe_direct_answer(key, label):
                return val
        for answer_key, phrases in self.ANSWER_SYNONYMS.items():
            if (answer_key in answers and any(phrase in label for phrase in phrases)
                    and self._safe_direct_answer(answer_key, label)):
                return answers[answer_key]
        return self._ai_answer_for(raw_label, field_type, options)

    def _safe_direct_answer(self, answer_key, label):
        if answer_key.lower() != "years of experience":
            return True
        hr_terms = (
            "hr", "human resources", "people", "employee relations", "performance management",
            "hrbp", "business partner", "talent", "compensation", "benefits", "hris",
            "total", "overall", "professional", "work experience",
        )
        risky_terms = (
            "construction", "real estate", "sales", "marketing", "finance", "accounting",
            "engineering", "software", "developer", "oracle", "bayanati", "mandarin",
            "arabic", "hospitality", "retail", "luxury", "f&b", "training",
        )
        if any(term in label for term in risky_terms) and not any(term in label for term in hr_terms):
            return False
        return any(term in label for term in hr_terms)

    def _ai_answer_for(self, question, field_type, options=None):
        question = self._clean_text(question)
        if not question:
            return None
        cache_key = (question.lower(), field_type, tuple(options or []))
        if cache_key in self._ai_answer_cache:
            return self._ai_answer_cache[cache_key]
        answer, reason = form_ai.answer_question(question, field_type, options or [], self.cfg)
        if options and answer:
            answer = self._match_option(answer, options)
        self._ai_answer_cache[cache_key] = answer
        if answer:
            form_ai.log_answer(question, field_type, options or [], answer, reason)
        else:
            form_ai.log_answer(question, field_type, options or [], "", reason)
        return answer

    def _select_options(self, select):
        options = []
        for option in select.query_selector_all("option"):
            text = self._clean_text(option.inner_text())
            value = option.get_attribute("value") or ""
            if value and text:
                options.append(text)
        return options

    def _radio_options(self, group):
        options = []
        for label in group.query_selector_all("label"):
            text = self._clean_text(label.inner_text())
            if text:
                options.append(text)
        if options:
            return options
        for radio in group.query_selector_all("input[type='radio']"):
            value = self._clean_text(radio.get_attribute("value") or radio.get_attribute("aria-label") or "")
            if value:
                options.append(value)
        return options

    def _match_option(self, answer, options):
        answer_lower = answer.strip().lower()
        for option in options:
            if answer_lower == option.lower():
                return option
        for option in options:
            option_lower = option.lower()
            if answer_lower in option_lower or option_lower in answer_lower:
                return option
        return None

    def _label_text(self, element):
        try:
            text = element.evaluate("""el => {
                const textOf = node => node && node.innerText ? node.innerText.trim() : "";
                if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
                if (el.getAttribute("placeholder")) return el.getAttribute("placeholder").trim();
                if (el.id && window.CSS && CSS.escape) {
                    const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (textOf(byFor)) return textOf(byFor);
                }
                const label = el.closest("label");
                if (textOf(label)) return textOf(label);
                const group = el.closest("fieldset, div[role='radiogroup'], .jobs-easy-apply-form-section__grouping");
                const legend = group ? group.querySelector("legend") : null;
                if (textOf(legend)) return textOf(legend);
                if (textOf(group)) return textOf(group);
                return el.getAttribute("name") || "";
            }""")
            return text or ""
        except Exception:
            return element.get_attribute("aria-label") or element.get_attribute("name") or ""

    def _select_dropdown_option(self, select, answer):
        answer_lower = answer.strip().lower()
        for option in select.query_selector_all("option"):
            text = (option.inner_text() or "").strip()
            value = option.get_attribute("value") or ""
            if not value:
                continue
            if answer_lower in {value.lower(), text.lower()}:
                select.select_option(value=value)
                return True
            if answer_lower in text.lower() or text.lower() in answer_lower:
                select.select_option(value=value)
                return True
        return False

    def _select_radio_option(self, group, answer):
        answer_lower = answer.strip().lower()
        for radio in group.query_selector_all("input[type='radio']"):
            value = (radio.get_attribute("value") or "").strip().lower()
            aria = (radio.get_attribute("aria-label") or "").strip().lower()
            if answer_lower in {value, aria}:
                radio.click(force=True)
                return True

        for label in group.query_selector_all("label"):
            label_text = (label.inner_text() or "").strip().lower()
            if answer_lower and (answer_lower == label_text or answer_lower in label_text):
                target_id = label.get_attribute("for")
                radio = group.query_selector(f'input[type="radio"]#{target_id}') if target_id else None
                if radio:
                    radio.click(force=True)
                else:
                    label.click(force=True)
                return True
        return False

    def _has_unanswered_required(self) -> bool:
        # LinkedIn marks required fields; if any required input/select is empty, flag it.
        modal = self.page.query_selector("div.jobs-easy-apply-modal") or self.page
        for el in self.page.query_selector_all(
                "input[required], select[required], textarea[required], "
                "input[aria-required='true'], select[aria-required='true'], textarea[aria-required='true']"):
            try:
                if not el.is_visible():
                    continue
            except Exception:
                pass
            try:
                if (el.get_attribute("type") or "").lower() == "radio":
                    checked = el.evaluate("""radio => {
                        if (!radio.name) return radio.checked;
                        return !!document.querySelector(`input[type="radio"][name="${CSS.escape(radio.name)}"]:checked`);
                    }""")
                    if checked:
                        continue
                    self._log_unanswered_question(el, "radio")
                    return True
                if not el.input_value():
                    tag = (el.evaluate("el => el.tagName") or "").lower()
                    field_type = (el.get_attribute("type") or tag or "field").lower()
                    self._log_unanswered_question(el, field_type)
                    return True
            except Exception:
                # selects without input_value(): check a chosen option
                pass
        # error text shown after a failed advance
        errors = self.page.query_selector_all(
            "[data-test-form-element-error-messages], .artdeco-inline-feedback--error")
        if errors:
            for err in errors:
                self._log_unanswered_question(err, "validation_error")
            self._log_modal_snapshot("validation_error")
            return True
        required_groups = modal.query_selector_all(
            ".jobs-easy-apply-form-section__grouping, fieldset, div[role='radiogroup']")
        for group in required_groups:
            try:
                text = self._clean_text(group.inner_text()).lower()
            except Exception:
                continue
            if "required" not in text:
                continue
            if group.query_selector("input:checked"):
                continue
            controls = group.query_selector_all("input, select, textarea")
            if controls and all(not self._control_has_value(control) for control in controls):
                self._log_unanswered_question(group, "required_group")
                return True
        return False

    def _control_has_value(self, control):
        try:
            if not control.is_visible():
                return True
        except Exception:
            pass
        try:
            control_type = (control.get_attribute("type") or "").lower()
            if control_type in {"radio", "checkbox"}:
                return bool(control.is_checked())
            return bool(control.input_value())
        except Exception:
            return True

    def _log_unanswered_question(self, element, field_type):
        question = self._label_text(element) or element.inner_text() or "(unknown question)"
        question = " ".join(question.split())
        key = (self.page.url, field_type, question)
        if key in self._unanswered_logged:
            return
        self._unanswered_logged.add(key)

        line = (
            f"{datetime.now().isoformat(timespec='seconds')}\t"
            f"field_type={field_type}\t"
            f"url={self.page.url}\t"
            f"question={question}\n"
        )
        try:
            with Path("unanswered_questions.log").open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _log_modal_snapshot(self, reason):
        try:
            modal = self.page.query_selector("div.jobs-easy-apply-modal")
            if not modal:
                return
            text = self._clean_text(modal.inner_text())
            if not text:
                return
            snippet = text[:1500]
            key = (self.page.url, reason, snippet)
            if key in self._unanswered_logged:
                return
            self._unanswered_logged.add(key)
            line = (
                f"{datetime.now().isoformat(timespec='seconds')}\t"
                f"field_type=modal_snapshot\t"
                f"reason={reason}\t"
                f"url={self.page.url}\t"
                f"question={snippet}\n"
            )
            with Path("unanswered_questions.log").open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _abandon(self):
        # Close the modal and discard the draft so nothing partial is submitted.
        if self._click_first_visible(['button[aria-label="Dismiss"]'], timeout=3000):
            self._wait()
            if self._click_first_visible([
                    'button[data-test-dialog-secondary-btn]',
                    'button:has-text("Discard")',
            ], timeout=3000):
                self._wait()

    def _dismiss_post_submit(self):
        if self._click_first_visible(['button[aria-label="Dismiss"]'], timeout=3000):
            self._wait()
