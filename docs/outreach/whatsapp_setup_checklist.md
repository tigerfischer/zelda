# WhatsApp Outreach Setup Checklist
## Do this once the new SIM arrives (~45 minutes total)

---

### Step 1 — Activate the SIM (5 min)
- Insert SIM into any phone (or dual-SIM slot)
- Recharge with any plan that includes data (Jio ₹179 / Airtel ₹179)
- Confirm you can make a call and receive an SMS

---

### Step 2 — Create the Green API account (10 min)
1. Go to **green-api.com** → Sign up (free tier is fine to start)
2. Create a new **instance** → you get an `instanceId` and `token`
3. In the instance dashboard → **QR Code** tab → scan with the new SIM's WhatsApp
4. Status should flip to **"Authorised"** (green)
5. In instance **Settings**, turn on:
   - `receiveWebhook` → off (we use polling, not webhook)
   - `incomingWebhook` → on
   - `outgoingAPIMessageWebhook` → on

Copy the two values you'll need:
```
Instance ID:  _______________
Token:        _______________
```

---

### Step 3 — Create the Telegram bot (10 min)
1. On Telegram, message **@BotFather** → `/newbot`
2. Give it a name (e.g. "Zelva Outreach") and username (e.g. `zelva_outreach_bot`)
3. Copy the **bot token** (looks like `123456789:AAF...`)
4. Open a chat with your new bot → click **Start**
5. Find your chat ID: open this URL in a browser (replace TOKEN):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Look for `"chat":{"id":XXXXXXXXX}` in the response — that's your chat ID

Copy:
```
Bot token:  _______________
Chat ID:    _______________
```

---

### Step 4 — Update `.env` (2 min)
Add these four lines to the project `.env` file:
```
TELEGRAM_BOT_TOKEN=<bot token from Step 3>
TELEGRAM_CHAT_ID=<chat ID from Step 3>
GREEN_API_INSTANCE_ID=<instance ID from Step 2>
GREEN_API_TOKEN=<token from Step 2>
```

---

### Step 5 — Install faster-whisper for call transcription (5 min)
```bash
conda activate zelda
conda install -c conda-forge faster-whisper
```
This downloads the Whisper model (~500 MB) on first use. Needed to transcribe call recordings from Drive.

---

### Step 6 — Load the outreach queue and start the bot (5 min)

```bash
conda activate zelda

# Load the generated messages into the review queue
# (replace <file> with the JSONL path printed by generate-outreach)
python -m zelda load-outreach --file data/outreach/ludhiana/messages_<run_id>.jsonl

# Start the bot — leave this running in a terminal tab
python -m zelda telegram-bot
```

The bot will immediately send the first draft to your Telegram for review.

---

### What happens next (automated)
| Event | What the bot does |
|---|---|
| Bot starts | Pushes all pending drafts to Telegram for review |
| You tap **Approve & Send** | Message sent via WhatsApp immediately |
| You tap **Edit** | Bot asks for new text, re-shows for approval |
| T+2 days after send | Bot sends personalized call brief to Telegram |
| Lead replies on WhatsApp | Bot drafts a reply, asks for your approval |
| You drop a recording in Drive | Bot transcribes it within 10 minutes, attaches to lead |

---

### Anti-ban reminders
- Send window is enforced automatically: **9am–7pm IST only**
- The bot spaces messages by default — don't try to blast a batch manually
- Keep volume under ~30 messages/day until the number has a 2-week history
- If a lead replies "stop" or "not interested" — **Skip** all future messages to that number
