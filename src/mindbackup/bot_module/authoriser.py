import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

from mindbackup.bot_module.utils import get_settings
from mindbackup.config import Settings


logger = logging.getLogger(__name__)


def _authorised(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    return user is not None and user.id in settings.allowed_users


async def _reject(update: Update) -> None:
    user = update.effective_user
    logger.warning(
        "Rejected message from unauthorised user id=%s username=%s",
        getattr(user, "id", "?"),
        getattr(user, "username", "?"),
    )
    if update.effective_message:
        await update.effective_message.reply_text(
            "This is a private bot and you're not on its allowlist."
        )

def authorizer(func):
    @functools.wraps(func)
    async def inner(update: Update, context: ContextTypes.DEFAULT_TYPE):
        settings = get_settings(context)
        if not _authorised(update, settings):
            return await _reject(update)
        return await func(update, context)

    return inner
