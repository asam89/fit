# Enhancement Spec — Model-Agnostic Keys, Settings & AI-Guided Health Intake

**Target executor:** Devin AI
**Parent specs:** `fitness-bot-requirements.md` + `fitness-bot-enhancement-checkins-dashboard.md`
**Owner:** Alex
**Last updated:** 2026-06-22
**Queue status:** Enhancement — sequence after the dashboard enhancement.

---

## 0. TL;DR for the agent

Five changes to the OCI-hosted fitness system + its web dashboard:

1. **Bring-your-own-key, model-agnostic inference.** Each user enters their own API key for **Anthropic (Claude)**, **OpenAI (ChatGPT)**, and **Google (Gemini)**. All AI calls in the system route through one provider abstraction — nothing is hardcoded to a single model.
2. **No top tabs.** The dashboard is a single scrolling surface. Navigation is by scroll/anchors, not a tab bar.
3. **Settings area** (entered via a gear, not tabs) containing **Profile** and **Connections** (AI keys + health/device sources + Telegram), plus notifications and data controls.
4. **Everything else lives on the dashboard**, including inline **prompts to input missing health data**.
5. **AI-guided, animated health intake** — a delightful, animated flow where the AI asks for the most important missing/stale health details one at a time, plus a plain **manual data-entry** panel.

> This spec **supersedes** the hardcoded `ANALYSIS_MODEL` / `ROUTER_MODEL` references in the parent specs. After this lands, those are per-user settings resolved through the provider layer.

---

## 1. Model-agnostic, bring-your-own-key inference

### 1.1 Goal
Every AI feature (freetext router, meal-parse fallback, daily briefings, weekly/monthly analysis, goal planning, miss-introspective, AI intake) calls a **single internal interface**. The user chooses the provider + model; the user supplies the key. Swapping providers requires zero code changes at the call sites.

### 1.2 Provider abstraction
Create `inference/` with a common interface:

```python
class LLMProvider(ABC):
    def complete(self, *, system: str, messages: list, model: str,
                 max_tokens: int = 1024, json_mode: bool = False) -> str: ...
    def validate_key(self, key: str) -> bool: ...     # cheap test call
    def list_models(self) -> list[str]: ...            # selectable models
```

Implementations, each normalizing the provider's quirks:
- **AnthropicProvider** — Messages API; `system` is top-level; JSON via prompt instruction.
- **OpenAIProvider** — Chat Completions / Responses API; `system` as a system message; native JSON mode where available.
- **GeminiProvider** — `generateContent`; system instruction field differs; JSON via response schema / instruction.

A factory resolves the right provider + key + model **per user** at call time:

```python
def get_inference(user) -> Callable:
    cred = creds_for(user, user.active_provider)   # decrypts key in memory only
    provider = PROVIDERS[user.active_provider]
    return lambda **kw: provider.complete(model=user.active_model, key=cred, **kw)
```

**Refactor all existing AI call sites** (router.py, meals.py, analysis.py, the briefings layer, the goal engine, the intake flow) to call this interface. No module should import a vendor SDK directly except inside its provider implementation. Normalize errors into one `InferenceError` type so the bot/dashboard can show a consistent message.

Provider-specific handling to get right: JSON output mode, system-prompt placement, token-limit parameter names, safety/refusal handling, and streaming (optional — can be non-streaming everywhere to start).

### 1.3 Key handling & security (non-negotiable)
- Keys are **encrypted at rest** (e.g. `cryptography` Fernet / AES-GCM). The master encryption key comes from the environment / secrets store, **never** the DB and never the repo.
- Keys are **decrypted in memory only** at the moment of a call; never written to logs, never returned to the client after entry.
- The UI only ever shows a **masked hint** (e.g. `sk-…aB12`), never the full key.
- **Redact** any key-shaped string from all logs and error traces.
- On save, **validate** the key with a cheap test call and store `validated_at`; surface a clear pass/fail in the UI.
- A user may store keys for **multiple providers** and switch the active one; only the active provider+model is used for inference.

### 1.4 Graceful "no key" behavior
If the active provider has no valid key:
- The bot replies (Telegram) with a friendly "Add an API key in Settings → Connections to enable AI features," not an error stack.
- The dashboard shows an inline prompt linking straight to the Connections section.
- Non-AI features (manual logging, charts, device ingest) keep working without any key.

