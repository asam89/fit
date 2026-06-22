# Enhancement Spec — Telegram Conversational Engine

**Target executor:** Devin AI
**Parent specs:** `fitness-bot-requirements.md` (expands the freetext router), `byok-settings-intake` (provider layer), `nutrition-targets` (targets/focus)
**Owner:** Alex
**Last updated:** 2026-06-22
**Queue status:** Core enhancement — build right after spec 3 (needs the model-agnostic provider layer). This is the primary interaction surface; treat it as high priority.

---

## 0. TL;DR for the agent

Make Telegram feel like talking to a sharp coach who never forgets. **Every** inbound message — text or voice — runs through one loop: **understand → act → respond.** The bot figures out what the message means, writes the right thing to the database, and replies with a short, thoughtful, context-aware response. After a meal it tells me what to focus on next (diet + training). I can also add or change health data and profile details just by saying or typing them. No message ever gets a dead-end "ok."

---

## 1. The universal message loop

Every message (after voice→text if needed) goes through the same four stages:

```
 inbound (text | voice)
        │
   ┌────▼────┐  voice → Whisper (provider-agnostic) → text
   │ INGEST  │
   └────┬────┘
        │
   ┌────▼────────┐  classify intent(s) + extract structured fields
   │ UNDERSTAND  │  (one LLM call, JSON out; deterministic fast-paths first)
   └────┬────────┘
        │
   ┌────▼────┐  write to the right table(s); normalize units; dedupe
   │  ACT    │  (meals, vitals, sleep, body_comp, profile, goals, answers)
   └────┬────┘
        │
   ┌────▼────────┐  assemble context (targets + today's totals + trend +
   │  RESPOND    │  what was just logged) → curated coaching reply
   └─────────────┘
```

The response is **always grounded in what was actually written to the DB** — it confirms the data landed, then adds one practical focus point. Never reply with content that wasn't persisted; never persist without confirming back.

---

## 2. UNDERSTAND — the NLU layer

### 2.1 Intent taxonomy
A message may contain **multiple** intents. Classify into any of:
- `meal_log` — food eaten ("two eggs and toast")
- `health_metric` — weight, sleep, resting HR/HRV, BP, SpO2, body-fat %, mood, energy, hydration
- `workout_log` — activity done ("did legs, 45 min")
- `profile_update` — age, height, sex, units, activity level
- `goal_update` — goal type, target weight, target date ("switch me to maintain")
- `query` — "how am I doing today / this week", "how much protein left?"
- `pending_answer` — a reply to a question the bot asked (check `data_requests` **first**)
- `correction` — fixing a prior entry ("actually that was 2 eggs")
- `general` — fitness/diet question or chit-chat with no data to store

### 2.2 Extraction
One structured call (through the provider layer) returns JSON: a list of intents, each with extracted fields, units as stated, a timestamp hint ("this morning", "last night"), and a confidence. Example shape:

```json
{ "intents": [
  { "type":"meal_log", "confidence":0.96,
    "items":[{"name":"eggs","qty":2,"unit":"whole"},{"name":"toast","qty":1,"unit":"slice"}],
    "meal_type":"breakfast", "when":"now" },
  { "type":"health_metric", "confidence":0.91,
    "metric":"sleep", "value":7, "unit":"hours", "when":"last_night" }
]}
```

### 2.3 Fast-paths (save tokens/latency)
Before the LLM call, try deterministic matches: a bare number while a weight question is pending → `pending_answer`; `weight 181`, `slept 7h`, `rhr 54` patterns → direct metric. Fall through to the LLM for anything non-trivial.

### 2.4 Confidence & disambiguation
- High confidence → act and confirm.
- Low confidence or genuine ambiguity → **ask one short clarifying question** instead of guessing wrong (e.g. "Was that 181 lb or your resting HR?"). Queue it in `data_requests` so the reply gets matched.
- Never silently misfile. A wrong write that looks confirmed is worse than a quick question.

---

## 3. ACT — writing to the database

