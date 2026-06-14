# JobHunterJP

Local LinkedIn Easy Apply helper for reviewing jobs, scoring fit, and submitting
only when configured rules and QC approve.

## Setup
```bat
cd /d "C:\path\to\JobHunterJP"
python -m pip install -r requirements.txt
python -m patchright install chromium
ollama pull qwen2.5:7b
python setup_wizard.py
python agent.py --dry-run
```

Use `run.bat` for a live run only after a clean dry-run review.

## Local AI
V1 uses Ollama locally for scoring, QC, and unknown Easy Apply form questions.
It does not require cloud LLM API keys.

## Private Files
`setup_wizard.py` creates a local `config.yaml` file. It contains personal
settings and is ignored by `.gitignore`.

For public sharing, use `config.example.yaml`.

## Build A Safe Zip
```powershell
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```

The release script creates a sanitized zip under `release/` and excludes
runtime logs, workbooks, secrets, browser profiles, caches, and local config.