### 1.5 Usage visibility (optional, nice)
Since users bring their own keys, there's no central billing. Optionally track per-user token counts in `llm_analysis` so the user can see their own usage in Settings.

---

## 2. Dashboard information architecture — single surface, no tabs

One scrolling page. **No top tab bar.** A gear icon (top-right) opens Settings. Sections, top to bottom:

```
┌────────────────────────────────────────────┐
│  greeting + date                    ⚙ gear  │
├────────────────────────────────────────────┤
│  TODAY  — calories/macros · activity · sleep │   ← snapshot
├────────────────────────────────────────────┤
│  ⚠ MISSING DATA prompts (inline cards)       │   ← §3
│     "No weight in 3 days — add it"           │
├────────────────────────────────────────────┤
│  ✦ AI GUIDED INTAKE  (animated entry point)  │   ← §4
├────────────────────────────────────────────┤
│  + LOG DATA  (manual quick-entry)            │   ← §5
├────────────────────────────────────────────┤
│  TRENDS  week·month·quarter·year·all-time    │   ← from dashboard enh.
│  + consistency heatmap                       │
├────────────────────────────────────────────┤
│  GOALS  (the fit.io goal engine)             │   ← from fit.io prototype
└────────────────────────────────────────────┘
```

- Movement between sections is scroll + optional sticky anchor pills (not tabs).
- The visual language follows the existing fit.io prototype: cool paper canvas, Space Grotesk display, single cobalt accent, green for wins, amber for the debrief/attention states. Keep it clean and uncluttered.

---

## 3. Missing-data prompts on the dashboard

- A **prompt strip** near the top renders one card per genuine gap, reusing the trigger logic and `data_requests` table from the check-ins enhancement (no meals today, stale weight, missing sleep sync, undescribed workout, low activity, etc.).
- Each card is one-tap actionable: it either opens the relevant quick-entry (e.g. a weight field) or launches the AI guided intake focused on that gap.
- Cards disappear the moment the data arrives. If there are no gaps, the strip collapses to a quiet "All caught up" line.
- These dashboard prompts and the Telegram nudges share the same gap engine so they never contradict each other.

---

## 4. AI-guided, animated health intake

The signature new experience: make entering important/missing health details feel effortless and a little delightful.

