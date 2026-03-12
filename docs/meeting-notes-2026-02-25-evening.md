# Meeting Notes -- February 25, 2026 (Evening)

**Date:** Tuesday, February 25, 2026
**Time:** ~8:30 PM - 9:40 PM EST
**Format:** Video call (Zoom)
**Source:** desk-mic + app-audio transcript (audio_assist.db)
**Note:** DB timestamps show ~80-90 min drift from actual wall clock; actual start was ~8:30 PM EST.

---

## Participants

- **Todd Deshane** (you)
- **Mark** -- client, Don Brown Bus / parts business owner
- **David** -- sales team member, works HubSpot contacts

---

## Executive Summary

Weekly status call covering three main areas: (1) the **daily lead scraping / HubSpot integration** workflow -- how to get AI-found leads properly imported into HubSpot with deduplication, contact enrichment, and ownership assignment, (2) the **sam.gov scraping** feature that was set up but forgotten about and needs verification before auto-importing, and (3) the **AI phone calling** system for customer outreach -- status on warm transfers, voice improvements, and the robocalling risk discussion. Key decisions: new contacts go into HubSpot owned by Tabitha, Todd will hand-craft a small test batch first, sam.gov data stays out of HubSpot until Mark reviews it, and the phone calling system needs warm transfer capability to the parts store.

---

## Topic 1: Daily Lead Scraping & HubSpot Integration

### Current State
- Todd has been sending daily lead reports (scraping various sources) as spreadsheets/emails
- Some leads are repetitive -- the same organizations keep showing up day after day
- The AI scraper doesn't always distinguish between genuinely new leads vs. old/stale data
- Some leads were found to be premature or out of date when David called on them
- Feedback from Tabitha: some contacts were already called / already in the system

### Problems Identified
- **Deduplication**: The scraper finds things but doesn't cross-reference with what's already in HubSpot, so duplicates appear
- **Data quality**: Some scraped contacts have no email or phone number; others have multiple contacts jammed into one cell
- **Temporal awareness**: The AI doesn't track time well -- sometimes surfaces 2-year-old data as "new"
- **Contact structure**: Some records have CEO, director, program manager etc. all in one field instead of separate contact records

### Agreed Workflow
1. **Check HubSpot first** -- before importing, verify the contact/company doesn't already exist; skip if marked unqualified
2. **Create individual contact records** -- each person gets their own record, associated with one company record
3. **Vehicle need goes in Notes** -- not a custom attribute, just add it as the first note on the contact
4. **Assign to Tabitha as owner** -- all new imports go to Tabitha, she quarterbacks assignment to Dave/Jen
5. **Small batch test first** -- Todd will hand-craft ~5 imports, Mark and David verify the format is right, then scale up
6. **Don't flood HubSpot** -- only import once the process is validated

### Contact Enrichment
- David suggested using **Apollo** to enrich contacts missing email/phone
- Apollo is David's agency tool with limited credits
- Todd can also use **Manus AI** for a second pass on hard-to-find contacts
- HubSpot's **ClearBit** (acquired by HubSpot) might help but may require higher plan or business email
- Plan: try Manus first, fall back to Apollo for the hard ones

