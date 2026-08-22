# LoveWork public-release review

Use this procedure before adding or updating anything in the fully public
`.git-lovework-public/` repository. Review file **contents and relationships**,
not just names. The aim is deliberate transparency: publish useful ideas and
engine code without accidentally publishing credentials, private people, live
career records, or an unnecessarily detailed attack map of the homelab.

## Output labels

Classify each principal as exactly one of:

| Label | Meaning | Required action |
|---|---|---|
| `PUBLISH` | Safe and useful as it stands. | May be intentionally staged. |
| `SCRUB` | Useful, but contains private or host-specific details. | Make a public copy or edit the selected public version; re-review it. |
| `TEMPLATE` | Operational configuration that should teach a pattern, not expose an installation. | Publish an `*.example`/generic equivalent only. |
| `KEEP PRIVATE` | Live data, personal record, credential material, or uncertain provenance. | Do not stage it. Offer a schema, generator, or synthetic fixture where useful. |

Do not treat “LJ is public online” as blanket consent for a record about LJ,
family members, applications, emails, or career preferences. Assess the
specific file and its aggregation effect.

## Review process

1. **Use the intended Git history explicitly.** Do not trust the `.git`
   symlink:

   ```bash
   git --git-dir=.git-lovework-public --work-tree=. status --short
   git --git-dir=.git-lovework-public --work-tree=. diff --cached --check
   git --git-dir=.git-lovework-public --work-tree=. diff --cached
   ```

2. **Inventory exact files and inspect actual content.** For data files,
   inspect schema, field names, and representative records without echoing a
   possible secret into a transcript. For code and tests, inspect fixtures as
   well as implementation. A test can disclose more than production code.

3. **Apply the six checks below.** Record a short reason beside every `SCRUB`,
   `TEMPLATE`, or `KEEP PRIVATE` decision.

4. **Check coupling and reproducibility.** Public code must either run with
   the public tree or fail clearly with documented configuration. Do not make a
   public test depend on private `profiles/`, `applications/`, Gmail records,
   or homelab state. Replace those dependencies with `profiles/example`, a
   temporary directory, or an inline synthetic fixture.

5. **Check provenance and rights.** Do not publish copied job adverts,
   personal correspondence, private reports, credentials, or material whose
   reuse is unclear. A short test fixture or a link to a primary public source
   is usually preferable to a stored full third-party document.

6. **Stage deliberately and re-review the staged diff.** Run relevant tests
   against the selected code. Do not commit or push unless LJ explicitly asks.

## Six content checks

### 1. Credentials and access

`KEEP PRIVATE`: API keys, OAuth tokens, cookie jars, passwords, private keys,
signed URLs, `.env`, or configuration containing them. Also treat a file as
private if it would directly locate or activate an authenticated account.

Paths and provider names alone are not credentials, but move to `SCRUB` when
their combination reveals more operational detail than the public explanation
needs.

### 2. People and private career data

`KEEP PRIVATE`: profiles, CV/bio text not deliberately published elsewhere,
applications, Gmail-derived records, correspondence, rejections, outcome
notes, personal logs, and generated reports/case packs.

`SCRUB`: tests and documents that embed real family members' names, career
states, work authorisation, preferences, application strategy, or detailed
biographical claims. Replace with an anonymous/example principal if the test
or idea remains useful.

### 3. Live data and aggregation

`KEEP PRIVATE`: registries, caches, databases, ledgers, crawl results, and
dataset JSONL created from real runs. Even when individual job adverts are
public, their aggregation can expose a person's interests, applications,
decision history, email metadata, and the private learning dataset.

Publish a README/schema, a deterministic generator, or a tiny synthetic
fixture instead. Never mistake a JSONL extension for harmless source code.

### 4. Homelab and operations

`SCRUB` or `TEMPLATE`: host names, user names, absolute paths, internal IPs,
Hermes profile names, message channels, cron schedules, log paths, and exact
operational state. These are acceptable in a consciously transparent design
write-up only when LJ chooses the reveal; otherwise generalise them.

Live launchd/systemd/Hermes configurations are `TEMPLATE`, not public runtime
files. Use names such as `com.example.lovework.plist.example`,
`/Users/USER/...`, and `PROFILE_NAME`.

### 5. Third-party material and provenance

`SCRUB` or `KEEP PRIVATE`: full third-party job text, emails, application-site
material, screenshots, and scraped outputs. Prefer a brief synthetic fixture,
a paraphrase, or a durable public URL. Keep attribution and licence terms for
public documents, code, and external quotations.

### 6. Project strategy and maintainability

The private decision ledger, live datasets, and profiles are part of
LoveWork's compounding judgment system. They need not be secret forever, but
they should not be published by default merely to make the repository look
active. Publish the *schema and method*; retain the *live evidence* unless LJ
chooses otherwise.

## Current exemplars (reviewed 2026-07-20)

These are examples, not permanent file-level approvals. Re-review after a
file changes.

| Type | Usual decision | Notes |
|---|---|---|
| Conceptual design docs such as the probabilistic simulator | `PUBLISH` | Check examples for private people and current operational details. |
| Generic engine modules such as application-pack preparation or run ledger/watchdog | `PUBLISH` | Exclude live run records and configuration. |
| Synthetic tests using `tmp_path`/example data | `PUBLISH` | Keep fixtures generic. |
| Tests loading LJ/VJ/KJ/PK profiles or a real application target | `SCRUB` | Convert to an example profile and fictional employer/role. |
| Operational meta-loop narrative | `SCRUB` | The architecture is public-worthy; generalise real schedules, profile names, paths, and message routes if desired. |
| Host-specific plist, Hermes cron JSON, and migration notes | `TEMPLATE` | Publish an intentionally generic installation example only. |
| `dataset/*.jsonl`, wiki reports, cache, registry, applications, outcomes | `KEEP PRIVATE` | Offer schema and synthetic data, not live records. |

## Release handoff

Report:

1. every file reviewed and its label;
2. what was scrubbed, templated, or excluded and why;
3. tests run and their result;
4. the exact staged path list for `.git-lovework-public`.

Do not claim that a scan proves absence of secrets. It is a useful check, not
a substitute for content and provenance review.
