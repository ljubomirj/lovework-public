# Work Authorization — (candidate name)

> Where this candidate may live and work, and which roles are auto-DROP because of
> visa/work-permit constraints. The matcher reads this for context, and the
> work-auth hard-kill (matcher.WORK_AUTH_KILL_PATTERNS) auto-DROPs roles like
> "US citizen only" / "no visa sponsorship" regardless of fit.

## Where I may live
- (e.g. UK, MK)

## Where I may work
- (e.g. UK, MK, remote company-to-company via an EOR, US company only if it has a UK entity)

## Hard deal-breakers (auto-DROP)
- US citizen / US person only
- "must be authorized to work in the US" without sponsorship
- "no visa sponsorship" / "cannot sponsor"
- W-2 only, US-based, no UK/EU hiring path

## Right to work summary
- Citizenship/residency: ...
- Sponsorship-requiring roles: out of scope unless a local hiring path exists.
