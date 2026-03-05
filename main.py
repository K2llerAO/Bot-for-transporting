import json
import logging
from pathlib import Path
import discord
from discord.ext import commands

TOKEN = "Bot token"           # TODO: move to .env later
PREFIX = "!"
ALLOWED = {656929089097170944, 1118539613527613481, 819926557559750657}

EMV_PCT  = 0.10
KG_RATE  = 400
SLOT_RATE = 15625

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")]
)
log = logging.getLogger("transport-bot")

DB     = Path("stats.db")
BACKUP = Path("backup.json")


def backup_read() -> tuple[int, int]:
    if not BACKUP.exists():
        return 0, 0
    try:
        data = json.loads(BACKUP.read_text())
        return data.get("emv", 0), data.get("tickets", 0)
    except Exception:
        log.warning("backup.json broken", exc_info=True)
        return 0, 0


def backup_write(emv: int, tickets: int):
    try:
        BACKUP.write_text(json.dumps({"emv": emv, "tickets": tickets}, indent=2))
    except Exception:
        log.warning("can't write backup", exc_info=True)


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                rowid   INTEGER PRIMARY KEY CHECK (rowid = 1),
                emv     INTEGER NOT NULL DEFAULT 0,
                tickets INTEGER NOT NULL DEFAULT 0
            )
        """)
        row = con.execute("SELECT emv, tickets FROM stats WHERE rowid=1").fetchone()
        if row is None:
            emv, tik = backup_read()
            con.execute("INSERT INTO stats VALUES (1, ?, ?)", (emv, tik))
            log.info(f"db init → restored {emv:,} emv / {tik:,} tickets" if emv or tik else "new db")
        else:
            log.info(f"db loaded → {row['emv']:,} emv / {row['tickets']:,} tickets")


def get_stats() -> tuple[int, int]:
    with db() as con:
        r = con.execute("SELECT emv, tickets FROM stats WHERE rowid=1").fetchone()
        return r["emv"], r["tickets"]


def add_ticket(silver: int) -> tuple[int, int]:
    with db() as con:
        con.execute("UPDATE stats SET emv = emv + ?, tickets = tickets + 1 WHERE rowid=1", (silver,))
        r = con.execute("SELECT emv, tickets FROM stats WHERE rowid=1").fetchone()
    backup_write(r["emv"], r["tickets"])
    return r["emv"], r["tickets"]


def reset_stats():
    with db() as con:
        con.execute("UPDATE stats SET emv=0, tickets=0 WHERE rowid=1")
    backup_write(0, 0)
    log.info("weekly stats reset")


def parse_silver(s: str) -> int:
    s = s.lower().replace(",", "").strip()
    if s.endswith("b"): return int(float(s[:-1]) * 1_000_000_000)
    if s.endswith("m"): return int(float(s[:-1]) * 1_000_000)
    if s.endswith("k"): return int(float(s[:-1]) * 1_000)
    try:
        return int(s)
    except ValueError:
        raise ValueError("bad silver amount")


def mil(v: int | float) -> str:
    return f"{v / 1_000_000:.1f}m"


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


def emb(title: str, color=discord.Color.gold()) -> discord.Embed:
    e = discord.Embed(title=title, color=color)
    e.set_footer(text="Albion Transport")
    return e


def admin_only():
    async def check(ctx):
        if ctx.author.id not in ALLOWED:
            await ctx.send(embed=emb("No access", discord.Color.red()))
            return False
        return True
    return commands.check(check)


@bot.command()
@admin_only()
async def complete(ctx, *, amount: str = None):
    if not amount:
        await ctx.send("`!complete 1.4b`  or  `!complete 850m` etc")
        return

    try:
        val = parse_silver(amount)
        if val <= 0:
            raise ValueError
    except:
        await ctx.send(f"can't parse '{amount}' — try 100m 2.5b 750k")
        return

    new_emv, new_tik = add_ticket(val)
    e = emb("Ticket done")
    e.add_field(name="Added", value=f"{val:,}", inline=False)
    e.add_field(name="Total EMV", value=f"{new_emv:,}", inline=True)
    e.add_field(name="Tickets", value=f"{new_tik:,}", inline=True)
    await ctx.send(embed=e)
    log.info(f"{ctx.author} completed {val:,} → {new_emv:,} emv / {new_tik} tickets")


@bot.command()
@admin_only()
async def stats(ctx):
    emv, tik = get_stats()
    e = emb("Weekly stats")
    e.add_field(name="EMV", value=f"{emv:,}", inline=True)
    e.add_field(name="Tickets", value=f"{tik:,}", inline=True)
    await ctx.send(embed=e)
    log.info(f"{ctx.author} checked stats → {emv:,} / {tik}")


@bot.command()
@admin_only()
async def reset(ctx):
    old_emv, old_tik = get_stats()
    reset_stats()
    e = emb(f"Reset done", discord.Color.green())
    e.description = f"Cleared {old_tik:,} tickets / {old_emv:,} silver"
    await ctx.send(embed=e)
    log.info(f"{ctx.author} reset stats (was {old_emv:,} / {old_tik})")


@bot.command()
@admin_only()
async def fee(ctx, emv: str = None, weight: str = None, slots: str = None):
    if not all([emv, weight, slots]):
        await ctx.send("`!fee 180m 2200 60`   ← emv weight slots")
        return

    try:
        v_emv   = parse_silver(emv)
        v_kg    = parse_silver(weight)
        v_slots = parse_silver(slots)
    except:
        await ctx.send(f"bad input: {emv} {weight} {slots}")
        return

    f_emv   = v_emv   * EMV_PCT
    f_kg    = v_kg    * KG_RATE
    f_slots = v_slots * SLOT_RATE

    fees = [
        (f_emv,   "EMV 10%"),
        (f_kg,    "Weight"),
        (f_slots, "Slots"),
    ]

    highest = max(fees, key=lambda x: x[0])
    fee_val, fee_type = highest

    e = emb("Transport fee")
    e.add_field(name="EMV",   value=f"{mil(f_emv)}",   inline=True)
    e.add_field(name="Weight", value=f"{mil(f_kg)}",    inline=True)
    e.add_field(name="Slots",  value=f"{mil(f_slots)}", inline=True)
    e.add_field(name="→ Fee", value=f"**{mil(fee_val)}** ({fee_type})", inline=False)
    await ctx.send(embed=e)
    log.info(f"{ctx.author} fee calc → {v_emv:,}emv {v_kg:,}kg {v_slots}slots | {fee_val:,.0f} ({fee_type})")


@bot.event
async def on_ready():
    log.info(f"Logged in → {bot.user} ({bot.user.id})")
    try:
        await bot.change_presence(activity=discord.Game("watching transports 🚚"))
    except:
        pass


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return

    log.warning(f"error in {ctx.command or 'unknown'}: {error}", exc_info=True)

    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send("usage wrong — check command format")
    else:
        await ctx.send(embed=emb("something fucked up", discord.Color.red()))


if __name__ == "__main__":
    init()
    bot.run(TOKEN, log_handler=None)