import asyncio
import os
import sqlite3
import re
import socket
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
AFK_REVIEW_CHANNEL_ID = int(os.getenv("AFK_REVIEW_CHANNEL_ID", "0"))
AFK_LIST_CHANNEL_ID = int(os.getenv("AFK_LIST_CHANNEL_ID", "1469722349975896095"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "afk_reports.db")


def parse_moderator_role_ids() -> set[int]:
    role_ids_raw = os.getenv("MODERATOR_ROLE_IDS", "").strip()
    if role_ids_raw:
        parsed_ids = set()
        for chunk in role_ids_raw.split(","):
            chunk = chunk.strip()
            if chunk:
                parsed_ids.add(int(chunk))
        return parsed_ids

    single_role_raw = os.getenv("MODERATOR_ROLE_ID", "0").strip()
    if not single_role_raw or single_role_raw == "0":
        return set()
    return {int(single_role_raw)}


MODERATOR_ROLE_IDS = parse_moderator_role_ids()


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing in environment variables.")

if not GUILD_ID or not AFK_REVIEW_CHANNEL_ID or not AFK_LIST_CHANNEL_ID:
    raise RuntimeError("GUILD_ID, AFK_REVIEW_CHANNEL_ID and AFK_LIST_CHANNEL_ID are required.")


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
db_lock = asyncio.Lock()
MSK = timezone(timedelta(hours=3))
instance_lock_socket: Optional[socket.socket] = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def msk_now() -> datetime:
    return datetime.now(MSK)


def acquire_instance_lock() -> None:
    global instance_lock_socket
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 39477))
    except OSError as exc:
        raise RuntimeError("Уже запущен другой экземпляр бота. Оставь только один процесс.") from exc
    instance_lock_socket = lock


def parse_afk_duration(duration_text: str) -> Optional[timedelta]:
    cleaned = duration_text.strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d+)([чhмmдd])", cleaned)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)
    if value <= 0:
        return None

    if unit in ("ч", "h"):
        return timedelta(hours=value)
    if unit in ("м", "m"):
        return timedelta(minutes=value)
    if unit in ("д", "d"):
        return timedelta(days=value)
    return None


def init_db() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS afk_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_tag TEXT NOT NULL,
                reason TEXT NOT NULL,
                until_text TEXT NOT NULL,
                until_at TEXT,
                game_nick TEXT,
                static_code TEXT,
                duration_text TEXT,
                comment TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by_id INTEGER,
                reviewed_by_tag TEXT,
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            )
            """
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(afk_reports)").fetchall()
        }
        if "until_at" not in columns:
            conn.execute("ALTER TABLE afk_reports ADD COLUMN until_at TEXT")
        if "game_nick" not in columns:
            conn.execute("ALTER TABLE afk_reports ADD COLUMN game_nick TEXT")
        if "static_code" not in columns:
            conn.execute("ALTER TABLE afk_reports ADD COLUMN static_code TEXT")
        if "duration_text" not in columns:
            conn.execute("ALTER TABLE afk_reports ADD COLUMN duration_text TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.commit()


def create_embed(
    report_id: int,
    user_tag: str,
    user_id: int,
    reason: str,
    until_text: str,
    game_nick: Optional[str],
    static_code: Optional[str],
    duration_text: Optional[str],
    status: str,
    reviewed_by_id: Optional[int] = None,
    reviewer_tag: Optional[str] = None,
) -> discord.Embed:
    color_map = {
        "pending": discord.Color.orange(),
        "approved": discord.Color.green(),
        "rejected": discord.Color.red(),
    }
    status_map = {
        "pending": "Ожидает решения",
        "approved": "Принято",
        "rejected": "Отклонено",
    }
    reviewer_field_map = {
        "approved": "Принял",
        "rejected": "Отклонил",
    }

    embed = discord.Embed(color=color_map.get(status, discord.Color.blurple()))
    embed.set_author(name=f"AFK-заявка #{report_id}")
    embed.add_field(name="Ваш ник в игре", value=game_nick or "—", inline=False)
    embed.add_field(name="Статик #", value=static_code or "—", inline=False)
    embed.add_field(name="Время афк", value=duration_text or "—", inline=False)
    embed.add_field(name="Причина", value=reason, inline=False)
    embed.add_field(name="Пользователь", value=f"<@{user_id}>", inline=False)
    embed.add_field(name="Username", value=user_tag, inline=False)
    embed.add_field(name="ID", value=str(user_id), inline=False)
    embed.add_field(name="Кого", value=f"<@{user_id}>", inline=True)
    embed.add_field(
        name=reviewer_field_map.get(status, "Рассмотрел"),
        value=(f"<@{reviewed_by_id}>" if reviewed_by_id else "—"),
        inline=True,
    )
    embed.add_field(name="Статус", value=status_map.get(status, status), inline=False)
    if reviewer_tag and status != "pending":
        embed.add_field(name="Проверил", value=reviewer_tag, inline=False)
    embed.set_footer(text=f"До: {until_text}")
    return embed


def get_report(report_id: int) -> Optional[sqlite3.Row]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM afk_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        return row


def insert_report(
    guild_id: int,
    user_id: int,
    user_tag: str,
    reason: str,
    until_text: str,
    game_nick: str,
    static_code: str,
    duration_text: str,
    comment: str,
    until_at: Optional[str],
) -> int:
    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO afk_reports (
                guild_id, user_id, user_tag, reason, until_text, until_at, game_nick, static_code, duration_text, comment, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                user_id,
                user_tag,
                reason,
                until_text,
                until_at,
                game_nick,
                static_code,
                duration_text,
                comment,
                utc_now().isoformat(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def update_report_status(report_id: int, status: str, reviewer_id: int, reviewer_tag: str) -> bool:
    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            """
            UPDATE afk_reports
            SET status = ?, reviewed_by_id = ?, reviewed_by_tag = ?, reviewed_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                status,
                reviewer_id,
                reviewer_tag,
                utc_now().isoformat(),
                report_id,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1


