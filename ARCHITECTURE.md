# Architecture

LoveWork is designed around a separation between an open discovery engine and
private human context.

```text
Opportunity sources
  career pages | public feeds | curated sources | personal inbox adapters
                              |
                              v
                    discovery and extraction
                              |
                              v
                      opportunity registry
                   first seen | last seen | state
                              |
                +-------------+-------------+
                |                           |
                v                           v
        contextual assessment       prior-contact memory
                |                           |
                +-------------+-------------+
                              |
                              v
                    recommendations/report
                              |
                              v
                   decisions and outcomes
                              |
                              +----> future assessment
```

## Main components

### Sources

Sources discover principal opportunities. Some are generic public adapters;
others may be private integrations configured by an individual. A common
interface allows new sources to be added without changing the rest of the
pipeline.

### Discovery and extraction

The crawler follows likely paths through organisation sites and turns
unstructured pages into opportunity records. LLM assistance is useful for
messy pages, but deterministic APIs and structured feeds are preferred where
available.

### Opportunity registry

The registry remembers every opportunity seen and its lifecycle. This makes
the system longitudinal: it can distinguish a new opening from one that has
persisted, changed, disappeared, or returned.

### Profile and assessment

Assessment combines a layered profile with opportunity evidence. Hard
constraints are applied before subjective scoring. The output is not only a
number; it records the reasoning, uncertainties, and recommended next action.

### Memory and feedback

The agent remembers prior applications, conversations, decisions, and outcomes
when the person chooses to provide them. Feedback should improve later
recommendations without making private history public.

### Interfaces

The same pipeline can be presented through a command-line interface, an agent
tool protocol, a local dashboard, or a future hosted service. These interfaces
should call one implementation rather than reproduce the matching logic.

## Public/private boundary

The reusable engine, schemas, interfaces, and synthetic examples can be open.
Real profiles, communications, application histories, private source
configuration, and longitudinal outcome data remain under the person's
control. See [PRIVACY.md](PRIVACY.md).

---

Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
