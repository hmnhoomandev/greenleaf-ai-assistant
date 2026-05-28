"""
GreenLeaf Logistics - Expense Validation Tool
=============================================
Reliability-first version with targeted semantic safeguards.

Design priorities:
- Never auto-approve a suspicious beverage if alcohol cannot be ruled out safely
- Deterministic rules first
- AI used only as a backup for ambiguous drink-like cases
- Fail safe: ambiguous or unverified cases go to manual review
- Support both dot and comma decimals (e.g. 34.99 CHF, 34,99 CHF)
- Reject clearly invalid business contexts (e.g. "previous company", "client of my wife")
- Support explicit non-alcoholic drink variants such as:
  - 0% beer
  - non-alcoholic gin
  - alcohol-free cocktail
  - virgin mojito
- Normalize Unicode punctuation to avoid matching bugs (e.g. didn’t vs didn't)
- In strict mode, vague lunch claims with a client do NOT get auto-approved:
  if alcohol status is not explicitly ruled out, the tool returns manual review

Handbook rules implemented:
- Client lunches are reimbursable only if at least one external client is present
- Maximum 35 CHF per person
- Alcohol is strictly non-reimbursable
- Receipts must be submitted via the ScanPro app

Integration contract:
- Called by brain.py
- Input: text (str) — full user message
- Output: dict with "answer" and "source"
"""

import os
import re
from functools import lru_cache
from difflib import get_close_matches

from google import genai
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# INITIALIZATION
# ─────────────────────────────────────────────

load_dotenv()

client = genai.Client(
    api_key=os.environ.get("GEMINI_API_KEY"),
    http_options={"api_version": "v1"}
)

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

SOURCE_LABEL = "GreenLeaf Handbook — Expense Policy"
MAX_AMOUNT_PER_PERSON = 35.0

# Supports:
# - 34.99 CHF
# - 34,99 CHF
# - 30chf
AMOUNT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:chf|francs?|fr\.?)",
    re.IGNORECASE
)

# Known alcohol words / brands / cocktails.
# Deterministic list = fast and reliable first-pass detection.
ALCOHOL_KEYWORDS = [
    "alcohol",
    "beer",
    "wine",
    "whisky",
    "whiskey",
    "wiskey",
    "vodka",
    "rum",
    "gin",
    "tequila",
    "champagne",
    "cider",
    "cocktail",
    "liquor",
    "spirits",
    "brandy",
    "bourbon",
    "scotch",
    "lager",
    "ale",
    "prosecco",
    "aperol",
    "campari",
    "martini",
    "mojito",
    "pina colada",
    "piña colada",
    "bloody mary",
    "moscow mule",
    "margarita",
    "negroni",
    "old fashioned",
    "cosmopolitan",
    "daiquiri",
    "spritz",
    "gin tonic",
    "gin and tonic",
    "cuba libre",
    "b52",
    "b-52",
    "green mexican",
    "green mexicain",
    "jack daniels",
    "jack daniel's",
    "jim beam",
    "johnnie walker",
    "johnny walker",
    "absolut",
    "smirnoff",
    "bacardi",
    "hennessy",
    "heineken",
    "corona",
    "guinness",
    "carlsberg",
    "jagermeister",
    "jägermeister",
    "moet",
    "moët",
    "chivas",
    "chivas regal",
    "monkey shoulder",
    "obolon",
    "rosé",
    "rose wine",
    "grolsch",
]

# Generic phrases that explicitly mean "no alcohol".
# These are useful for phrases like:
# - we didn't drink alcohol
# - no alcohol
# - without alcohol
GENERIC_NON_ALCOHOLIC_PATTERNS = [
    "didnt drink alcohol",
    "didn't drink alcohol",
    "did not drink alcohol",
    "we didnt drink alcohol",
    "we didn't drink alcohol",
    "we did not drink alcohol",
    "no alcohol",
    "without alcohol",
]

# Markers that explicitly describe a drink as non-alcoholic.
# These should apply to the specific nearby drink phrase.
NON_ALCOHOLIC_DRINK_MARKERS = [
    "non-alcoholic",
    "non alcoholic",
    "alcohol-free",
    "alcohol free",
    "virgin",
    "0%",
    "0.0%",
]

# Valid external-party indicators.
CLIENT_KEYWORDS = [
    "client",
    "customer",
    "guest",
    "prospect",
    "business partner",
    "external client",
]

