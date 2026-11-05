import io
import json
import os
import random
import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
# On Railway: set DATA_FILE to a volume path, e.g. /data/data.json
DATA_FILE = os.getenv("DATA_FILE", "data.json")

# ----------------------------------------------------------------------------
# Data (simple JSON file)
# ----------------------------------------------------------------------------

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "giveaways": {},
        "warns": {},
        "tickets": {},
        "settings": {},
        "honeypots": {},  # guild_id -> {channel_ids: [], action: "ban"|"kick"|"timeout", timeout_minutes: 60}
    }


data = load_data()


def save():
    # Create directory if using a volume path
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def parse_duration(text: str):
    """'30s', '10m', '2h', '1d' -> seconds"""
    m = re.fullmatch(r"(\d+)([smhd])", text.strip().lower())
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


# ----------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------

class CommunityBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Persistent buttons (work after restart)
        self.add_view(GiveawayView())
        self.add_view(TicketOpenView())
        self.add_view(TicketCloseView())
        await self.tree.sync()
        giveaway_loop.start()

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")
        print(f"Discord.py version: {discord.__version__}")
        print(f"Data file: {DATA_FILE}")


bot = CommunityBot()


# ----------------------------------------------------------------------------
# GIVEAWAY
# ----------------------------------------------------------------------------

def giveaway_embed(g: dict, ended: bool = False, winners=None):
    embed = discord.Embed(
        title=f"🎉 {g['prize']}",
        color=discord.Color.red() if ended else discord.Color.gold(),
    )
    if ended:
        text = ", ".join(f"<@{w}>" for w in winners) if winners else "No participants"
        embed.description = f"**Ended**\nWinners: {text}"
    else:
        embed.description = (
            f"Click the button to enter!\n\n"
            f"Ends: <t:{g['end_ts']}:R>\n"
            f"Winners: **{g['winners']}**\n"
            f"Hosted by: <@{g['host']}>"
        )
    embed.set_footer(text=f"Participants: {len(g['participants'])}")
    return embed


class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Enter",
        emoji="🎉",
        style=discord.ButtonStyle.primary,
        custom_id="giveaway_join",
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = data["giveaways"].get(str(interaction.message.id))
        if not g or g["ended"]:
            return await interaction.response.send_message(
                "This giveaway has ended.", ephemeral=True
            )
        uid = interaction.user.id
        if uid in g["participants"]:
            g["participants"].remove(uid)
            msg = "You are no longer participating."
        else:
            g["participants"].append(uid)
            msg = "You are now participating. Good luck! 🍀"
        save()
        await interaction.message.edit(embed=giveaway_embed(g))
        await interaction.response.send_message(msg, ephemeral=True)


