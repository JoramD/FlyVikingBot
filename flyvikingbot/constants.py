"""Branding shared by the embeds."""

BRAND_COLOUR = 0xED2001


def with_emoji(title: str, emoji: str | None) -> str:
    """'<emoji> title', or just the title when no emoji is set (EMOJI_FLYVIKING_V in .env)."""
    return f"{emoji} {title}" if emoji else title
