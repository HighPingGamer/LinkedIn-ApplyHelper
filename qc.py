"""Local Ollama QC layer.

The scorer makes the first decision; this double-checks jobs that are about to
be submitted. V1 deliberately avoids cloud APIs so candidate data stays local.
"""

import json
import re

import ollama


PROMPT = """You are a QC reviewer for an automated job-application agent. A local model
already decided these roles are worth applying to. Catch its mistakes BEFORE submission.

APPROVE only if ALL hold:
  - role genuinely matches the candidate (not overstated by local model)
  - location and salary consistent with candidate targets
  - no red flags (scam, commission-only, wrong seniority, unrelated field)
  - role is not restricted to local nationals / citizens only
Otherwise REJECT with a one-line reason.

Return ONLY a JSON array, no prose, no markdown:
[{{"id": "<id>", "approve": true|false, "reason": "<one line>"}}]

CANDIDATE PROFILE:
{profile}

ROLES TO REVIEW:
{roles}"""


def detect_provider(preferred="auto"):
    """V1 supports local Ollama only."""
    return "ollama" if preferred in {"auto", "ollama", "local"} else None


def _build_roles_block(candidates, jd_chars):
    parts = []
    for c in candidates:
        parts.append(
            f'--- id: {c["id"]}\n'
            f'title: {c["title"]}\n'
            f'location: {c["loc"]}\n'
            f'salary_parsed_aed: {c["score"].get("salary_aed_month")}\n'
            f'local_fit_score: {c["score"].get("fit_score")}\n'
            f'jd_excerpt: {c["jd"][:jd_chars]}\n'
        )
    return "\n".join(parts)


def _extract_array(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not m:
        raise ValueError("no JSON array in QC output")
    return json.loads(m.group(0))


def _call_ollama(model, prompt):
    resp = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.1},
    )
    return resp["message"]["content"]


def qc_batch(candidates, cfg):
    """Returns {id: (approve: bool, reason: str)}. Empty dict = QC unavailable."""
    provider = detect_provider(cfg.get("qc_provider", "ollama"))
    if not provider or not candidates:
        return {}

    model = cfg.get("qc_ollama_model") or cfg.get("ollama_model", "qwen2.5:7b")
    jd_chars = cfg.get("qc_jd_excerpt_chars", 1500)
    batch_size = cfg.get("qc_max_batch", 15)
    results = {}

    for i in range(0, len(candidates), batch_size):
        chunk = candidates[i:i + batch_size]
        prompt = PROMPT.format(
            profile=cfg["profile_summary"],
            roles=_build_roles_block(chunk, jd_chars),
        )
        try:
            raw = _call_ollama(model, prompt)
            for item in _extract_array(raw):
                results[str(item["id"])] = (
                    bool(item.get("approve")),
                    item.get("reason", ""),
                )
        except Exception as e:
            for c in chunk:
                results[str(c["id"])] = (None, f"qc_error: {e}")
    return results