async def finish_giveaway(message_id: str):
    g = data["giveaways"].get(message_id)
    if not g or g["ended"]:
        return False
    channel = bot.get_channel(g["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(g["channel_id"])
        except (discord.NotFound, discord.Forbidden):
            return False
    pool = list(g["participants"])
    winners = (
        random.sample(pool, min(g["winners"], len(pool))) if pool else []
    )
    g["ended"] = True
    g["winner_ids"] = winners
    save()
    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(
            embed=giveaway_embed(g, ended=True, winners=winners), view=None
        )
    except discord.NotFound:
        pass
    if winners:
        mentions = ", ".join(f"<@{w}>" for w in winners)
        await channel.send(
            f"🎉 Congratulations {mentions}! You won **{g['prize']}**!"
        )
    else:
        await channel.send(
            f"Giveaway **{g['prize']}** ended with no participants."
        )
    return True


@tasks.loop(seconds=15)
async def giveaway_loop():
    now = int(time.time())
    for mid, g in list(data["giveaways"].items()):
        if not g["ended"] and g["end_ts"] <= now:
            await finish_giveaway(mid)


# ---------- Giveaway Dashboard ----------

def _active_giveaways(guild_id: int | None = None) -> list[tuple[str, dict]]:
    """Return (message_id, giveaway_dict) for active (not ended) giveaways."""
    result = []
    for mid, g in data["giveaways"].items():
        if g.get("ended"):
            continue
        if guild_id is not None:
            ch = bot.get_channel(g.get("channel_id", 0))
            if ch is None or (hasattr(ch, "guild") and ch.guild and ch.guild.id != guild_id):
                continue
        result.append((mid, g))
    return result


def _ended_giveaways(guild_id: int | None = None, limit: int = 15) -> list[tuple[str, dict]]:
    result = []
    for mid, g in data["giveaways"].items():
        if not g.get("ended"):
            continue
        if guild_id is not None:
            ch = bot.get_channel(g.get("channel_id", 0))
            if ch is None or (hasattr(ch, "guild") and ch.guild and ch.guild.id != guild_id):
                continue
        result.append((mid, g))
    return result[-limit:]


def dashboard_embed(guild: discord.Guild) -> discord.Embed:
    active = _active_giveaways(guild.id)
    embed = discord.Embed(
        title="🎉 Giveaway Dashboard",
        description=(
            "Use the buttons below to manage giveaways.\n\n"
            "**Start** – Create a new giveaway (opens a form)\n"
            "**End** – End an active giveaway early\n"
            "**Reroll** – Pick new winners for an ended giveaway\n"
            "**List** – Show active giveaways"
        ),
        color=discord.Color.gold(),
    )
    if active:
        lines = []
        for mid, g in active[:10]:
            lines.append(
                f"• **{g['prize']}** — ends <t:{g['end_ts']}:R> "
                f"({len(g['participants'])} entries) · `{mid}`"
            )
        embed.add_field(
            name=f"Active ({len(active)})",
            value="\n".join(lines) or "None",
            inline=False,
        )
    else:
        embed.add_field(name="Active", value="No active giveaways.", inline=False)
    embed.set_footer(text="Only users with Manage Server can use this.")
    return embed


class GiveawayStartModal(discord.ui.Modal, title="Start Giveaway"):
    prize = discord.ui.TextInput(
        label="Prize",
        placeholder="e.g. Nitro, $10 Steam Gift Card",
        max_length=200,
        required=True,
    )
    duration = discord.ui.TextInput(
        label="Duration",
        placeholder="e.g. 30m, 2h, 1d, 7d",
        max_length=10,
        required=True,
        default="1h",
    )
    winners = discord.ui.TextInput(
        label="Number of winners",
        placeholder="1 – 20",
        max_length=2,
        required=True,
        default="1",
    )
    channel_id = discord.ui.TextInput(
        label="Channel ID (optional)",
        placeholder="Leave empty = current channel",
        max_length=20,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        seconds = parse_duration(self.duration.value)
        if not seconds:
            return await interaction.response.send_message(
                "Invalid duration. Use e.g. `30m`, `2h`, `1d`.", ephemeral=True
            )
        try:
            num_winners = int(self.winners.value)
            if not 1 <= num_winners <= 20:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "Winners must be a number between 1 and 20.", ephemeral=True
            )

        channel = interaction.channel
        if self.channel_id.value.strip():
            try:
                ch = interaction.guild.get_channel(int(self.channel_id.value.strip()))
                if ch is None:
                    ch = await bot.fetch_channel(int(self.channel_id.value.strip()))
                if not isinstance(ch, discord.TextChannel):
                    raise ValueError
                channel = ch
            except (ValueError, discord.NotFound, discord.Forbidden):
                return await interaction.response.send_message(
                    "Invalid channel ID.", ephemeral=True
                )

        g = {
            "channel_id": channel.id,
            "prize": self.prize.value.strip(),
            "end_ts": int(time.time()) + seconds,
            "winners": num_winners,
            "participants": [],
            "ended": False,
            "host": interaction.user.id,
        }
        message = await channel.send(embed=giveaway_embed(g), view=GiveawayView())
        data["giveaways"][str(message.id)] = g
        save()
        await interaction.response.send_message(
            f"✅ Giveaway **{g['prize']}** started in {channel.mention}!\n"
            f"Message ID: `{message.id}`",
            ephemeral=True,
        )


class GiveawayEndModal(discord.ui.Modal, title="End Giveaway"):
    message_id = discord.ui.TextInput(
        label="Giveaway Message ID",
        placeholder="Right-click the giveaway message → Copy Message ID",
        max_length=25,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        mid = self.message_id.value.strip()
        ok = await finish_giveaway(mid)
        if ok:
            await interaction.response.send_message(
                f"✅ Giveaway `{mid}` has been ended.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Giveaway not found or already ended.", ephemeral=True
            )


class GiveawayRerollModal(discord.ui.Modal, title="Reroll Winners"):
    message_id = discord.ui.TextInput(
        label="Giveaway Message ID",
        placeholder="Message ID of an ended giveaway",
        max_length=25,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        mid = self.message_id.value.strip()
        g = data["giveaways"].get(mid)
        if not g or not g.get("ended"):
            return await interaction.response.send_message(
                "Giveaway not found or not yet ended.", ephemeral=True
            )
        if not g.get("participants"):
            return await interaction.response.send_message(
                "There were no participants.", ephemeral=True
            )
        winners = random.sample(
            g["participants"], min(g["winners"], len(g["participants"]))
        )
        mentions = ", ".join(f"<@{w}>" for w in winners)
        await interaction.response.send_message(
            f"🎉 New winners for **{g['prize']}**: {mentions}"
        )


class GiveawaySelectEnd(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Select a giveaway to end…",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="gw_select_end",
        )

    async def callback(self, interaction: discord.Interaction):
        mid = self.values[0]
        ok = await finish_giveaway(mid)
        if ok:
            await interaction.response.send_message(
                f"✅ Giveaway `{mid}` has been ended.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Giveaway not found or already ended.", ephemeral=True
            )


class GiveawaySelectReroll(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Select a giveaway to reroll…",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="gw_select_reroll",
        )

    async def callback(self, interaction: discord.Interaction):
        mid = self.values[0]
        g = data["giveaways"].get(mid)
        if not g or not g.get("ended"):
            return await interaction.response.send_message(
                "Giveaway not found or not yet ended.", ephemeral=True
            )
        if not g.get("participants"):
            return await interaction.response.send_message(
                "There were no participants.", ephemeral=True
            )
        winners = random.sample(
            g["participants"], min(g["winners"], len(g["participants"]))
        )
        mentions = ", ".join(f"<@{w}>" for w in winners)
        await interaction.response.send_message(
            f"🎉 New winners for **{g['prize']}**: {mentions}"
        )


class GiveawayDashboardView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    @discord.ui.button(label="Start", emoji="✨", style=discord.ButtonStyle.green, row=0)
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GiveawayStartModal())

    @discord.ui.button(label="End", emoji="⏹️", style=discord.ButtonStyle.red, row=0)
    async def btn_end(self, interaction: discord.Interaction, button: discord.ui.Button):
        active = _active_giveaways(interaction.guild.id)
        if not active:
            return await interaction.response.send_message(
                "No active giveaways to end.", ephemeral=True
            )
        # Prefer select menu if few enough; otherwise modal
        if len(active) <= 25:
            options = [
                discord.SelectOption(
                    label=g["prize"][:90] or "Giveaway",
                    description=f"Ends <t:{g['end_ts']}:R> · {len(g['participants'])} entries",
                    value=mid,
                    emoji="🎉",
                )
                for mid, g in active[:25]
            ]
            view = discord.ui.View(timeout=60)
            view.add_item(GiveawaySelectEnd(options))
            await interaction.response.send_message(
                "Select the giveaway to end:", view=view, ephemeral=True
            )
        else:
            await interaction.response.send_modal(GiveawayEndModal())

    @discord.ui.button(label="Reroll", emoji="🎲", style=discord.ButtonStyle.blurple, row=0)
    async def btn_reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        ended = _ended_giveaways(interaction.guild.id)
        # Also include ones that have winner_ids
        if not ended:
            return await interaction.response.send_message(
                "No ended giveaways to reroll. End one first.", ephemeral=True
            )
        if len(ended) <= 25:
            options = [
                discord.SelectOption(
                    label=g["prize"][:90] or "Giveaway",
                    description=f"{len(g.get('participants', []))} participants",
                    value=mid,
                    emoji="🎲",
                )
                for mid, g in ended[:25]
            ]
            view = discord.ui.View(timeout=60)
            view.add_item(GiveawaySelectReroll(options))
            await interaction.response.send_message(
                "Select the giveaway to reroll:", view=view, ephemeral=True
            )
        else:
            await interaction.response.send_modal(GiveawayRerollModal())

    @discord.ui.button(label="List", emoji="📋", style=discord.ButtonStyle.grey, row=0)
    async def btn_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        active = _active_giveaways(interaction.guild.id)
        if not active:
            return await interaction.response.send_message(
                "No active giveaways.", ephemeral=True
            )
        embed = discord.Embed(title="📋 Active Giveaways", color=discord.Color.gold())
        for mid, g in active[:15]:
            embed.add_field(
                name=g["prize"],
                value=(
                    f"Ends: <t:{g['end_ts']}:R>\n"
                    f"Winners: **{g['winners']}**\n"
                    f"Entries: **{len(g['participants'])}**\n"
                    f"Channel: <#{g['channel_id']}>\n"
                    f"ID: `{mid}`"
                ),
                inline=True,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.grey, row=1)
    async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=dashboard_embed(interaction.guild),
            view=GiveawayDashboardView(interaction.guild.id),
        )


@bot.tree.command(name="giveaway", description="Open the Giveaway Dashboard")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def giveaway_dashboard(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=dashboard_embed(interaction.guild),
        view=GiveawayDashboardView(interaction.guild.id),
        ephemeral=True,
    )


# ----------------------------------------------------------------------------
# TICKETS
# ----------------------------------------------------------------------------

def guild_settings(guild_id: int) -> dict:
    return data["settings"].setdefault(str(guild_id), {})


class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create Ticket",
        emoji="🎫",
        style=discord.ButtonStyle.green,
        custom_id="ticket_open",
    )
    async def open_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        guild = interaction.guild
        cfg = guild_settings(guild.id)
        # Does the user already have an open ticket?
        for cid, uid in data["tickets"].items():
            if uid == interaction.user.id and guild.get_channel(int(cid)):
                return await interaction.response.send_message(
                    f"You already have a ticket: <#{cid}>", ephemeral=True
                )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        role = guild.get_role(cfg.get("support_role", 0))
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )
        category = guild.get_channel(cfg.get("category", 0))
        name = re.sub(r"[^a-z0-9-]", "", interaction.user.name.lower()) or "user"
        channel = await guild.create_text_channel(
            f"ticket-{name}",
            category=category,
            overwrites=overwrites,
            reason=f"Ticket by {interaction.user}",
        )
        data["tickets"][str(channel.id)] = interaction.user.id
        save()
        embed = discord.Embed(
            title="🎫 Ticket",
            description=(
                "Describe your issue, the team will respond soon.\n"
                "Use the button below to close this ticket."
            ),
            color=discord.Color.green(),
        )
        ping = role.mention if role else ""
        await channel.send(
            f"{interaction.user.mention} {ping}",
            embed=embed,
            view=TicketCloseView(),
        )
        await interaction.response.send_message(
            f"Ticket created: {channel.mention}", ephemeral=True
        )


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        emoji="🔒",
        style=discord.ButtonStyle.red,
        custom_id="ticket_close",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        channel = interaction.channel
        owner_id = data["tickets"].get(str(channel.id))
        is_staff = interaction.user.guild_permissions.manage_channels
        if interaction.user.id != owner_id and not is_staff:
            return await interaction.response.send_message(
                "You are not allowed to close this ticket.", ephemeral=True
            )
        await interaction.response.send_message(
            "Ticket will be closed in 5 seconds ..."
        )
        # Create transcript
        lines = []
        async for m in channel.history(limit=None, oldest_first=True):
            ts = m.created_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"[{ts}] {m.author}: {m.clean_content}")
        transcript = discord.File(
            io.BytesIO("\n".join(lines).encode("utf-8")),
            filename=f"{channel.name}.txt",
        )
        log = interaction.guild.get_channel(
            guild_settings(interaction.guild.id).get("log_channel", 0)
        )
        if log:
            await log.send(
                f"Ticket **{channel.name}** closed by {interaction.user.mention}",
                file=transcript,
            )
        data["tickets"].pop(str(channel.id), None)
        save()
        await discord.utils.sleep_until(
            datetime.now(timezone.utc) + timedelta(seconds=5)
        )
        await channel.delete(reason=f"Ticket closed by {interaction.user}")