# Policy-style questions.
POLICY_KEYWORDS = [
    "policy",
    "rule",
    "rules",
    "limit",
    "maximum",
    "how do i submit",
    "how can i submit",
    "how to submit",
    "submit receipt",
    "submit receipts",
    "receipt submission",
    "receipt",
    "receipts",
    "scanpro",
    "what app",
    "which app",
    "can i expense alcohol",
    "what can be expensed",
]

# Clearly safe meal words.
SAFE_MEAL_WORDS = {
    "lunch",
    "dinner",
    "breakfast",
    "meal",
    "sandwich",
    "salad",
    "pizza",
    "pasta",
    "burger",
    "snack",
    "soup",
    "rice",
    "steak",
    "fish",
    "chicken",
    "dessert",
}

# General drink-like wording that can justify AI fallback.
SUSPICIOUS_DRINK_WORDS = [
    "drink",
    "drank",
    "bar",
    "pub",
    "bottle",
    "glass",
    "shot",
    "cocktail",
    "aperitif",
    "digestif",
]

# Contexts that clearly suggest the expense is not for the current company.
INVALID_COMPANY_CONTEXT_PATTERNS = [
    "previous company",
    "former employer",
    "past job",
    "old company",
    "last company",
    "previous employer",
]

# Contexts that mention a "client" but not in a valid GreenLeaf business sense.
INVALID_CLIENT_CONTEXT_PATTERNS = [
    "client of my wife",
    "client of my husband",
    "my wife's client",
    "my husbands client",
    "my husband's client",
    "client of my friend",
    "my friend's client",
    "friends client",
    "friend's client",
    "client of my previous company",
    "former company client",
    "client of another company",
    "a client of another company",
    "another company client",
    "client from another company",
]

# Signals that the user is describing a concrete meal/claim context.
# Used to avoid auto-approval when alcohol status is unknown.
MEAL_CONTEXT_KEYWORDS = [
    "lunch",
    "dinner",
    "breakfast",
    "meal",
    "restaurant",
    "bill",
    "receipt",
]

