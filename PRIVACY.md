# Privacy

LoveWork works only if it can understand a person in unusually rich context.
That makes privacy part of the architecture, not an optional policy layer.

## What may be public

- The project mission, design, and documentation.
- Generic engine code and public-source adapters.
- Data schemas and interfaces.
- Anonymised or entirely synthetic examples.
- Aggregate project statistics that cannot be traced to a person.

## What is private by default

- Real principal profiles, CVs, biographies, constraints, and aspirations.
- Application histories, correspondence, interviews, and outcomes.
- Email-derived leads and personally curated source lists.
- Model reasoning that reveals private preferences or past contact.
- Live registries, reports, caches, logs, and longitudinal learning datasets.
- Credentials, deployment configuration, and home infrastructure details.

## Principles

### The person controls the record

A person should be able to inspect, correct, export, and remove the information
LoveWork holds about them. Model inferences should not silently become facts.

### Private data is not training material by default

Running a personal agent does not imply permission to publish, pool, sell, or
use that person's history to train a shared model.

### Public examples must be safe by construction

The public repository uses fictional data. Removing names from a real profile
or report is not necessarily anonymisation: choices, career history, location,
and outcomes can re-identify someone.

### Integrations are optional

Inbox and application-history integrations should be replaceable adapters.
The core system must remain useful when a person chooses not to connect them.

### Minimise before storing

LoveWork should store the least sensitive representation needed for the task,
make retention visible, and avoid copying raw personal material when a derived
fact is sufficient.

## Status

This document states the project's design intent. It is not yet a privacy
policy for a generally available hosted service; such a service would require
specific operational, legal, security, retention, and consent provisions.

---

Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
