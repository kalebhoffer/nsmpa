# Contributing and corrections

This project publishes evidence about real organizations. Accuracy matters more than anything else here.

## Reporting an error in the evidence

If an excerpt is misquoted, mislabelled (e.g. marked "supportive" when it is not), attributed to the wrong
organization, or out of date, please open an issue with:

- the evidence ID (the `ID` column in `evidence/*/NSMPA_evidence.xlsx`) or the organization and URL,
- what is wrong, and a link to the current source.

Corrections are applied by fixing the classifier or the data, re-running `nsmpa reclassify`, and regenerating the
packet. Every correction is recorded in `CHANGELOG.md`.

Organizations that want their own policy reflected accurately are especially welcome to open an issue or send the
current policy text; it will be recorded as a researcher capture with its source and date.

## Code changes

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,ai]'
ruff check src tests
python -m pytest -q
```

Tests never touch the network or spend search credits. When you change the sentence classifier, add the sentence that
motivated the change to `tests/` (both the case you fixed and a case that must not change), and run `nsmpa reclassify`
on existing runs so stored results match the new rules.
