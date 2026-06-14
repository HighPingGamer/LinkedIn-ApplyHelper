"""Scores a job description against the user's profile using a local Ollama model.
Returns a dict the agent uses to decide: apply, or skip (and why)."""

import json
import re
import ollama


SYSTEM = """You are a careful job-fit screener. You receive a candidate profile and a
job description. Respond with ONLY a JSON object, no prose, no markdown fences, with keys:
  "salary_aed_month": integer or null   (monthly AED if stated, else null)
  "location_ok": boolean                (does the role's location match the candidate's acceptable list?)
  "fit_score": integer 0-100            (how well the candidate matches the core requirements)
  "red_flags": array of strings         (any concerning phrases you found; empty if none)
  "can_pull": boolean                   (is this a role the candidate can realistically do?)
  "reason": string                      (one short sentence explaining the score)
Be honest. Do not inflate fit_score. If the role is clearly senior beyond the candidate
or in an unrelated field, score it low."""


def _extract_json(text: str) -> dict:
    """Local models sometimes wrap JSON in fences or add stray text. Be forgiving."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in model output: {text[:200]}")
    return json.loads(match.group(0))


def score_job(model: str, profile_summary: str, acceptable_locations: list,
              red_flag_keywords: list, jd_text: str) -> dict:
    user = (
        f"CANDIDATE PROFILE:\n{profile_summary}\n\n"
        f"ACCEPTABLE LOCATIONS: {', '.join(acceptable_locations)}\n"
        f"KNOWN RED-FLAG PHRASES: {', '.join(red_flag_keywords)}\n\n"
        f"JOB DESCRIPTION:\n{jd_text[:6000]}"
    )
    resp = ollama.chat(
        model=model,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": user}],
        options={"temperature": 0.1},
    )
    raw = resp["message"]["content"]
    try:
        data = _extract_json(raw)
    except Exception as e:
        # Fail safe: if the model output is unparseable, treat as "do not apply".
        return {"salary_aed_month": None, "location_ok": False, "fit_score": 0,
                "red_flags": [f"scorer_parse_error: {e}"], "can_pull": False,
                "reason": "Could not parse model output; skipping for safety."}
    # Normalise / defend against missing keys
    data.setdefault("salary_aed_month", None)
    data.setdefault("location_ok", False)
    data.setdefault("fit_score", 0)
    data.setdefault("red_flags", [])
    data.setdefault("can_pull", False)
    data.setdefault("reason", "")
    return data


def passes_filters(score: dict, cfg: dict) -> tuple:
    """Returns (should_apply: bool, reason: str) given a score dict and config."""
    floor = cfg.get("min_salary_aed_month")
    sal = score.get("salary_aed_month")
    fit = score.get("fit_score", 0)
    min_fit = cfg.get("min_fit_score", 70)
    if floor and sal is not None and sal < floor:
        return False, f"salary {sal} below floor {floor}"
    if not score.get("location_ok"):
        return False, "location not acceptable"
    if score.get("red_flags"):
        return False, "red flags: " + "; ".join(score["red_flags"])
    if fit < min_fit:
        if not score.get("can_pull"):
            reason = score.get("reason") or "model judged role not a realistic fit"
            return False, f"fit {fit} below threshold {min_fit}; {reason}"
        return False, f"fit {fit} below threshold {min_fit}"
    if not score.get("can_pull"):
        reason = score.get("reason") or "model concern"
        return True, f"fit {fit}; sending to QC despite local can_pull=false: {reason}"
    return True, f"fit {fit}, {score.get('reason', '')}"
