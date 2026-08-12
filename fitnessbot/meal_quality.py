"""Meal quality scoring — powers the coach's natural nudge on indulgent meals.

Produces a short, factual signal (flags + positives) that gets fed into the
shared persona prompt so the coach can react like a human: call out a greasy
meal with personality, nudge toward movement, and give genuine credit when the
meal was actually good. Never used to shame — see COACH_PERSONA boundaries.
"""

import random

from fitnessbot.health_benefits import calc_calories_burned

# Name keywords -> short flag phrase the coach can riff on
_KEYWORD_FLAGS = [
    (("deep fried", "deep-fried", "fried", "tempura", "katsu", "batter"), "deep-fried"),
    (("mcdonald", "burger king", "wendy", "taco bell", "kfc", "popeye", "big mac",
      "whopper", "mcnugget", "fast food", "drive thru", "drive-thru", "combo meal"), "fast food"),
    (("soda", "coke", "pepsi", "sprite", "mountain dew", "energy drink", "slurpee",
      "frappuccino", "sweet tea", "lemonade"), "liquid sugar"),
    (("candy", "chocolate bar", "donut", "doughnut", "cake", "cookie", "brownie",
      "ice cream", "milkshake", "gummy", "pastry", "cinnamon roll", "pie"), "dessert"),
    (("pizza", "burger", "cheeseburger", "fries", "poutine", "wings", "nachos",
      "hot dog", "onion rings", "mozzarella stick", "chips"), "greasy takeout"),
    (("beer", "wine", "vodka", "whiskey", "rum", "tequila", "cocktail", "margarita",
      "hard seltzer"), "alcohol"),
]

_POSITIVE_KEYWORDS = [
    (("chicken", "turkey", "salmon", "tuna", "cod", "shrimp", "steak", "lean beef",
      "eggs", "tofu", "tempeh", "lentil", "beans", "greek yogurt", "cottage cheese"), "solid protein source"),
    (("salad", "broccoli", "spinach", "kale", "asparagus", "greens", "vegetable",
      "veggies", "peppers", "zucchini", "cauliflower", "brussels"), "vegetables on the plate"),
    (("quinoa", "oats", "oatmeal", "brown rice", "sweet potato", "whole grain",
      "whole wheat", "barley", "farro"), "quality carbs"),
    (("berries", "apple", "banana", "orange", "avocado", "nuts", "almond", "walnut"), "whole-food sides"),
]

# Thresholds for macro-based flags
SUGAR_HEAVY_G = 40
SODIUM_HEAVY_MG = 1200
BIG_CALORIE_HIT = 800
VERY_BIG_CALORIE_HIT = 1000
LOW_PROTEIN_G = 20
FAT_HEAVY_G = 40
LOW_FIBER_G = 3

INDULGENT_SCORE = 60
SOLID_SCORE = 75


def _sum(items: list[dict], key: str) -> float:
    return sum(float(i.get(key) or 0) for i in items)


def score_meal(items: list[dict]) -> dict:
    """Score a logged meal 0-100 and describe why.

    Returns dict with: score, label ("solid"|"mixed"|"indulgent"),
    is_indulgent, flags (what's off), positives (what's good).
    """
    if not items:
        return {"score": 70, "label": "mixed", "is_indulgent": False,
                "flags": [], "positives": []}

    names = " ".join(str(i.get("name") or "") for i in items).lower()
    calories = _sum(items, "calories")
    protein = _sum(items, "protein")
    fat = _sum(items, "fat")
    fiber = _sum(items, "fiber")
    sugar = _sum(items, "sugar")
    sodium = _sum(items, "sodium")

    # Start at a neutral baseline, not perfect — a plate earns "solid" by
    # having something good on it, not merely by avoiding red flags.
    score = 80
    flags: list[str] = []

    for keywords, label in _KEYWORD_FLAGS:
        if any(kw in names for kw in keywords):
            flags.append(label)
            score -= 22

    if sugar >= SUGAR_HEAVY_G:
        flags.append(f"{sugar:.0f}g sugar")
        score -= 18
    if sodium >= SODIUM_HEAVY_MG:
        flags.append(f"{sodium:.0f}mg sodium")
        score -= 12
    if calories >= BIG_CALORIE_HIT and protein < LOW_PROTEIN_G:
        flags.append(f"{calories:.0f} cal for only {protein:.0f}g protein")
        score -= 22
    if calories >= VERY_BIG_CALORIE_HIT:
        score -= 10
    if fat >= FAT_HEAVY_G and fiber < LOW_FIBER_G:
        flags.append("fat-heavy with almost no fiber")
        score -= 14

    positives: list[str] = []
    for keywords, label in _POSITIVE_KEYWORDS:
        if any(kw in names for kw in keywords):
            positives.append(label)
            score += 5
    if protein >= 30:
        positives.append(f"{protein:.0f}g protein")
        score += 5
    if fiber >= 8:
        positives.append(f"{fiber:.0f}g fiber")
        score += 5

    score = max(0, min(100, score))
    # "solid" requires something actually good on the plate, not just the
    # absence of red flags — otherwise a plain bagel would earn praise.
    if score >= SOLID_SCORE and positives:
        label = "solid"
    elif score >= INDULGENT_SCORE:
        label = "mixed"
    else:
        label = "indulgent"

    return {
        "score": score,
        "label": label,
        "is_indulgent": score < INDULGENT_SCORE,
        "flags": flags,
        "positives": positives,
    }


