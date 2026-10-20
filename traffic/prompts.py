"""Synthetic sample prompts, one set per feature.

Every prompt is invented for this lab. Invocation logging stores prompt and response text,
so nothing real may ever be sent. A feature added to the config without an entry here gets
a generic prompt built from its name.
"""

from __future__ import annotations

SAMPLE_PREFIX = "[SAMPLE DATA - MT-01 lab, fictional content] "

PROMPTS: dict[str, list[str]] = {
    "query-rewrite": [
        "Rewrite this shopper search into a precise product query: 'cheap waterproof hiking boots women wide fit'. "
        "Return the rewritten query and three alternative phrasings.",
        "A user searched 'laptop for video editing under 1500'. Rewrite it as a structured query with fields "
        "for category, budget, and must-have specifications.",
        "Turn this vague search into a clear one: 'that blue kettle that whistles'. List the attributes you inferred.",
    ],
    "result-summary": [
        "Summarize these three fictional search results for a shopper in two sentences each: "
        "(1) TrailMax 2 boots, waterproof, 4.5 stars; (2) Ridge Lite boots, breathable, 4.2 stars; "
        "(3) Summit Pro boots, insulated, 4.7 stars.",
        "Write a short comparison of two fictional robot vacuums: Model A has 120 minutes of battery and mapping; "
        "Model B has 90 minutes and a self-emptying base.",
        "Give a one-paragraph overview of these fictional results for 'standing desk': an electric desk, "
        "a manual crank desk, and a desktop converter.",
    ],
    "ticket-triage": [
        "Classify this fictional support ticket by urgency and category: 'My order #A-1001 arrived with a cracked "
        "screen and I need it replaced before Friday.' Explain your reasoning briefly.",
        "Triage this fictional ticket: 'I was charged twice for my subscription this month.' "
        "Return category, priority, and the team that should own it.",
        "Assign a priority to this fictional message: 'The app logs me out every few minutes on my tablet.'",
    ],
    "reply-draft": [
        "Draft a polite reply to a fictional customer whose package is three days late. Offer a tracking update "
        "and a small store credit.",
        "Write a short, friendly answer to a fictional customer asking how to reset their password.",
        "Draft a reply apologizing to a fictional customer for a double charge and confirming a refund is in progress.",
    ],
    "report-narrative": [
        "Write a three-sentence narrative for a fictional weekly report: site visits up 12 percent, "
        "conversion flat at 3.1 percent, average order value up 4 percent.",
        "Explain this fictional trend in plain language: support tickets fell 18 percent "
        "after a new help page launched.",
        "Summarize this fictional quarter for executives: revenue up 6 percent, churn down 1 point, two new regions.",
    ],
}


def prompt_for(feature: str, index: int) -> str:
    options = PROMPTS.get(feature)
    if not options:
        options = [f"Give a short, fictional example of a '{feature}' task and complete it in under 100 words."]
    return SAMPLE_PREFIX + options[index % len(options)]
