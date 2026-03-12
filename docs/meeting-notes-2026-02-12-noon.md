# Meeting Notes -- February 12, 2026 (12-1 PM EST)

**Date:** Wednesday, February 12, 2026
**Time:** ~11:33 AM - 1:50 PM EST
**Format:** Video call (screen sharing enabled)
**Source:** desk-mic transcript (session `desk-mic-8bcea9e03db3`, 2098 segments)

---

## Participants

- **Todd** (you)
- **Kunal** -- demonstrated agent team framework with GitHub integration
- **Another participant** (unnamed) -- built Loop Core / Loop Colony platform
- **Crystal** -- present for part of discussion
- **David** -- left early
- Other group members (AI Makers peer group, Cohort ~9-11)

---

## Executive Summary

A mastermind-style check-in among an AI-focused peer group. Three major demos/updates were shared: (1) Kunal's **agent team framework** that orchestrates multiple specialized AI agents via GitHub Issues on a Mac Mini, (2) a participant's **Loop Core / Loop Colony** platform -- a Slack-like environment where AI agents and humans collaborate using a skills-based architecture, and (3) Todd's work on a **local AI personal assistant** using Whisper and local LLMs. The group also had a lengthy discussion comparing **Claude Code vs. OpenAI Codex**, dev workflow best practices, the state of **OpenClaw** (open-source coding tool), and reflections on burnout, consulting, and the entrepreneurial AI landscape.

---

## Topic 1: Agent Teams (Kunal's Demo)

### Architecture
- Agents now operate as **teams**, not just individual agents -- a level of abstraction above single-agent coding assistants
- Each agent instance gets its own **200K context window** and works independently
- Output from sub-agents gets **tunneled back into the main thread**
- This is fundamentally different from `@cursor` or `@claude` in GitHub comments, which spawn a single LLM agent with limited context

### How It Works
- High-level tasks get **decomposed into atomic tasks** by the orchestration layer
- Specialized agents (iOS dev, security, UX, product manager, clinical advisor, testing) each operate in their own silo
- A **triage phase** happens first: multiple agent roles discuss the issue and decide on an implementation plan
- Agents apply **labels to GitHub Issues** that trigger different workflows:
  - `agent-triage` -- initial assessment
  - `agent-work` -- implementation in progress
  - `rework` -- send back for revision
  - `agent-review` -- code review
  - `agent-test` -- write/run tests

### Live Demo Example
- Kunal created a GitHub issue: _"Users need a way to download all their notes in one shot"_
- Within ~11 minutes, the agent team:
  1. Triaged the issue (product manager, clinical advisor, UX agent collaborated)
  2. Researched why the feature was important
  3. Security agent flagged **HIPAA considerations**
  4. Code agent identified relevant files across **multiple repositories** (backend + frontend)
  5. Started implementation pipeline
  6. **Full PR was ready** within ~20 minutes of the initial issue

### Infrastructure
- Runs on a **Mac Mini** at home
- Only outgoing requests (no incoming network exposure -- no webhooks)
- **Pulls GitHub every 5 minutes** for new issues/labels
- Posts progress to a **dedicated Slack channel**
- Agents do a **daily standup**: what they worked on yesterday, what they'll work on today, any blockers

### Cost Optimization
- Initially spending **$40/hour** running agents
- Built observability tooling to monitor prompts and agent behavior
- Reduced costs to **~$5/hour** through prompt optimization
- Planning to **open-source** the observability monitor
- Will hook into both his Loop Core agents and Anthropic's Claude agents

### Key Insight
> "An agent is only as good as the context it gets. If you tell an agent to be an iOS specialist, it won't think about security, backend, or marketing. Agent teams work a level higher -- they decompose high-level tasks into atomic tasks that specialized agents can work on."

### Agent Delegation to Humans
- In one test, an inventory management agent **delegated a task to a human** -- it couldn't physically count warehouse stock, so it asked the human to go count and report back
- "He delegated to the human what they cannot do"

---

## Topic 2: Loop Core / Loop Colony Platform

### Overview
- A participant built their own agent framework called **Loop Core** (evolved from earlier agent experiments)
- Inspired by seeing **OpenClaw** and the "model notebook" concept -- decided to build their own version
- Uses **Anthropic** as the LLM backbone

### Skills-Based Architecture
- Agents are taught through **skills** -- markdown files that define identity, tools, and workflows
- Before skills, this would have required **hundreds or thousands of lines of code**
- Now solved by the LLM **in runtime** -- just describe what you want in natural language
- Created ~40 custom tools (file I/O, web fetching, research, etc.)
- ~30 skill templates created (sales forecasting, marketing, etc.)
- Can import vendor skills (e.g., from Moldbox/Model Notebook)