@bot.tree.command(
    name="ticket_setup", description="Create a ticket panel in this channel"
)
@app_commands.describe(
    category="Category for new tickets",
    support_role="Role that can see tickets",
    log_channel="Channel for transcripts",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def ticket_setup(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    support_role: discord.Role,
    log_channel: discord.TextChannel = None,
):
    cfg = guild_settings(interaction.guild.id)
    cfg["category"] = category.id
    cfg["support_role"] = support_role.id
    if log_channel:
        cfg["log_channel"] = log_channel.id
    save()
    embed = discord.Embed(
        title="🎫 Support",
        description="Need help? Click below to open a private ticket.",
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=embed, view=TicketOpenView())
    await interaction.response.send_message("Panel created.", ephemeral=True)


# ----------------------------------------------------------------------------
# MODERATION
# ----------------------------------------------------------------------------

def can_act(interaction: discord.Interaction, target: discord.Member) -> str | None:
    """Returns an error message or None if everything is fine."""
    guild = interaction.guild
    if target.id == interaction.user.id:
        return "You cannot do that to yourself."
    if target.id == guild.owner_id:
        return "You cannot punish the server owner."
    if (
        interaction.user.id != guild.owner_id
        and target.top_role >= interaction.user.top_role
    ):
        return "This person has an equal or higher role than you."
    if target.top_role >= guild.me.top_role:
        return (
            "My role is not high enough. Move it higher in the role settings."
        )
    return None


@bot.tree.command(name="kick", description="Kick a member")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
@app_commands.checks.has_permissions(kick_members=True)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
):
    if err := can_act(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.kick(reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(
        f"👢 {member} has been kicked. Reason: {reason}"
    )


@bot.tree.command(name="ban", description="Ban a member")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
):
    if err := can_act(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.ban(reason=f"{interaction.user}: {reason}", delete_message_days=0)
    await interaction.response.send_message(
        f"🔨 {member} has been banned. Reason: {reason}"
    )


@bot.tree.command(name="unban", description="Unban a user by ID")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
    except (ValueError, discord.NotFound):
        return await interaction.response.send_message(
            "User not found or not banned.", ephemeral=True
        )
    await interaction.response.send_message(f"✅ {user} has been unbanned.")


@bot.tree.command(name="timeout", description="Timeout a member")
@app_commands.describe(duration="e.g. 10m, 2h, 1d (max 28 days)")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    reason: str = "No reason provided",
):
    if err := can_act(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    seconds = parse_duration(duration)
    if not seconds or seconds > 28 * 86400:
        return await interaction.response.send_message(
            "Invalid duration. Examples: `10m`, `2h`, `1d` (max 28 days)",
            ephemeral=True,
        )
    await member.timeout(
        timedelta(seconds=seconds), reason=f"{interaction.user}: {reason}"
    )
    await interaction.response.send_message(
        f"🔇 {member.mention} is timed out for **{duration}**. Reason: {reason}"
    )


@bot.tree.command(name="untimeout", description="Remove a timeout")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    await interaction.response.send_message(
        f"🔊 Timeout removed from {member.mention}."
    )


@bot.tree.command(name="warn", description="Warn a member")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(
    interaction: discord.Interaction, member: discord.Member, reason: str
):
    if err := can_act(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    warns = (
        data["warns"]
        .setdefault(str(interaction.guild.id), {})
        .setdefault(str(member.id), [])
    )
    warns.append(
        {"reason": reason, "mod": interaction.user.id, "ts": int(time.time())}
    )
    save()
    text = (
        f"⚠️ {member.mention} has been warned ({len(warns)}. warning). "
        f"Reason: {reason}"
    )
    # Automatic punishment at 3 warnings
    if len(warns) >= 3:
        await member.timeout(
            timedelta(hours=1), reason="Reached 3 warnings"
        )
        text += "\nAutomatically timed out for 1 hour (3 warnings)."
    await interaction.response.send_message(text)
    try:
        await member.send(
            f"You have been warned on **{interaction.guild.name}**: {reason}"
        )
    except discord.Forbidden:
        pass


@bot.tree.command(name="warns", description="Show warnings")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def warns(interaction: discord.Interaction, member: discord.Member):
    entries = (
        data["warns"]
        .get(str(interaction.guild.id), {})
        .get(str(member.id), [])
    )
    if not entries:
        return await interaction.response.send_message(
            f"{member} has no warnings.", ephemeral=True
        )
    lines = [
        f"**{i}.** <t:{w['ts']}:d> - {w['reason']} (by <@{w['mod']}>)"
        for i, w in enumerate(entries, 1)
    ]
    embed = discord.Embed(
        title=f"Warnings for {member}",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clearwarns", description="Clear all warnings")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarns(interaction: discord.Interaction, member: discord.Member):
    data["warns"].get(str(interaction.guild.id), {}).pop(str(member.id), None)
    save()
    await interaction.response.send_message(
        f"Warnings for {member.mention} have been cleared."
    )


@bot.tree.command(name="purge", description="Delete messages")
@app_commands.describe(amount="1 to 100")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(
    interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]
):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(
        f"🗑️ Deleted {len(deleted)} messages.", ephemeral=True
    )


# ----------------------------------------------------------------------------
# AUTO-MOD: Word filter + Anti-Link + Anti-Caps + Anti-Term/Limited
# ----------------------------------------------------------------------------

DEFAULT_BANNED_WORDS = [
    # Malware / Stealer
    "rat",
    "malware",
    "ransomware",
    "stealer",
    "session stealer",
    "cookie stealer",
    "token stealer",
    "keylogger",
    "token grabber",
    "token logger",
    "botnet",
    "ddos",
    # Term / Account terminator
    "term",
    "terminator",
    "account term",
    "mass term",
    "term service",
    "term tool",
    "account terminator",
    "mass report",
    "massreport",
    # Limited / Unlimiter
    "limited",
    "unlimit",
    "unlimiter",
    "unlimit account",
    "limited account",
    "phone bypass",
    "phone verify bypass",
    "age bypass",
]

# --- Bypass protection ---------------------------------------------
LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
ZERO_WIDTH = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"), None
)
HOMOGLYPHS = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
        "і": "i", "ј": "j", "к": "k", "м": "m", "н": "h", "т": "t", "в": "b",
        "ѕ": "s", "ԁ": "d", "ɡ": "g", "ɑ": "a", "ı": "i", "α": "a", "ο": "o",
        "ρ": "p", "ε": "e", "ι": "i", "κ": "k", "τ": "t", "υ": "u", "ν": "v",
    }
)