# ─────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """
    Normalize common Unicode punctuation so matching logic is stable.

    Example:
    - didn’t -> didn't
    """
    return (
        text.replace("’", "'")
            .replace("“", '"')
            .replace("”", '"')
            .replace("–", "-")
            .replace("—", "-")
    )


def extract_amount(text: str):
    """
    Extract CHF-like amount from text.

    Returns:
        float if found, otherwise None
    """
    match = AMOUNT_RE.search(text)
    if not match:
        return None

    amount_str = match.group(1).replace(",", ".")
    return float(amount_str)


def looks_like_policy_question(text: str) -> bool:
    """
    Detect whether the user is asking about policy/process
    instead of validating a concrete expense case.

    If the message contains a concrete expense case
    (amount / client / reimbursement intent), do not treat it
    as a pure policy question even if words like "rule" appear.
    """
    text_lower = text.lower()

    has_policy_keyword = any(keyword in text_lower for keyword in POLICY_KEYWORDS)
    has_amount = extract_amount(text) is not None
    has_client = has_client_context(text)
    has_reimbursement_intent = any(
        phrase in text_lower
        for phrase in ["reimburse", "expense", "claim", "can i reimburse", "can i expense", "approve"]
    )

    return has_policy_keyword and not (has_amount or has_client or has_reimbursement_intent)


def answer_expense_policy(text: str) -> dict:
    """
    Handles general expense policy questions.
    """
    text_lower = text.lower()

    if (
        "receipt" in text_lower
        or "receipts" in text_lower
        or "submit" in text_lower
        or "submission" in text_lower
        or "scanpro" in text_lower
        or "app" in text_lower
    ):
        return {
            "answer": (
                'All receipts must be scanned using the "ScanPro" app. '
                "We no longer accept physical paper receipts or photos of crumpled paper."
            ),
            "source": SOURCE_LABEL,
        }

    if "limit" in text_lower or "maximum" in text_lower or "35" in text_lower:
        return {
            "answer": "Client meals are reimbursable up to a maximum of 35 CHF per person.",
            "source": SOURCE_LABEL,
        }

    if "alcohol" in text_lower:
        return {
            "answer": "Alcohol is strictly non-reimbursable under the GreenLeaf expense policy.",
            "source": SOURCE_LABEL,
        }

    return {
        "answer": (
            "I can help with expense rules, reimbursement limits, alcohol restrictions, "
            "and receipt submission via ScanPro."
        ),
        "source": SOURCE_LABEL,
    }


def manual_review_response(reason: str) -> dict:
    """
    Standard response for ambiguous cases.

    Reliability-first principle:
    if we are not sure, we do not auto-approve.
    """
    return {
        "answer": (
            f"⚠️ I could not safely validate this expense automatically.\n"
            f"• {reason}\n\n"
            "Please provide more detail or contact Finance / HR if needed."
        ),
        "source": SOURCE_LABEL,
    }


def contains_generic_non_alcoholic_pattern(text: str) -> bool:
    """
    Detect generic phrases like:
    - we didn't drink alcohol
    - no alcohol
    - without alcohol

    These phrases should override only the generic word 'alcohol',
    not specific named drinks like wine or mojito.
    """
    text_lower = text.lower()
    return any(pattern in text_lower for pattern in GENERIC_NON_ALCOHOLIC_PATTERNS)


def extract_non_alcoholic_drink_phrases(text: str) -> list[str]:
    """
    Extract explicitly non-alcoholic drink phrases such as:
    - 0% beer
    - non-alcoholic gin
    - alcohol-free cocktail
    - virgin mojito

    Safety-first version:
    capture only ONE drink token after the marker.
    This prevents swallowing phrases like:
    - 0% beer and vodka
    """
    text_lower = text.lower()

    patterns = [
        r"\b0[.,]?0?%\s+[a-zA-Z][a-zA-Z0-9'-]*\b",
        r"\bnon[- ]alcoholic\s+[a-zA-Z][a-zA-Z0-9'-]*\b",
        r"\balcohol[- ]free\s+[a-zA-Z][a-zA-Z0-9'-]*\b",
        r"\bvirgin\s+[a-zA-Z][a-zA-Z0-9'-]*\b",
    ]

    matches = []
    for pattern in patterns:
        matches.extend(re.findall(pattern, text_lower))

    return matches

def contains_specific_alcohol_keywords(text: str) -> bool:
    """
    Deterministic alcohol detection excluding the generic word 'alcohol'.

    Important:
    If a specific alcohol keyword appears ONLY inside an explicit
    non-alcoholic phrase, it should not count as alcohol.

    Example:
    - "0% beer" -> should NOT count as alcohol
    - "non-alcoholic gin" -> should NOT count as alcohol
    - "virgin mojito" -> should NOT count as alcohol
    - "wine" -> should count as alcohol
    """
    text_lower = text.lower()
    protected_non_alcoholic_phrases = extract_non_alcoholic_drink_phrases(text)

    sanitized_text = text_lower
    for phrase in protected_non_alcoholic_phrases:
        sanitized_text = sanitized_text.replace(phrase, " ")

    specific_keywords = [kw for kw in ALCOHOL_KEYWORDS if kw != "alcohol"]

    for keyword in specific_keywords:
        if " " in keyword:
            if keyword in sanitized_text:
                return True
        else:
            if re.search(rf"\b{re.escape(keyword)}\b", sanitized_text):
                return True

    return False


def contains_generic_alcohol_word(text: str) -> bool:
    """
    Detect the standalone generic word 'alcohol'.

    This is separate from specific drinks/brands.
    """
    return re.search(r"\balcohol\b", text.lower()) is not None


def contains_fuzzy_alcohol_keywords(text: str) -> bool:
    """
    Detect likely misspellings of known alcohol words/brands.

    Examples:
    - guiness -> guinness
    - martiny -> martini

    Important:
    Explicit non-alcoholic drink phrases are stripped first,
    so fuzzy matching does not incorrectly reject:
    - non-alcoholic beer
    - virgin mojito
    """
    text_lower = text.lower()
    protected_non_alcoholic_phrases = extract_non_alcoholic_drink_phrases(text)

    sanitized_text = text_lower
    for phrase in protected_non_alcoholic_phrases:
        sanitized_text = sanitized_text.replace(phrase, " ")

    words = re.findall(r"\b[\w'-]+\b", sanitized_text)
    single_word_keywords = [
        kw for kw in ALCOHOL_KEYWORDS
        if " " not in kw and kw != "alcohol"
    ]

    for word in words:
        match = get_close_matches(word, single_word_keywords, n=1, cutoff=0.88)
        if match:
            return True

    return False


def has_client_context(text: str) -> bool:
    """
    Check whether an external business contact is mentioned.
    """
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in CLIENT_KEYWORDS)


def has_invalid_company_context(text: str) -> bool:
    """
    Detect clearly invalid company context.
    """
    text_lower = text.lower()
    return any(pattern in text_lower for pattern in INVALID_COMPANY_CONTEXT_PATTERNS)


def has_invalid_client_context(text: str) -> bool:
    """
    Detect client wording that is not clearly a valid GreenLeaf client.
    """
    text_lower = text.lower()

    if any(pattern in text_lower for pattern in INVALID_CLIENT_CONTEXT_PATTERNS):
        return True

    regex_patterns = [
        r"\b(client|customer|guest|prospect|business partner)\s+of\s+another company\b",
        r"\b(client|customer|guest|prospect|business partner)\s+from\s+another company\b",
        r"\banother company\s+(client|customer|guest|prospect|business partner)\b",
    ]

    return any(re.search(pattern, text_lower) for pattern in regex_patterns)


def looks_alcohol_suspicious(text: str) -> bool:
    """
    Identify general drink-like wording that can justify AI fallback.

    Note:
    Explicit non-alcoholic drink phrases alone should not trigger suspicion.
    """
    text_lower = text.lower()

    protected_non_alcoholic_phrases = extract_non_alcoholic_drink_phrases(text)
    sanitized_text = text_lower
    for phrase in protected_non_alcoholic_phrases:
        sanitized_text = sanitized_text.replace(phrase, " ")

    return any(word in sanitized_text for word in SUSPICIOUS_DRINK_WORDS)


def extract_named_item_phrase(text: str):
    """
    Extract a named consumable item from common expense phrasing.

    Supports drinks/brands/cocktails, including alphanumeric names like B52.
    """
    text_lower = text.lower()

    patterns = [
        r"\bi had (?:a|an)\s+([a-zA-Z0-9][a-zA-Z0-9' -]{1,40})\s+with\s+(?:a\s+)?(?:external\s+)?(?:client|customer|guest|prospect|business partner)\b",
        r"\bcan i expense (?:a|an)\s+([a-zA-Z0-9][a-zA-Z0-9' -]{1,40})\s+with\s+(?:a\s+)?(?:external\s+)?(?:client|customer|guest|prospect|business partner)\b",
        r"\bi ordered (?:a|an)\s+([a-zA-Z0-9][a-zA-Z0-9' -]{1,40})\s+with\s+(?:a\s+)?(?:external\s+)?(?:client|customer|guest|prospect|business partner)\b",
        r"\bi spent \d+(?:[.,]\d+)?\s*(?:chf|francs?|fr\.?)\s+on\s+([a-zA-Z0-9][a-zA-Z0-9' -]{1,40})\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            return match.group(1).strip()

    return None


def is_safe_meal_phrase(phrase: str) -> bool:
    """
    Check whether the extracted phrase is clearly a meal item.
    """
    if not phrase:
        return False

    words = set(re.findall(r"\b[\w'-]+\b", phrase.lower()))
    return bool(words & SAFE_MEAL_WORDS)


def looks_like_ambiguous_drink_case(text: str) -> bool:
    """
    Returns True when:
    - message has client context
    - amount exists
    - text appears to reference a named consumable item
    - item is not clearly a safe meal
    """
    if extract_amount(text) is None:
        return False

    if not has_client_context(text):
        return False

    phrase = extract_named_item_phrase(text)
    if not phrase:
        return False

    if is_safe_meal_phrase(phrase):
        return False

    return True


def contains_meal_plus_drink_pattern(text: str) -> bool:
    """
    Detect patterns like:
    - I had lunch with an external client with a B52 for 30 CHF
    - I had dinner with a customer with a green mexicain for 30 CHF

    Explicit non-alcoholic phrases should not trigger this.
    """
    text_lower = text.lower()

    if not any(meal in text_lower for meal in ["lunch", "dinner", "breakfast", "meal"]):
        return False

    protected_non_alcoholic_phrases = extract_non_alcoholic_drink_phrases(text)
    sanitized_text = text_lower
    for phrase in protected_non_alcoholic_phrases:
        sanitized_text = sanitized_text.replace(phrase, " ")

    pattern = (
        r"\b(?:lunch|dinner|breakfast|meal)\b.*?"
        r"\bwith\s+(?:an?\s+)?(?:external\s+)?(?:client|customer|guest|prospect|business partner)\b.*?"
        r"\bwith\s+(?:a|an)\s+([a-zA-Z0-9][a-zA-Z0-9' -]{1,40})\b"
    )

    match = re.search(pattern, sanitized_text)
    if not match:
        return False

    candidate = match.group(1).strip()
    if candidate in SAFE_MEAL_WORDS:
        return False

    return True


def has_explicit_no_alcohol_confirmation(text: str) -> bool:
    """
    Return True if the user explicitly confirms that alcohol was not included.

    This covers:
    - generic statements like "we didn't drink alcohol"
    - explicit non-alcoholic drink phrases like "0% beer", "virgin mojito"

    In strict mode, approval requires this kind of signal when a meal claim is made.
    """
    return (
        contains_generic_non_alcoholic_pattern(text)
        or len(extract_non_alcoholic_drink_phrases(text)) > 0
    )


def looks_like_meal_claim(text: str) -> bool:
    """
    Return True if the user appears to be describing a concrete meal/expense case.

    Example:
    - lunch with a customer for 30 CHF
    - dinner in a restaurant with a client

    In strict mode, these cases should not be auto-approved unless alcohol
    is explicitly ruled out.
    """
    text_lower = text.lower()

    has_meal_keyword = any(keyword in text_lower for keyword in MEAL_CONTEXT_KEYWORDS)
    has_amount = extract_amount(text) is not None
    has_client = has_client_context(text)

    return has_amount and has_client and has_meal_keyword


@lru_cache(maxsize=256)
def check_alcohol_with_ai(text: str):
    """
    Cached AI alcohol detector.

    Return values:
        True  -> alcohol detected
        False -> no alcohol detected
        None  -> AI service unavailable or unclear result

    AI is a fallback only.
    """
    prompt = (
        f"You are a strict expense auditor. Analyze this expense text: '{text}'. "
        "Decide whether the item mentioned is alcoholic. "
        "Treat cocktails, spirits, wine, beer, liqueurs, and alcohol brands as alcoholic. "
        "Treat explicit phrases like '0% beer', 'non-alcoholic', 'alcohol-free', and 'virgin mojito' as non-alcoholic. "
        "If the message is ambiguous or you are not sure, answer UNCLEAR. "
        "Reply with exactly one word only: YES, NO, or UNCLEAR."
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        result = response.text.strip().upper()
        print(f"DEBUG [AI Audit]: Alcohol detected? {result}")

        if result == "YES":
            return True
        if result == "NO":
            return False
        return None

    except Exception as e:
        print(f"CRITICAL AI ERROR: {e}")
        return None


# ─────────────────────────────────────────────
# MAIN FUNCTION
# ─────────────────────────────────────────────

def validate_expense(text: str) -> dict:
    """
    Reliability-first flow:
    1. Normalize text
    2. Policy question check
    3. Amount extraction
    4. Client context check
    5. Invalid company/client context checks
    6. Generic non-alcoholic patterns
    7. Exact alcohol detection
    8. Fuzzy alcohol detection
    9. Contradiction handling
    10. AI audit for ambiguous drink-like cases
    11. Strict-mode missing alcohol confirmation check
    12. Manual review instead of approval when uncertainty remains
    """
    # Step 0 — normalize text first
    text = normalize_text(text)

    # Step 1 — expense policy questions
    if looks_like_policy_question(text):
        return answer_expense_policy(text)

    # Step 2 — amount extraction
    amount = extract_amount(text)
    if amount is None:
        return manual_review_response(
            "Could not detect the expense amount in CHF. "
            "Please include the amount, for example: 'Lunch with a client for 20 CHF'."
        )

    # Step 3 — client context
    has_client = has_client_context(text)

    # Step 4 — invalid company/client context
    invalid_company_context = has_invalid_company_context(text)
    invalid_client_context = has_invalid_client_context(text)

    # Step 5 — generic non-alcoholic phrasing
    explicit_generic_non_alcoholic = contains_generic_non_alcoholic_pattern(text)

    # Step 6 — exact alcohol detection
    specific_keyword_alcohol = contains_specific_alcohol_keywords(text)

    # Generic 'alcohol' only counts if there is no generic negation.
    generic_alcohol_word = False
    if not explicit_generic_non_alcoholic:
        generic_alcohol_word = contains_generic_alcohol_word(text)

    # Step 7 — fuzzy alcohol detection
    fuzzy_alcohol = False
    if not explicit_generic_non_alcoholic and not specific_keyword_alcohol and not generic_alcohol_word:
        fuzzy_alcohol = contains_fuzzy_alcohol_keywords(text)

    has_real_alcohol = specific_keyword_alcohol or generic_alcohol_word or fuzzy_alcohol

    # Step 8 — contradiction handling
    # Example:
    # - "0% beer and whiskey"
    # - "non-alcoholic gin and wine"
    explicit_non_alcoholic_drinks_present = len(extract_non_alcoholic_drink_phrases(text)) > 0
    if explicit_non_alcoholic_drinks_present and has_real_alcohol:
        return manual_review_response(
            "The request contains both non-alcoholic and alcoholic items. I cannot safely validate this expense."
        )

    # Step 9 — AI audit for ambiguous drink-like cases
    ai_alcohol = False
    ai_checked = False
    if not explicit_generic_non_alcoholic and not has_real_alcohol:
        if (
            looks_alcohol_suspicious(text)
            or looks_like_ambiguous_drink_case(text)
            or contains_meal_plus_drink_pattern(text)
        ):
            ai_checked = True
            ai_alcohol = check_alcohol_with_ai(text)

    has_alcohol = has_real_alcohol or (ai_alcohol is True)

    # Step 10 — apply handbook rules
    reasons = []

    if amount > MAX_AMOUNT_PER_PERSON:
        reasons.append(f"Amount is above the {MAX_AMOUNT_PER_PERSON} CHF limit")

    if has_alcohol:
        if specific_keyword_alcohol or generic_alcohol_word:
            reasons.append("Receipt contains alcohol or a known alcohol brand")
        elif fuzzy_alcohol:
            reasons.append("Receipt likely contains alcohol (matched against known alcohol terms/brands)")
        else:
            reasons.append("Receipt contains alcohol (detected by AI audit)")

    if not has_client:
        reasons.append(
            "Expenses must be associated with an external client, customer, guest, prospect, or business partner"
        )

    if invalid_company_context:
        reasons.append("This expense refers to a previous or non-current employer context")

    if invalid_client_context:
        reasons.append("This client relationship is not clearly a valid GreenLeaf external business client")

    # Step 11 — strict-mode missing alcohol confirmation check
    # If the user describes a meal claim with a client, but does not explicitly
    # rule out alcohol, the bot should not auto-approve.
    if (
        not reasons
        and looks_like_meal_claim(text)
        and not has_explicit_no_alcohol_confirmation(text)
    ):
        return manual_review_response(
            "Please confirm whether any alcohol was included in the bill. I cannot safely auto-approve this meal expense without explicit no-alcohol confirmation."
        )

    # Step 12 — reliability-first safety gate
    # If the case looks like a drink case but alcohol still cannot be ruled out safely,
    # do manual review instead of approval.
    if not has_alcohol and has_client and not invalid_company_context and not invalid_client_context:
        ambiguous_drink_case = (
            looks_like_ambiguous_drink_case(text)
            or contains_meal_plus_drink_pattern(text)
        )

        if ambiguous_drink_case:
            if ai_checked and ai_alcohol is None:
                return manual_review_response(
                    "This appears to be a named beverage or drink-like item, but I could not safely determine whether it contains alcohol."
                )

            if not ai_checked:
                return manual_review_response(
                    "This appears to be a named beverage or drink-like item, and I cannot safely auto-approve it without confirming whether it contains alcohol."
                )

    # Step 13 — final decision
    if not reasons:
        return {
            "answer": "✅ Approved based on the provided information. Please submit the receipt via the ScanPro app.",
            "source": SOURCE_LABEL,
        }

    reason_str = "\n• ".join(reasons)

    return {
        "answer": (
            f"❌ Rejected based on the provided information:\n"
            f"• {reason_str}\n\n"
            "Please submit receipts via the ScanPro app."
        ),
        "source": SOURCE_LABEL,
    }
