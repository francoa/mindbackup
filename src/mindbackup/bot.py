"""Telegram ingest bot.

Standalone long-polling process rather than a plugin inside some larger
assistant: deterministic, no LLM in the hot path, and it owns its bot token
outright instead of contending with another process for the same one.

Design rule from spec §5.6 — silent failure is the one unacceptable outcome.
Every path through `handle_voice` ends in a Telegram reply.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from mindbackup.bot_module.authoriser import authorizer
from mindbackup.bot_module.utils import get_settings
from mindbackup.browse import (
    CB_PAGE,
    CB_TOPIC,
    back_keyboard,
    chunk_message,
    render_topic_list,
    resolve_topic,
    topic_body,
    topics_keyboard,
)
from mindbackup.config import Settings, load_settings
from mindbackup.extract import LLMError, extract_atoms
from mindbackup.pipeline import TranscriptionError, VaultWriteError, ingest_audio, local_today
from mindbackup.review import (
    CB_APPROVE,
    CB_DISCARD,
    CB_EDIT,
    PendingReview,
    apply_review,
    render_filed,
    render_review,
    review_keyboard,
)
from mindbackup.topics import known_topics, topic_counts

logger = logging.getLogger(__name__)

MAX_PREVIEW_CHARS = 500

HELP_TEXT = (
    "🎙 *Voice Mind Backup*\n\n"
    "Send me a voice note and I'll transcribe it into your Obsidian vault.\n\n"
    "/get\\_topic — browse what's been filed, one topic at a time\n"
    "/status — show where memos go and today's count\n"
    "/help — this message"
)


@authorizer
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_markdown(HELP_TEXT)


@authorizer
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    message = update.effective_message
    if message is None:
        return

    memo_path = settings.memo_path
    today = local_today(settings)
    try:
        total = len(list(memo_path.glob("*.md"))) if memo_path.is_dir() else 0
        today_count = (
            len(list(memo_path.glob(f"{today.isoformat()}*.md")))
            if memo_path.is_dir()
            else 0
        )
        vault_state = "reachable" if memo_path.parent.is_dir() else "MISSING"
    except OSError as exc:
        total, today_count, vault_state = 0, 0, f"error: {exc}"

    await message.reply_text(
        f"Vault: {vault_state}\n"
        f"Memos folder: {memo_path}\n"
        f"Memos total: {total} (today: {today_count})\n"
        f"Transcription: {settings.stt_provider}/{settings.stt_model}\n"
        f"Language: {settings.stt_language or 'auto-detect'}"
    )


@authorizer
async def cmd_get_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the vault's topics; `/get_topic <name>` jumps straight to one."""
    settings = get_settings(context)
    message = update.effective_message
    if message is None:
        return

    topics = known_topics(settings)

    requested = " ".join(context.args or []).strip()
    if requested:
        match = resolve_topic(requested, topics)
        if match is not None:
            await _send_topic(message, match, settings)
            return
        await message.reply_text(f"🤷 No topic matching “{requested}”. Pick one:")

    await message.reply_text(
        render_topic_list(topics),
        parse_mode="Markdown",
        reply_markup=topics_keyboard(topics, 0, topic_counts(settings)) if topics else None,
    )


async def _send_topic(message, topic: str, settings: Settings, edit: bool = False) -> None:
    """Send a whole topic page, split across as many messages as it takes.

    No parse_mode: the page is arbitrary text the user may have edited by hand,
    and a stray asterisk must not turn into a Telegram parse error that eats
    the whole reply.
    """
    body = topic_body(topic, settings)
    if not body:
        text = f"📄 *{topic}* is empty."
        if edit:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=back_keyboard())
        else:
            await message.reply_text(text, parse_mode="Markdown", reply_markup=back_keyboard())
        return

    parts = chunk_message(f"📄 {topic}\n\n{body}")
    last = len(parts) - 1
    for index, part in enumerate(parts):
        markup = back_keyboard() if index == last else None
        if index == 0 and edit:
            await message.edit_text(part, reply_markup=markup)
        else:
            await message.reply_text(part, reply_markup=markup)