_pattern_cache = {}

# Discord invites + general links
INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.(?:gg|com|me|io)(?:/invite)?|discord\.new)/[a-zA-Z0-9-]+",
    re.IGNORECASE,
)
URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+",
    re.IGNORECASE,
)
DEFAULT_ALLOWED_DOMAINS = [
    "discord.com",
    "discord.gg",
    "discordapp.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
    "tenor.com",
    "giphy.com",
    "youtube.com",
    "youtu.be",
    "imgur.com",
    "github.com",
    "twitter.com",
    "x.com",
    "reddit.com",
]


def normalize(text: str) -> str:
    text = text.lower().translate(ZERO_WIDTH)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.translate(HOMOGLYPHS).translate(LEET)


def word_pattern(word: str) -> re.Pattern:
    if word not in _pattern_cache:
        parts = []
        for part in normalize(word).split():
            parts.append(r"[^a-z0-9]{0,2}".join(re.escape(c) + "+" for c in part))
        phrase = r"[^a-z0-9]{0,3}".join(parts)
        _pattern_cache[word] = re.compile(rf"(?<![a-z0-9]){phrase}(?![a-z0-9])")
    return _pattern_cache[word]


def get_banned_words(guild_id: int) -> list:
    cfg = guild_settings(guild_id)
    if "banned_words" not in cfg:
        cfg["banned_words"] = list(DEFAULT_BANNED_WORDS)
        save()
    return cfg["banned_words"]


def get_automod_settings(guild_id: int) -> dict:
    """Default settings for Anti-Link / Anti-Caps."""
    cfg = guild_settings(guild_id)
    defaults = {
        "anti_link": True,
        "anti_invite": True,
        "anti_caps": True,
        "caps_percent": 70,
        "caps_min_length": 8,
        "allowed_domains": list(DEFAULT_ALLOWED_DOMAINS),
        "anti_link_action": "delete",
    }
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
    return cfg


