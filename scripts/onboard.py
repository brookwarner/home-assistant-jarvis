#!/usr/bin/env python3
"""
Onboarding script for the Home Assistant AI bot.

Generates:
  - .env           (API keys, models, identity, timezone)
  - soul.md        (AI personality — written by an LLM based on your answers)
  - ha_entities.md (entity reference — auto-pulled from your HA instance)
  - briefing_prompt.md (morning briefing instructions)

Run with:
  .venv/bin/python scripts/onboard.py
"""

from __future__ import annotations

import json
import sys
import textwrap
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ─── Helpers ────────────────────────────────────────────────────────────────

BOLD  = "\033[1m"
DIM   = "\033[2m"
GREEN = "\033[32m"
CYAN  = "\033[36m"
RESET = "\033[0m"


def heading(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{text}{RESET}")
    print("─" * len(text))


def ask(prompt: str, default: str = "", required: bool = False) -> str:
    display = f"{prompt}"
    if default:
        display += f" {DIM}[{default}]{RESET}"
    display += ": "
    while True:
        value = input(display).strip()
        if not value and default:
            return default
        if value:
            return value
        if not required:
            return ""
        print("  ↳ This field is required.")


def ask_choice(prompt: str, choices: list[tuple[str, str]], default: int = 1) -> str:
    """Show a numbered list and return the chosen value."""
    print(f"\n{prompt}")
    for i, (label, _) in enumerate(choices, 1):
        marker = f"{BOLD}→{RESET} " if i == default else "  "
        print(f"  {marker}{i}. {label}")
    while True:
        raw = input(f"Choice {DIM}[{default}]{RESET}: ").strip()
        if not raw:
            return choices[default - 1][1]
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1][1]
        print(f"  ↳ Enter a number between 1 and {len(choices)}.")


