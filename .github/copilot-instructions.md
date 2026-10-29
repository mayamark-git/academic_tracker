# Copilot instructions

## Project

- Academic Tracker is a Japanese Streamlit application for managing academic papers, researchers, academic societies, and research-topic discovery.
- The entry point is `app.py`. Shared persistence is implemented in `database.py`; domain logic is split across `cron_fetcher.py`, `research_discovery.py`, `research_watch.py`, `researcher_profiles.py`, `gemini_client.py`, and `utils.py`.
- Read `README.md`, `docs/spec.md`, and `SPEC_CONTEXT.md` before making changes that affect application behavior. The current implementation is authoritative when older planning documents differ.

## Development rules

- Preserve existing Japanese UI wording and follow established module, error-handling, and formatting patterns.
- Keep external API calls behind the existing helpers/adapters where practical. Surface failures to the user; do not silently replace failed external results with guesses or success-shaped defaults.
- Treat AI output as unverified. Keep evidence/source links where available and do not invent bibliographic or researcher facts.
- Make database schema changes idempotent in `database.py` or the appropriate schema initializer. Preserve existing user data and verify both existing and empty database behavior.
- Do not use the personal `papers.db`, PDFs, or `.env` as test fixtures. Tests should use temporary directories or in-memory databases.
- Never add credentials, local research data, PDFs, database files, or generated personal data to source control. Keep `.env`, `papers.db*`, and `pdf_files/` excluded.
- Update directly related documentation when behavior changes.

## Validation

- Run focused tests for the changed module, then use `python -m unittest discover -v` for broader validation when appropriate.
- The app can be started on Windows with `start_app.bat` or with `python -m streamlit run app.py`.
