# Security

## Sensitive Files
Do not upload these files or folders:
- `.env`
- `config.yaml`
- `applications_log*.xlsx`
- `*.log`
- `seen_jobs.json`
- `profile/`, `AutomationProfile/`, or Chrome user-data folders
- resumes, cover letters, or generated archives from real runs

## API Keys
V1 uses local Ollama models and does not require OpenRouter, Anthropic, OpenAI,
or other cloud LLM API keys. If optional cloud providers are added later, keep
keys in `.env` or process environment variables only.

## Local AI Data
Candidate profile text, job descriptions, and form questions are sent to local
Ollama models. Review `config.yaml` before each run and avoid storing unnecessary
personal data in profile text or Easy Apply answers.

## Release Checklist
Before publishing a zip:
1. Run `python -m py_compile agent.py browser.py scorer.py qc.py form_ai.py logger.py setup_wizard.py`.
2. Run `powershell -ExecutionPolicy Bypass -File .\build_release.ps1`.
3. Inspect the zip contents.
4. Confirm the zip contains `config.example.yaml`, not `config.yaml` or `.env`.