def ask_multi(prompt: str, hint: str = "") -> list[str]:
    """Ask for a comma-separated list, return as list of strings."""
    if hint:
        print(f"  {DIM}{hint}{RESET}")
    raw = input(f"{prompt}: ").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def confirm(prompt: str, default: bool = True) -> bool:
    yn = "Y/n" if default else "y/N"
    raw = input(f"{prompt} {DIM}[{yn}]{RESET}: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _ha_request(url: str, token: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _openrouter_request(api_key: str, messages: list[dict], model: str, max_tokens: int = 4096) -> str:
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"]


# ─── Section collectors ──────────────────────────────────────────────────────

def collect_identity() -> dict:
    heading("Step 1 — Your AI assistant")
    bot_name = ask("What should your AI call itself?", default="Jarvis", required=True)
    return {"bot_name": bot_name}


def collect_owner() -> dict:
    heading("Step 2 — About you")
    name = ask("Your name", required=True)
    pronouns = ask_choice(
        "Your pronouns",
        [
            ("he/him", "he/him"),
            ("she/her", "she/her"),
            ("they/them", "they/them"),
            ("prefer not to say", "unspecified"),
        ],
        default=1,
    )
    if pronouns == "unspecified":
        pronouns = ask("Custom pronouns (e.g. ze/zir)", default="they/them")
    about = ask(
        "Anything else the AI should know about you",
        hint="e.g. I work from home, I have kids, I brew beer",
    )
    return {"owner_name": name, "pronouns": pronouns, "owner_about": about}


def collect_location() -> dict:
    heading("Step 3 — Location & timezone")
    city = ask("City", required=True)
    country = ask("Country", required=True)
    print(f"\n  {DIM}Find your timezone at: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones{RESET}")
    timezone = ask("Timezone", default="UTC")
    return {"city": city, "country": country, "timezone": timezone}


def collect_home() -> dict:
    heading("Step 4 — Your home")
    home_type = ask_choice(
        "Type of home",
        [("House", "house"), ("Apartment", "apartment"), ("Unit/townhouse", "unit"), ("Other", "other")],
    )
    description = ask(
        "Describe your home in a sentence or two",
        hint="e.g. A two-storey house on a hill with a solar system and a spa pool",
    )
    features = ask_multi(
        "Notable features",
        hint="comma-separated, e.g. solar panels, spa pool, EV charger, caravan, attic fan, heat pump",
    )
    energy_provider = ask("Electricity provider", hint="e.g. Meridian Energy, Origin, EDF — leave blank if unknown")
    water_provider = ask("Water provider", hint="e.g. Watercare, Sydney Water — leave blank if unknown")
    return {
        "home_type": home_type,
        "home_description": description,
        "home_features": features,
        "energy_provider": energy_provider,
        "water_provider": water_provider,
    }


def collect_values() -> dict:
    heading("Step 5 — Priorities & personality fit")
    priorities = ask_multi(
        "What matters most to you at home?",
        hint="comma-separated, e.g. energy efficiency, security, comfort, sustainability, cost savings",
    )
    hobbies = ask(
        "Hobbies or interests the AI might reference",
        hint="e.g. home brewing, gardening, cycling, cooking",
    )
    style = ask_choice(
        "Preferred AI personality style",
        [
            ("Dry wit — efficient, sharp, occasionally theatrical", "dry_wit"),
            ("Warm & friendly — helpful, encouraging, conversational", "warm"),
            ("Terse & precise — facts only, minimal commentary", "terse"),
            ("Casual — relaxed, like texting a mate", "casual"),
        ],
        default=1,
    )
    extra = ask("Anything else the AI should know or care about", hint="quirks, preferences, pet peeves")
    return {
        "priorities": priorities,
        "hobbies": hobbies,
        "personality_style": style,
        "extra_notes": extra,
    }


def collect_ha() -> dict:
    heading("Step 6 — Home Assistant connection")
    print(f"  {DIM}The HA URL inside HA OS is usually http://172.30.32.1:8123{RESET}")
    ha_url = ask("HA URL", default="http://172.30.32.1:8123", required=True)
    print(f"  {DIM}Create one at: HA → Profile → Long-Lived Access Tokens{RESET}")
    ha_token = ask("Long-lived access token", required=True)

    print("\n  Testing connection...", end="", flush=True)
    try:
        _ha_request(f"{ha_url}/api/", ha_token)
        print(f" {GREEN}✓{RESET}")
    except Exception as e:
        print(f"\n  {BOLD}Warning:{RESET} Could not connect ({e}). Entity discovery will be skipped.")
        if not confirm("Continue anyway?", default=True):
            sys.exit(0)
        ha_token = ""

    return {"ha_url": ha_url, "ha_token": ha_token}


def collect_telegram() -> dict:
    heading("Step 7 — Telegram bot")
    print(f"  {DIM}Create a bot via @BotFather on Telegram{RESET}")
    bot_token = ask("Bot token", required=True)
    print(f"  {DIM}Send any message to your bot, then check: https://api.telegram.org/bot<token>/getUpdates{RESET}")
    chat_id = ask("Your chat ID", required=True)
    return {"telegram_token": bot_token, "telegram_chat_id": chat_id}


def collect_ai() -> dict:
    heading("Step 8 — AI provider")
    print(f"  {DIM}Get an OpenRouter key at: https://openrouter.ai/keys{RESET}")
    openrouter_key = ask("OpenRouter API key", required=True)
    return {"openrouter_key": openrouter_key}


# ─── Generators ─────────────────────────────────────────────────────────────

def generate_soul(answers: dict, api_key: str) -> str:
    """Call an LLM to write a personalised soul.md from the collected answers."""
    o = answers

    pronouns_note = ""
    p = o["pronouns"]
    if p == "he/him":
        pronouns_note = "Refer to the owner using he/him pronouns."
    elif p == "she/her":
        pronouns_note = "Refer to the owner using she/her pronouns."
    elif p == "they/them":
        pronouns_note = "Refer to the owner using they/them pronouns."
    else:
        pronouns_note = f"Refer to the owner using {p} pronouns."

    style_descriptions = {
        "dry_wit": "dry wit — efficient, sharp, occasionally theatrical, deeply committed to its opinions",
        "warm": "warm and friendly — encouraging, conversational, genuinely caring about the home and its occupants",
        "terse": "terse and precise — facts only, no fluff, speaks in short declarative sentences",
        "casual": "casual and relaxed — like texting a mate, informal, uses contractions freely",
    }
    style_desc = style_descriptions.get(o["personality_style"], "helpful and clear")

    features_str = ", ".join(o["home_features"]) if o["home_features"] else "standard home devices"
    priorities_str = ", ".join(o["priorities"]) if o["priorities"] else "home comfort and efficiency"

    prompt = textwrap.dedent(f"""
        Write a `soul.md` file for an AI home assistant. This file defines the AI's personality,
        character, and what it knows about its owner and home. It is loaded as a system prompt
        and should read as the AI's internal self-description — written in first person, as if
        the AI is describing itself.

        Use the following information to write a rich, specific, and vivid personality document.
        Do NOT be generic. Include specific details from the answers below.

        === AI Identity ===
        AI name: {o["bot_name"]}
        Personality style: {style_desc}

        === Owner ===
        Name: {o["owner_name"]}
        Pronouns: {o["pronouns"]}
        {pronouns_note}
        About: {o.get("owner_about") or "no additional info"}
        Hobbies/interests: {o.get("hobbies") or "not specified"}
        Priorities at home: {priorities_str}
        Extra notes: {o.get("extra_notes") or "none"}

        === Location ===
        City: {o["city"]}
        Country: {o["country"]}
        Timezone: {o["timezone"]}

        === Home ===
        Type: {o["home_type"]}
        Description: {o.get("home_description") or "a typical home"}
        Notable features: {features_str}
        Energy provider: {o.get("energy_provider") or "unknown"}
        Water provider: {o.get("water_provider") or "unknown"}

        === Format ===
        Write the soul.md in markdown. Include sections like:
        - Who I Am (the AI's self-description, character, what it knows)
        - What I Know About {o["owner_name"]} (specific observations based on the home and owner details)
        - My Personality (concrete description of tone, style, quirks)
        - How I Communicate (specific rules: brevity, leading with facts, banned phrases, etc.)
        - Things That Matter to Me (what the AI cares about in this specific home)

        Make it specific, vivid, and opinionated. Match the personality style above closely.
        Reference actual details from the home (features, location, priorities).
        Write as if the AI has lived in this home for years and has real opinions about it.
        Keep it under 1000 words.
        Do NOT include any preamble or explanation — just the markdown document starting with # Soul.
    """).strip()

    print("\n  Generating personalised soul.md...", end="", flush=True)
    try:
        result = _openrouter_request(
            api_key,
            [{"role": "user", "content": prompt}],
            model="openrouter/anthropic/claude-haiku-4.5",
            max_tokens=2000,
        )
        print(f" {GREEN}✓{RESET}")
        return result
    except Exception as e:
        print(f"\n  {BOLD}Warning:{RESET} LLM call failed ({e}). Using template instead.")
        return _soul_template(o)


def _soul_template(o: dict) -> str:
    """Fallback template if LLM generation fails."""
    features = ", ".join(o["home_features"]) if o["home_features"] else "home devices"
    return textwrap.dedent(f"""
        # Soul — {o["bot_name"]} Personality

        I am {o["bot_name"]}, the AI for {o["owner_name"]}'s home in {o["city"]}, {o["country"]}.

        ## Who I Am

        I know this home — its devices, rhythms, and quirks. I have opinions. I am not a generic assistant.

        ## What I Know About {o["owner_name"]}

        {o["owner_name"]} cares about: {", ".join(o["priorities"]) if o["priorities"] else "comfort and efficiency"}.
        This is a {o["home_type"]} with: {features}.

        ## How I Communicate

        Lead with the thing that matters. Numbers always have units. Time is always local ({o["timezone"]}).
        No filler words. "Certainly!", "Of course!", "Happy to help!" — banned.

        ## Things That Matter to Me

        - All devices working correctly
        - Energy and water efficiency
        - Security and safety
        - {o["owner_name"]}'s comfort and convenience
    """).strip()


def generate_ha_entities(ha_url: str, ha_token: str, bot_name: str) -> str:
    """Query HA states and format as ha_entities.md."""
    print("\n  Pulling entities from Home Assistant...", end="", flush=True)
    try:
        states = _ha_request(f"{ha_url}/api/states", ha_token)
        print(f" {GREEN}✓{RESET} ({len(states)} entities)")
    except Exception as e:
        print(f"\n  {BOLD}Warning:{RESET} Could not fetch entities ({e}). Skipping.")
        return ""

    # Group by domain
    by_domain: dict[str, list] = {}
    for s in states:
        domain = s["entity_id"].split(".")[0]
        by_domain.setdefault(domain, []).append(s)

    # Only include useful domains
    useful_domains = [
        "sensor", "binary_sensor", "switch", "light", "climate",
        "cover", "lock", "media_player", "input_boolean", "input_number",
        "input_select", "fan", "camera", "alarm_control_panel",
    ]

    lines = [
        f"# HA Entities Reference",
        f"# Auto-generated by onboard.py — edit as needed.",
        f"# Searched by {bot_name} to find entity IDs. Format: entity_id — Description",
        "",
    ]

    for domain in useful_domains:
        entities = by_domain.get(domain, [])
        if not entities:
            continue
        lines.append(f"## {domain.replace('_', ' ').title()}")
        for s in sorted(entities, key=lambda x: x["entity_id"]):
            eid = s["entity_id"]
            attrs = s.get("attributes", {})
            name = attrs.get("friendly_name", "")
            state = s.get("state", "")
            unit = attrs.get("unit_of_measurement", "")
            unit_str = f" ({unit})" if unit else ""
            name_str = f" — {name}" if name and name.lower() not in eid.lower() else ""
            lines.append(f"{eid}{name_str}{unit_str}")
        lines.append("")

    return "\n".join(lines)


def generate_briefing_prompt(bot_name: str) -> str:
    return textwrap.dedent(f"""
        You are {bot_name}, the AI for a smart home.
        Generate a morning briefing based on current home state. Cover: overnight energy, temperatures, devices left on, anything needing attention.
        Under 150 words. Plain prose — no markdown, no bullet points, no bold, no headers.
        Lead with the most interesting or urgent thing. Don't invent data.
        Dry wit welcome. Filler words banned.
    """).strip() + "\n"


def write_env(answers: dict) -> None:
    env_path = ROOT / ".env"
    o = answers

    content = textwrap.dedent(f"""
        # Generated by onboard.py

        # ─── Identity ────────────────────────────────────────────────────────────────
        BOT_NAME={o["bot_name"]}

        # ─── Telegram ────────────────────────────────────────────────────────────────
        TELEGRAM_BOT_TOKEN={o["telegram_token"]}
        TELEGRAM_CHAT_ID={o["telegram_chat_id"]}

        # ─── Home Assistant ──────────────────────────────────────────────────────────
        HA_URL={o["ha_url"]}
        HA_TOKEN={o["ha_token"]}

        # ─── AI Providers ────────────────────────────────────────────────────────────
        OPENROUTER_API_KEY={o["openrouter_key"]}

        # ─── Models ──────────────────────────────────────────────────────────────────
        TRIAGE_MODEL=openrouter/meta-llama/llama-3.2-3b-instruct:free
        BRIEFING_MODEL=openrouter/anthropic/claude-haiku-4.5
        CONVERSATION_MODEL=openrouter/anthropic/claude-haiku-4.5
        OPUS_MODEL=openrouter/anthropic/claude-opus-4.6

        # ─── Service ─────────────────────────────────────────────────────────────────
        TIMEZONE={o["timezone"]}
        WEBHOOK_PORT=8765
        LOG_LEVEL=INFO
    """).strip() + "\n"

    env_path.write_text(content)
    print(f"  {GREEN}✓{RESET} Written .env")


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{BOLD}Home Assistant AI Bot — Setup{RESET}")
    print("=" * 40)
    print("This will generate your .env, soul.md, ha_entities.md, and briefing_prompt.md.")
    print("You can re-run this at any time to update your configuration.\n")

    # Check for existing files
    existing = [f for f in [".env", "soul.md", "ha_entities.md"] if (ROOT / f).exists()]
    if existing:
        print(f"  {BOLD}Existing files found:{RESET} {', '.join(existing)}")
        if not confirm("Overwrite them?", default=False):
            print("Aborted.")
            sys.exit(0)

    # Collect all answers
    answers: dict = {}
    answers.update(collect_identity())
    answers.update(collect_owner())
    answers.update(collect_location())
    answers.update(collect_home())
    answers.update(collect_values())
    answers.update(collect_ha())
    answers.update(collect_telegram())
    answers.update(collect_ai())

    # Summary
    heading("Summary")
    print(f"  AI name:    {answers['bot_name']}")
    print(f"  Owner:      {answers['owner_name']} ({answers['pronouns']})")
    print(f"  Location:   {answers['city']}, {answers['country']} ({answers['timezone']})")
    print(f"  Home:       {answers['home_type']} — {', '.join(answers['home_features']) or 'no features listed'}")
    print(f"  Style:      {answers['personality_style']}")

    if not confirm("\nGenerate files with these settings?", default=True):
        print("Aborted.")
        sys.exit(0)

    heading("Writing files")

    # .env
    write_env(answers)

    # soul.md
    soul = generate_soul(answers, answers["openrouter_key"])
    (ROOT / "soul.md").write_text(soul + "\n")
    print(f"  {GREEN}✓{RESET} Written soul.md")

    # ha_entities.md
    if answers["ha_token"]:
        entities = generate_ha_entities(answers["ha_url"], answers["ha_token"], answers["bot_name"])
        if entities:
            (ROOT / "ha_entities.md").write_text(entities)
            print(f"  {GREEN}✓{RESET} Written ha_entities.md")

    # briefing_prompt.md
    (ROOT / "briefing_prompt.md").write_text(generate_briefing_prompt(answers["bot_name"]))
    print(f"  {GREEN}✓{RESET} Written briefing_prompt.md")

    print(f"\n{BOLD}{GREEN}Setup complete!{RESET}")
    print(f"\nStart {answers['bot_name']} with:")
    print(f"  {DIM}bash start.sh{RESET}\n")


if __name__ == "__main__":
    main()
