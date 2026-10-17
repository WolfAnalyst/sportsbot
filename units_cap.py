"""v6.72 max-units rule, the same for every tipster with a per-tip units cap.

Wilson 2026-10-18: "if the stated units is within 2 times of the max, still bet the tip
up to the max units and send the rest to manual (this is applied to all bookies) and
clearly state 1. placed xxx 2. what is left 3. because of max stake units limit ... is xxx".
(17 Oct: a 5u Bettors Edge NBL card went wholly to manual against the 4u image cap.)

- units <= cap        : place it all, nothing to say.
- cap < units <= 2 cap: place the cap, the rest is a MANUAL alert (split + note below).
- units > 2 cap       : each path keeps what it did before v6.72 (a reject path still sends
                        the whole tip to manual as a likely misread; a clamp path still
                        places the cap) and a clamp path now also sends this note.
"""


def split(units, cap) -> tuple:
    """(place_units, left_units, within_2x) for a tip of `units` against `cap`."""
    u = float(units or 0)
    c = float(cap or 0)
    if c <= 0:
        # A zero/negative/unset cap places NOTHING (review H4: the old min() gave a zero
        # stake; returning the full units here would have placed it uncapped).
        return 0.0, u, False
    if u <= c + 1e-9:
        return u, 0.0, True
    return c, round(u - c, 4), u <= 2 * c + 1e-9


def _money(x: float) -> str:
    return f"${x:,.2f}".replace(".00", "")


def manual_note(tipster: str, selection: str, units, cap, unit_size, placed_note: str = "") -> str:
    """The three-line note: what is placed, what is left, and why."""
    u, c, us = float(units), max(0.0, float(cap or 0)), float(unit_size or 0)
    left = round(u - c, 4)
    over = "" if u <= 2 * c + 1e-9 else (
        f"\nNote: {u:g}u is more than twice the {c:g}u limit, check it is not a misread.")
    return (f"{selection}\n"
            f"1. Auto-placing: {c:g}u ({_money(c * us)}){placed_note}\n"
            f"2. Left for you to place manually: {left:g}u ({_money(left * us)})\n"
            f"3. Why: the max units limit for {tipster} is {c:g}u per tip "
            f"(tipped {u:g}u){over}")


def alert(notifier, channel: str, tipster: str, selection: str, units, cap, unit_size,
          log=None,
          placed_note: str = (", placing now: its own alert says if it placed; if it can't "
                              "place, that alert sends it to manual too")) -> None:
    """Send the note as a MANUAL alert. Never raises (an alert failure must not stop the
    capped bet from placing)."""
    try:
        notifier.notify_text_manual_alert(
            channel, manual_note(tipster, selection, units, cap, unit_size, placed_note),
            header="OVER MAX UNITS, rest MANUAL")
    except Exception as e:
        if log is not None:
            log.error(f"[{channel}] max-units manual note failed: {e}")
