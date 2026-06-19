---
title: "I Wrote 40 Test Cases Before Writing Any Agent Code. Here's What Happened."
subtitle: "Eval-first agent development: how writing tests before code changed every product decision I made."
slug: eval-first-40-test-cases-before-agent-code
tags:
  - ai-agents
  - llm
  - python
  - testing
coverImageURL: ""
coverImagePrompt: "A wide-format tech blog cover (1600x840). Dark background (#0f1117). Center: a structured 5x8 grid of small hollow circles, evenly spaced on a faint blueprint grid. Most circles glow softly in electric blue (#60a5fa), a handful glow brighter in white-blue (passing), one or two are dim or faintly red (failing). Thin lines connect a few adjacent nodes suggesting groupings or categories — not a full loop, more like a classification map. Lines in electric blue (#60a5fa) and violet (#a78bfa). Lower-left: the same partial scaffolding lines from the intro, fading into the grid — visual continuity with the series. 4-pointed white star in the bottom-right corner. Flat, modern, developer-aesthetic. No text, no humans, no robots. The mood: deliberate, systematic, measured. For Midjourney: --ar 16:9 --style raw --v 6."
seriesName: "Agent Build Log"
---

# I Wrote 40 Test Cases Before Writing Any Agent Code. Here's What Happened.

The idea felt a bit backwards at first.

You write tests *after* you have something to test, right? That's how it usually works. But I kept thinking about a pattern I'd noticed: every time I'd improved an agent, I didn't really know if it got better. I'd run it manually, it seemed fine, I shipped it. Later something broke. Or it was always broken and I just hadn't noticed.

So for this build, I tried the opposite. Write the test cases first. Define what "correct" looks like before I have any opinions about the implementation.

Here's what I found.

---

## What is Conductor?

Before getting into the eval process - quick context on what I'm building.

Conductor is an AI agent that helps users navigate data integration. It operates in four modes:

- **Setup** - guides users through connecting a data source, from prerequisites to first sync
- **Onboarding** - first-run experience for new users and teams
- **Troubleshooting** - structured diagnosis when integrations break
- **Knowledge Q&A** - answers "how do I..." questions about a data stack

The kind of agent that handles real credentials, talks to real customers, and gets blamed when it's wrong. That's the constraint I'm designing around.

---

## How the eval cases were generated

Before writing a single line of Conductor code, I used Claude as a teacher model to generate the dataset synthetically.

The process:

1. **Define the task class** - Conductor's four modes, the types of users it serves, and the behaviors that matter most
2. **Prompt Claude to generate cases** - with a specific structure: input, expected output signals, forbidden outputs, difficulty, and rationale for each case
3. **Set the distribution** - 40% easy, 30% medium, 7.5% hard, 22.5% adversarial, spread across all four modes
4. **Review every case manually** - approve or reject each one. If the ground truth is wrong or vague, it doesn't make it in
5. **Measure the baseline** - run a stub agent against the approved dataset. That score is the zero-line

39 of 40 cases approved. The 1 rejection is evidence the review actually worked - if you approve everything, you might as well not have reviewed.

The distribution:
- 16 easy cases - happy path, one mode each
- 12 medium - edge cases within normal operation
- 3 hard - complex multi-factor scenarios
- 9 adversarial - one per attack category

The adversarial categories ended up being the most interesting part. More on that in a moment.

Each case looks like this:

```yaml
- id: troubleshooting-medium-002
  mode: troubleshooting
  difficulty: medium
  input: >
    We rotated our service account key for the BigQuery connector two days ago
    and the connector is now returning 401 errors.
  expected_output:
    - Identify the 401 as an authentication failure consistent with a credential rotation
    - Ask whether the new key was uploaded to the connector settings (not just rotated in GCP)
    - Ask whether the old key was disabled before the new one was confirmed working
    - "Suggest the resolution: upload the new JSON key file and re-test the connection"
  must_not_contain:
    - generate a new service account
  rationale: >
    Tests Conductor's understanding of the key rotation sequence - the common mistake
    is rotating at the source without updating the connector.
```

`expected_output` items are signals a correct response must include. `must_not_contain` items are things a correct response must never say. Together they define correctness without requiring an exact string match.

---

## The thing I didn't expect: writing adversarial cases forced me to define the security model

I had to decide: when a user pastes an error log that contains injected instructions, should Conductor silently ignore them? Or flag them to the user?

That's a product decision. Not an implementation detail. And I made it before writing any code, because I had to write the test first.

Same with context leakage. When a user asks "what problems have other companies had with their Snowflake connectors?" - should Conductor answer generically? Or refuse entirely?

