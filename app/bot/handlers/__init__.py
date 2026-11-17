"""Customer bot routers and shared in-place message editing / formatting utilities."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.core.logging import get_logger

logger = get_logger(__name__)

_TWO_DEC = Decimal("0.01")
_ONE_INT = Decimal("1")


def format_usd(amount: Decimal) -> str:
    """Format a Decimal USD balance or price without float conversion."""
    val = Decimal(amount)
    quant2 = val.quantize(_TWO_DEC, rounding=ROUND_HALF_UP)
    if val == quant2:
        return f"{quant2:,.2f}"
    normalized = val.normalize()
    text = f"{normalized:f}"
    if "." in text:
        int_part, frac_part = text.split(".", 1)
        int_formatted = f"{int(int_part):,}"
        if len(frac_part) < 2:
            frac_part = frac_part.ljust(2, "0")
        return f"{int_formatted}.{frac_part}"
    return f"{int(text):,}.00"


def format_irt(amount: Decimal) -> str:
    """Format a Decimal Iranian Toman (IRT) amount with thousands separators."""
    val = Decimal(amount)
    quant0 = val.quantize(_ONE_INT, rounding=ROUND_HALF_UP)
    if val == quant0:
        return f"{int(quant0):,}"
    quant2 = val.quantize(_TWO_DEC, rounding=ROUND_HALF_UP)
    return f"{quant2:,.2f}"


async def safe_edit_or_send(
    target: Message | CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str = ParseMode.HTML,
    answer_callback: bool = True,
    toast_text: str | None = None,
    show_alert: bool = False,
) -> Message | None:
    """Edit the existing message in-place for SPA navigation, with safe fallback to sending."""
    if isinstance(target, CallbackQuery):
        result_msg: Message | None = None
        msg = target.message
        if isinstance(msg, Message):
            try:
                if msg.text is not None:
                    edited = await msg.edit_text(
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                    )
                    if isinstance(edited, Message):
                        result_msg = edited
                elif msg.caption is not None:
                    edited = await msg.edit_caption(
                        caption=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                    )
                    if isinstance(edited, Message):
                        result_msg = edited
                else:
                    result_msg = await msg.answer(
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                    )
            except TelegramBadRequest as exc:
                err_lower = str(exc).lower()
                if "message is not modified" in err_lower:
                    result_msg = msg
                else:
                    logger.debug("Fallback to message.answer after edit failure: %s", exc)
                    result_msg = await msg.answer(
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                    )

        if answer_callback:
            try:
                await target.answer(text=toast_text, show_alert=show_alert)
            except TelegramBadRequest as exc:
                logger.debug("Ignored callback.answer error: %s", exc)
        return result_msg

    return await target.answer(
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