def get_active_approved_reports(guild_id: int) -> list[sqlite3.Row]:
    now_iso = utc_now().isoformat()
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, user_tag, reason, until_text, until_at, reviewed_by_tag, game_nick, static_code
            FROM afk_reports
            WHERE guild_id = ?
              AND status = 'approved'
              AND until_at IS NOT NULL
              AND until_at > ?
            ORDER BY until_at ASC
            """,
            (guild_id, now_iso),
        ).fetchall()
        return rows


def has_recent_duplicate(guild_id: int, user_id: int, game_nick: str, static_code: str, duration_text: str, reason: str) -> bool:
    threshold_iso = (utc_now() - timedelta(seconds=15)).isoformat()
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM afk_reports
            WHERE guild_id = ?
              AND user_id = ?
              AND game_nick = ?
              AND static_code = ?
              AND duration_text = ?
              AND reason = ?
              AND created_at > ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (guild_id, user_id, game_nick, static_code, duration_text, reason, threshold_iso),
        ).fetchone()
        return row is not None


def get_bot_state(key: str) -> Optional[str]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def set_bot_state(key: str, value: str) -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def build_afk_list_embed(rows: list[sqlite3.Row]) -> discord.Embed:
    embed = discord.Embed(
        title="Активные AFK",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow(),
    )
    if not rows:
        embed.description = "Список пуст."
        return embed

    lines = [
        f"#{row['id']} | {row['game_nick'] or row['user_tag']} | статик: {row['static_code'] or '—'} | до: {row['until_text']}"
        for row in rows[:25]
    ]
    embed.description = "\n".join(lines)
    if len(rows) > 25:
        embed.set_footer(text=f"Показано 25 из {len(rows)}")
    return embed


async def refresh_afk_list_message() -> None:
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    channel = guild.get_channel(AFK_LIST_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    async with db_lock:
        rows = get_active_approved_reports(GUILD_ID)
        list_message_id_raw = get_bot_state("afk_list_message_id")

    embed = build_afk_list_embed(rows)
    target_message: Optional[discord.Message] = None
    if list_message_id_raw and list_message_id_raw.isdigit():
        try:
            target_message = await channel.fetch_message(int(list_message_id_raw))
        except discord.NotFound:
            target_message = None
        except discord.Forbidden:
            return
        except discord.HTTPException:
            return

    if target_message is None:
        try:
            target_message = await channel.send(embed=embed, view=AFKListView())
        except discord.Forbidden:
            return
        except discord.HTTPException:
            return

        async with db_lock:
            set_bot_state("afk_list_message_id", str(target_message.id))
        return

    try:
        await target_message.edit(embed=embed, view=AFKListView())
    except discord.Forbidden:
        return
    except discord.HTTPException:
        return


@tasks.loop(seconds=60)
async def afk_list_updater() -> None:
    await refresh_afk_list_message()


async def submit_afk_report(
    interaction: discord.Interaction,
    game_nick: str,
    static_code: str,
    duration_text: str,
    reason: str,
) -> tuple[bool, str, Optional[int]]:
    if not interaction.guild:
        return False, "Команда доступна только на сервере.", None

    parsed_duration = parse_afk_duration(duration_text)
    if parsed_duration is None:
        return False, "Неверный формат времени AFK. Используй: 2ч, 30м или 1д.", None

    until_at_utc = utc_now() + parsed_duration
    until_text = until_at_utc.astimezone(MSK).strftime("%d.%m.%Y %H:%M")

    async with db_lock:
        if has_recent_duplicate(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            game_nick=game_nick,
            static_code=static_code,
            duration_text=duration_text,
            reason=reason,
        ):
            return False, "Похожая заявка уже была создана только что. Подожди пару секунд.", None

        report_id = insert_report(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            user_tag=str(interaction.user),
            reason=reason,
            until_text=until_text,
            game_nick=game_nick,
            static_code=static_code,
            duration_text=duration_text,
            comment="",
            until_at=until_at_utc.isoformat(),
        )

    review_channel = interaction.guild.get_channel(AFK_REVIEW_CHANNEL_ID)
    if not isinstance(review_channel, discord.TextChannel):
        return False, "Канал для заявок не найден. Проверь AFK_REVIEW_CHANNEL_ID.", None

    embed = create_embed(
        report_id=report_id,
        user_tag=str(interaction.user),
        user_id=interaction.user.id,
        reason=reason,
        until_text=until_text,
        game_nick=game_nick,
        static_code=static_code,
        duration_text=duration_text,
        status="pending",
    )
    try:
        role_mentions = " ".join(f"<@&{role_id}>" for role_id in sorted(MODERATOR_ROLE_IDS))
        mention_text = f"{role_mentions} новая AFK-заявка." if role_mentions else "Новая AFK-заявка."
        await review_channel.send(
            content=mention_text,
            embed=embed,
            view=DecisionView(),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.Forbidden:
        return (
            False,
            "Я не вижу канал заявок или не могу туда писать. Проверь права бота и AFK_REVIEW_CHANNEL_ID.",
            None,
        )
    except discord.HTTPException:
        return False, "Не удалось отправить заявку из-за ошибки Discord API. Попробуй снова через минуту.", None

    return True, f"Заявка #{report_id} отправлена на проверку.", report_id


class AFKReportModal(discord.ui.Modal, title="AFK отчет"):
    game_nick = discord.ui.TextInput(label="Ваш ник в игре", max_length=64, required=True)
    static_code = discord.ui.TextInput(label="Статик #", max_length=32, required=True)
    duration_text = discord.ui.TextInput(
        label="Время AFK (2ч / 30м / 1д)",
        max_length=16,
        required=True,
        placeholder="Например: 1д",
    )
    reason = discord.ui.TextInput(label="Причина", style=discord.TextStyle.paragraph, max_length=512, required=True)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ok, message, _ = await submit_afk_report(
            interaction=interaction,
            game_nick=str(self.game_nick),
            static_code=str(self.static_code),
            duration_text=str(self.duration_text),
            reason=str(self.reason),
        )
        await interaction.response.send_message(message, ephemeral=True)
        if ok:
            await refresh_afk_list_message()


class AFKListView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="AFK отчет", style=discord.ButtonStyle.primary, custom_id="afk_open_report_modal")
    async def open_report_modal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AFKReportModal())


class DecisionView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @staticmethod
    def _extract_report_id(message: discord.Message) -> Optional[int]:
        if not message.embeds:
            return None
        embed = message.embeds[0]
        source_text = ""
        if embed.title:
            source_text = embed.title
        elif embed.author and embed.author.name:
            source_text = embed.author.name
        if "#" not in source_text:
            return None
        try:
            return int(source_text.split("#")[-1])
        except ValueError:
            return None

    async def _process_decision(
        self,
        interaction: discord.Interaction,
        report_id: int,
        new_status: str,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return

        if MODERATOR_ROLE_IDS:
            user_roles = {role.id for role in getattr(interaction.user, "roles", [])}
            if user_roles.isdisjoint(MODERATOR_ROLE_IDS):
                await interaction.response.send_message(
                    "У тебя нет прав на проверку заявок.",
                    ephemeral=True,
                )
                return

        async with db_lock:
            updated = update_report_status(
                report_id=report_id,
                status=new_status,
                reviewer_id=interaction.user.id,
                reviewer_tag=str(interaction.user),
            )
            row = get_report(report_id)

        if not row:
            await interaction.response.send_message("Заявка не найдена.", ephemeral=True)
            return

        embed = create_embed(
            report_id=report_id,
            user_tag=row["user_tag"],
            user_id=row["user_id"],
            reason=row["reason"],
            until_text=row["until_text"],
            game_nick=row["game_nick"],
            static_code=row["static_code"],
            duration_text=row["duration_text"],
            status=row["status"],
            reviewed_by_id=row["reviewed_by_id"],
            reviewer_tag=row["reviewed_by_tag"],
        )

        if not updated:
            await interaction.response.defer()
            await interaction.message.edit(embed=embed, view=None)
            return

        await interaction.response.edit_message(embed=embed, view=None)
        await refresh_afk_list_message()

        user = interaction.client.get_user(row["user_id"]) or await interaction.client.fetch_user(row["user_id"])
        verdict = "принята" if new_status == "approved" else "отклонена"
        dm_text = f"Твоя AFK-заявка #{report_id} была {verdict} модератором {interaction.user}."
        try:
            await user.send(dm_text)
        except discord.Forbidden:
            pass

    @discord.ui.button(
        label="Принять",
        style=discord.ButtonStyle.success,
        custom_id="afk_approve",
    )
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.message:
            await interaction.response.send_message("Сообщение не найдено.", ephemeral=True)
            return
        report_id = self._extract_report_id(interaction.message)
        if report_id is None:
            await interaction.response.send_message("Не удалось определить номер заявки.", ephemeral=True)
            return
        await self._process_decision(interaction, report_id, "approved")

    @discord.ui.button(
        label="Отклонить",
        style=discord.ButtonStyle.danger,
        custom_id="afk_reject",
    )
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.message:
            await interaction.response.send_message("Сообщение не найдено.", ephemeral=True)
            return
        report_id = self._extract_report_id(interaction.message)
        if report_id is None:
            await interaction.response.send_message("Не удалось определить номер заявки.", ephemeral=True)
            return
        await self._process_decision(interaction, report_id, "rejected")


@bot.event
async def on_ready() -> None:
    init_db()
    bot.add_view(AFKListView())
    bot.add_view(DecisionView())
    guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=guild)
    await refresh_afk_list_message()
    if not afk_list_updater.is_running():
        afk_list_updater.start()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="afk", description="Создать AFK-заявку", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    game_nick="Твой ник в игре",
    static_code="Номер статика (например: 320362)",
    duration_text="Время AFK: 2ч / 30м / 1д",
    reason="Причина AFK",
)
async def afk(
    interaction: discord.Interaction,
    game_nick: str,
    static_code: str,
    duration_text: str,
    reason: str,
) -> None:
    _, message, _ = await submit_afk_report(
        interaction=interaction,
        game_nick=game_nick,
        static_code=static_code,
        duration_text=duration_text,
        reason=reason,
    )
    await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    acquire_instance_lock()
    bot.run(TOKEN)