### Action Items
- Todd: Import a small test batch into HubSpot (contacts from today's scrape), owned by Tabitha, with vehicle need in notes
- Todd: Fix data structure issue (split multi-person cells into individual contact records)
- Todd: Cross-reference against existing HubSpot data before importing
- Mark/David: Review the test batch and provide feedback on format
- David: Forward the P4 state expansion email to Todd

---

## Topic 2: Sam.gov Scraping

### Discovery
- Mark noticed sam.gov data appearing at the bottom of the daily scrape emails
- Todd had set up the sam.gov scraper earlier but forgot to follow up on it
- The AI "doesn't forget things" -- it just started doing it when the date became possible

### Decision
- **Do NOT auto-import sam.gov data into HubSpot yet**
- Mark wants to review the data first -- it's pre-solicitation and he doesn't know enough about it
- Todd will verify the sam.gov scraper is getting good information
- Once Mark reviews and approves, they'll decide whether to push it to HubSpot
- David: "It's good you called that out because if it was in the list I would have just sent it"

### Action Items
- Todd: Verify sam.gov scraper output quality
- Mark: Review sam.gov data and decide if/when to import
- Todd: Keep sam.gov data separate from auto-import pipeline for now

---

## Topic 3: AI Phone Calling System

### Status Update
- Jim provided a script that the team liked
- Todd sent test calls to Mark, David, and Andy
- **Voice quality improved** from previous version -- "quite a bit better than the previous"
- **Bug fixed**: voicemails were cutting off due to a code error; second batch fixed this
- **Issue**: Andy said "no" during a call and it hung up on him -- Todd investigating

### Mark's Feedback
- The call asked "How are you today?" and Mark wanted to skip the small talk
- Mark's preference: straight to the point -- "Hi, this is [name] from Don Brown Parts Department. We have a special on [product] for the next 30 days. Visit our website at [URL]. Click the gold star. Free shipping. Thank you. Done."
- No interaction needed for the announcement-style calls

### Todd's Response on Personalization
- The system can be **customized per person** -- "We learned, okay, Mark doesn't like the small talk"
- For people who do like small talk, keep it within a tight limit
- Since these are repeat customers, the small cost of a slightly longer call is worth building relationships
- "It's all going to be personalized"

### Key Discussion: Robocalling Risk
- Todd raised the concern: if you robocall people who don't want it, you can get **blocked/spam-listed**
- Once spam-listed, even people who want to talk to you can't reach you
- Both Todd and Andy discussed this concern independently
- Need to be careful about who gets called and ensure it feels natural, not like a robocall

### Warm Transfer Feature
- Andy requested: if someone asks a question or wants help, **warm transfer them to the parts store**
- This is what Todd is working on next
- Will need testing once implemented

### Plan
- Start with **landline-only customers** (known landlines from the database)
- A/B test: announcement-only calls vs. interactive calls
- Track who was called, then check if they made a purchase
- Andy needs to approve what goes out before mass calling begins
- Todd: "I like the pain because if we can get past that pain and get to something that works, then we can do it for everybody"

### Action Items
- Todd: Build warm transfer capability to parts store number
- Todd: Test warm transfer functionality
- Todd: Investigate why Andy's call hung up when he said "no"
- Todd: Prepare test batch of calls for landline customers
- Mark/Andy: Approve final call script before broader rollout

---

## Topic 4: HubSpot Data Hygiene

### Issues Found
- David's contacts: many with no email, many with no phone number
- Some contacts only have company-level notes, not individual-level
- David may have been tracking things on a personal spreadsheet instead of in HubSpot
- A LinkedIn lead came in with a callback in August but no follow-up task was set

### Recommendations
- David should create **tasks** in HubSpot when making notes (follow-up reminders)
- Notes should be at the **individual contact level**, not just company level
- High-level notes can also go on the company record
- AI could potentially auto-create follow-up tasks for contacts with no recent activity

---

## General Themes

- **Iterate before automating**: hand-craft small batches, get feedback, then automate
- **Don't flood the system**: quality over quantity for lead imports
- **Human wrangling still needed**: AI scraping is imperfect, needs human verification for the foreseeable future
- **Think automation-first**: any repetitive manual task should be considered for AI automation
- Todd sees the same patterns across all the data sources -- the engineering effort is similar for each

---

## Next Steps (Summary)

| Who | Action | Priority |
|-----|--------|----------|
| Todd | Import small test batch of contacts into HubSpot (owned by Tabitha) | High |
| Todd | Fix multi-person contact structure for clean import | High |
| Todd | Verify sam.gov scraper quality | Medium |
| Todd | Build warm transfer for AI phone calls | Medium |
| Todd | Investigate call hang-up bug | Medium |
| Todd | Look into Apollo API integration for contact enrichment | Low |
| Mark | Review sam.gov data before import decision | Medium |
| Mark/David | Review test batch in HubSpot and provide feedback | High |
| David | Forward P4 state expansion email | High |
| David | Start using HubSpot tasks for follow-up tracking | Ongoing |

---

## Wrap-up

- Mark: "Good till next week. Give me a shout during the week if you want to talk about the phone call stuff."
- Todd: "I'll follow up with HubSpot right away -- the ones we can do -- and test that."
- David confirmed he's ready; Mark noted David was "ready to go to sleep"
- Todd: "I've got some more stuff to do but I'm pretty exhausted"
- Goodbyes at ~9:40 PM EST
