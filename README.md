# LoveWork

**Work that you love, so you never work a day in your life.**

LoveWork is a personal work-discovery agent. Movie stars and successful
creatives have human agents who look continuously for interesting projects,
people, and opportunities. LoveWork asks what it would take to make that kind
of representation available to everyone.

It is not merely a job-search engine. A job is work done for someone else for
money; worthwhile work can also be a collaboration, commission, research
project, company, challenge, residency, or direction that does not yet have a
conventional title.

LoveWork builds a living model of a person's experience, circumstances,
constraints, interests, and possible futures. It searches widely, assesses
opportunities in that context, remembers decisions and outcomes, and improves
its future recommendations.

This repository is the early public face of an active work in progress. The
ideas and design are public before the product is finished because they are
already useful to discuss, test, and build upon.

## Start here

- [Manifesto](MANIFESTO.md) — why LoveWork should exist.
- [Why LoveWork](docs/why-lovework.md) — the problem with episodic job search.
- [Profile model](docs/profile-model.md) — representing a person without
  reducing them to a CV.
- [Intelligence layer](docs/intelligence-layer.md) — the longitudinal judgment
  loop at the centre of the system.
- [Architecture](ARCHITECTURE.md) — the current technical shape.
- [Roadmap](ROADMAP.md) — what exists and what comes next.
- [Privacy](PRIVACY.md) — the boundary between open software and private human
  data.
- [Synthetic example](examples/report.md) — what a future report could look
  like, using fictional data.

## How it works

```text
public sources -> discovery and crawling -> opportunity registry
                                           |
private profile -> contextual assessment --+
                                           |
decisions and outcomes -> learning loop -> reports and suggestions
```

The working prototype already crawls public career and opportunity sources,
tracks their lifecycle, scores them against layered candidate profiles, checks
prior contact, and writes a browsable local wiki. The public release will grow
from this documentation-first foundation toward a reusable engine.

## Status

Early and experimental. Interfaces, terminology, and implementation will
change. The present repository exists so LoveWork has a public address while
the project is still being shaped.

## Licensing

Software is licensed under Apache License 2.0. The manifesto and documentation
are licensed under Creative Commons Attribution 4.0 International. See
[LICENSE](LICENSE).
