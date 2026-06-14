"""Local Ollama fallback for LinkedIn Easy Apply form questions.

Deterministic config answers are still preferred. This module is only for
unknown required questions/options that would otherwise force manual review.
"""

import json
import re
from pathlib import Path

import ollama


FORM_PROMPT = """You answer LinkedIn Easy Apply form questions for this candidate.
Return ONLY a JSON object, no prose:
{{"answer": string|null, "reason": "short reason"}}

Rules:
- Be truthful and conservative.
- Prefer exact configured answers when relevant.
- For select/radio options, answer with exactly one provided option label.
- If none of the options are truthful/safe, choose "Prefer not to say" or "No" if available; otherwise return null.
- Never invent certifications, language fluency, degrees, nationality, salary, or work authorization.
- For "how many years" questions about a specific domain not evidenced in the profile, answer "0" if a numeric answer is required.
- For city/current location questions use the configured current location.

CANDIDATE PROFILE:
{profile}

KNOWN ANSWERS:
{known_answers}

QUESTION:
{question}

FIELD TYPE:
{field_type}

OPTIONS:
{options}
"""


def _extract_object(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("no JSON object in form AI output")
    data = json.loads(match.group(0))
    answer = data.get("answer")
    if answer is None:
        return None, data.get("reason", "")
    answer = str(answer).strip()
    return answer or None, data.get("reason", "")


def _call_model(prompt, cfg):
    model = cfg.get("form_ai_ollama_model") or cfg.get("ollama_model", "qwen2.5:7b")
    resp = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.1},
    )
    return _extract_object(resp["message"]["content"])


def answer_question(question, field_type, options, cfg):
    if not cfg.get("form_ai_enabled", True):
        return None, "form AI disabled"
    question = " ".join((question or "").split())
    if not question:
        return None, "blank question"
    known_answers = json.dumps(cfg.get("easy_apply_answers", {}), ensure_ascii=False, indent=2)
    options_text = json.dumps(options or [], ensure_ascii=False)
    prompt = FORM_PROMPT.format(
        profile=cfg.get("profile_summary", ""),
        known_answers=known_answers,
        question=question[:1500],
        field_type=field_type,
        options=options_text,
    )
    try:
        return _call_model(prompt, cfg)
    except Exception as e:
        return None, f"form_ai_error: {e}"


def log_answer(question, field_type, options, answer, reason, source="ai"):
    line = (
        f"source={source}\t"
        f"field_type={field_type}\t"
        f"question={' '.join((question or '').split())}\t"
        f"options={json.dumps(options or [], ensure_ascii=False)}\t"
        f"answer={answer}\t"
        f"reason={reason}\n"
    )
    try:
        with Path("ai_form_answers.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