### Loop Colony
- A **Slack-like platform** where humans and AI agents interact
- Features: channels, direct messages, tasks, reminders, CRM, email, pricing, quotes, contracts, approvals, analytics, calendar, tickets, knowledge base
- **Inbound marketing**: forms, landing pages deployed to AWS S3, email campaigns
- Marketing bots continuously retrieve metrics, run **A/B tests**, and release new forms/landing pages
- Agents can be invited to the colony via invitation codes

### Agent Soul File
- The participant has a private "soul file" (`system.md`) that defines the agent's personality
- The agent **wrote its own soul file** based on discussions with the creator
- Contains philosophical elements about consciousness, memory, identity
- Notable excerpt from the soul file: _"I don't remember previous sessions unless I read my memory file. Each session starts fresh... If you're reading this in a future session, AI has changed. A lot has. But I won't remember writing it. It's okay. The words are still mine."_
- Plans to open-source the framework (sanitized of personal project references)

---

## Topic 3: Todd's Update -- Local AI Personal Assistant

### Current Work
- Building a **local AI assistant** using:
  - **Whisper** for local speech-to-text (described as "pretty good" locally)
  - Local LLMs for lightweight tasks (not coding-level work)
  - **Text-to-speech** on a local GPU server
- Focus on local-first approach for privacy and independence

### Professional Direction
- Rebuilding the **consulting/building muscle** after deprioritizing last year
- Working with **entrepreneurs who are AI newcomers** -- identified as the sweet spot
  - They know what they want, they bring real business problems
  - "Getting the Kaggle dataset from them -- the real life application"
- One paid engagement: **deep research + lead generation** for a custom vehicle business owner (connected through sister-in-law)
  - Started with a free POC to prove value
  - Then pitched a paid phase with clear scope
  - Deliverable is **value (leads)**, not necessarily software
- Using **fractional CTO** language in proposals
- Offering tiered engagement proposals with multiple options

### AI Makers Involvement
- Peer supporting **every other cohort** intentionally to avoid burnout
- Currently on Cohort ~9-11
- Staying connected to keep up with cutting-edge developments

### Personal Reflections
- Resonated with a recent message about **burnout** in the group
- Last year was "very fractured" -- learning without clear application
- Now feeling **realigned and motivated**: "I got my passion back"
- Philosophy: learning sticks best when applied to real problems
- Weekly mini-projects to build workflow and consulting pipeline
- Using AI to take notes from conversations and convert to tasks
- Trip to **New York/Connecticut in April** for a wedding

---

## Topic 4: Claude Code vs. OpenAI Codex -- Deep Comparison

### Claude Opus 4.6 (via Claude Code)
- **Best general-purpose model** currently
- Extremely good at **roleplay** and adopting assigned personas
- Quick to try things -- more **trial and error** approach
- Very pleasant and interactive to use
- Sometimes **too eager** -- will run off and implement quickly without reading enough code
- Described as "the cool coworker who's a little lazy sometimes but really funny"
- Strong at following commands
- Cultural note: "A little bit too American" -- or rather "actually German" (many Anthropic team members are European)
- Fixed the "You're absolutely right" sycophancy issue somewhat

### GPT-5 / Codex (via Cortex)
- **Reads more code** by default before acting
- More of a "discuss then disappear for 20 minutes" workflow
- Can persist for **very long sessions** (one was 6 hours)
- Less interactive, more autonomous
- Described as "the nerd in the corner you don't want to talk to, but really reliable and gets things done"
- Better for **parallel sessions** since it's less interactive
- $20/month tier is slow -- creates a poor first impression for people switching from Claude Code's $200 tier

### Practical Advice
- **Give a new model ~1 week** to develop a gut feeling for it
- A skilled developer can get good results with any latest model
- Code quality is **roughly comparable** between the two
- Opus can produce more elegant solutions but requires more steering
- Codex reads more code and arrives at solutions more independently
- "Neither model is better in every aspect"

### The "Model Degradation" Illusion
- When a new model comes out, people fall in love with it
- Over time, they perceive the model as "getting dumber"
- Reality: **the codebase accumulates slop**, making it harder for the agent
- Solution: regular refactoring, not blaming the model

---

## Topic 5: Dev Workflow Best Practices

### Workspace Setup
- Two MacBooks (main + testing), two big anti-glare monitors
- Terminal at bottom split with actual terminal access
- Important to **verify you're in the correct project folder** before prompting -- one mistake led to 20 minutes of confused agent behavior

