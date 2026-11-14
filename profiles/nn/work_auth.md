# Work Authorization — NN

## Passports, and exactly what each one confers (2026-09-23)

A passport confers the right to live and work in its own country — and, when that
country is in the EU, in the whole of the EU/EEA.

- **UK passport → the UK.** Unrestricted.
- **MK passport → North Macedonia only.** *Not* an EU right: MK is outside the
  EU/EEA, so it grants nothing anywhere else.
- **NN holds no EU passport.** He therefore has **no EU free movement**: an EU/EEA
  role is only workable if the employer can sponsor a permit or contract him
  through a UK entity / employer-of-record arrangement. Treat "EU-located" as a
  *question to answer in the advert*, never as a green signal on its own.

> **North Macedonian passport — recorded for completeness, not a location
> signal.** It barely moves job prospects: a role advertised in the UK is not
> going to be located in MK, so do not rank a role higher or lower for it. Its
> only real use is a genuinely MK-based role (an MK employer, or a remote role
> tied to an MK entity).

## Where I may live
- **UK** ( London ) — primary base.
- **MK** ( North Macedonia ) — secondary.

## Where I may work
- **UK** — unrestricted.
- **MK** — unrestricted.
- **Remote, company-to-company** — fine if the employer can contract me via a UK entity
  or an employer-of-record (e.g. via Indigo Labs / Deel-style arrangement); I am set up for that.
- **US company** — only OK if it has a **UK presence / UK entity** that can employ me, OR
  sponsors a route I qualify for. A purely US-based W-2 / "US person only" role is NOT OK.

## Hard deal-breakers (auto-DROP)
Any role that requires:
- **US citizen / US person / US national only**
- **"must be authorized to work in the US"** (without sponsorship)
- **"no visa sponsorship"** / **"cannot sponsor"**
- **W-2 only**, US-based, no UK/EU hiring path

is an automatic DROP — I am not eligible, so do not surface it.

## Green location signals
- **Remote (worldwide / global)** — good.
- **London / UK** — good.
- **EU-located roles** — *not* a green signal on their own (see the passports
  section): NN has UK + MK passports only, no EU free movement. Good **only** with
  a permit/sponsorship route or a UK-entity / employer-of-record contract path.
- **Remote (US)** — only OK if explicitly open to non-US/contracted hires; otherwise DROP.

## Right to work summary
Citizenship/residency: UK + MK. No US work authorization and no current US visa.
Sponsorship-requiring US roles are out of scope unless a UK hiring path exists.

## Work location — allow-list (2026-10-09)

<!-- lovework:work-location-allowed: UK, MK -->

My body may be in the **UK** or **MK** (or anywhere *remote*, contracted through
a UK entity / employer of record). An advert whose **workplace** is anywhere
else — Paris on-site, Berlin, Stockholm, a US office — is an automatic DROP: it
needs a relocation and a right-to-work path I do not have (UK + MK passports
only, no EU free movement).

This is the allow-list dual of `work-auth-exclude-countries`, which VJ and KJ use
to *exclude* countries. An exclude-list cannot express "UK and MK only", which is
why the 2026-10-03 Alder Works Partners rows (Paris, on-site) scored GO 9.7 with six packs
prepared while the EU note above said the opposite.

The machine rule is permissive by construction, like its siblings: an advert that
states any remote option is left to the model, so is a mixed location that names
a UK/MK place ("Paris; London"), and an unrecognised place is never killed —
because a kill that fires on a place it did not recognise would drop real work.

## Work mode (2026-10-06)

<!-- lovework:work-mode: remote-only -->

**Remote-only.** An advert that is on-site, with zero remote or hybrid option, is
disqualified — regardless of how good the role looks or how commutable the city
is. The 2026-10-03 Callosum GO (9.7, "London on-site → commutable from
London" scored as a plus) is the ruling case: the label is a fact to enforce,
not a fit question for the model. Hybrid is fine; remote (UK/EU timezones)
preferred.
