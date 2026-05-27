from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.embeds import parse_color_strict
from utils.helpers import attachment_to_file
from utils.permissions import has_bot_relay_access

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
EMBED_TOTAL_LIMIT = 6000
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
MODAL_TEXT_INPUT_LIMIT = 4000
FOOTER_LIMIT = 2048


@dataclass(slots=True)
class RelayTarget:
    guild: discord.Guild
    channel: discord.TextChannel | discord.Thread
    me: discord.Member


@dataclass(slots=True)
class PreparedImage:
    url: str | None = None
    warning: str | None = None


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _filename_ext(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").split(";", 1)[0].strip().lower()
    if content_type in ALLOWED_IMAGE_CONTENT_TYPES:
        return True
    return _filename_ext(attachment.filename) in ALLOWED_IMAGE_EXTENSIONS


def _embed_text_total(*values: str | None) -> int:
    return sum(len(value or "") for value in values)


class SayEmbedModal(discord.ui.Modal, title="Отправить embed"):
    embed_title = discord.ui.TextInput(
        label="Заголовок",
        required=False,
        max_length=TITLE_LIMIT,
        placeholder="Необязательный заголовок",
    )
    description = discord.ui.TextInput(
        label="Основной текст",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=MODAL_TEXT_INPUT_LIMIT,
        placeholder="Текст embed. Markdown и переносы строк сохраняются.",
    )
    color = discord.ui.TextInput(
        label="Цвет HEX",
        required=False,
        max_length=7,
        placeholder="#5865F2",
    )
    footer = discord.ui.TextInput(
        label="Footer",
        required=False,
        max_length=FOOTER_LIMIT,
        placeholder="Необязательная нижняя подпись",
    )
    image_url = discord.ui.TextInput(
        label="Ссылка на картинку",
        required=False,
        max_length=2000,
        placeholder="https://example.com/image.png",
    )

    def __init__(self, cog: "RelayCog", target: RelayTarget, image: discord.Attachment | None) -> None:
        super().__init__()
        self.cog = cog
        self.target = target
        self.image = image

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_say_embed_modal(
            interaction,
            self.target,
            title_text=str(self.embed_title),
            description=str(self.description),
            color_text=str(self.color),
            footer_text=str(self.footer),
            image_url_text=str(self.image_url),
            image=self.image,
        )


@dataclass(slots=True)
class AfishaDraft:
    type_text: str
    title_text: str
    date_time: str
    duration: str
    genres: str


class SayAfishaDetailsModal(discord.ui.Modal, title="Афиша: детали"):
    country_year = discord.ui.TextInput(label="Страна / год", required=False, max_length=300)
    cast = discord.ui.TextInput(label="Актёры / участники", required=False, max_length=800)
    description = discord.ui.TextInput(
        label="Описание",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=MODAL_TEXT_INPUT_LIMIT,
    )
    image_url = discord.ui.TextInput(
        label="Ссылка на картинку",
        required=False,
        max_length=2000,
        placeholder="Если заполнено, оно важнее загруженного файла.",
    )
    footer = discord.ui.TextInput(label="Footer", required=False, max_length=FOOTER_LIMIT)

    def __init__(
        self,
        cog: "RelayCog",
        target: RelayTarget,
        image: discord.Attachment | None,
        draft: AfishaDraft,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.target = target
        self.image = image
        self.draft = draft

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_say_afisha_details_modal(
            interaction,
            self.target,
            self.draft,
            country_year=str(self.country_year),
            cast=str(self.cast),
            description=str(self.description),
            image_url_text=str(self.image_url),
            footer_text=str(self.footer),
            image=self.image,
        )


class SayAfishaMainModal(discord.ui.Modal, title="Афиша: основное"):
    type_text = discord.ui.TextInput(label="Тип", required=True, max_length=80, placeholder="Фильм / сериал / событие")
    title_text = discord.ui.TextInput(label="Название", required=True, max_length=TITLE_LIMIT)
    date_time = discord.ui.TextInput(label="Дата и время", required=False, max_length=200)
    duration = discord.ui.TextInput(label="Длительность", required=False, max_length=120)
    genres = discord.ui.TextInput(label="Жанры", required=False, max_length=300)

    def __init__(self, cog: "RelayCog", target: RelayTarget, image: discord.Attachment | None) -> None:
        super().__init__()
        self.cog = cog
        self.target = target
        self.image = image

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = AfishaDraft(
            type_text=str(self.type_text),
            title_text=str(self.title_text),
            date_time=str(self.date_time),
            duration=str(self.duration),
            genres=str(self.genres),
        )
        await interaction.response.send_modal(SayAfishaDetailsModal(self.cog, self.target, self.image, draft))


class RelayCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _reply_ephemeral(self, interaction: discord.Interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def _get_target_channel(self, channel: discord.TextChannel | None) -> discord.TextChannel | None:
        if channel is not None:
            return channel

        if not self.bot.settings.target_channel_id:
            return None

        found = self.bot.get_channel(self.bot.settings.target_channel_id)
        if isinstance(found, discord.TextChannel):
            return found

        try:
            fetched = await self.bot.fetch_channel(self.bot.settings.target_channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception("Failed to fetch target channel %s", self.bot.settings.target_channel_id)
            return None

        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def _forward_attachments(self, attachments: list[discord.Attachment]) -> list[discord.File]:
        files: list[discord.File] = []
        for attachment in attachments:
            file = await attachment_to_file(attachment)
            if file is not None:
                files.append(file)
        return files

    def _has_relay_access(self, member: discord.Member | discord.User) -> bool:
        return isinstance(member, discord.Member) and has_bot_relay_access(member, self.bot.settings)

    def _has_say_access(self, member: discord.Member | discord.User) -> bool:
        return isinstance(member, discord.Member) and member.guild_permissions.administrator

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    @staticmethod
    def _has_send_permissions(
        channel: discord.TextChannel | discord.Thread,
        me: discord.Member,
        needs_embed: bool = False,
        needs_attach: bool = False,
    ) -> bool:
        perms = channel.permissions_for(me)
        if not perms.send_messages:
            return False
        if needs_embed and not perms.embed_links:
            return False
        if needs_attach and not perms.attach_files:
            return False
        return True

    async def _safe_send(
        self,
        channel: discord.TextChannel | discord.Thread,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        files: list[discord.File] | None = None,
    ) -> bool:
        try:
            await channel.send(content=content, embed=embed, files=files or [])
            return True
        except discord.HTTPException as exc:
            logger.exception("Failed to send message into channel %s: %s", channel.id, exc)
            return False

    async def _resolve_current_text_target(
        self,
        interaction: discord.Interaction,
        *,
        command_name: str,
        needs_attach: bool = False,
    ) -> RelayTarget | None:
        if not self._has_say_access(interaction.user):
            await interaction.response.send_message(f"У вас недостаточно прав для /{command_name}.", ephemeral=True)
            return None

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return None

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Команда доступна только в текстовых каналах сервера.", ephemeral=True)
            return None

        me = self._bot_member_in_guild(guild)
        if me is None or not self._has_send_permissions(channel, me, needs_embed=True, needs_attach=needs_attach):
            await interaction.response.send_message(
                "У бота нет прав отправки embed/файлов в текущий канал.",
                ephemeral=True,
            )
            return None

        return RelayTarget(guild=guild, channel=channel, me=me)

    async def _validate_remote_image_url(self, raw_url: str | None) -> PreparedImage:
        image_url = _clean_optional(raw_url)
        if image_url is None:
            return PreparedImage()
        if not _is_http_url(image_url):
            return PreparedImage(warning="Ссылка на картинку должна начинаться с http:// или https://. Embed отправлен без картинки.")

        session = self.bot.session
        if session is None or session.closed:
            return PreparedImage(url=image_url)

        try:
            async with session.head(image_url, allow_redirects=True, timeout=10) as response:
                content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if 200 <= response.status < 400 and content_type in ALLOWED_IMAGE_CONTENT_TYPES:
                    return PreparedImage(url=image_url)
                if response.status in {403, 405}:
                    raise RuntimeError("HEAD rejected")
        except Exception:
            try:
                async with session.get(image_url, allow_redirects=True, timeout=10) as response:
                    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                    if 200 <= response.status < 400 and content_type in ALLOWED_IMAGE_CONTENT_TYPES:
                        return PreparedImage(url=image_url)
            except Exception:
                logger.info("Image URL preflight failed: %s", image_url)

        return PreparedImage(warning="Картинка по ссылке не загрузилась или не похожа на изображение. Embed отправлен без картинки.")

    async def _send_embed_with_optional_attachment_fallback(
        self,
        target: RelayTarget,
        embed: discord.Embed,
        *,
        attachment: discord.Attachment | None,
        use_attachment_url: bool,
    ) -> tuple[bool, str | None]:
        sent = await self._safe_send(target.channel, embed=embed)
        if sent:
            return True, None

        if attachment is None or not use_attachment_url:
            return False, None

        try:
            file = await attachment_to_file(attachment)
        except discord.HTTPException:
            logger.exception("Failed to read image attachment for fallback: attachment=%s", attachment.id)
            return False, "Не удалось загрузить файл картинки. Возможно, файл слишком большой или Discord отклонил вложение."

        embed.set_image(url=f"attachment://{file.filename}")
        sent = await self._safe_send(target.channel, embed=embed, files=[file])
        if sent:
            return True, "Ссылка attachment не отправилась напрямую, поэтому картинка отправлена как вложение."
        return False, "Discord не принял embed или файл картинки. Проверьте размер файла и права бота."

    async def _prepare_embed_image(
        self,
        embed: discord.Embed,
        *,
        image_url_text: str | None,
        image: discord.Attachment | None,
    ) -> tuple[discord.Attachment | None, bool, str | None]:
        prepared = await self._validate_remote_image_url(image_url_text)
        if prepared.url is not None:
            embed.set_image(url=prepared.url)
            return image, False, None
        if prepared.warning:
            return image, False, prepared.warning
        if image is not None:
            embed.set_image(url=image.url)
            return image, True, None
        return None, False, None

    async def handle_say_embed_modal(
        self,
        interaction: discord.Interaction,
        target: RelayTarget,
        *,
        title_text: str,
        description: str,
        color_text: str,
        footer_text: str,
        image_url_text: str,
        image: discord.Attachment | None,
    ) -> None:
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound:
                logger.warning("Relay modal interaction expired before defer: guild=%s", interaction.guild_id)
                return

        parsed_color = parse_color_strict(_clean_optional(color_text))
        if _clean_optional(color_text) and parsed_color is None:
            await self._reply_ephemeral(interaction, "Некорректный HEX цвет. Используйте формат #5865F2.")
            return

        title_value = _clean_optional(title_text)
        footer_value = _clean_optional(footer_text)
        if _embed_text_total(title_value, description, footer_value) > EMBED_TOTAL_LIMIT:
            await self._reply_ephemeral(interaction, "Embed слишком длинный. Общий лимит текста: 6000 символов.")
            return

        embed = discord.Embed(
            title=title_value,
            description=description,
            color=parsed_color or discord.Color.blurple(),
        )
        if footer_value:
            embed.set_footer(text=footer_value)

        attachment, use_attachment_url, warning = await self._prepare_embed_image(
            embed,
            image_url_text=image_url_text,
            image=image,
        )
        sent, fallback_warning = await self._send_embed_with_optional_attachment_fallback(
            target,
            embed,
            attachment=attachment,
            use_attachment_url=use_attachment_url,
        )
        if not sent:
            await self._reply_ephemeral(
                interaction,
                fallback_warning or "Не удалось отправить embed. Проверьте права бота и параметры.",
            )
            return

        warnings = [value for value in (warning, fallback_warning) if value]
        suffix = f"\nПредупреждение: {' '.join(warnings)}" if warnings else ""
        await self._reply_ephemeral(interaction, f"✅ Embed отправлен.{suffix}")

    async def handle_say_afisha_details_modal(
        self,
        interaction: discord.Interaction,
        target: RelayTarget,
        draft: AfishaDraft,
        *,
        country_year: str,
        cast: str,
        description: str,
        image_url_text: str,
        footer_text: str,
        image: discord.Attachment | None,
    ) -> None:
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound:
                logger.warning("Relay afisha modal interaction expired before defer: guild=%s", interaction.guild_id)
                return

        type_value = _clean_optional(draft.type_text) or "Афиша"
        title_value = _clean_optional(draft.title_text) or "Без названия"
        footer_value = _clean_optional(footer_text)

        parts = [
            f"**Тип:** {type_value}",
            f"**Дата и время:** {_clean_optional(draft.date_time) or 'не указано'}",
            f"**Длительность:** {_clean_optional(draft.duration) or 'не указана'}",
            f"**Жанры:** {_clean_optional(draft.genres) or 'не указаны'}",
            f"**Страна / год:** {_clean_optional(country_year) or 'не указано'}",
            f"**Актёры / участники:** {_clean_optional(cast) or 'не указаны'}",
            "",
            description,
        ]
        embed_description = "\n".join(parts)
        if len(embed_description) > DESCRIPTION_LIMIT:
            await self._reply_ephemeral(interaction, "Описание афиши слишком длинное. Лимит description: 4096 символов.")
            return
        if _embed_text_total(title_value, embed_description, footer_value) > EMBED_TOTAL_LIMIT:
            await self._reply_ephemeral(interaction, "Embed афиши слишком длинный. Общий лимит текста: 6000 символов.")
            return

        embed = discord.Embed(title=title_value, description=embed_description, color=discord.Color.dark_gold())
        if footer_value:
            embed.set_footer(text=footer_value)

        attachment, use_attachment_url, warning = await self._prepare_embed_image(
            embed,
            image_url_text=image_url_text,
            image=image,
        )
        sent, fallback_warning = await self._send_embed_with_optional_attachment_fallback(
            target,
            embed,
            attachment=attachment,
            use_attachment_url=use_attachment_url,
        )
        if not sent:
            await self._reply_ephemeral(
                interaction,
                fallback_warning or "Не удалось отправить афишу. Проверьте права бота и параметры.",
            )
            return

        warnings = [value for value in (warning, fallback_warning) if value]
        suffix = f"\nПредупреждение: {' '.join(warnings)}" if warnings else ""
        await self._reply_ephemeral(interaction, f"✅ Афиша отправлена.{suffix}")

    @app_commands.command(name="say", description="Отправить сообщение от имени бота в текущий канал")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def say(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        if not self._has_say_access(interaction.user):
            await interaction.response.send_message("У вас недостаточно прав для /say.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Команда доступна только в текстовых каналах сервера.", ephemeral=True)
            return

        me = self._bot_member_in_guild(guild)
        if me is None or not self._has_send_permissions(channel, me):
            await interaction.response.send_message("У бота нет прав отправки сообщений в текущий канал.", ephemeral=True)
            return

        sent = await self._safe_send(channel, content=text)
        if not sent:
            await interaction.response.send_message("Не удалось отправить сообщение. Проверьте права бота.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)

    @app_commands.command(name="say_embed", description="Открыть форму embed для текущего канала")
    @app_commands.describe(image="Необязательная PNG/JPG/WEBP/GIF картинка для большого изображения embed")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def say_embed(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment | None = None,
    ) -> None:
        if image is not None and not _is_image_attachment(image):
            await interaction.response.send_message("Файл должен быть картинкой: PNG, JPEG/JPG, WEBP или GIF.", ephemeral=True)
            return

        target = await self._resolve_current_text_target(
            interaction,
            command_name="say_embed",
            needs_attach=image is not None,
        )
        if target is None:
            return

        await interaction.response.send_modal(SayEmbedModal(self, target, image))

    @app_commands.command(name="say_afisha", description="Открыть форму афиши для текущего канала")
    @app_commands.describe(image="Необязательная PNG/JPG/WEBP/GIF картинка для большого изображения афиши")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def say_afisha(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment | None = None,
    ) -> None:
        if image is not None and not _is_image_attachment(image):
            await interaction.response.send_message("Файл должен быть картинкой: PNG, JPEG/JPG, WEBP или GIF.", ephemeral=True)
            return

        target = await self._resolve_current_text_target(
            interaction,
            command_name="say_afisha",
            needs_attach=image is not None,
        )
        if target is None:
            return

        await interaction.response.send_modal(SayAfishaMainModal(self, target, image))


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(RelayCog(bot))