- Map each extracted intent to its table(s): `meals`+`meal_items` (resolve foods via the meal pipeline), `vitals`, `sleep`, `body_composition`/`weight_trend`, `exercise`, `users`, `goals`, or resolve a `data_requests` row.
- **Normalize units** at write time (lb↔kg, h↔min, °F↔°C) to canonical DB units; respect the user's stated unit.
- **Timestamps:** default now; honor "last night" / "this morning" hints.
- **Idempotency / dedupe:** don't double-log if a device already synced the same metric for that window; reconcile instead.
- **Corrections & undo:** "actually that was 2 eggs" edits the last matching entry; `/undo` removes the last write. Always confirm the corrected values.
- **Profile / goal changes are higher-stakes** (they move targets): apply, then state the change *and* its effect ("Goal → maintain. Daily target moves 2,200 → 2,640 kcal."), with easy undo.

---

## 4. RESPOND — the curated coaching reply

### 4.1 Context assembly (deterministic, before the LLM call)
Build a compact digest: the user's current targets (calories + macros), today's running totals and what's left, the weight trend, the item(s) just logged, time of day, and any open gaps. **Feed the digest, not raw rows** — keeps it cheap and accurate.

### 4.2 Response contract
Every reply = **confirmation + one practical focus point**, tight (2–4 short lines). Voice and tone follow the house style: plain, specific, honest, lightly motivating, never a robotic dump, never alarmist, no medical claims. The **numbers are deterministic** (computed in code and passed in); the LLM writes the *guidance*, not the figures, so totals can't be hallucinated.

### 4.3 Meal-specific focus (a headline requirement)
After a `meal_log`, the focus point is **diet + training aware**: where the day now stands and what to prioritize next. It should reference real remaining macros and the goal — e.g. protein still needed, carb timing around training, an easy swap, or "you've got room." Not generic praise.

### 4.4 Fallbacks
If the LLM call fails or the user has no key, still send a deterministic confirmation ("Logged: 2 eggs, toast — 320 kcal, 22g protein. 95/165g protein today.") so the loop never dead-ends.

---

## 5. Adding health data & profile from chat (text or voice)

Anything in §2.1's `health_metric`, `workout_log`, `profile_update`, `goal_update` can be set conversationally:
- "weight 181 this morning", "slept 6 and a half hours", "resting heart rate 54", "bodyfat 18%", "drank 2 liters", "feeling wiped today"
- "I'm 32 now", "I'm 180 cm", "I train 5 days a week now", "switch my goal to lean bulk", "target 175 lb by September"

Each updates the profile/metric tables and confirms back with the effect where relevant. Multi-data messages ("weight 181, slept 7, did legs") write **all** parts in one turn and confirm each. These share the same write layer as the AI-guided intake (spec 3) — one code path, two entry points.

---

## 6. Voice specifics

- Voice → Whisper (behind the provider-agnostic transcription interface) → identical loop as text.
- If transcription is low-confidence or empty, ask the user to resend rather than logging garbage.
- Optionally echo the transcript on first use so the user trusts what was heard ("Heard: 'two eggs and toast' ✓").

---

## 7. Conversation state & light memory

- **Pending questions:** always match an incoming message against open `data_requests` before other intents.
- **Short context window:** keep the last few turns available so follow-ups resolve ("and a coffee" right after a meal appends to it; "no, the other one" corrects).
- **Today's state** is always in the digest so the bot speaks with continuity ("that's your 3rd meal today").
- No long-term chat transcript storage beyond what's needed; the DB is the memory.

---

## 8. Cross-cutting (provider, cost, transparency)

- **Provider-agnostic:** all understanding + response calls go through the spec-3 inference layer using the user's active model. This fires on every message, so be **token-efficient**: fast-paths, digested context, and cache the targets.
- **BYO-key reality:** every message costs the user tokens on their own key — keep calls lean; one understand call + one respond call per message max (combine when feasible).
- **Transparency & trust:** always echo what was understood and stored; make correction/undo trivial. A confident wrong log erodes trust fastest.
- **Safety:** no medical diagnosis or advice; on distress or disordered-eating signals, drop the coaching tone and respond with care.

---

## 9. Schema touchpoints

- Reuse `meals`/`meal_items`, `vitals`, `sleep`, `body_composition`, `weight_trend`, `exercise`, `users`, `goals`, `data_requests`, `nutrition_targets`, `llm_analysis`.
- Optional **`message_log`** — id, user_id, received_at, channel (text/voice), transcript, detected_intents (JSON), writes (JSON of table+id), response_text, model, tokens. Gives an audit trail and a dataset to improve the NLU later.

---

