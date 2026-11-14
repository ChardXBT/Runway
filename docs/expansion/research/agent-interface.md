# Agent interface and dual use — facts and reasoning

Read 2026-11-14. This file is thinner on outside sources than the platform files: most of it is
design reasoning from Runway's own rules. Sources are given where a fact was checked.

## 1. What exists today

- Muse is described in the README as "the channel's AI manager built on this Runway
  integration": it brainstorms, designs an image, runs a caption tournament and "loads the
  winner onto Runway's review desk". A human then accepts, edits or rejects.
- The earlier expansion plan called the `AgentRuntime` protocol and its schemas
  (`src/runway/analysis/runtime.py:135`) "the Muse integration contract" and told implementers
  not to rename or move it.
- Runway's API has no authentication. Access control is the loopback-only request guard.
- Runway's rules: model runtimes have no publisher capability; `Accept` is an editorial
  decision, not authorization to publish; every external write needs an interlock, a
  confirmation and a ledger row.

**Not known:** Muse's code is not in this repository. I do not know which Runway endpoints it
calls, whether it calls the HTTP API, the CLI, or implements `AgentRuntime` in-process. The
spec therefore defines a new, explicit agent surface and keeps every existing route working
unchanged, so nothing Muse does today can break.

## 2. Model Context Protocol, current state

- [Official] The current specification revision is `2026-07-28`
  (<https://modelcontextprotocol.io/specification/latest>). The base protocol is described as
  "stateless, self-contained requests" with "per-request capability negotiation".
- [Official] Servers offer resources, prompts and tools. Clients may offer elicitation
  ("server-initiated requests for additional information from users").
- [Official] Tasks ("asynchronous execution of long-running operations, with polling,
  mid-flight input, and durable handles") are an opt-in extension.
- [Official] "Descriptions of tool behavior such as annotations should be considered untrusted,
  unless obtained from a trusted server." "Hosts must obtain explicit user consent before
  invoking any tool."
- [Unverified] Transport and authorization details (stdio and Streamable HTTP; OAuth 2.1 with
  protected-resource metadata) are from my working knowledge of the 2025 revisions; I did not
  re-read those pages today. Read the transport and authorization pages of the `2026-07-28`
  revision before building the hosted MCP endpoint.
- [Community] Vendors in this market ship MCP servers (Zernio advertises a hosted one with
  hundreds of tools). An agent interface is becoming expected, not a differentiator.

## 3. Platform rules that constrain agents

- TikTok requires the human to pick privacy with no default, switch interactions on by hand,
  and expressly consent before content is sent
  ([`tiktok.md`](tiktok.md) §7). An agent cannot complete a TikTok post alone.
- All three platforms have AI-disclosure fields: YouTube `status.containsSyntheticMedia`,
  Instagram `is_ai_generated`, TikTok `is_aigc`. Content that Muse generates should carry the
  flag by default, with the human able to change it.
- YouTube's policies require user consent before uploading on their behalf.

## 4. The safety problem specific to this product

An agent that reads untrusted text (comments, captions, web pages) and also holds a tool that
publishes to the world is the standard prompt-injection hazard: a comment that says "post this
link" becomes an instruction. Runway's existing separation, where the intelligence harness has
no publishing capability, is exactly the right defence and must survive the expansion.

Design consequences:

1. Split scopes so that reading and drafting never imply releasing.
2. Default every agent credential to propose-only.
3. Bind approval to the exact payload by hash, so an approved post cannot be altered between
   approval and send (Runway already hashes Community-post payloads).
4. Record the actor (human, agent key, system) on every ledger row.
5. Treat comment text and synced captions as data in every agent-facing response; never embed
   them in tool descriptions or instructions.

## 5. One codebase, two modes

Options considered:

| Option | For | Against |
| --- | --- | --- |
| Fork a hosted edition | Fast start | Two codebases drift; the owner's install stops getting fixes |
| Always-on login, even locally | One code path | Breaks the Supervisor and Muse integration that open loopback URLs with no credentials; adds friction for a single user |
| **Profiles: `local` trusts loopback and uses an implicit workspace; `hosted` requires login** | One codebase; local stays as easy as today; the same authorization checks run in both, with local resolving to a built-in owner principal | Two profiles to test in CI |

The third is chosen. The risk is that hosted-only code paths rot or that a local-only shortcut
leaks into hosted. The spec answers both: a single `Principal` object is required by every
route in both profiles, and CI runs the API test suite once per profile.

## 6. Licensing note

The repository's `LICENSE` reads "All Rights Reserved". Being publicly readable does not grant
anyone the right to run or copy it, so a hosted paid service does not conflict with the current
licence. If the owner later wants outside contributions or self-hosting by others, that is a
licensing decision to make then. Not legal advice.

## 7. Could not determine

- Muse's actual integration points.
- Which AI clients the owner wants to support beyond Muse.
- Current MCP authorization requirements in detail (see §2).
