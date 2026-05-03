# WhatsApp First Outreach Message — Generic v1

**Status:** Active template  
**Channel:** WhatsApp (cold first message)  
**Personalization:** City only (`[City]` is the sole dynamic field — we always have it)  
**Language:** English

---

## The Message

```
Hi Doctor — your clinic is likely leaving three things on the table every week:

• New patients from Google — every "dentist near me" search goes to the clinic with the strongest Google Business Profile and the most recent reviews; we optimize both
• Appointment slots lost to no-shows — because no reminder went out 24 hours before
• Repeat visits from past patients — most clinics have hundreds of patients who haven't returned in 6+ months and have never been followed up on

Zelva handles all three automatically: Google profile optimization, review collection, appointment reminders, and patient recall — all over WhatsApp, built for Indian dental clinics.

Worth 5 minutes to see what this looks like for your clinic?
```

---

## Design Rationale

**Opening line:** "Leaving on the table" is more neutral than "losing" — it implies untapped upside, not blame. Sets up the three bullets as recoverable opportunities, not failures.

**The three bullets — explicit outcomes:**
Each bullet names the desired outcome, then names the gap preventing it:
- Bullet 1 = *New patients from Google* (the doctor wants more patients; Google is the main free acquisition channel). The gap: competitors with better GBP + more reviews outrank them. "We optimize both" closes the loop on the fix right inside the problem statement — so GBP optimization isn't buried in the solution line.
- Bullet 2 = *Appointment slots lost to no-shows* (concrete cost: ₹500–3,000 per slot gone dark). The gap: no reminder workflow. Every independent clinic knows this pain viscerally.
- Bullet 3 = *Repeat visits from past patients* (explicit recurring revenue opportunity). "Hundreds of patients who haven't returned in 6+ months" makes the size of the opportunity tangible — it's not a theoretical benefit, it's a specific cohort sitting in their own patient register. Most Indian clinics have zero recall automation.

Naming the outcome first ("new patients from Google," "repeat visits") gives the doctor something to want, not just a problem to recognise.

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
