# Example profile — the shape every principal follows

This directory is **fictional**. It exists so the three-layer contract is
readable and copyable, not to be scored.

To start a principal:

```bash
cp -r profiles/example profiles/<name>
```

Then replace every file's content with the principal's own material — keep the
structure, not the words. `soul.md` and `work_auth.md` are mandatory; at least
one `roles/<role>.md` is mandatory; `possibilities.md` should come from the
principal's own notes rather than from inference.

| File | Layer | Read when |
|---|---|---|
| `soul.md` | — | always: identity, wants, avoids |
| `work_auth.md` | — | always, and by the pre-LLM hard-kill |
| `cv-short.md` | 2 (tip) | always |
| `bio-long.md` | 1 (long path) | on demand |
| `possibilities.md` | 3 (branching) | always; carries `matcher signal:` lines |
| `roles/<role>.md` | — | one per run, selected by `--role` |

Check that it loads before adding tests:

```bash
cd lovework-agent
../venv/bin/python3 -c "import config; print(config.load_profile_text('<name>', 'general'))"
```

A profile that carries a **machine-readable** rule (an allow-list of places, a
country exclude-list, an ideology exclusion) declares it as an HTML-comment
marker at the top of the relevant file — see `docs/03-profiles.md` and the
matcher's pre-LLM checks. Prose is read by the model as a preference; only the
marker kills an advert deterministically.