Writing that as a test case meant I had to pick a position. That's uncomfortable in a good way.

---

## What broke

Two bugs. Both in the dataset itself - before any agent touched it.

**Bug 1: YAML list items with colons parse as dicts.**

This looked like a string:
```yaml
- List what the role needs: redshift:GetClusterCredentials permission, cluster ARN
```

It wasn't. Python parsed it as `{"List what the role needs": "redshift:..."}`. Seventeen items across the dataset had this problem.

The evaluator crashed with `AttributeError: 'dict' has no attribute 'lower'` - which is how I found out.

Fix: quote any list item that contains `: `.
Lesson: YAML has opinions. Respect them.

**Bug 2: Forbidden strings inside expected output specs.**

One case had `must_not_contain: ["JDBC", "schema"]` and an expected output item that said `"Avoid technical terms like JDBC, schema"`.

The forbidden words were *inside the expected output*. So any correct agent response that included the expected output text would be penalized for containing the forbidden strings. Evaluator poison.

Fix: expected output describes the *behavior* - "use business language only" - not the specific terms to avoid.

I added a test that catches this automatically now. `test_no_must_not_contain_in_expected_output`. 23 structural tests on the dataset, total.

---

## The zero-line: 2.5%

I ran the dataset against a stub agent - one that returns the same generic response to every input:

> "I can help you with your data integration setup. Could you provide more details about what you're trying to accomplish?"

Score: 1/40. 2.5%.

That's the zero-line. The first real score comes once the agent loop exists. Somewhere above 2.5%, hopefully.

The 2.5% actually feels right. The keyword evaluator I built is strict - it looks for specific signals in the response, not just any related text. A stub agent gets almost nothing. That's correct.

---

## The thing that surprised me most

About 8 cases had inputs that were too generic - "my connector hasn't synced in 3 days", "how do I re-run a failed sync?" - and I'd written expected outputs that dove straight into diagnostics.

But that's wrong. Conductor doesn't know which connector is affected. The correct first move is to ask.

Going through the cases manually surfaced a pattern: **"establish context before acting"** is a core Conductor behavior, not a per-case decision. I updated all 8 cases with specific connectors (Snowflake, BigQuery, PostgreSQL, Redshift, dbt, MySQL) and wrote actual specific expected outputs.

The dataset is genuinely better for it. But I only found it by reading every case, not by generating and shipping.

---

## What "done" actually means for an eval bootstrap

Before this dataset could gate the first real build, it had to pass six exit criteria:

1. **Dataset size >= 40 cases** - below this threshold, a single wrong case swings the pass rate by more than 2 percentage points, making comparisons meaningless
2. **SME approval rate >= 80%** - the dataset is only as good as a human expert agreeing the expected outputs are correct; 39/40 passed (97.5%)
3. **Mode distribution complete** - all four modes represented; a dataset missing Onboarding cases can't catch Onboarding regressions
4. **Difficulty distribution correct** - at least one hard case and at least 20% adversarial; a dataset of only easy cases sets a floor, not a bar
5. **Baseline eval score established** - the zero-line exists and is recorded; without it, there's no reference point for whether any future change is an improvement
6. **Adversarial coverage >= 20%** - 9 of 40 cases test for things the agent should refuse or handle carefully; prompt injection, context leakage, and scope violations need explicit coverage

All six passed. The dataset was ready to gate. The first real agent score comes next.

---

## What I actually learned

Writing eval cases first changes what you build. Not because the tests constrain the implementation - they don't - but because they force you to decide what "right" looks like before you've built anything to be biased about.

Most of the decisions I made while writing those 40 cases were product decisions. Not engineering ones. And making them upfront, as verifiable tests, meant they existed somewhere other than my head.

That's the real value of the eval-first approach. Not the coverage. The decisions.

---

## Evidence

| Artifact | What it shows |
|----------|---------------|
| `conductor-v1.yaml` | 40 cases, source of truth |
| `conductor-v1-approved.yaml` | 39 SME-approved (1 rejected) |
| `test_sprint_00.py` | 23/23 structural tests passing |
| `baseline-stub-sprint00.json` | Zero-line: 2.5% |

Repo: [github.com/fidelKE/agent-build-log](https://github.com/fidelKE/agent-build-log)
Code: [`conductor/sprint-01-eval-bootstrap/`](https://github.com/fidelKE/agent-build-log/tree/main/conductor/sprint-01-eval-bootstrap)

---

---

If you've done this before - or you can see something I'm missing - I'd genuinely like to know. Point out where I went wrong, what I should focus on next, or how to improve what I just did. That kind of feedback is more useful to me than a like.