# Realistic single bouts. Deliberately NOT derived from the meal's calories —
# "you owe 2 hours of cardio for that burger" is punishment framing and
# produces absurd numbers. These are just achievable ways to move today.
_MOVEMENT_BOUTS = (
    ("brisk walk", 3.5, 20),
    ("brisk walk", 3.5, 30),
    ("easy bike", 6.0, 20),
    ("jog", 8.0, 20),
    ("bodyweight circuit", 5.0, 15),
    ("walk outside", 3.5, 15),
)


def movement_options(weight_kg: float = 80.0, count: int = 3) -> list[str]:
    """Suggest realistic, achievable movement bouts with their approximate burn.

    Framed as an invitation to move, never as punishment for eating.
    """
    bouts = list(_MOVEMENT_BOUTS)
    random.shuffle(bouts)
    options = []
    for label, met, minutes in bouts[:count]:
        cal = calc_calories_burned(met, weight_kg, minutes)
        options.append(f"~{minutes} min {label} (~{cal} cal)")
    return options


def build_meal_quality_signal(items: list[dict], *, weight_kg: float = 80.0,
                              worked_out_today: bool = False) -> str:
    """Compact, factual line describing meal quality for the coaching prompt."""
    q = score_meal(items)
    parts = [f"MEAL QUALITY: {q['label']} ({q['score']}/100)"]
    if q["flags"]:
        parts.append("what's off: " + ", ".join(q["flags"]))
    if q["positives"]:
        parts.append("what's good: " + ", ".join(q["positives"]))

    if q["is_indulgent"] and not worked_out_today:
        opts = movement_options(weight_kg, count=2)
        if opts:
            parts.append("no workout logged today; suggest getting moving, e.g. " + " or ".join(opts))
        else:
            parts.append("no workout logged today")
    return " | ".join(parts)


# Rotating phrasings for the no-LLM fallback path, so even the offline reply
# has personality instead of one fixed sentence.
_INDULGENT_NUDGES = (
    "Not the cleanest plate you've logged — no drama, just make the next one count.",
    "That one was for the soul. Let's balance it out with the next meal.",
    "Honest read: that's a heavy one. Doesn't undo anything — just steer the next meal.",
    "Fair enough, we all do it. Next meal, lead with protein and something green.",
    "That's a treat, not a habit — keep it that way and you're fine.",
)

_SOLID_PRAISE = (
    "That's a well-built plate — keep stacking those.",
    "Good one. That's the kind of meal that actually moves the needle.",
    "Nice — real food, real protein. More of that.",
    "Solid choice. Your future self says thanks.",
)

_MOVE_NUDGES = (
    "Get up and move: {opt}.",
    "Nothing logged today — go grab {opt} and shake it off.",
    "Best move right now: {opt}. Just get out the door.",
    "Put {opt} on the board today and you're square.",
)


def fallback_nudge(items: list[dict], *, weight_kg: float = 80.0,
                   worked_out_today: bool = False) -> str:
    """Short, varied nudge used when the LLM is unavailable."""
    q = score_meal(items)
    if q["label"] == "solid":
        return random.choice(_SOLID_PRAISE)
    if not q["is_indulgent"]:
        return ""

    line = random.choice(_INDULGENT_NUDGES)
    if not worked_out_today:
        opts = movement_options(weight_kg, count=1)
        if opts:
            line += " " + random.choice(_MOVE_NUDGES).format(opt=opts[0])
    return line