### 4.1 Behavior
- The active AI provider determines **which important details are missing or stale** and produces a **short, prioritized question sequence** (e.g. last night's sleep, this morning's weight, resting HR if no device sync, today's mood/energy, any workout).
- Presented as a **card-by-card flow** — one question per card — that **adapts**: the next question can change based on the previous answer.
- Each answer writes straight to the DB (and clears the matching `data_requests` / prompt card) before advancing.
- Launchable manually ("Catch me up") or from a missing-data card. Always skippable per question and exitable at any point; partial progress is saved.

### 4.2 Motion & input design
- Smooth, tasteful **animated transitions** between cards (slide/fade), a slim progress indicator, and a satisfying confirmation micro-interaction on each save. Animations serve flow, not decoration.
- **Fast input affordances** per data type: tap-chips ("Slept: Great / OK / Poor"), a number stepper/slider for weight, quick presets, and optional **voice entry** (reuse the existing Whisper path) so a value can be spoken.
- **Accessibility floor:** full keyboard operation, visible focus, and `prefers-reduced-motion` honored (transitions reduce to instant). Performant on mobile.

### 4.3 Implementation notes
- Build as a self-contained component fed by an `/api/intake/next` endpoint that asks the provider for the next question(s) given current data, and `/api/intake/answer` that persists and returns the following step.
- The question-generation prompt must return structured JSON (question text, data field, input type, options/units) so the UI can render the right control deterministically. Validate and fall back to a fixed question set if the AI response is malformed, so intake never dead-ends.

---

## 5. Manual data entry

A plain **"Log data"** panel for when the user just wants to type a value:
- Quick-add for: **weight**, **sleep** (duration/quality), **resting HR / HRV / BP**, **workout** (type/duration), **meal** (routes through the existing food parser), **mood/energy**, **hydration**.
- Correct **units per field** (respect the user's units preference), sensible validation, timestamp defaults to now but is editable.
- A free-text / voice quick-log box that runs through the existing freetext router so "weight 181, slept 7 hours" logs both in one go.
- Everything written here updates the same tables and instantly refreshes the Today snapshot and any open prompt cards.

---

## 6. Settings (gear → not tabs)

Entered from the gear icon; rendered as a slide-over panel or a dedicated `/settings` route with a simple left rail of sections (still not top tabs). Sections:

**Profile**
- Name, sex, height, birthdate, timezone, units preference, activity baseline.
- Goal & targets (cut/bulk/maintain, target weight, calorie/macro targets) — shared with the goal engine.

**Connections**
- **AI providers:** add / validate / remove keys for **Anthropic**, **OpenAI**, **Google**; pick the **active provider + model** from `list_models()`. Show masked hints and `validated_at`. Inline "test key" button.
- **Health sources:** the Health Auto Export webhook token (view / regenerate), Oura / Garmin / etc. connectors.
- **Telegram:** linked account status.

**Notifications**
- Briefing times, quiet hours, nudge categories on/off (reuse the check-ins enhancement settings).

**Data**
- Export (CSV/JSON), and delete-my-data.

---

## 7. Schema additions

- **`llm_credentials`** — id, user_id, provider (`anthropic`/`openai`/`google`), encrypted_key, key_hint, model, is_active, validated_at, created_at. (Unique on user_id+provider.)
- Extend **`users`** with any missing profile fields (sex, height, birthdate, units_pref, activity_baseline) and `active_provider`, `active_model`.
- **`intake_sessions`** (optional) — id, user_id, started_at, completed_at, questions_asked, answers_captured — for auditing/Improving the intake flow.
- Reuse `data_requests` and `llm_analysis` from the parent specs.

---

## 8. Acceptance criteria (definition of done)

- All AI features run through the provider abstraction; **no vendor SDK is imported outside its provider class**, and switching active provider/model in Settings changes behavior with no code edit.
- A user can add, **validate**, mask, switch, and remove keys for Claude, ChatGPT, and Gemini. Keys are **encrypted at rest**, decrypted only in memory, and **never appear in logs or the client** after entry.
- With no valid key, the system degrades gracefully (friendly prompts, non-AI features still work) — it never throws a raw error at the user.
- The dashboard is a **single scrolling surface with no top tabs**; Settings opens from the gear.
- Missing-data prompt cards appear only for real gaps, are one-tap actionable, and vanish when the data lands — consistent with the Telegram nudges.
- The **AI-guided intake** runs an adaptive, animated, card-by-card flow with fast inputs (chips/stepper/voice), saves each answer immediately, is fully skippable/exitable, and honors `prefers-reduced-motion` and keyboard nav. It never dead-ends on a bad AI response (fixed fallback set).
- Manual **Log data** entry works for every metric with correct units/validation and instantly refreshes the dashboard.
- Settings → **Profile** and **Connections** persist and drive the rest of the app.

---

## 9. Suggested build order

1. **Provider abstraction + `llm_credentials` + encryption** — refactor all existing AI call sites onto it. (Foundational; do first.)
2. **Settings → Connections** (key add/validate/switch) + **Profile**, with the graceful no-key path.
3. **Dashboard re-IA** to the single-surface layout (remove any tabs), wire the gear → Settings.
4. **Missing-data prompt strip** on the dashboard (reuse the gap engine).
5. **Manual Log-data panel.**
6. **AI-guided animated intake** (the polish layer — endpoints, adaptive prompt, animated component, voice input, fallbacks).

Ship 1–2 before anything else; the rest of the system depends on the inference layer being solid and provider-agnostic.

---

## 10. Open questions for me (Alex)

1. Default model per provider out of the box (e.g. Claude Sonnet / GPT-class / Gemini Pro) — or leave it blank until the user picks?
2. Single active provider at a time, or allow **per-feature** provider choice (e.g. cheap model for routing, stronger model for weekly analysis)?
3. Voice input inside the AI intake — include from day one, or Phase 2 of this enhancement?
4. Settings as a **slide-over panel** or a dedicated **/settings route**? (Both avoid tabs; just a layout preference.)
5. For BYO keys, do you want the optional **per-user token-usage** view in Settings, or skip it?