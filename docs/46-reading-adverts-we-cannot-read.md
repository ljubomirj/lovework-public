# Chapter 46 — Reading an advert we cannot read: LinkedIn, logins, and the limit of the open web

> **Audience:** LJ, builders. **Status: BUILT (2026-10-14).** The identity fix and
> the closed-advert kill are live; §4's option B is implemented
> (`browser_fetch.py` + `browser_login.sh`), verified live on the John Lewis advert
> (provider `chrome`, *"No longer accepting applications"* read through LJ's
> session). The one thing still pending is **LJ's two-minute login** — run
> `./browser_login.sh`, log in to LinkedIn in the window it opens, close it.
> **Companions:** [43](43-the-lovework-loop.md) (the loop), [45](45-the-two-closing-piles.md)
> (the closing piles), [15](15-principal-data-boundaries.md) (per-principal credentials).

## 1. What LJ spotted

A VJ pack had been prepared for **John Lewis Partnership — Pricing Analyst**, and
the advert says *"No longer accepting applications"*. He checked it in an
incognito window and saw only an anodyne page — the advert needs a LinkedIn login.

## 2. What actually happened — the machinery *did* catch it

| When | What |
|---|---|
| 2026-09-12 09:02 | the world pass recorded **`expired`** — *"advert closed: 'no longer accepting applications'"* |
| 2026-10-09 | a fresh sweep scored the same advert **GO 8.5/10** and cut a pack |

Both were true because the two records never met: the verdict was filed against
`…/linkedin.com/comm/jobs/view/4446786931/` and the pack stored
`…/linkedin.com/jobs/view/4446786931/`. **One advert, two strings.**

Two fixes, both live:

1. **One advert-URL identity** (`lead_identity.advert_url_key`): normalises the
   scheme, a `www.` prefix, LinkedIn's `/comm` mail form, a trailing slash and
   tracking parameters. The re-check, the piles and the assessment all key on it —
   and applying it retired **81 more packs** (lj 8, vj 73) whose closed verdicts
   had been sitting in the cache, unattached, since September.
2. **A closed advert is never a lead** (`matcher._check_closed_advert_kill`): the
   assessment consults the dated verdicts and the piles *before* the model call —
   an advert that is closed is a **DROP**, whatever it would score. The check is
   per-principal; an unnamed principal makes it inert rather than reading
   somebody else's state.

## 3. Why the fetch could not save us — and where it *did* work

LJ's incognito test is the general case: **LinkedIn shows a logged-out visitor a
login wall**, not the advert. LoveWork's fetcher is Exa (`exa,http`), a third-party
reader with no session — so for *some* LinkedIn adverts we get the public shell and
for others nothing, and `link_check` already has a verdict for that: **`authwall`**.

The lesson from this case is narrower and more useful than "we cannot read
LinkedIn": **when we cannot read the page, absence of evidence is not evidence of
life.** An unreadable advert must not be counted as open, and the honest state is
"we could not check this" — which is what the verdict says, once something acts on
it.

## 4. Can LoveWork use LJ's logged-in Chrome on host-a? — a proposal

**Technically, yes.** host-a already runs a Chrome with LJ's profile, and a
logged-in session is a *credential* like the Gmail token: a browser profile
directory, a per-principal credential, a copy of nothing else. Three shapes, in
increasing order of commitment:

| Shape | What it gives | Cost and risk |
|---|---|---|
| **A. Reuse the profile directory** (a headless Chrome pointed at `~/.config/google-chrome` with the principal's profile) | the rendered page, as he sees it | one host, one profile, one login; a crash can corrupt a profile he uses interactively |
| **B. A dedicated principal browser profile** (a separate Chrome profile on host-a, logged in once by LJ, used only by LoveWork) | the same reads, with no risk to his daily browsing | he logs in once; the credential is per-host and per-principal, like `credentials/gmail/` |
| **C. Employer-side first** (resolve the advert to the company's own posting and read *that*) | an open page, no login, no ToS question | not always available; needs a resolver per source |

**My recommendation: C, then B — and never A.** Employer-side first because the
same role is usually posted on the company's own careers page (and a *company*
page is what LoveWork already reads well); B when the employer has no public
posting, with LJ's explicit consent and a profile that is not his daily one; A
because it is the cheap answer and the one that can cost him his own session.

Two boundaries I would hold regardless of the shape:

- **Read, never act.** A logged-in session may open an advert and read it. It may
  not click *Apply*, message a recruiter or fill a form — the doctrine that the
  principal acts is what keeps an agent with credentials from becoming an agent
  with consequences.
- **Per-principal, always.** LJ's LinkedIn session is LJ's. VJ's adverts are read
  by VJ's agent with VJ's credential, or not at all (ch15). A shared session would
  be the exact leak the four-principal design exists to prevent.

**What LJ must decide:** whether to stand up B now (a dedicated profile + one
login), to try C first (I can measure how many LinkedIn-sourced adverts resolve to
an employer page), or to live with `authwall` as an honest "cannot verify" until a
principal asks for more.

**Interim behaviour, no decision needed:** an `authwall` verdict is recorded and
the advert is *not* retired (we do not know it is closed), but it is also not
evidence of life — the pack stays, the brief can say "unverifiable", and the
principal's own eyes are the check.
