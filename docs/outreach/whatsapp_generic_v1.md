# WhatsApp First Outreach Message — Generic v1

**Status:** Active template  
**Channel:** WhatsApp (cold first message)  
**Personalization:** City only (`[City]` is the sole dynamic field — we always have it)  
**Language:** English

---

## The Message

```
Hi Doctor — your clinic is likely leaving three things on the table every week:

• Google-generated new patients — the "dentist near me" searches that go to competitors with a better-optimized profile and more reviews
• Appointments lost to no-shows — because no WhatsApp reminder went out the day before
• Repeat patient visits — the 6-month recall that no one ever followed up on

Zelva handles all three automatically: we optimize your Google Business Profile, collect reviews, send appointment reminders, and bring lapsed patients back — all over WhatsApp, built specifically for Indian dental clinics.

Worth 5 minutes to see what this looks like for your clinic?
```

---

## Design Rationale

**Opening line:** "Leaving on the table" is more neutral than "losing" — it implies untapped upside, not blame. Sets up the three bullets as recoverable opportunities, not failures.

**The three bullets — explicit outcomes:**
Each bullet names the outcome the doctor wants, then explains what's currently blocking it:
- Bullet 1 = *Google-generated new patients* (explicit). The block: competitors with better GBP + more reviews. This primes both the GBP optimization and review collection value props.
- Bullet 2 = *Appointments lost to no-shows* (concrete cost: ₹500–3,000 per slot). The block: no reminder workflow. Every independent clinic knows this pain.
- Bullet 3 = *Repeat patient visits* (explicit recurring revenue). The block: no recall system. Most Indian clinics have zero automation here.

Naming the outcome first ("Google-generated new patients," "repeat patient visits") gives the doctor something to want, not just a problem to recognise.

**Solution line — what we actually do:**
Four specific capabilities listed plainly: GBP optimization, review collection, appointment reminders, patient recall. This is the "how we help" made fully explicit. "All over WhatsApp" signals channel fit. "Built specifically for Indian dental clinics" is the differentiation claim against Grexa (horizontal) and every US tool (SMS + USD-priced).

**CTA:** "Worth 5 minutes" asks them to evaluate, not commit. "Your clinic" makes it feel individual even in the generic version.

**What this deliberately avoids:**
- Clinic name or specific review counts (saves it for the AI personalization agent)
- Price (never in first contact)
- Emojis (keeps it professional for doctor-class recipient)

---

## What the AI Personalization Agent Should Tweak (Phase A.2)

The AI agent will receive this template + a `LeadEnrichment` record and produce a lead-specific version. Slots it should consider filling or rewriting:

| Signal available | Personalisation move |
|---|---|
| Low review count (< 50) | Line 1 pivot: "clinics with fewer than 50 Google reviews are losing patients to competitors every day" |
| No website | Add: "...and helps you build a digital presence without needing a website" |
| High review count but low velocity | Angle: "getting more of your happy patients to leave reviews automatically" |
| Rating 3.5–4.2 | Angle: "closing the gap between your actual quality and your online rating" |
| Practo listing exists | Add Practo trap angle: "...and helps you own the patient relationship instead of Practo owning it for you" |
| No owner response on reviews | Add: "responds to Google reviews on your behalf" |
| Clinic name known | "Hi Doctor at [ClinicName]" opener |

The agent should pick **at most two** personalisation moves per message to keep it short.

---

## Anti-Ban Guardrails (for Phase B implementation)

- One message per number, never follow up on the same thread unless they reply
- 30–90 second random delay between sends (not a blast)
- Send window: 9am–7pm IST only
- Message text should vary slightly per batch (rephrase, not reuse verbatim) — Green API terms
- Stop immediately if a number replies "stop", "remove", or similar