async def auto_punish(
    message: discord.Message, reason: str, notice: str, timeout_minutes: int = 0
) -> bool:
    """Delete message, add warning, optional timeout, log."""
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        return False

    warns = (
        data["warns"]
        .setdefault(str(message.guild.id), {})
        .setdefault(str(message.author.id), [])
    )
    warns.append({"reason": reason, "mod": bot.user.id, "ts": int(time.time())})
    save()

    text = f"🚫 {message.author.mention}, {notice} ({len(warns)}/3 warnings)"
    if len(warns) >= 3 or timeout_minutes > 0:
        mins = timeout_minutes or 60
        try:
            await message.author.timeout(
                timedelta(minutes=mins), reason=f"Auto-Mod: {reason}"
            )
            text += f"\nAutomatically timed out for {mins} min."
        except discord.Forbidden:
            pass

    try:
        await message.channel.send(text, delete_after=10)
    except discord.Forbidden:
        pass

    log = message.guild.get_channel(
        guild_settings(message.guild.id).get("log_channel", 0)
    )
    if log:
        embed = discord.Embed(
            title="Auto-Mod: Message removed", color=discord.Color.red()
        )
        embed.add_field(
            name="User", value=f"{message.author.mention} ({message.author.id})"
        )
        embed.add_field(name="Channel", value=message.channel.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(
            name="Message", value=(message.content or "-")[:1000], inline=False
        )
        await log.send(embed=embed)
    return True


# -------------------- Word filter --------------------

async def check_filter(message: discord.Message) -> bool:
    if message.author.bot or not message.guild:
        return False
    if message.author.guild_permissions.manage_messages:
        return False
    text = normalize(message.content)
    hit = next(
        (w for w in get_banned_words(message.guild.id) if word_pattern(w).search(text)),
        None,
    )
    if not hit:
        return False
    return await auto_punish(
        message,
        reason=f"Banned word: {hit}",
        notice="this word is not allowed here.",
    )


# -------------------- Anti-Link --------------------

def _is_allowed_domain(url: str, allowed: list[str]) -> bool:
    url_lower = url.lower()
    for domain in allowed:
        if domain in url_lower:
            return True
    return False


async def check_links(message: discord.Message) -> bool:
    if message.author.bot or not message.guild:
        return False
    if message.author.guild_permissions.manage_messages:
        return False

    cfg = get_automod_settings(message.guild.id)
    content = message.content or ""

    # 1) Discord invites
    if cfg.get("anti_invite", True) or cfg.get("anti_link", True):
        if INVITE_RE.search(content):
            return await auto_punish(
                message,
                reason="Discord invite link",
                notice="Discord invite links are not allowed here.",
                timeout_minutes=10,
            )

    # 2) Other links
    if cfg.get("anti_link", True):
        urls = URL_RE.findall(content)
        allowed = cfg.get("allowed_domains", DEFAULT_ALLOWED_DOMAINS)
        for url in urls:
            if not _is_allowed_domain(url, allowed):
                return await auto_punish(
                    message,
                    reason=f"Unauthorized link: {url[:80]}",
                    notice="external links are not allowed here.",
                    timeout_minutes=5,
                )
    return False


# -------------------- Anti-Caps --------------------

async def check_caps(message: discord.Message) -> bool:
    if message.author.bot or not message.guild:
        return False
    if message.author.guild_permissions.manage_messages:
        return False

    cfg = get_automod_settings(message.guild.id)
    if not cfg.get("anti_caps", True):
        return False

    content = message.content or ""
    letters = [c for c in content if c.isalpha()]
    if len(letters) < cfg.get("caps_min_length", 8):
        return False

    upper = sum(1 for c in letters if c.isupper())
    percent = (upper / len(letters)) * 100
    if percent >= cfg.get("caps_percent", 70):
        return await auto_punish(
            message,
            reason=f"CAPS spam ({percent:.0f}%)",
            notice="please don't write in all caps.",
        )
    return False


# -------------------- Commands --------------------

filter_group = app_commands.Group(
    name="filter",
    description="Manage banned words",
    guild_only=True,
    default_permissions=discord.Permissions(manage_messages=True),
)


@filter_group.command(name="add", description="Ban a word or phrase")
async def filter_add(interaction: discord.Interaction, word: str):
    word = word.lower().strip()
    words = get_banned_words(interaction.guild.id)
    if word in words:
        return await interaction.response.send_message(
            "This word is already on the list.", ephemeral=True
        )
    words.append(word)
    save()
    await interaction.response.send_message(
        f"✅ `{word}` is now banned.", ephemeral=True
    )


@filter_group.command(name="remove", description="Remove a word from the list")
async def filter_remove(interaction: discord.Interaction, word: str):
    word = word.lower().strip()
    words = get_banned_words(interaction.guild.id)
    if word not in words:
        return await interaction.response.send_message(
            "This word is not on the list.", ephemeral=True
        )
    words.remove(word)
    save()
    await interaction.response.send_message(
        f"✅ `{word}` is allowed again.", ephemeral=True
    )


@filter_group.command(name="list", description="Show all banned words")
async def filter_list(interaction: discord.Interaction):
    words = get_banned_words(interaction.guild.id)
    text = ", ".join(f"`{w}`" for w in words) or "No words."
    await interaction.response.send_message(
        f"**Banned words:**\n{text}", ephemeral=True
    )


bot.tree.add_command(filter_group)


automod = app_commands.Group(
    name="automod",
    description="Anti-Link, Anti-Caps and settings",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@automod.command(name="status", description="Show current Auto-Mod settings")
async def automod_status(interaction: discord.Interaction):
    cfg = get_automod_settings(interaction.guild.id)
    domains = ", ".join(f"`{d}`" for d in cfg.get("allowed_domains", [])[:15])
    if len(cfg.get("allowed_domains", [])) > 15:
        domains += " …"
    embed = discord.Embed(title="🛡️ Auto-Mod Status", color=discord.Color.blue())
    embed.add_field(
        name="Anti-Link",
        value="✅ On" if cfg.get("anti_link") else "❌ Off",
        inline=True,
    )
    embed.add_field(
        name="Anti-Invite",
        value="✅ On" if cfg.get("anti_invite") else "❌ Off",
        inline=True,
    )
    embed.add_field(
        name="Anti-Caps",
        value="✅ On" if cfg.get("anti_caps") else "❌ Off",
        inline=True,
    )
    embed.add_field(
        name="Caps threshold",
        value=f"{cfg.get('caps_percent', 70)}% (min. {cfg.get('caps_min_length', 8)} chars)",
        inline=False,
    )
    embed.add_field(name="Allowed domains", value=domains or "—", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@automod.command(name="antilink", description="Toggle Anti-Link")
@app_commands.describe(status="on or off")
@app_commands.choices(
    status=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def automod_antilink(interaction: discord.Interaction, status: app_commands.Choice[str]):
    cfg = get_automod_settings(interaction.guild.id)
    cfg["anti_link"] = status.value == "on"
    save()
    await interaction.response.send_message(
        f"Anti-Link is now **{'on' if cfg['anti_link'] else 'off'}**.",
        ephemeral=True,
    )


@automod.command(name="antiinvite", description="Toggle Discord invite blocking only")
@app_commands.describe(status="on or off")
@app_commands.choices(
    status=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def automod_antiinvite(
    interaction: discord.Interaction, status: app_commands.Choice[str]
):
    cfg = get_automod_settings(interaction.guild.id)
    cfg["anti_invite"] = status.value == "on"
    save()
    await interaction.response.send_message(
        f"Anti-Invite is now **{'on' if cfg['anti_invite'] else 'off'}**.",
        ephemeral=True,
    )


@automod.command(name="anticaps", description="Toggle Anti-Caps")
@app_commands.describe(status="on or off", percent="Caps percentage threshold (optional)")
@app_commands.choices(
    status=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def automod_anticaps(
    interaction: discord.Interaction,
    status: app_commands.Choice[str],
    percent: app_commands.Range[int, 50, 100] = None,
):
    cfg = get_automod_settings(interaction.guild.id)
    cfg["anti_caps"] = status.value == "on"
    if percent is not None:
        cfg["caps_percent"] = percent
    save()
    msg = f"Anti-Caps is now **{'on' if cfg['anti_caps'] else 'off'}**."
    if percent is not None:
        msg += f" Threshold: **{percent}%**."
    await interaction.response.send_message(msg, ephemeral=True)


@automod.command(name="allowdomain", description="Add a domain to the whitelist")
@app_commands.describe(domain="e.g. example.com")
async def automod_allowdomain(interaction: discord.Interaction, domain: str):
    domain = domain.lower().strip().removeprefix("https://").removeprefix("http://").removeprefix("www.")
    domain = domain.split("/")[0]
    cfg = get_automod_settings(interaction.guild.id)
    domains = cfg.setdefault("allowed_domains", list(DEFAULT_ALLOWED_DOMAINS))
    if domain in domains:
        return await interaction.response.send_message(
            "Domain is already allowed.", ephemeral=True
        )
    domains.append(domain)
    save()
    await interaction.response.send_message(
        f"✅ `{domain}` is now allowed.", ephemeral=True
    )


@automod.command(name="removedomain", description="Remove a domain from the whitelist")
@app_commands.describe(domain="e.g. example.com")
async def automod_removedomain(interaction: discord.Interaction, domain: str):
    domain = domain.lower().strip()
    cfg = get_automod_settings(interaction.guild.id)
    domains = cfg.get("allowed_domains", [])
    if domain not in domains:
        return await interaction.response.send_message(
            "Domain is not on the list.", ephemeral=True
        )
    domains.remove(domain)
    save()
    await interaction.response.send_message(
        f"✅ `{domain}` has been removed.", ephemeral=True
    )


bot.tree.add_command(automod)

# Anti-Spam: 5 messages in 5 seconds -> 5 minute timeout
recent = defaultdict(lambda: deque(maxlen=5))


# ----------------------------------------------------------------------------
# HONEYPOT
# ----------------------------------------------------------------------------

def get_honeypot(guild_id: int) -> dict:
    hp = data.setdefault("honeypots", {}).setdefault(
        str(guild_id),
        {
            "channel_ids": [],
            "action": "ban",  # ban | kick | timeout
            "timeout_minutes": 60,
            "enabled": True,
            "delete_message": True,
            "log": True,
        },
    )
    # ensure keys exist for older data
    hp.setdefault("channel_ids", [])
    hp.setdefault("action", "ban")
    hp.setdefault("timeout_minutes", 60)
    hp.setdefault("enabled", True)
    hp.setdefault("delete_message", True)
    hp.setdefault("log", True)
    return hp


async def check_honeypot(message: discord.Message) -> bool:
    """If message is in a honeypot channel, punish the author. Returns True if handled."""
    if message.author.bot or not message.guild:
        return False
    # Staff immune
    if message.author.guild_permissions.manage_messages:
        return False
    if message.author.guild_permissions.manage_guild:
        return False

    hp = get_honeypot(message.guild.id)
    if not hp.get("enabled", True):
        return False
    if message.channel.id not in hp.get("channel_ids", []):
        return False

    member = message.author
    action = hp.get("action", "ban")
    reason = "Honeypot triggered"

    # Delete the bait message
    if hp.get("delete_message", True):
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

    # Log
    if hp.get("log", True):
        log = message.guild.get_channel(
            guild_settings(message.guild.id).get("log_channel", 0)
        )
        if log:
            embed = discord.Embed(
                title="🍯 Honeypot Triggered",
                color=discord.Color.dark_red(),
            )
            embed.add_field(
                name="User",
                value=f"{member.mention} (`{member.id}`)",
                inline=False,
            )
            embed.add_field(name="Channel", value=message.channel.mention, inline=True)
            embed.add_field(name="Action", value=action.upper(), inline=True)
            embed.add_field(
                name="Message",
                value=(message.content or "*empty*")[:500],
                inline=False,
            )
            try:
                await log.send(embed=embed)
            except discord.Forbidden:
                pass

    # Punish
    try:
        if action == "ban":
            await member.ban(reason=reason, delete_message_days=0)
        elif action == "kick":
            await member.kick(reason=reason)
        else:  # timeout
            mins = int(hp.get("timeout_minutes", 60))
            await member.timeout(timedelta(minutes=mins), reason=reason)
    except discord.Forbidden:
        # Can't punish – still count as handled
        pass
    except discord.HTTPException:
        pass

    return True


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if message.author.guild_permissions.manage_messages:
        return

    # Honeypot first – highest priority
    if await check_honeypot(message):
        return

    # Order: Links → Caps → Word filter → Spam
    if await check_links(message):
        return
    if await check_caps(message):
        return
    if await check_filter(message):
        return

    times = recent[(message.guild.id, message.author.id)]
    now = time.time()
    times.append(now)
    if len(times) == 5 and now - times[0] < 5:
        times.clear()
        try:
            await message.author.timeout(timedelta(minutes=5), reason="Spam")
            await message.channel.send(
                f"🔇 {message.author.mention} has been timed out for 5 minutes due to spam.",
                delete_after=10,
            )
        except discord.Forbidden:
            pass


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot or not after.guild:
        return
    if after.author.guild_permissions.manage_messages:
        return
    if await check_honeypot(after):
        return
    if await check_links(after):
        return
    if await check_caps(after):
        return
    await check_filter(after)


# ----------------------------------------------------------------------------
# MAIN ADMIN DASHBOARD
# ----------------------------------------------------------------------------

def main_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    cfg = get_automod_settings(guild.id)
    active_gw = _active_giveaways(guild.id)
    open_tickets = sum(
        1
        for cid, uid in data["tickets"].items()
        if guild.get_channel(int(cid)) is not None
    )
    banned = get_banned_words(guild.id)
    warn_count = sum(
        len(v)
        for gid, users in data.get("warns", {}).items()
        if str(gid) == str(guild.id)
        for v in users.values()
    )
    hp = get_honeypot(guild.id)
    hp_channels = hp.get("channel_ids", [])

    embed = discord.Embed(
        title="⚙️ Admin Dashboard",
        description=(
            f"Server: **{guild.name}**\n"
            "Control everything with the buttons below."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🎉 Giveaways",
        value=f"**{len(active_gw)}** active",
        inline=True,
    )
    embed.add_field(
        name="🎫 Tickets",
        value=f"**{open_tickets}** open",
        inline=True,
    )
    embed.add_field(
        name="⚠️ Warnings",
        value=f"**{warn_count}** total",
        inline=True,
    )
    embed.add_field(
        name="🍯 Honeypot",
        value=(
            f"{'✅ ON' if hp.get('enabled') else '❌ OFF'} · "
            f"**{len(hp_channels)}** channel(s) · "
            f"Action: **{hp.get('action', 'ban').upper()}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛡️ Auto-Mod",
        value=(
            f"Link: {'✅' if cfg.get('anti_link') else '❌'} · "
            f"Invite: {'✅' if cfg.get('anti_invite') else '❌'} · "
            f"Caps: {'✅' if cfg.get('anti_caps') else '❌'}\n"
            f"Banned words: **{len(banned)}**"
        ),
        inline=False,
    )
    log_id = guild_settings(guild.id).get("log_channel")
    cat_id = guild_settings(guild.id).get("category")
    role_id = guild_settings(guild.id).get("support_role")
    embed.add_field(
        name="📌 Setup",
        value=(
            f"Log channel: {f'<#{log_id}>' if log_id else '—'}\n"
            f"Ticket category: {f'<#{cat_id}>' if cat_id else '—'}\n"
            f"Support role: {f'<@&{role_id}>' if role_id else '—'}"
        ),
        inline=False,
    )
    embed.set_footer(text="Manage Server permission required · Panel expires after 3 min")
    return embed


class FilterAddModal(discord.ui.Modal, title="Add Banned Word"):
    word = discord.ui.TextInput(
        label="Word or phrase",
        placeholder="e.g. stealer, limited account",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        w = self.word.value.lower().strip()
        words = get_banned_words(interaction.guild.id)
        if w in words:
            return await interaction.response.send_message(
                "Already on the list.", ephemeral=True
            )
        words.append(w)
        save()
        await interaction.response.send_message(
            f"✅ `{w}` is now banned.", ephemeral=True
        )


class FilterRemoveModal(discord.ui.Modal, title="Remove Banned Word"):
    word = discord.ui.TextInput(
        label="Word or phrase",
        placeholder="Exact word to remove",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        w = self.word.value.lower().strip()
        words = get_banned_words(interaction.guild.id)
        if w not in words:
            return await interaction.response.send_message(
                "Not on the list.", ephemeral=True
            )
        words.remove(w)
        save()
        await interaction.response.send_message(
            f"✅ `{w}` is allowed again.", ephemeral=True
        )


class DomainAddModal(discord.ui.Modal, title="Allow Domain"):
    domain = discord.ui.TextInput(
        label="Domain",
        placeholder="e.g. example.com",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        d = (
            self.domain.value.lower()
            .strip()
            .removeprefix("https://")
            .removeprefix("http://")
            .removeprefix("www.")
        )
        d = d.split("/")[0]
        cfg = get_automod_settings(interaction.guild.id)
        domains = cfg.setdefault("allowed_domains", list(DEFAULT_ALLOWED_DOMAINS))
        if d in domains:
            return await interaction.response.send_message(
                "Already allowed.", ephemeral=True
            )
        domains.append(d)
        save()
        await interaction.response.send_message(
            f"✅ `{d}` is now allowed.", ephemeral=True
        )


class AdminDashboardView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    # ---- Row 0: Modules ----
    @discord.ui.button(label="Giveaways", emoji="🎉", style=discord.ButtonStyle.primary, row=0)
    async def btn_giveaways(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=dashboard_embed(interaction.guild),
            view=GiveawayDashboardView(interaction.guild.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Honeypot", emoji="🍯", style=discord.ButtonStyle.primary, row=0)
    async def btn_honeypot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=honeypot_panel_embed(interaction.guild),
            view=HoneypotPanelView(interaction.guild),
            ephemeral=True,
        )

    @discord.ui.button(label="Auto-Mod", emoji="🛡️", style=discord.ButtonStyle.primary, row=0)
    async def btn_automod(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = AutoModPanelView(interaction.guild.id)
        await interaction.response.send_message(
            embed=automod_panel_embed(interaction.guild),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Filter", emoji="🔤", style=discord.ButtonStyle.primary, row=0)
    async def btn_filter(self, interaction: discord.Interaction, button: discord.ui.Button):
        words = get_banned_words(interaction.guild.id)
        text = ", ".join(f"`{w}`" for w in words[:40]) or "None"
        if len(words) > 40:
            text += f" … (+{len(words) - 40})"
        embed = discord.Embed(
            title="🔤 Banned Words",
            description=text,
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"{len(words)} words total")
        await interaction.response.send_message(
            embed=embed, view=FilterPanelView(), ephemeral=True
        )

    @discord.ui.button(label="Tickets", emoji="🎫", style=discord.ButtonStyle.primary, row=1)
    async def btn_tickets(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = guild_settings(interaction.guild.id)
        open_t = [
            (cid, uid)
            for cid, uid in data["tickets"].items()
            if interaction.guild.get_channel(int(cid))
        ]
        embed = discord.Embed(title="🎫 Tickets", color=discord.Color.green())
        embed.add_field(
            name="Open tickets",
            value=str(len(open_t)) or "0",
            inline=True,
        )
        embed.add_field(
            name="Category",
            value=f"<#{cfg['category']}>" if cfg.get("category") else "Not set",
            inline=True,
        )
        embed.add_field(
            name="Support role",
            value=f"<@&{cfg['support_role']}>" if cfg.get("support_role") else "Not set",
            inline=True,
        )
        embed.add_field(
            name="Log channel",
            value=f"<#{cfg['log_channel']}>" if cfg.get("log_channel") else "Not set",
            inline=True,
        )
        if open_t:
            lines = [f"<#{cid}> — <@{uid}>" for cid, uid in open_t[:15]]
            embed.add_field(
                name="Current tickets",
                value="\n".join(lines),
                inline=False,
            )
        embed.set_footer(text="Use /ticket_setup to configure the panel")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- Row 1/2: Quick toggles ----
    @discord.ui.button(label="Toggle Link", emoji="🔗", style=discord.ButtonStyle.secondary, row=1)
    async def btn_toggle_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_automod_settings(interaction.guild.id)
        cfg["anti_link"] = not cfg.get("anti_link", True)
        save()
        state = "ON" if cfg["anti_link"] else "OFF"
        await interaction.response.send_message(
            f"Anti-Link is now **{state}**.", ephemeral=True
        )
        try:
            await interaction.message.edit(embed=main_dashboard_embed(interaction.guild))
        except Exception:
            pass

    @discord.ui.button(label="Toggle Invite", emoji="📨", style=discord.ButtonStyle.secondary, row=1)
    async def btn_toggle_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_automod_settings(interaction.guild.id)
        cfg["anti_invite"] = not cfg.get("anti_invite", True)
        save()
        state = "ON" if cfg["anti_invite"] else "OFF"
        await interaction.response.send_message(
            f"Anti-Invite is now **{state}**.", ephemeral=True
        )
        try:
            await interaction.message.edit(embed=main_dashboard_embed(interaction.guild))
        except Exception:
            pass

    @discord.ui.button(label="Toggle Caps", emoji="🔠", style=discord.ButtonStyle.secondary, row=2)
    async def btn_toggle_caps(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_automod_settings(interaction.guild.id)
        cfg["anti_caps"] = not cfg.get("anti_caps", True)
        save()
        state = "ON" if cfg["anti_caps"] else "OFF"
        await interaction.response.send_message(
            f"Anti-Caps is now **{state}**.", ephemeral=True
        )
        try:
            await interaction.message.edit(embed=main_dashboard_embed(interaction.guild))
        except Exception:
            pass

    # ---- Row 2: Utility ----
    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.grey, row=2)
    async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=main_dashboard_embed(interaction.guild),
            view=AdminDashboardView(interaction.guild.id),
        )

    @discord.ui.button(label="Close", emoji="✖️", style=discord.ButtonStyle.red, row=2)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Dashboard closed.", embed=None, view=None
        )


def honeypot_panel_embed(guild: discord.Guild) -> discord.Embed:
    hp = get_honeypot(guild.id)
    channels = hp.get("channel_ids", [])
    ch_text = ", ".join(f"<#{c}>" for c in channels[:15]) or "*None selected*"
    if len(channels) > 15:
        ch_text += f" … (+{len(channels) - 15})"
    embed = discord.Embed(
        title="🍯 Honeypot Panel",
        description=(
            "Anyone (except staff) who posts in a honeypot channel "
            "is automatically punished.\n\n"
            "Select channels below to add/remove them as honeypots."
        ),
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name="Status",
        value="✅ ENABLED" if hp.get("enabled") else "❌ DISABLED",
        inline=True,
    )
    embed.add_field(
        name="Action",
        value=f"**{hp.get('action', 'ban').upper()}**",
        inline=True,
    )
    if hp.get("action") == "timeout":
        embed.add_field(
            name="Timeout",
            value=f"{hp.get('timeout_minutes', 60)} min",
            inline=True,
        )
    embed.add_field(
        name=f"Honeypot channels ({len(channels)})",
        value=ch_text,
        inline=False,
    )
    embed.set_footer(text="Staff with Manage Messages are immune")
    return embed


class HoneypotChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild: discord.Guild):
        super().__init__(
            placeholder="Select channels to toggle as honeypot…",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=min(10, 25),
            custom_id="hp_channel_select",
        )
        self.guild_ref = guild

    async def callback(self, interaction: discord.Interaction):
        hp = get_honeypot(interaction.guild.id)
        ids = hp.setdefault("channel_ids", [])
        added, removed = [], []
        for ch in self.values:
            if ch.id in ids:
                ids.remove(ch.id)
                removed.append(ch.mention)
            else:
                ids.append(ch.id)
                added.append(ch.mention)
        save()
        parts = []
        if added:
            parts.append(f"➕ Added: {', '.join(added)}")
        if removed:
            parts.append(f"➖ Removed: {', '.join(removed)}")
        await interaction.response.edit_message(
            embed=honeypot_panel_embed(interaction.guild),
            view=HoneypotPanelView(interaction.guild),
        )
        if parts:
            await interaction.followup.send("\n".join(parts), ephemeral=True)


class HoneypotPanelView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(HoneypotChannelSelect(guild))

    @discord.ui.button(label="Enable/Disable", emoji="⚡", style=discord.ButtonStyle.primary, row=1)
    async def btn_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        hp = get_honeypot(interaction.guild.id)
        hp["enabled"] = not hp.get("enabled", True)
        save()
        await interaction.response.edit_message(
            embed=honeypot_panel_embed(interaction.guild),
            view=HoneypotPanelView(interaction.guild),
        )

    @discord.ui.button(label="Action: Ban", emoji="🔨", style=discord.ButtonStyle.danger, row=1)
    async def btn_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        hp = get_honeypot(interaction.guild.id)
        hp["action"] = "ban"
        save()
        await interaction.response.edit_message(
            embed=honeypot_panel_embed(interaction.guild),
            view=HoneypotPanelView(interaction.guild),
        )

    @discord.ui.button(label="Action: Kick", emoji="👢", style=discord.ButtonStyle.secondary, row=1)
    async def btn_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        hp = get_honeypot(interaction.guild.id)
        hp["action"] = "kick"
        save()
        await interaction.response.edit_message(
            embed=honeypot_panel_embed(interaction.guild),
            view=HoneypotPanelView(interaction.guild),
        )

    @discord.ui.button(label="Action: Timeout", emoji="🔇", style=discord.ButtonStyle.secondary, row=2)
    async def btn_timeout(self, interaction: discord.Interaction, button: discord.ui.Button):
        hp = get_honeypot(interaction.guild.id)
        hp["action"] = "timeout"
        save()
        await interaction.response.edit_message(
            embed=honeypot_panel_embed(interaction.guild),
            view=HoneypotPanelView(interaction.guild),
        )

    @discord.ui.button(label="Clear All", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def btn_clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        hp = get_honeypot(interaction.guild.id)
        hp["channel_ids"] = []
        save()
        await interaction.response.edit_message(
            embed=honeypot_panel_embed(interaction.guild),
            view=HoneypotPanelView(interaction.guild),
        )

    @discord.ui.button(label="Back", emoji="⬅️", style=discord.ButtonStyle.grey, row=2)
    async def btn_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=main_dashboard_embed(interaction.guild),
            view=AdminDashboardView(interaction.guild.id),
        )


def automod_panel_embed(guild: discord.Guild) -> discord.Embed:
    cfg = get_automod_settings(guild.id)
    domains = cfg.get("allowed_domains", [])
    domain_text = ", ".join(f"`{d}`" for d in domains[:12])
    if len(domains) > 12:
        domain_text += f" … (+{len(domains) - 12})"
    embed = discord.Embed(title="🛡️ Auto-Mod Panel", color=discord.Color.blue())
    embed.add_field(
        name="Anti-Link",
        value="✅ ON" if cfg.get("anti_link") else "❌ OFF",
        inline=True,
    )
    embed.add_field(
        name="Anti-Invite",
        value="✅ ON" if cfg.get("anti_invite") else "❌ OFF",
        inline=True,
    )
    embed.add_field(
        name="Anti-Caps",
        value="✅ ON" if cfg.get("anti_caps") else "❌ OFF",
        inline=True,
    )
    embed.add_field(
        name="Caps threshold",
        value=f"{cfg.get('caps_percent', 70)}% (min {cfg.get('caps_min_length', 8)} chars)",
        inline=False,
    )
    embed.add_field(name="Allowed domains", value=domain_text or "—", inline=False)
    return embed


class AutoModPanelView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id

    @discord.ui.button(label="Link ON/OFF", emoji="🔗", style=discord.ButtonStyle.secondary, row=0)
    async def t_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_automod_settings(interaction.guild.id)
        cfg["anti_link"] = not cfg.get("anti_link", True)
        save()
        await interaction.response.edit_message(
            embed=automod_panel_embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Invite ON/OFF", emoji="📨", style=discord.ButtonStyle.secondary, row=0)
    async def t_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_automod_settings(interaction.guild.id)
        cfg["anti_invite"] = not cfg.get("anti_invite", True)
        save()
        await interaction.response.edit_message(
            embed=automod_panel_embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Caps ON/OFF", emoji="🔠", style=discord.ButtonStyle.secondary, row=0)
    async def t_caps(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_automod_settings(interaction.guild.id)
        cfg["anti_caps"] = not cfg.get("anti_caps", True)
        save()
        await interaction.response.edit_message(
            embed=automod_panel_embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Allow Domain", emoji="➕", style=discord.ButtonStyle.green, row=1)
    async def add_domain(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DomainAddModal())

    @discord.ui.button(label="Back", emoji="⬅️", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=main_dashboard_embed(interaction.guild),
            view=AdminDashboardView(interaction.guild.id),
        )


class FilterPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Add Word", emoji="➕", style=discord.ButtonStyle.green, row=0)
    async def add_word(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FilterAddModal())

    @discord.ui.button(label="Remove Word", emoji="➖", style=discord.ButtonStyle.red, row=0)
    async def remove_word(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FilterRemoveModal())

    @discord.ui.button(label="Refresh List", emoji="🔄", style=discord.ButtonStyle.grey, row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        words = get_banned_words(interaction.guild.id)
        text = ", ".join(f"`{w}`" for w in words[:40]) or "None"
        if len(words) > 40:
            text += f" … (+{len(words) - 40})"
        embed = discord.Embed(
            title="🔤 Banned Words",
            description=text,
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"{len(words)} words total")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Back", emoji="⬅️", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=main_dashboard_embed(interaction.guild),
            view=AdminDashboardView(interaction.guild.id),
        )


@bot.tree.command(name="dashboard", description="Open the Admin Dashboard")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def dashboard_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=main_dashboard_embed(interaction.guild),
        view=AdminDashboardView(interaction.guild.id),
        ephemeral=True,
    )


@bot.tree.command(name="honeypot", description="Open the Honeypot panel")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def honeypot_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=honeypot_panel_embed(interaction.guild),
        view=HoneypotPanelView(interaction.guild),
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to do that."
    else:
        msg = f"Error: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is missing! Set the variable in Railway under Variables."
        )
    bot.run(TOKEN)
