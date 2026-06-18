---
title: "I've Built AI Agents. Now I'm Learning to Build Them Properly - From the Ground Up."
subtitle: "Starting a build-in-public series on agent engineering - the craft, not just the code."
slug: building-ai-agents-properly-from-the-ground-up
tags:
  - ai-agents
  - llm
  - software-engineering
  - python
coverImageURL: ""
seriesName: "Agent Build Log"
---

# I've Built AI Agents. Now I'm Learning to Build Them Properly - From the Ground Up.

I've been working with AI agents for a while now. I can get something working. I can ship a demo.

But there's a gap between "it works" and "I understand why it works" - and an even bigger gap between that and "I'd trust this with a real customer." I've never fully closed either of those gaps in a way I could explain to someone else.

So I'm starting fresh. Not because everything I've built was wrong - some of it was fine. But I want to build the foundation properly this time. Understand the pieces, not just the outcome.

I'm building **Conductor** - a technical co-pilot for data integration - from scratch. Not to ship a product (though eventually, maybe). Mostly to understand the craft properly. What does a well-built agent actually look like? What does it take to know it's working? What breaks first when it meets the real world?

I'll be sharing everything as I go - the experiments, the failures, the things that surprised me. If you're on a similar journey, or you've already solved some of these problems, I'd love to have you along. Your experience and opinions are as useful to me as anything I'll build.

---

## What is Conductor?

Conductor helps users connect data sources, troubleshoot when things break, and answer "how do I..." questions about their data stack. Four modes: setup guidance, technical onboarding, troubleshooting, and knowledge Q&A.

It's the kind of agent that would sit in front of real users. Which means it can't just work - it needs to work *reliably*, handle credentials without leaking them, know when to escalate to a human, and not hallucinate an answer just because a user pushes back.

That constraint is the point. Building something easy to demo is, well, easy. Building something you'd trust is different.

---

## How this series works

I'm breaking the build into 12 sprints. Each one focuses on a specific concept - tool design, memory, RAG, security, multi-tenancy - and produces something real: working code, a test suite, and an honest write-up of what happened.

The format is always the same:

- What I wanted to understand
- What I built to test it
- What broke
- What I actually learned

I won't smooth over the failures. The failures are usually where the useful stuff is.

---

## Where I'm starting: before writing any code

The first thing I'm building isn't the agent.

It's the eval dataset that will tell me whether the agent is any good.

40 cases, written before a single line of agent code exists. Covering all four of Conductor's modes. Including 9 adversarial cases designed to break it in specific ways.

That might sound backwards. It kind of is. I'll explain why in the next post - and show what I found when I actually tried it.

---

*I'm starting this chapter to learn properly, not to perform expertise I don't have. If you're on a similar journey - or you've already figured out the parts I'm about to struggle with - follow along. Point out where I went wrong, what I should focus on next, or how to improve what I just did. That's exactly the kind of conversation I'm after.*