## 10. Worked examples (input → understanding → writes → reply)

> Use these as behavioral test cases. Numbers illustrative; targets = 2,200 kcal / 165g protein.

**A. Meal (text)** — "had a chicken burrito bowl for lunch"
→ `meal_log`; resolve via food pipeline; write meal+items.
→ *"Logged lunch — chicken burrito bowl, ~680 kcal, 46g protein. That's 1,180 kcal and 95g protein on the day. You've got ~70g protein left for one more solid meal — aim dinner at lean protein + veg and you'll land it clean."*

**B. Meal (voice)** — 🎤 "two scrambled eggs, oatmeal, and a black coffee"
→ transcribe → `meal_log`.
→ *"Heard: 2 eggs, oatmeal, black coffee ✓ — 320 kcal, 22g protein. Good protein-forward start. Front-load a bit more protein at lunch to stay ahead of your 165g target."*

**C. Multi-data** — "weight 181 this morning, slept about 7 hours, did legs for 45 min"
→ three intents → `body_composition`/`weight_trend`, `sleep`, `exercise`.
→ *"Got all three: weight 181.0 lb (trend ▼0.3 this week), sleep 7h, legs 45m. Solid recovery + training day — make sure dinner has enough carbs to refuel after that session."*

**D. Profile change** — "I train 5 days a week now"
→ `profile_update` (activity → active).
→ *"Updated activity to 5×/week. That bumps your maintenance estimate, so your daily target moves 2,200 → 2,340 kcal. Protein target holds at 165g."*

**E. Query** — "how am I doing today?"
→ `query`; no write.
→ *"1,180 / 2,200 kcal, 95 / 165g protein, fiber 14 / 31g. You're light on protein and fiber with the evening to go — a protein + veg-heavy dinner fixes both."*

**F. Correction** — "actually that burrito was a double protein"
→ `correction` on last meal.
→ *"Updated — bumped that bowl to 62g protein. Day's now 111g. Only ~54g to go, you're in good shape."*

**G. Ambiguous** — "181"
→ if weight question pending → log weight; else low confidence →
→ *"Quick check — is 181 your weight (lb) or something else?"*

**H. Answer to pending** — bot earlier asked "How'd you sleep?" → user: "rough, maybe 5 hours"
→ `pending_answer` → write sleep, resolve request.
→ *"Logged 5h, noted as rough. Go easier on training intensity today and prioritize protein — short sleep makes recovery the priority."*

---

## 11. Acceptance criteria (definition of done)

- **Every** text and voice message produces a grounded, curated reply — confirmation + one practical focus point — and never a dead-end.
- Voice and text run the identical understand→act→respond loop.
- Multi-intent messages write **all** parts in one turn and confirm each.
- Meal logs yield **diet + training-aware** guidance referencing real remaining macros and goal, not generic praise.
- Health metrics, workouts, profile, and goals can all be set by chat/voice and confirm their effect (targets shift shown for profile/goal changes).
- Pending questions match before other intents; corrections and `/undo` work and confirm.
- Ambiguous/low-confidence input triggers a single clarifying question, never a silent miswrite.
- All AI calls run through the provider layer on the user's key; pipeline is token-lean (≤2 LLM calls/message); deterministic fallback fires with no key or on API failure.
- Numbers in replies are computed in code, never generated by the model.

---

## 12. Build order

1. Loop skeleton + `message_log` + deterministic confirmation responses (no coaching yet).
2. UNDERSTAND layer: intent + extraction JSON via provider, with fast-paths and confidence/disambiguation.
3. ACT layer: writes/normalization/dedupe for every intent type + corrections/undo.
4. RESPOND layer: context digest + coaching reply + meal-specific focus + fallbacks.
5. Voice path + transcript echo; multi-intent handling; pending-answer matching.
6. Tune tone against the §10 examples; add safety redirects.

---

## 13. Open questions for me (Alex)

1. One combined message per reply, or a fast "✓ logged" immediately + the coaching line a beat later?
2. Confirm profile/goal changes before applying (since they move targets), or apply-then-show-with-undo?
3. How chatty by default — always a focus point, or stay terse on routine logs and save guidance for meals + end-of-day?
4. Echo the voice transcript every time, or only when confidence is low?
5. Keep the optional `message_log` (full audit + future NLU training data), or store nothing beyond the structured data for privacy?