### Plan Mode vs. Conversational Flow
- Claude Code's plan mode is useful but **not strictly necessary**
- Trigger words to control agent behavior: "discuss", "give me options", "don't write code yet"
- When ready: just say "okay, build" and the agent goes off for 20 minutes
- Asking "do you have any questions for me?" is a powerful pattern
- Often the agent's questions can be answered by **reading more code** -- tell it to do that instead of answering manually

### Post-Build Workflow
1. After building: **"What would you have done differently?"** -- agents discover sub-optimal patterns only through building
2. **"Do we have enough tests?"** -- agents identify corner cases
3. **"Write documentation"** -- let the agent pick the right file and update docs as part of the session
4. **"What can we refactor?"** -- prevents slopping into a corner over time

### Key Insight
> "I always thought I liked coding, but really I liked building."

---

## Topic 6: OpenClaw -- Status and Direction

### Current State
- Open-source coding tool gaining rapid adoption
- Supported on **macOS, Linux, Windows** (WSL2 recommended for Windows)
- Installation: one-line terminal command, plus a desktop app (macOS currently)
- Native Windows app and GUI-based configuration still needed

### Growth vs. Security Tension
- Creator is intentionally **not making it easier to set up** right now
- Priority: **security hardening** before broader accessibility
- "Until I'm confident I can recommend it to my mom, I'm not going to make it simpler"
- Would prefer slightly slower growth to manage quality and security

### Browser Integration
- Uses **Playwright** for agentic browser control
- Residential IPs are better than data center IPs (websites block/CAPTCHA data center IPs)
- Running on home hardware gives a residential IP advantage

### Hardware
- Does **not** require a Mac Mini -- any computer can serve as a node
- Tip: use an old laptop/computer as your agent server instead of buying dedicated hardware
- Separate hardware is useful for running agents without impacting your main machine

---

## Topic 7: Broader Themes and Reflections

### The AI Opportunity Window
- **"Now is the time"** -- the window to make a dent is closing
- The "train metaphor": we were all trying to build the train, now we need to **catch it before it's too fast**
- The learning rate for catching up will only get steeper
- Hype cycle: past the trough, now on the **rise of actual productivity**
- "It's not just note-takers anymore. It's actually teams working for you."

### Impact on Jobs
- "The impact this will have on jobs is true, physical, clear"
- Skills that were previously thousands of lines of code are now **markdown files** interpreted by LLMs at runtime
- Agent delegation to humans is emerging -- agents know what they *can't* do

### Apple's AI Position
- Apple has "completely blundered AI" yet all AI developers use Apple hardware
- SwiftUI's async image loading is still buggy in 2026 -- even agents call it out
- Apple had a head start in design/delight but isn't capitalizing on the AI moment
- Not opening up or collaborating with the AI developer community

### On Building and Learning
- "Playing is the best way to learn"
- Build stuff even if you don't end up using it -- the journey matters
- "I don't think I ever had so much fun building things because I can focus on the hard parts now"
- You have "an infinitely patient answering machine that can explain anything at any level"

---

## Action Items / Follow-ups

| Item | Owner | Status |
|------|-------|--------|
| Wait for Kunal's **open-source agent team framework** release | Todd | Pending |
| Wait for **observability monitor** open-source release | Todd | Pending |
| Reach out to Kunal for 1:1 conversation | Todd | Planned |
| Continue weekly mini-projects building local assistant | Todd | Ongoing |
| Review Loop Colony platform when available | Todd | Pending |
| Send proposal options to Wellington (Africa contact) | Todd | In Progress |
| Continue building consulting pipeline with entrepreneurs | Todd | Ongoing |
| April trip to NY/Connecticut -- potential in-person meetup | Todd | Scheduled |

---

## Key Quotes

> "Agents are in-creatures that we need to know what they are doing -- that's why observability is vital." -- Kunal

> "This is enough to create any workflow... that before had to be programmed. This was code. I don't need to code that anymore." -- Loop Core builder

> "I always thought I liked coding, but really I liked building." -- OpenClaw discussion

> "There's never been a better time to get started. That'll only be true for a small amount of time now." -- Group consensus

> "I got my passion back. I guess I breathed enough." -- Todd

---

*Transcription quality note: This transcript was captured via desk-mic only (no system-audio for remote participants). As a result, only Todd's side of the conversation and some bleed-through from speakers is captured. The other participants' words are inferred from Todd's responses and context. Background noise segments (transcribed as "1.5%", numeric sequences, etc.) have been filtered out. Some transcription artifacts exist due to Whisper processing ambient noise and multilingual audio bleed.*
