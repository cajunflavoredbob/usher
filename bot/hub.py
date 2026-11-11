"""Shared Hub pieces (see ADR 0009 and the Hub entry in GLOSSARY.md).

A Hub is a message that summarises one area (Account, ...) and offers its
actions as buttons. Flows started from a Hub normally end on a message with a
back button to that Hub, plus a Try again button when a step failed. Each Hub
registers a render function here; the back button re-renders it in place.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.callback_prefixes import HUB_OPEN
from bot.shared import edit_or_send, record_btn

HUB_MAX_ROW = 2
# user_data key holding the name of the Hub a running flow was started from.
HUB_ORIGIN_KEY = "hub_origin"


@dataclass(frozen=True)
class HubView:
    """What a Hub renders to: HTML text plus its action buttons."""
    text: str
    buttons: list[InlineKeyboardButton]


@dataclass(frozen=True)
class Hub:
    """A registered Hub: the label on its back button and its async
    render(update, ctx) -> HubView."""
    label: str
    render: Callable


HUBS: dict[str, Hub] = {}


def register_hub(name: str, label: str, render) -> None:
    HUBS[name] = Hub(label, render)


def hub_rows(buttons) -> list[list[InlineKeyboardButton]]:
    """Chunk buttons into rows of at most HUB_MAX_ROW."""
    buttons = list(buttons)
    return [buttons[i:i + HUB_MAX_ROW]
            for i in range(0, len(buttons), HUB_MAX_ROW)]


def back_button(hub: str) -> InlineKeyboardButton:
    label = HUBS[hub].label
    return InlineKeyboardButton(f"⬅️ {label}", callback_data=f"{HUB_OPEN}:{hub}")


def try_again_button(callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton("🔁 Try again", callback_data=callback_data)


def end_markup(hub: str, retry: Optional[str] = None) -> InlineKeyboardMarkup:
    """Keyboard for a message that ends a flow started from `hub`: the back
    button alone, or Try again and the back button together in one row."""
    back = back_button(hub)
    if retry is None:
        return InlineKeyboardMarkup([[back]])
    return InlineKeyboardMarkup([[try_again_button(retry), back]])


def hub_origin(ctx) -> Optional[str]:
    return ctx.user_data.get(HUB_ORIGIN_KEY)


def set_hub_origin(ctx, hub: Optional[str]) -> None:
    """Remember the Hub a flow was started from; None clears it."""
    if hub is None:
        ctx.user_data.pop(HUB_ORIGIN_KEY, None)
    else:
        ctx.user_data[HUB_ORIGIN_KEY] = hub


async def _render(update, ctx, hub: str):
    view: HubView = await HUBS[hub].render(update, ctx)
    return view.text, InlineKeyboardMarkup(hub_rows(view.buttons))


async def send_hub(update, ctx, hub: str) -> None:
    """Reply with the Hub as a new message."""
    text, markup = await _render(update, ctx, hub)
    sent = await update.effective_message.reply_text(
        text, reply_markup=markup, parse_mode="HTML")
    record_btn(ctx.application, update.effective_user.id, sent)


async def show_hub(update, ctx, hub: str) -> None:
    """Edit the tapped message into the Hub."""
    text, markup = await _render(update, ctx, hub)
    sent = await edit_or_send(update.callback_query, text,
                              reply_markup=markup, parse_mode="HTML")
    record_btn(ctx.application, update.effective_user.id, sent)


async def hub_open_callback(update, ctx) -> None:
    """Handler for the back button (pattern ^hubo:<hub>$)."""
    q = update.callback_query
    await q.answer()
    hub = q.data.split(":", 1)[1]
    if hub not in HUBS:
        return
    await show_hub(update, ctx, hub)


def keyboard_callbacks(message) -> list[str]:
    """The callback_data of every button in `message`'s inline keyboard."""
    markup = getattr(message, "reply_markup", None)
    return [
        btn.callback_data
        for row in getattr(markup, "inline_keyboard", ()) or ()
        for btn in row
        if isinstance(getattr(btn, "callback_data", None), str)
    ]


def came_from_hub(message, hub: str) -> bool:
    """True when `message`'s inline keyboard holds the back button to `hub`."""
    return f"{HUB_OPEN}:{hub}" in keyboard_callbacks(message)


async def end_flow(update, ctx, text: str, hub: Optional[str],
                   retry: Optional[str] = None, *,
                   typed_text: Optional[str] = None,
                   parse_mode: Optional[str] = None, edit: bool = True) -> None:
    """Say how a flow step ended. Without a Hub (`hub` None) the message is
    `typed_text or text` with no buttons, as for a typed command. With one it
    is `text` carrying the back button (plus Try again when `retry` is set),
    recorded with the button gate. Callback updates edit the tapped message
    unless `edit` is False; message updates reply."""
    q = update.callback_query
    kwargs = {"parse_mode": parse_mode} if parse_mode else {}
    if hub is None:
        shown = typed_text or text
        if q is not None and edit:
            await q.edit_message_text(shown, **kwargs)
        else:
            await update.effective_message.reply_text(shown, **kwargs)
        return
    markup = end_markup(hub, retry)
    if q is not None and edit:
        sent = await edit_or_send(q, text, reply_markup=markup, **kwargs)
    else:
        sent = await update.effective_message.reply_text(
            text, reply_markup=markup, **kwargs)
    record_btn(ctx.application, update.effective_user.id, sent)