@authorizer
async def handle_topic_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Paging through the topic list, and opening one."""
    settings = get_settings(context)
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    message = query.message
    if message is None:
        return

    data = query.data or ""
    topics = known_topics(settings)

    if data.startswith(CB_TOPIC):
        topic = resolve_topic(data[len(CB_TOPIC) :], topics)
        if topic is None:
            # The page was renamed or deleted in Obsidian since the list was drawn.
            await query.edit_message_text(
                "🤷 That topic is gone. /get_topic to see what's there now."
            )
            return
        await _send_topic(message, topic, settings, edit=True)
        return

    page = 0
    if data.startswith(CB_PAGE):
        try:
            page = int(data[len(CB_PAGE) :])
        except ValueError:
            page = 0

    await query.edit_message_text(
        render_topic_list(topics, page),
        parse_mode="Markdown",
        reply_markup=topics_keyboard(topics, page, topic_counts(settings)) if topics else None,
    )


@authorizer
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice note / audio file -> transcript -> vault, with a reply either way."""
    settings = get_settings(context)
    message = update.effective_message
    if message is None:
        return
    media = message.voice or message.audio or message.document
    if media is None:
        await message.reply_text("⚠️ No audio found in that message.")
        return

    status = await message.reply_text("⏳ Transcribing…")

    with tempfile.TemporaryDirectory(prefix="mindbackup-") as tmp:
        try:
            telegram_file = await context.bot.get_file(media.file_id)
            suffix = Path(getattr(media, "file_name", "") or "voice.ogg").suffix or ".ogg"
            audio_path = Path(tmp) / f"{media.file_unique_id}{suffix}"
            await telegram_file.download_to_drive(custom_path=str(audio_path))
        except Exception as exc:
            logger.exception("Download failed")
            await status.edit_text(f"❌ Could not download the audio from Telegram: {exc}")
            return

        try:
            result = await asyncio.to_thread(
                ingest_audio,
                audio_path,
                settings,
                recorded_at=message.date,
                source="telegram",
            )
        except (TranscriptionError, VaultWriteError) as exc:
            logger.error("Ingest failed: %s", exc)
            await status.edit_text(f"❌ {exc}")
            return
        except Exception as exc:
            logger.exception("Unexpected ingest failure")
            await status.edit_text(f"❌ Unexpected failure: {type(exc).__name__}: {exc}")
            return

    preview = result.transcript.text
    if len(preview) > MAX_PREVIEW_CHARS:
        preview = preview[:MAX_PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"

    await status.edit_text(f"✅ Saved as *{result.memo.path.name}*\n\n{preview}", parse_mode="Markdown")

    # The memo is now safe on disk. Everything past this point is a bonus that
    # must never be able to undo that (spec C4).
    await _offer_extraction(update, context, result, settings)


async def _offer_extraction(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result,
    settings: Settings,
) -> None:
    """Extract atoms and offer them for review, as a separate message.

    Non-blocking by construction: the user may ignore this entirely. Any
    failure degrades to a short note, because the memo is already saved.
    """
    if not settings.llm_configured:
        return

    message = update.effective_message
    if message is None:
        return

    thinking = await message.reply_text("🧠 Extracting…")

    try:
        extraction = await asyncio.to_thread(
            extract_atoms,
            result.transcript.text,
            settings,
            known_topics(settings),
        )
    except LLMError as exc:
        logger.warning("Extraction failed for %s: %s", result.memo.path.name, exc)
        await thinking.edit_text(
            f"⚠️ Saved, but extraction failed: {exc}\n"
            f"Run `mindbackup extract` later to retry.",
            parse_mode="Markdown",
        )
        return
    except Exception as exc:
        logger.exception("Unexpected extraction failure")
        await thinking.edit_text(f"⚠️ Saved, but extraction failed: {type(exc).__name__}: {exc}")
        return

    if not extraction.atoms:
        await thinking.edit_text("🧠 Nothing worth extracting from that one.")
        return

    review = PendingReview(
        memo_name=result.memo.path.stem,
        memo_date=result.memo.memo_date.isoformat(),
        atoms=extraction.atoms,
        audio=result.archived_audio.name if result.archived_audio else None,
    )

    sent = await thinking.edit_text(
        render_review(extraction, result.memo.path.name),
        parse_mode="Markdown",
        reply_markup=review_keyboard(extraction),
    )
    # edit_text returns True (not a Message) when the edit is a no-op; without
    # a message id there is nothing to key the pending review on.
    if isinstance(sent, bool):
        logger.warning("Could not track review message for %s.", result.memo.path.name)
        return
    context.application.bot_data.setdefault("reviews", {})[sent.message_id] = review


@authorizer
async def handle_review_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve / edit / discard on a pending extraction."""
    settings = get_settings(context)
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    reviews: dict = context.application.bot_data.setdefault("reviews", {})
    message = query.message
    review = reviews.get(message.message_id) if message else None

    if message is None or review is None:
        # Bot restarted, or already actioned. Say so rather than failing mutely.
        await query.edit_message_text(
            "⌛ That review expired. Run `mindbackup extract` to redo it.",
            parse_mode="Markdown",
        )
        return

    if query.data == CB_DISCARD:
        reviews.pop(message.message_id, None)
        await query.edit_message_text("🗑 Discarded. The transcript is still saved.")
        return

    if query.data == CB_EDIT:
        await query.edit_message_text(
            f"{render_review_plain(review)}\n\n"
            "✏️ Editing in Telegram isn't built yet — edit the topic pages in "
            "Obsidian, or re-run `mindbackup extract --all`.",
            parse_mode="Markdown",
        )
        return

    try:
        filed = await asyncio.to_thread(apply_review, review, settings)
    except VaultWriteError as exc:
        await query.edit_message_text(f"❌ Could not file: {exc}")
        return

    reviews.pop(message.message_id, None)
    await query.edit_message_text(render_filed(filed), parse_mode="Markdown")


def render_review_plain(review: PendingReview) -> str:
    lines = [f"🧠 From *{review.memo_name}*:", ""]
    for index, atom in enumerate(review.atoms, start=1):
        lines.append(f"• {index}. {atom.text}")
    return "\n".join(lines)


@authorizer
async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "Send me a voice note. Text messages aren't saved in Milestone 1."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last-resort net: never let an exception die silently in the logs alone."""
    logger.exception("Unhandled bot error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"❌ Internal error: {type(context.error).__name__}: {context.error}"
            )
        except Exception:
            logger.exception("Could not deliver the error message to Telegram")


def build_application(settings: Settings):
    settings.validate_for_bot()
    app = ApplicationBuilder().token(settings.telegram_token).build()
    app.bot_data["settings"] = settings

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("get_topic", cmd_get_topic))
    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_voice
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_other))
    app.add_handler(CallbackQueryHandler(handle_review_button, pattern=r"^mb:"))
    app.add_handler(CallbackQueryHandler(handle_topic_button, pattern=r"^mbt:"))
    app.add_error_handler(on_error)
    return app


def run(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    app = build_application(settings)
    logger.info(
        "Starting bot. Memos -> %s, STT %s/%s, %d allowed user(s).",
        settings.memo_path,
        settings.stt_provider,
        settings.stt_model,
        len(settings.allowed_users),
    )
    app.run_polling(drop_pending_updates=False)
