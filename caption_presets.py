"""
Ready-made caption styles an admin can pick instead of writing a template.

Each preset is an ordinary caption template (see utils.render_caption_template
for the placeholder syntax), so everything that works for a hand-written
/setcaption template works here too - including the per-user /privacy setting:
a user who turned privacy on shows as "Anonymous" in any identity field, in
every preset below.

The empty template is special: it means "use the built-in default caption",
whose uploader block is controlled by the separate show-uploader-info switch.
"""

from utils import render_caption_template, template_uses_identity

# Order here is the order shown in the picker.
CAPTION_PRESETS = [
    {
        "key": "default",
        "label": "Built-in default",
        "desc": "The bot's standard caption. Uploader block follows the on/off switch below.",
        "template": "",
    },
    {
        "key": "minimal",
        "label": "Minimal",
        "desc": "File name only.",
        "template": "📁 {filename}",
    },
    {
        "key": "simple",
        "label": "Simple",
        "desc": "File name and size.",
        "template": "📁 {filename}\n💾 {size}",
    },
    {
        "key": "anonymous",
        "label": "No uploader info",
        "desc": "Name, size and date. Never mentions who uploaded it.",
        "template": "📁 {filename}\n💾 {size}\n🕒 {date}",
    },
    {
        "key": "credit",
        "label": "Credit line",
        "desc": "Name, size, and a \"Shared by\" line.",
        "template": "📁 {filename}\n💾 {size}\n🙋 Shared by {uploader}",
    },
    {
        "key": "compact",
        "label": "Compact",
        "desc": "Everything on one line.",
        "template": "📁 {filename} · {size} · by {uploader}",
    },
    {
        "key": "card",
        "label": "Card",
        "desc": "Boxed title with uploader and date underneath.",
        "template": (
            "┏━━━━━━━━━━━━━━━━┓\n"
            "  📁 {filename}\n"
            "┗━━━━━━━━━━━━━━━━┛\n"
            "💾 Size: {size}\n"
            "👤 By: {uploader} ({username})\n"
            "🕒 {date}"
        ),
    },
    {
        "key": "fancy",
        "label": "Fancy",
        "desc": "Decorated, with a divider line.",
        "template": (
            "✨ {filename}\n"
            "━━━━━━━━━━━━━━\n"
            "📦 {size}\n"
            "💫 Uploaded by {uploader}\n"
            "📅 {date}"
        ),
    },
    {
        "key": "detailed",
        "label": "Detailed",
        "desc": "Every field: name, size, uploader, @username, user ID and date.",
        "template": (
            "📁 **{filename}**\n\n"
            "💾 **Size:** {size}\n"
            "👤 **Uploader:** {uploader}\n"
            "🔖 **Username:** {username}\n"
            "🆔 **User ID:** `{user_id}`\n"
            "🕒 **Uploaded:** {date}"
        ),
    },
]

PRESETS_BY_KEY = {p["key"]: p for p in CAPTION_PRESETS}


def _norm(text: str) -> str:
    """One normal form for comparing names: lowercase, with spaces and hyphens
    treated alike, so "Built-in default", "built in default" and
    "built_in_default" all mean the same style."""
    return "_".join((text or "").lower().replace("-", " ").replace("_", " ").split())


def find_preset(text: str):
    """Look a preset up by key or by label, ignoring case, spaces and hyphens."""
    wanted = _norm(text)
    if not wanted:
        return None
    for p in CAPTION_PRESETS:
        if wanted in (_norm(p["key"]), _norm(p["label"])):
            return p
    return None


def current_preset(template: str):
    """The preset matching the saved template exactly, or None for a custom one."""
    template = (template or "").strip()
    for p in CAPTION_PRESETS:
        if p["template"].strip() == template:
            return p
    return None


def shows_identity(preset: dict) -> bool:
    """True if the preset can reveal who uploaded the file."""
    return template_uses_identity(preset["template"])


class SampleUser:
    first_name = "Jordan"
    last_name = "Lee"
    username = "jordanlee"
    id = 123456789


class SampleMessage:
    from_user = SampleUser()


def render_sample(template: str) -> str:
    """Render a template with sample values, for previews in the picker."""
    if not template.strip():
        return "📁 example_video.mp4"
    return render_caption_template(template, SampleMessage(), "example_video.mp4", 254_800_000)
