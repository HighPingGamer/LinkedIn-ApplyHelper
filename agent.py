"""Main loop with cloud QC.

  python agent.py            # full session using config.yaml
  python agent.py --dry-run  # score + QC, but NEVER submit (recommended first run)

Flow:
  Phase 1 (local, unlimited): walk new jobs, read JD, hard-filter, Ollama score.
                              Passing jobs become apply-candidates.
  Phase 2 (cloud, 1 call):    batch all candidates to the QC model -> approve/reject.
  Phase 3 (local):            Easy Apply only to QC-approved jobs.
  End:                        write applications_log.xlsx.
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

import yaml

import scorer
import qc
from browser import LinkedInBrowser
from logger import SessionLog


def load_seen(path):
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_seen(path, seen):
    with open(path, "w") as f:
        json.dump(sorted(seen), f)


def quick_location_ok(loc_text, acceptable):
    loc_text = (loc_text or "").lower()
    return any(a.lower() in loc_text for a in acceptable)


def canonical_job_key(job):
    url = job.get("url") or ""
    parsed = urlparse(url)
    current_job = parse_qs(parsed.query).get("currentJobId")
    if current_job and current_job[0]:
        return current_job[0]
    match = re.search(r"/jobs/view/(\d+)", parsed.path)
    if match:
        return match.group(1)
    return str(job["id"])


def print_top_fit_scores(scored_jobs, limit=5):
    if not scored_jobs:
        print("\nTop fit scores: no locally scored jobs this run.")
        return

    def fit_value(job):
        fit = job["score"].get("fit_score")
        return fit if isinstance(fit, (int, float)) else -1

    unique_jobs = {}
    for job in scored_jobs:
        job_id = canonical_job_key(job)
        if job_id not in unique_jobs or fit_value(job) > fit_value(unique_jobs[job_id]):
            unique_jobs[job_id] = job

    ranked = sorted(unique_jobs.values(), key=fit_value, reverse=True)[:limit]
    print(f"\nTop {len(ranked)} by local fit score:")
    for idx, job in enumerate(ranked, 1):
        fit = job["score"].get("fit_score")
        salary = job["score"].get("salary_aed_month")
        salary_text = f"{salary} AED" if salary is not None else "salary n/a"
        print(f"{idx}. {fit} - {job['title']} | {job['loc']} | {salary_text} | {job['url']}")


def skip_reason_bucket(note):
    note = re.sub(r"^(target|search):\s*", "", note or "", flags=re.I)
    low = note.lower()
    if "dry run" in low and "would apply" in low:
        return "DRY RUN would apply"
    if "qc rejected" in low:
        if any(term in low for term in ("national only", "nationals only", "citizen only", "citizens only", "locals only")):
            return "QC rejected - nationality/citizenship restricted"
        if any(term in low for term in ("mandarin", "arabic", "language", "fluency")):
            return "QC rejected - language requirement"
        if any(term in low for term in ("junior", "under-seniority", "step down")):
            return "QC rejected - too junior"
        if any(term in low for term in ("recruitment only", "talent acquisition only", "narrow focus", "specialized")):
            return "QC rejected - too narrow/specialized"
        if "scam" in low or "generic gmail" in low:
            return "QC rejected - scam risk"
        return "QC rejected - other"
    if "model judged role not a realistic fit" in low:
        return "Local model - not realistic fit"
    if "location not acceptable" in low or "location pre-filter" in low:
        return "Local - location not acceptable"
    if "below threshold" in low:
        return "Local - fit below threshold"
    if "red flags" in low:
        if any(term in low for term in ("national only", "nationals only", "citizen only", "citizens only", "locals only")):
            return "Local red flag - nationality/citizenship restricted"
        if "commission only" in low:
            return "Local red flag - commission only"
        if "arabic" in low or "mandarin" in low:
            return "Local red flag - language requirement"
        return "Local red flag - other"
    if "empty jd" in low:
        return "Empty JD"
    if "salary" in low and "below floor" in low:
        return "Local - salary below floor"
    return note[:80] or "(blank)"


def print_skip_reason_summary(rows, limit=12):
    skipped = [r for r in rows if str(r[5]).lower() == "skipped"]
    if not skipped:
        return
    counts = {}
    for row in skipped:
        reason = skip_reason_bucket(row[8])
        counts[reason] = counts.get(reason, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    print("\nSkip reason summary:")
    for reason, count in ranked[:limit]:
        print(f"- {count}: {reason}")


def print_dry_run_apply_candidates(rows, limit=15):
    candidates = []
    for row in rows:
        note = row[8] or ""
        if "dry run" in note.lower() and "would apply" in note.lower():
            candidates.append(row)

    if not candidates:
        print("\nDry-run would apply: none.")
        return

    def fit_value(row):
        fit = row[4]
        return fit if isinstance(fit, (int, float)) else -1

    ranked = sorted(candidates, key=fit_value, reverse=True)[:limit]
    print(f"\nDry-run would apply ({len(candidates)} total):")
    for idx, row in enumerate(ranked, 1):
        fit = row[4]
        print(f"{idx}. {fit} - {row[1]} | {row[2]} | {row[7]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="score + QC but never submit")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    seen = load_seen(cfg["seen_jobs_file"])
    log = SessionLog()
    resume = None if cfg.get("tailor_resume") else cfg.get("base_resume_pdf")
    resume_modified = bool(cfg.get("tailor_resume"))
    cap = cfg["max_applications_per_session"]
    candidates = []          # jobs that passed local scoring -> go to QC
    scored_jobs = []         # all jobs that received a local fit score

    def evaluate_job(jid, jd, loc, url, job_title="", company="", source="search"):
        canonical_id = canonical_job_key({"id": jid, "url": url})
        seen.add(str(jid))
        seen.add(canonical_id)
        if not jd:
            log.add(f"(job {canonical_id})", loc, None, 0, "skipped", False, url, "empty JD")
            return
        if not quick_location_ok(loc, cfg["acceptable_locations"]) and not cfg.get("remote_ok"):
            log.add(f"(job {canonical_id})", loc, None, 0, "skipped", False, url, "location pre-filter")
            return

        score = scorer.score_job(cfg["ollama_model"], cfg["profile_summary"],
                                 cfg["acceptable_locations"], cfg["red_flag_keywords"], jd)
        should, why = scorer.passes_filters(score, cfg)
        title = job_title or (jd.split("\n", 1)[0][:60]) or f"job {canonical_id}"
        company_role = f"{company} - {title}" if company else title
        scored_jobs.append({"id": canonical_id, "url": url, "title": title,
                            "loc": loc, "score": score})

        note_prefix = f"{source}: " if source else ""
        if not should:
            log.add(company_role, loc, score.get("salary_aed_month"),
                    score.get("fit_score"), "skipped", False, url, note_prefix + "local: " + why)
            return

        candidates.append({"id": canonical_id, "url": url, "title": company_role,
                           "loc": loc, "jd": jd, "score": score, "why": why})

    with LinkedInBrowser(cfg) as lb:
        target_ignore_seen = cfg.get("target_jobs_ignore_seen", True)
        for target_url in cfg.get("target_job_urls", []) or []:
            target_id = canonical_job_key({"id": target_url, "url": target_url})
            if not target_ignore_seen and target_id in seen:
                continue
            try:
                jd, loc, url, job_title, company = lb.inspect_job_url(target_url)
            except Exception as e:
                seen.add(target_id)
                log.add(f"(target job {target_id})", "", None, 0, "failed", False,
                        target_url, f"target open error: {e}")
                continue
            evaluate_job(target_id, jd, loc, url, job_title, company, "target")

        # ── Phase 1: local discovery + scoring ───────────────────
        for keyword in cfg["search_keywords"]:
            lb.search(keyword, cfg["search_location"])
            for jid, card in lb.job_cards():
                if jid in seen:
                    continue
                try:
                    jd, loc, url, job_title, company = lb.open_job(card)
                except Exception as e:
                    seen.add(jid)
                    log.add(f"(job {jid})", "", None, 0, "failed", False, "", f"open error: {e}")
                    continue
                canonical_id = canonical_job_key({"id": jid, "url": url})
                if canonical_id in seen:
                    seen.add(jid)
                    continue
                evaluate_job(jid, jd, loc, url, job_title, company, "search")

        # ── Phase 2: batched cloud QC ────────────────────────────
        verdicts = {}
        if cfg.get("qc_enabled") and candidates:
            verdicts = qc.qc_batch(candidates, cfg)
        provider = qc.detect_provider(cfg.get("qc_provider", "auto"))
        qc_available = bool(provider) and cfg.get("qc_enabled")

        # ── Phase 3: apply to approved ───────────────────────────
        applied = 0
        for c in candidates:
            if applied >= cap:
                log.add(c["title"], c["loc"], c["score"].get("salary_aed_month"),
                        c["score"].get("fit_score"), "skipped", resume_modified, c["url"],
                        "session cap reached")
                continue

            approve, qc_reason = verdicts.get(str(c["id"]), (None, "no QC verdict"))

            if qc_available:
                if approve is False:
                    log.add(c["title"], c["loc"], c["score"].get("salary_aed_month"),
                            c["score"].get("fit_score"), "skipped", resume_modified, c["url"],
                            "QC rejected: " + qc_reason)
                    continue
                if approve is None:               # QC errored on this one
                    if cfg.get("qc_fallback") == "abort":
                        log.add(c["title"], c["loc"], c["score"].get("salary_aed_month"),
                                c["score"].get("fit_score"), "skipped", resume_modified, c["url"],
                                "QC error, fallback=abort: " + qc_reason)
                        continue
            else:
                # No cloud provider available
                if cfg.get("qc_fallback") == "abort":
                    log.add(c["title"], c["loc"], c["score"].get("salary_aed_month"),
                            c["score"].get("fit_score"), "skipped", resume_modified, c["url"],
                            "QC unavailable, fallback=abort")
                    continue

            note_prefix = ("QC approved: " + qc_reason) if qc_available else "local-only (no QC)"

            if args.dry_run:
                log.add(c["title"], c["loc"], c["score"].get("salary_aed_month"),
                        c["score"].get("fit_score"), "skipped", resume_modified, c["url"],
                        "DRY RUN — would apply [" + note_prefix + "]")
                continue

            try:
                lb.open_job_url(c["url"])
                status, note = lb.easy_apply(cfg["easy_apply_answers"], resume)
            except Exception as e:
                status, note = "failed", f"apply error: {e}"
            if status == "submitted":
                applied += 1
            if status in {"failed", "skipped"} and (
                    status == "failed" or "manual needed" in note or "no Easy Apply button" in note):
                seen.discard(str(c["id"]))
                seen.discard(canonical_job_key(c))
            log.add(c["title"], c["loc"], c["score"].get("salary_aed_month"),
                    c["score"].get("fit_score"), status, resume_modified, c["url"],
                    f"{note} [{note_prefix}]")

    if args.dry_run:
        print("\nDry run: seen job history was not updated.")
    else:
        save_seen(cfg["seen_jobs_file"], seen)
    sub, skip, fail, log_path = log.write(cfg["output_xlsx"])
    qc_state = provider if qc_available else "none"
    print(f"\nDone. QC provider: {qc_state}. Submitted {sub}, skipped {skip}, failed {fail}.")
    print(f"Log written to {log_path}")
    if args.dry_run:
        print_top_fit_scores(scored_jobs)
        print_dry_run_apply_candidates(log.rows)
        print_skip_reason_summary(log.rows)


if __name__ == "__main__":
    sys.exit(main())
