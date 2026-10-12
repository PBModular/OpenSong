import asyncio
import datetime
import logging
import math
import os
import time
from typing import Optional, List, Dict, Tuple

from pyrogram import Client, errors, filters, enums
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy import select, func, delete, case, text

from base.module import BaseModule, command, callback_query, allowed_for
from config import config
from .db import Base, ChatSettings, SongPost, SongVote, PendingApproval
from .services.spotify import SpotifyService, TrackInfo, SUPPORTED_GENRES
from .services.downloader import YouTubeDownloader, DownloadedTrack

logger = logging.getLogger(__name__)

GENRES_PER_PAGE = 6
AVAILABLE_INTERVALS = [1, 3, 6, 12, 24, 48]
AVAILABLE_DURATIONS = [300, 480, 600, 900, 1200]  # in seconds: 5m, 8m, 10m, 15m, 20m


class OpenSongModule(BaseModule):
    @property
    def help_page(self):
        return self.S["help"]

    @property
    def db_meta(self):
        return Base.metadata

    def on_init(self):
        self._is_stopping = False
        self._scheduler_task: Optional[asyncio.Task] = None
        self._chat_locks: Dict[int, asyncio.Lock] = {}

        # Initialize Spotify service
        sp_id = self.module_config.get("spotify_client_id", "")
        sp_secret = self.module_config.get("spotify_client_secret", "")
        self.spotify = SpotifyService(client_id=sp_id, client_secret=sp_secret)

        # Initialize YouTube downloader
        temp_dir = os.path.join(self.module_path, "temp")
        self.downloader = YouTubeDownloader(temp_dir=temp_dir)
        self.logger.info("OpenSong module initialized.")

    async def on_db_ready(self):
        """Called when database is fully ready."""
        # Ensure database schema is migrated for newly introduced columns
        try:
            async with self.db.engine.begin() as conn:
                res = await conn.execute(text("PRAGMA table_info(opensong_chat_settings)"))
                existing_cols = {row[1] for row in res.fetchall()}
                if existing_cols:
                    if "unpin_previous" not in existing_cols:
                        await conn.execute(
                            text("ALTER TABLE opensong_chat_settings ADD COLUMN unpin_previous BOOLEAN DEFAULT 1")
                        )
                        self.logger.info("Migrated opensong_chat_settings: added unpin_previous column.")
                    if "last_pinned_message_id" not in existing_cols:
                        await conn.execute(
                            text("ALTER TABLE opensong_chat_settings ADD COLUMN last_pinned_message_id INTEGER")
                        )
                        self.logger.info("Migrated opensong_chat_settings: added last_pinned_message_id column.")
                    if "max_duration_seconds" not in existing_cols:
                        await conn.execute(
                            text("ALTER TABLE opensong_chat_settings ADD COLUMN max_duration_seconds INTEGER DEFAULT 600")
                        )
                        self.logger.info("Migrated opensong_chat_settings: added max_duration_seconds column.")
        except Exception as e:
            self.logger.warning("Migration check error for opensong_chat_settings: %s", e)

        # Ensure default target chat from config if specified
        target_chat = self.module_config.get("target_chat_id")
        if target_chat:
            try:
                await self._get_or_create_settings(int(target_chat))
            except Exception as e:
                self.logger.warning("Failed to initialize target_chat_id %s from config: %s", target_chat, e)

        # Start background scheduler loop
        if not self._scheduler_task or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            self.logger.info("OpenSong background scheduler task started.")

    def on_unload(self):
        self._is_stopping = True
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
        self.downloader.cleanup_temp_dir()

    def _get_chat_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    async def _is_admin(self, update) -> bool:
        """Checks if update user is bot owner or chat admin."""
        user = getattr(update, "from_user", None)
        if not user:
            return False
        if user.id == config.owner or user.username == config.owner:
            return True
        chat = getattr(update, "message", None) and update.message.chat or getattr(update, "chat", None)
        if not chat or chat.type in (enums.ChatType.PRIVATE,):
            return True
        try:
            member = await self.bot.get_chat_member(chat.id, user.id)
            return member.status in (enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR)
        except Exception as e:
            self.logger.warning("Error checking admin status for user %s: %s", user.id, e)
            return False

    async def _get_or_create_settings(self, chat_id: int) -> ChatSettings:
        """Retrieves or creates settings for the given chat."""
        async with self.db.session_maker() as session:
            settings = await session.scalar(select(ChatSettings).where(ChatSettings.chat_id == chat_id))
            if not settings:
                default_genres = self.module_config.get("genres", SUPPORTED_GENRES[:10])
                approver = self.module_config.get("approver_username") or config.owner or "Ultra119"
                interval = int(self.module_config.get("interval_hours", 24))
                pin = bool(self.module_config.get("pin_song", True))
                unpin = bool(self.module_config.get("unpin_previous", True))
                max_dur = int(self.module_config.get("max_duration_seconds", 600))
                approval = bool(self.module_config.get("require_approval", True))

                settings = ChatSettings(
                    chat_id=chat_id,
                    is_enabled=True,
                    interval_hours=interval,
                    pin_song=pin,
                    unpin_previous=unpin,
                    last_pinned_message_id=None,
                    max_duration_seconds=max_dur,
                    require_approval=approval,
                    approver_username=str(approver).lstrip("@"),
                    included_genres=",".join(default_genres),
                    last_sent_at=None,
                    next_run_at=time.time() + (interval * 3600),
                )
                session.add(settings)
                await session.commit()
                await session.refresh(settings)
            else:
                updated = False
                if getattr(settings, "unpin_previous", None) is None:
                    settings.unpin_previous = bool(self.module_config.get("unpin_previous", True))
                    updated = True
                if getattr(settings, "max_duration_seconds", None) is None:
                    settings.max_duration_seconds = int(self.module_config.get("max_duration_seconds", 600))
                    updated = True
                if updated:
                    await session.commit()
                    await session.refresh(settings)
            return settings

    # =========================================================================
    # Keyboard & UI Builders
    # =========================================================================

    def _format_time(self, ts: Optional[float]) -> str:
        if not ts:
            return "N/A"
        try:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "N/A"

    def _build_main_settings_text(self, settings: ChatSettings, chat_title: str) -> str:
        s = self.S["settings"]
        status_text = s["enabled"] if settings.is_enabled else s["disabled"]
        pin_text = s["enabled"] if settings.pin_song else s["disabled"]
        unpin_text = s["enabled"] if getattr(settings, "unpin_previous", True) else s["disabled"]
        approval_text = s["enabled"] if settings.require_approval else s["disabled"]
        approver = settings.approver_username or "None"
        genres = settings.get_genres_list()
        next_run = self._format_time(settings.next_run_at)
        max_duration_min = round((getattr(settings, "max_duration_seconds", 600) or 600) / 60)

        return s["info"].format(
            chat_title=chat_title,
            status_text=status_text,
            interval=settings.interval_hours,
            next_run=next_run,
            pin_text=pin_text,
            unpin_text=unpin_text,
            approval_text=approval_text,
            approver=approver,
            max_duration=max_duration_min,
            genres_count=len(genres),
            total_genres=len(SUPPORTED_GENRES),
        )

    def _build_main_settings_markup(self, settings: ChatSettings) -> InlineKeyboardMarkup:
        s = self.S["settings"]
        chat_id = settings.chat_id
        pin_status = s["enabled_short"] if settings.pin_song else s["disabled_short"]
        unpin_status = s["enabled_short"] if getattr(settings, "unpin_previous", True) else s["disabled_short"]
        auto_status = s["enabled_short"] if settings.is_enabled else s["disabled_short"]
        appr_status = s["enabled_short"] if settings.require_approval else s["disabled_short"]
        approver = settings.approver_username or "None"
        genres_count = len(settings.get_genres_list())
        dur_min = round((getattr(settings, "max_duration_seconds", 600) or 600) / 60)

        buttons = [
            [
                InlineKeyboardButton(
                    s["btn_interval"].format(interval=settings.interval_hours),
                    callback_data=f"os_menu:interval:{chat_id}",
                ),
                InlineKeyboardButton(
                    s["btn_duration"].format(duration=dur_min),
                    callback_data=f"os_toggle:duration:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    s["btn_pin"].format(status=pin_status),
                    callback_data=f"os_toggle:pin:{chat_id}",
                ),
                InlineKeyboardButton(
                    s["btn_unpin_prev"].format(status=unpin_status),
                    callback_data=f"os_toggle:unpin_prev:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    s["btn_approval"].format(status=appr_status),
                    callback_data=f"os_toggle:approval:{chat_id}",
                ),
                InlineKeyboardButton(
                    s["btn_status"].format(status=auto_status),
                    callback_data=f"os_toggle:active:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    s["btn_approver"].format(user=approver),
                    callback_data=f"os_menu:approver:{chat_id}",
                ),
                InlineKeyboardButton(
                    s["btn_genres"].format(count=genres_count),
                    callback_data=f"os_menu:genres:{chat_id}:0",
                ),
            ],
            [
                InlineKeyboardButton(
                    s["btn_top"],
                    callback_data=f"os_action:top:{chat_id}",
                ),
                InlineKeyboardButton(
                    s["btn_send_now"],
                    callback_data=f"os_action:send_now:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    s["btn_refresh"],
                    callback_data=f"os_menu:refresh:{chat_id}",
                ),
                InlineKeyboardButton(
                    s["btn_close"],
                    callback_data=f"os_action:close:{chat_id}",
                ),
            ],
        ]
        return InlineKeyboardMarkup(buttons)

    def _build_interval_markup(self, chat_id: int, current_interval: int) -> InlineKeyboardMarkup:
        s = self.S["settings"]
        suffix = s.get("hours_suffix", "год")
        rows = []
        row = []
        for hours in AVAILABLE_INTERVALS:
            mark = "• " if hours == current_interval else ""
            btn = InlineKeyboardButton(
                f"{mark}{hours} {suffix}", callback_data=f"os_interval:set:{chat_id}:{hours}"
            )
            row.append(btn)
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        rows.append(
            [InlineKeyboardButton(s["btn_back"], callback_data=f"os_menu:main:{chat_id}")]
        )
        return InlineKeyboardMarkup(rows)

    def _build_genres_markup(self, chat_id: int, current_genres: list[str], page: int = 0) -> InlineKeyboardMarkup:
        s = self.S["settings"]
        total_pages = math.ceil(len(SUPPORTED_GENRES) / GENRES_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        start_idx = page * GENRES_PER_PAGE
        page_genres = SUPPORTED_GENRES[start_idx : start_idx + GENRES_PER_PAGE]

        rows = []
        genre_row = []
        for g in page_genres:
            is_enabled = g in current_genres
            mark = "✓ " if is_enabled else ""
            btn = InlineKeyboardButton(
                f"{mark}{g.title()}", callback_data=f"os_genre:toggle:{chat_id}:{g}:{page}"
            )
            genre_row.append(btn)
            if len(genre_row) == 2:
                rows.append(genre_row)
                genre_row = []
        if genre_row:
            rows.append(genre_row)

        # Bulk control row
        rows.append(
            [
                InlineKeyboardButton(
                    s["btn_enable_all"],
                    callback_data=f"os_genre:all:{chat_id}:{page}",
                ),
                InlineKeyboardButton(
                    s["btn_disable_all"],
                    callback_data=f"os_genre:none:{chat_id}:{page}",
                ),
            ]
        )

        # Pagination row
        prev_txt = s.get("prev_btn", "← Назад")
        next_txt = s.get("next_btn", "Далі →")
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(prev_txt, callback_data=f"os_menu:genres:{chat_id}:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="dummy")
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(next_txt, callback_data=f"os_menu:genres:{chat_id}:{page + 1}")
            )
        rows.append(nav_row)

        # Back row
        rows.append(
            [InlineKeyboardButton(s["btn_back"], callback_data=f"os_menu:main:{chat_id}")]
        )
        return InlineKeyboardMarkup(rows)

    def _build_approver_markup(self, chat_id: int) -> InlineKeyboardMarkup:
        s = self.S["settings"]
        buttons = [
            [
                InlineKeyboardButton(
                    s["btn_set_me"],
                    callback_data=f"os_approver:set_me:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    s["btn_reset_default"],
                    callback_data=f"os_approver:reset:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    s["btn_back"],
                    callback_data=f"os_menu:main:{chat_id}",
                )
            ],
        ]
        return InlineKeyboardMarkup(buttons)

    # =========================================================================
    # Commands
    # =========================================================================

    @allowed_for(["owner", "chat_admins"])
    @command(["opensong", "opensong_settings", "song_settings"])
    async def settings_cmd(self, client: Client, message: Message):
        """Opens interactive OpenSong settings panel for current chat."""
        chat_id = message.chat.id
        chat_title = message.chat.title or message.chat.first_name or f"Chat {chat_id}"
        settings = await self._get_or_create_settings(chat_id)
        text = self._build_main_settings_text(settings, chat_title)
        markup = self._build_main_settings_markup(settings)
        await message.reply(text, reply_markup=markup, disable_web_page_preview=True)

    @allowed_for(["owner", "chat_admins"])
    @command(["opensong_send", "song_send"])
    async def send_now_cmd(self, client: Client, message: Message):
        """Immediately triggers searching and sending a random song."""
        chat_id = message.chat.id
        status_msg = await message.reply(self.S["actions"]["searching"])
        asyncio.create_task(self._handle_manual_send(chat_id, status_msg, message.from_user.id))

    async def _get_top_songs_text(self, chat_id: int, chat_title: str) -> str:
        score_expr = func.coalesce(func.sum(case((SongVote.vote == "like", 1), else_=0)), 0) - func.coalesce(
            func.sum(case((SongVote.vote == "dislike", 1), else_=0)), 0
        )
        async with self.db.session_maker() as session:
            stmt = (
                select(
                    SongPost,
                    func.coalesce(func.sum(case((SongVote.vote == "like", 1), else_=0)), 0).label("likes"),
                    func.coalesce(func.sum(case((SongVote.vote == "dislike", 1), else_=0)), 0).label("dislikes"),
                )
                .join(SongVote, SongPost.id == SongVote.post_id)
                .where(SongPost.chat_id == chat_id)
                .group_by(SongPost.id)
                .order_by(
                    score_expr.desc(),
                    func.coalesce(func.sum(case((SongVote.vote == "like", 1), else_=0)), 0).desc(),
                )
                .limit(10)
            )
            rows = (await session.execute(stmt)).all()

        if not rows:
            return self.S["top"]["empty"]

        lines = [self.S["top"]["header"].format(chat_title=chat_title)]
        for rank, (post, likes, dislikes) in enumerate(rows, start=1):
            score = (likes or 0) - (dislikes or 0)
            score_str = f"+{score}" if score > 0 else str(score)
            sp_url = post.spotify_url or "https://open.spotify.com"
            lines.append(
                self.S["top"]["item"].format(
                    rank=rank,
                    title=post.title,
                    artist=post.artist,
                    score=score_str,
                    likes=likes or 0,
                    dislikes=dislikes or 0,
                    spotify_url=sp_url,
                ).rstrip()
            )
        return "\n".join(lines)

    @command(["opensong_top", "song_top", "top_songs"])
    async def top_songs_cmd(self, client: Client, message: Message):
        """Displays top voted songs in current chat."""
        chat_id = message.chat.id
        chat_title = message.chat.title or message.chat.first_name or f"Chat {chat_id}"
        text = await self._get_top_songs_text(chat_id, chat_title)
        await message.reply(text, disable_web_page_preview=True)

    @allowed_for(["owner", "chat_admins"])
    @command("opensong_approver")
    async def set_approver_cmd(self, client: Client, message: Message):
        """Changes the approver username for the current chat: /opensong_approver @username"""
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(self.S["actions"]["approver_invalid"])
            return

        new_approver = parts[1].strip().lstrip("@")
        if not new_approver:
            await message.reply(self.S["actions"]["approver_invalid"])
            return

        async with self.db.session_maker() as session:
            settings = await session.scalar(select(ChatSettings).where(ChatSettings.chat_id == message.chat.id))
            if not settings:
                settings = await self._get_or_create_settings(message.chat.id)
            settings.approver_username = new_approver
            await session.commit()

        await message.reply(self.S["actions"]["approver_updated"].format(user=new_approver))

    # =========================================================================
    # Settings Callback Query Handlers
    # =========================================================================

    @callback_query(filters.regex(r"^dummy$"))
    async def dummy_callback(self, client: Client, call: CallbackQuery):
        await call.answer()

    @callback_query(filters.regex(r"^os_action:close:(-?\d+)$"))
    async def handle_close(self, client: Client, call: CallbackQuery):
        if not await self._is_admin(call):
            await call.answer(self.S["settings"]["no_permission"], show_alert=True)
            return
        try:
            await call.message.delete()
        except Exception:
            try:
                await call.message.edit_text(self.S["settings"]["closed"])
            except Exception:
                pass
        await call.answer()

    @callback_query(filters.regex(r"^os_menu:(main|interval|genres|approver|refresh):(-?\d+)(?::(\d+))?$"))
    async def handle_menu_navigation(self, client: Client, call: CallbackQuery):
        if not await self._is_admin(call):
            await call.answer(self.S["settings"]["no_permission"], show_alert=True)
            return

        parts = call.data.split(":")
        menu_name = parts[1]
        chat_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 and parts[3] else 0

        settings = await self._get_or_create_settings(chat_id)
        chat_title = (
            call.message.chat.title
            or call.message.chat.first_name
            or f"Chat {chat_id}"
        )

        try:
            if menu_name in ("main", "refresh"):
                text = self._build_main_settings_text(settings, chat_title)
                markup = self._build_main_settings_markup(settings)
                await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
                if menu_name == "refresh":
                    await call.answer(self.S["settings"]["saved"])
                else:
                    await call.answer()

            elif menu_name == "interval":
                text = self.S["settings"]["interval_header"].format(interval=settings.interval_hours)
                markup = self._build_interval_markup(chat_id, settings.interval_hours)
                await call.message.edit_text(text, reply_markup=markup)
                await call.answer()

            elif menu_name == "genres":
                genres = settings.get_genres_list()
                text = self.S["settings"]["genres_header"].format(
                    enabled_count=len(genres), total_count=len(SUPPORTED_GENRES)
                )
                markup = self._build_genres_markup(chat_id, genres, page=page)
                await call.message.edit_text(text, reply_markup=markup)
                await call.answer()

            elif menu_name == "approver":
                approver = settings.approver_username or "None"
                text = self.S["settings"]["approver_header"].format(approver=approver)
                markup = self._build_approver_markup(chat_id)
                await call.message.edit_text(text, reply_markup=markup)
                await call.answer()

        except errors.MessageNotModified:
            await call.answer(self.S["settings"]["saved"])
        except Exception as e:
            self.logger.error("Error in menu navigation: %s", e)
            await call.answer("Error updating menu")

    @callback_query(filters.regex(r"^os_action:top:(-?\d+)$"))
    async def handle_top_action(self, client: Client, call: CallbackQuery):
        chat_id = int(call.data.split(":")[2])
        chat_title = call.message.chat.title or call.message.chat.first_name or f"Chat {chat_id}"
        text = await self._get_top_songs_text(chat_id, chat_title)
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(self.S["settings"]["btn_back"], callback_data=f"os_menu:main:{chat_id}")]]
        )
        try:
            await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except errors.MessageNotModified:
            pass
        await call.answer()

    @callback_query(filters.regex(r"^os_toggle:(pin|unpin_prev|duration|active|approval):(-?\d+)$"))
    async def handle_toggle(self, client: Client, call: CallbackQuery):
        if not await self._is_admin(call):
            await call.answer(self.S["settings"]["no_permission"], show_alert=True)
            return

        parts = call.data.split(":")
        target = parts[1]
        chat_id = int(parts[2])

        async with self.db.session_maker() as session:
            settings = await session.scalar(select(ChatSettings).where(ChatSettings.chat_id == chat_id))
            if not settings:
                settings = await self._get_or_create_settings(chat_id)

            if target == "pin":
                settings.pin_song = not settings.pin_song
            elif target == "unpin_prev":
                settings.unpin_previous = not getattr(settings, "unpin_previous", True)
            elif target == "duration":
                cur_dur = getattr(settings, "max_duration_seconds", 600) or 600
                if cur_dur in AVAILABLE_DURATIONS:
                    idx = AVAILABLE_DURATIONS.index(cur_dur)
                    next_dur = AVAILABLE_DURATIONS[(idx + 1) % len(AVAILABLE_DURATIONS)]
                else:
                    next_dur = AVAILABLE_DURATIONS[0]
                settings.max_duration_seconds = next_dur
            elif target == "active":
                settings.is_enabled = not settings.is_enabled
                if settings.is_enabled and (not settings.next_run_at or settings.next_run_at < time.time()):
                    settings.next_run_at = time.time() + (settings.interval_hours * 3600)
            elif target == "approval":
                settings.require_approval = not settings.require_approval

            await session.commit()
            await session.refresh(settings)

        chat_title = call.message.chat.title or call.message.chat.first_name or f"Chat {chat_id}"
        text = self._build_main_settings_text(settings, chat_title)
        markup = self._build_main_settings_markup(settings)
        try:
            await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except errors.MessageNotModified:
            pass
        await call.answer(self.S["settings"]["saved"])

    @callback_query(filters.regex(r"^os_interval:set:(-?\d+):(\d+)$"))
    async def handle_set_interval(self, client: Client, call: CallbackQuery):
        if not await self._is_admin(call):
            await call.answer(self.S["settings"]["no_permission"], show_alert=True)
            return

        parts = call.data.split(":")
        chat_id = int(parts[2])
        new_hours = int(parts[3])

        async with self.db.session_maker() as session:
            settings = await session.scalar(select(ChatSettings).where(ChatSettings.chat_id == chat_id))
            if not settings:
                settings = await self._get_or_create_settings(chat_id)

            settings.interval_hours = new_hours
            settings.next_run_at = time.time() + (new_hours * 3600)
            await session.commit()
            await session.refresh(settings)

        chat_title = call.message.chat.title or call.message.chat.first_name or f"Chat {chat_id}"
        text = self._build_main_settings_text(settings, chat_title)
        markup = self._build_main_settings_markup(settings)
        try:
            await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except errors.MessageNotModified:
            pass
        await call.answer(self.S["settings"]["saved"])

    @callback_query(filters.regex(r"^os_genre:(toggle|all|none):(-?\d+):([^:]+):(\d+)$"))
    async def handle_genre_toggle(self, client: Client, call: CallbackQuery):
        if not await self._is_admin(call):
            await call.answer(self.S["settings"]["no_permission"], show_alert=True)
            return

        parts = call.data.split(":")
        action = parts[1]
        chat_id = int(parts[2])
        genre = parts[3]
        page = int(parts[4])

        async with self.db.session_maker() as session:
            settings = await session.scalar(select(ChatSettings).where(ChatSettings.chat_id == chat_id))
            if not settings:
                settings = await self._get_or_create_settings(chat_id)

            current_genres = set(settings.get_genres_list())

            if action == "toggle":
                if genre in current_genres:
                    if len(current_genres) <= 1:
                        await call.answer(self.S["settings"]["min_genres_error"], show_alert=True)
                        return
                    current_genres.remove(genre)
                else:
                    current_genres.add(genre)
            elif action == "all":
                current_genres = set(SUPPORTED_GENRES)
            elif action == "none":
                # Keep at least the default pop genre so it's not empty
                current_genres = {"pop"}
                await call.answer(self.S["settings"]["min_genres_error"], show_alert=True)

            settings.set_genres_list(list(current_genres))
            await session.commit()
            await session.refresh(settings)

        updated_list = settings.get_genres_list()
        text = self.S["settings"]["genres_header"].format(
            enabled_count=len(updated_list), total_count=len(SUPPORTED_GENRES)
        )
        markup = self._build_genres_markup(chat_id, updated_list, page=page)
        try:
            await call.message.edit_text(text, reply_markup=markup)
        except errors.MessageNotModified:
            pass
        await call.answer()

    @callback_query(filters.regex(r"^os_approver:(set_me|reset):(-?\d+)$"))
    async def handle_approver_setting(self, client: Client, call: CallbackQuery):
        if not await self._is_admin(call):
            await call.answer(self.S["settings"]["no_permission"], show_alert=True)
            return

        action = call.data.split(":")[1]
        chat_id = int(call.data.split(":")[2])

        async with self.db.session_maker() as session:
            settings = await session.scalar(select(ChatSettings).where(ChatSettings.chat_id == chat_id))
            if not settings:
                settings = await self._get_or_create_settings(chat_id)

            if action == "set_me":
                new_username = call.from_user.username
                if not new_username:
                    await call.answer(
                        "⚠️ You do not have a public @username set on your Telegram account!",
                        show_alert=True,
                    )
                    return
                settings.approver_username = new_username.lstrip("@")
            elif action == "reset":
                default_user = (
                    self.module_config.get("approver_username") or config.owner or "Ultra119"
                )
                settings.approver_username = str(default_user).lstrip("@")

            await session.commit()
            await session.refresh(settings)

        approver = settings.approver_username or "None"
        text = self.S["settings"]["approver_header"].format(approver=approver)
        markup = self._build_approver_markup(chat_id)
        try:
            await call.message.edit_text(text, reply_markup=markup)
        except errors.MessageNotModified:
            pass
        await call.answer(self.S["settings"]["saved"])

    @callback_query(filters.regex(r"^os_action:send_now:(-?\d+)$"))
    async def handle_send_now_action(self, client: Client, call: CallbackQuery):
        if not await self._is_admin(call):
            await call.answer(self.S["settings"]["no_permission"], show_alert=True)
            return

        chat_id = int(call.data.split(":")[2])
        await call.answer(self.S["actions"]["searching"])
        status_msg = await call.message.reply(self.S["actions"]["searching"])
        asyncio.create_task(self._handle_manual_send(chat_id, status_msg, call.from_user.id))

    async def _handle_manual_send(self, chat_id: int, status_msg: Message, requester_id: int):
        success, info = await self.process_song_send(chat_id)
        if success:
            if info == "approval_sent":
                settings = await self._get_or_create_settings(chat_id)
                await status_msg.edit_text(
                    self.S["actions"]["sent_approval"].format(approver=settings.approver_username)
                )
            else:
                await status_msg.edit_text(self.S["actions"]["sent_success"])
        else:
            await status_msg.edit_text(self.S["actions"]["send_error"])

    # =========================================================================
    # Voting Flow (Like / Dislike)
    # =========================================================================

    def _build_vote_markup(self, post_id: int, likes: int, dislikes: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"👍 {likes}", callback_data=f"os_vote:like:{post_id}"),
                    InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"os_vote:dislike:{post_id}"),
                ]
            ]
        )

    @callback_query(filters.regex(r"^os_vote:(like|dislike):(\d+)$"))
    async def handle_vote(self, client: Client, call: CallbackQuery):
        parts = call.data.split(":")
        vote_type = parts[1]
        post_id = int(parts[2])
        user_id = call.from_user.id

        async with self.db.session_maker() as session:
            # Check post exists
            post = await session.scalar(select(SongPost).where(SongPost.id == post_id))
            if not post:
                await call.answer("Song post not found in database.", show_alert=True)
                return

            existing_vote = await session.scalar(
                select(SongVote).where(SongVote.post_id == post_id, SongVote.user_id == user_id)
            )

            feedback_text = ""
            if existing_vote:
                if existing_vote.vote == vote_type:
                    # Toggle off
                    await session.delete(existing_vote)
                    feedback_text = self.S["song_message"]["vote_removed"]
                else:
                    # Switch vote
                    existing_vote.vote = vote_type
                    existing_vote.voted_at = time.time()
                    feedback_text = self.S["song_message"]["vote_switched"].format(
                        vote="👍" if vote_type == "like" else "👎"
                    )
            else:
                new_vote = SongVote(
                    post_id=post_id,
                    user_id=user_id,
                    vote=vote_type,
                    voted_at=time.time(),
                )
                session.add(new_vote)
                feedback_text = (
                    self.S["song_message"]["vote_liked"]
                    if vote_type == "like"
                    else self.S["song_message"]["vote_disliked"]
                )

            await session.commit()

            # Count total votes
            likes_count = await session.scalar(
                select(func.count(SongVote.id)).where(SongVote.post_id == post_id, SongVote.vote == "like")
            ) or 0
            dislikes_count = await session.scalar(
                select(func.count(SongVote.id)).where(SongVote.post_id == post_id, SongVote.vote == "dislike")
            ) or 0

        # Update message buttons
        markup = self._build_vote_markup(post_id, likes_count, dislikes_count)
        try:
            await call.message.edit_reply_markup(reply_markup=markup)
        except errors.MessageNotModified:
            pass
        except Exception as e:
            self.logger.warning("Error updating vote markup: %s", e)

        await call.answer(feedback_text)

    # =========================================================================
    # Approval Flow in Bot DM
    # =========================================================================

    def _build_approval_markup(self, approval_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        self.S["approval"]["btn_approve"],
                        callback_data=f"os_appr:approve:{approval_id}",
                    ),
                    InlineKeyboardButton(
                        self.S["approval"]["btn_reject"],
                        callback_data=f"os_appr:reject:{approval_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        self.S["approval"]["btn_reroll"],
                        callback_data=f"os_appr:reroll:{approval_id}",
                    )
                ],
            ]
        )

    @callback_query(filters.regex(r"^os_appr:(approve|reject|reroll):(\d+)$"))
    async def handle_approval_callback(self, client: Client, call: CallbackQuery):
        parts = call.data.split(":")
        action = parts[1]
        approval_id = int(parts[2])

        async with self.db.session_maker() as session:
            pending = await session.scalar(
                select(PendingApproval).where(PendingApproval.id == approval_id)
            )
            if not pending or pending.status != "pending":
                await call.answer("Approval request already resolved or expired.", show_alert=True)
                return

            # Verify that clicker is the designated approver or bot owner
            user = call.from_user
            is_authorized = (
                user.id == config.owner
                or (user.username and user.username.lower() == str(config.owner).lower())
                or (pending.approver_username and user.username and user.username.lower() == pending.approver_username.lower())
            )

            if not is_authorized:
                await call.answer(
                    self.S["approval"]["not_approver"].format(approver=pending.approver_username),
                    show_alert=True,
                )
                return

            chat_id = pending.chat_id
            target_chat = None
            try:
                target_chat = await client.get_chat(chat_id)
            except Exception:
                pass
            chat_title = target_chat.title if target_chat else f"Chat {chat_id}"

            if action == "approve":
                pending.status = "approved"
                await session.commit()
                await call.answer()

                # Publish the song to target chat
                await self._publish_song_to_chat(
                    chat_id=chat_id,
                    title=pending.title,
                    artist=pending.artist,
                    genre=pending.genre,
                    spotify_url=pending.spotify_url or "",
                    duration=pending.duration,
                    audio_path=pending.audio_path,
                    cover_path=pending.cover_path,
                )

                # Edit DM message
                approver_name = user.username or user.first_name
                await call.message.edit_text(
                    self.S["approval"]["approved"].format(user=approver_name, chat_title=chat_title)
                )

                # Clean up files
                self.downloader.cleanup_files(pending.audio_path, pending.cover_path)

            elif action == "reject":
                pending.status = "rejected"
                await session.commit()
                await call.answer()

                approver_name = user.username or user.first_name
                await call.message.edit_text(
                    self.S["approval"]["rejected"].format(user=approver_name)
                )
                self.downloader.cleanup_files(pending.audio_path, pending.cover_path)

            elif action == "reroll":
                pending.status = "rerolled"
                await session.commit()
                await call.answer()

                await call.message.edit_text(self.S["approval"]["rerolling"])
                self.downloader.cleanup_files(pending.audio_path, pending.cover_path)

                # Fetch and prepare a new song for approval
                asyncio.create_task(self.process_song_send(chat_id))

    # =========================================================================
    # Core Flow: Song Fetch, Download & Send
    # =========================================================================

    async def _publish_song_to_chat(
        self,
        chat_id: int,
        title: str,
        artist: str,
        genre: str,
        spotify_url: str,
        duration: int,
        audio_path: str,
        cover_path: Optional[str] = None,
    ) -> Optional[Message]:
        """Publishes the song audio file to the destination chat with Like/Dislike voting."""
        caption = self.S["song_message"]["caption"].format(
            title=title,
            artist=artist,
            genre=genre,
            spotify_url=spotify_url or "https://open.spotify.com",
        )

        # Temporary dummy keyboard, will update with post_id
        initial_markup = self._build_vote_markup(0, 0, 0)

        sent_msg = None
        try:
            sent_msg = await self.bot.send_audio(
                chat_id=chat_id,
                audio=audio_path,
                caption=caption,
                duration=duration,
                performer=artist,
                title=title,
                thumb=cover_path if cover_path and os.path.exists(cover_path) else None,
                reply_markup=initial_markup,
            )
        except Exception as e:
            self.logger.error("Failed to send audio to chat %s: %s", chat_id, e)
            return None

        # Record post in DB
        async with self.db.session_maker() as session:
            post = SongPost(
                chat_id=chat_id,
                message_id=sent_msg.id,
                title=title,
                artist=artist,
                genre=genre,
                spotify_url=spotify_url,
                posted_at=time.time(),
            )
            session.add(post)
            await session.commit()
            await session.refresh(post)

            # Update reply markup with correct post_id
            real_markup = self._build_vote_markup(post.id, 0, 0)
            try:
                await sent_msg.edit_reply_markup(reply_markup=real_markup)
            except Exception as e:
                self.logger.warning("Failed to update post reply markup: %s", e)

        # Pin message if enabled and unpin previous
        async with self.db.session_maker() as session:
            settings = await session.scalar(select(ChatSettings).where(ChatSettings.chat_id == chat_id))
            if settings:
                if settings.pin_song:
                    if getattr(settings, "unpin_previous", True) and settings.last_pinned_message_id:
                        try:
                            await self.bot.unpin_chat_message(chat_id, settings.last_pinned_message_id)
                            self.logger.info(
                                "Unpinned previous song message %s in chat %s",
                                settings.last_pinned_message_id,
                                chat_id,
                            )
                        except Exception as unpin_err:
                            self.logger.warning("Could not unpin previous song message in %s: %s", chat_id, unpin_err)

                    try:
                        await self.bot.pin_chat_message(chat_id, sent_msg.id, disable_notification=True)
                        settings.last_pinned_message_id = sent_msg.id
                        self.logger.info("Pinned song message %s in chat %s", sent_msg.id, chat_id)
                    except Exception as pin_err:
                        self.logger.warning("Could not pin song message in %s: %s", chat_id, pin_err)

                # Restart the 24h cycle countdown from this publication moment
                settings.last_sent_at = time.time()
                settings.next_run_at = time.time() + (settings.interval_hours * 3600)
                await session.commit()

        return sent_msg

    async def process_song_send(self, chat_id: int) -> Tuple[bool, str]:
        """
        Full workflow for a chat:
        1. Fetch random Spotify track based on chat's enabled genres
        2. Download audio from YouTube via yt-dlp
        3. If DM approval enabled: send approval prompt to approver's DM
           Otherwise: post directly to chat with Like/Dislike buttons.
        """
        lock = self._get_chat_lock(chat_id)
        if lock.locked():
            self.logger.warning("Song send already in progress for chat %s, skipping.", chat_id)
            return False, "in_progress"

        async with lock:
            settings = await self._get_or_create_settings(chat_id)
            genres = settings.get_genres_list()
            max_duration = getattr(settings, "max_duration_seconds", 600) or 600

            # Query recently posted songs in this chat (last 60 days) to avoid repeats
            exclude_titles = set()
            try:
                async with self.db.session_maker() as session:
                    cutoff = time.time() - (60 * 86400)
                    recent_posts = await session.scalars(
                        select(SongPost.title).where(
                            SongPost.chat_id == chat_id, SongPost.posted_at >= cutoff
                        )
                    )
                    exclude_titles = {t.lower().strip() for t in recent_posts.all() if t}
            except Exception as e:
                self.logger.warning("Could not fetch recent post history for duplicate filtering: %s", e)

            # 1. Search in Spotify with history exclusion and max_duration limit
            track_info: Optional[TrackInfo] = await self.spotify.get_random_song(
                genres, exclude_titles=exclude_titles, max_duration=max_duration
            )
            if not track_info:
                self.logger.error("Could not obtain any song for genres: %s", genres)
                return False, "spotify_failed"

            self.logger.info(
                "Selected song for chat %s: '%s' by '%s' [%s]",
                chat_id,
                track_info.title,
                track_info.artist,
                track_info.genre,
            )

            # 2. Download from YouTube with max_duration limit
            downloaded: Optional[DownloadedTrack] = await self.downloader.download_track(
                title=track_info.title,
                artist=track_info.artist,
                cover_url=track_info.cover_url,
                max_duration=max_duration,
            )
            if not downloaded or not os.path.exists(downloaded.audio_path):
                self.logger.error("Failed to download YouTube audio for %s - %s", track_info.artist, track_info.title)
                return False, "download_failed"

            duration = downloaded.duration or track_info.duration

            # 3. Check Approval requirement
            if settings.require_approval:
                approver_username = (
                    settings.approver_username
                    or self.module_config.get("approver_username")
                    or config.owner
                    or "Ultra119"
                )
                approver_username = str(approver_username).lstrip("@")

                # Resolve approver user
                approver_user = None
                try:
                    approver_user = await self.bot.get_users(approver_username)
                except Exception as e:
                    self.logger.warning("Could not resolve approver @%s: %s", approver_username, e)

                if approver_user:
                    # Save pending approval
                    async with self.db.session_maker() as session:
                        pending = PendingApproval(
                            chat_id=chat_id,
                            approver_username=approver_username,
                            title=track_info.title,
                            artist=track_info.artist,
                            genre=track_info.genre,
                            spotify_url=track_info.spotify_url,
                            duration=duration,
                            audio_path=downloaded.audio_path,
                            cover_path=downloaded.cover_path,
                            status="pending",
                            created_at=time.time(),
                        )
                        session.add(pending)
                        await session.commit()
                        await session.refresh(pending)
                        pending_id = pending.id

                    # Get chat title
                    target_chat = None
                    try:
                        target_chat = await self.bot.get_chat(chat_id)
                    except Exception:
                        pass
                    chat_title = target_chat.title if target_chat else f"Chat {chat_id}"

                    approval_caption = self.S["approval"]["caption"].format(
                        title=track_info.title,
                        artist=track_info.artist,
                        genre=track_info.genre,
                        chat_title=chat_title,
                        spotify_url=track_info.spotify_url or "https://open.spotify.com",
                    )
                    approval_markup = self._build_approval_markup(pending_id)

                    try:
                        dm_msg = await self.bot.send_audio(
                            chat_id=approver_user.id,
                            audio=downloaded.audio_path,
                            caption=approval_caption,
                            duration=duration,
                            performer=track_info.artist,
                            title=track_info.title,
                            thumb=downloaded.cover_path if downloaded.cover_path and os.path.exists(downloaded.cover_path) else None,
                            reply_markup=approval_markup,
                        )

                        async with self.db.session_maker() as session:
                            p = await session.scalar(select(PendingApproval).where(PendingApproval.id == pending_id))
                            if p:
                                p.dm_message_id = dm_msg.id
                                await session.commit()

                        self.logger.info("Sent approval request to @%s (ID: %s)", approver_username, approver_user.id)
                        return True, "approval_sent"

                    except (errors.UserIsBlocked, errors.PeerIdInvalid, errors.RPCError) as dm_err:
                        self.logger.warning(
                            "Cannot DM approver @%s (%s). Falling back to direct post: %s",
                            approver_username,
                            approver_user.id,
                            dm_err,
                        )

            # Direct publish (if approval disabled or approver could not be reached via DM)
            sent = await self._publish_song_to_chat(
                chat_id=chat_id,
                title=track_info.title,
                artist=track_info.artist,
                genre=track_info.genre,
                spotify_url=track_info.spotify_url,
                duration=duration,
                audio_path=downloaded.audio_path,
                cover_path=downloaded.cover_path,
            )

            # Cleanup audio file
            self.downloader.cleanup_files(downloaded.audio_path, downloaded.cover_path)
            return bool(sent), "sent"

    # =========================================================================
    # Scheduler Loop
    # =========================================================================

    async def _scheduler_loop(self):
        """Background loop running every 60 seconds checking for due song sends."""
        self.logger.info("OpenSong scheduler loop started.")
        while not self._is_stopping:
            try:
                await self._check_due_chats()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Exception in OpenSong scheduler loop: %s", e, exc_info=True)

            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

    async def _check_due_chats(self):
        now = time.time()
        async with self.db.session_maker() as session:
            due_chats = (
                await session.scalars(
                    select(ChatSettings).where(
                        ChatSettings.is_enabled == True,
                        ChatSettings.next_run_at != None,
                        ChatSettings.next_run_at <= now,
                    )
                )
            ).all()

        for chat in due_chats:
            chat_id = chat.chat_id
            self.logger.info("Chat %s is due for a new song!", chat_id)
            asyncio.create_task(self._run_scheduled_chat_send(chat_id))

    async def _run_scheduled_chat_send(self, chat_id: int):
        try:
            await self.process_song_send(chat_id)
        except Exception as e:
            self.logger.error("Error executing scheduled song send for chat %s: %s", chat_id, e, exc_info=True)
        finally:
            async with self.db.session_maker() as session:
                settings = await session.scalar(select(ChatSettings).where(ChatSettings.chat_id == chat_id))
                if settings:
                    settings.last_sent_at = time.time()
                    settings.next_run_at = time.time() + (settings.interval_hours * 3600)
                    await session.commit()

