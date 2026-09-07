from telegram.ext import ContextTypes

from mindbackup.config import Settings


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]