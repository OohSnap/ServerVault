import os, re, json, zipfile, hashlib, asyncio, traceback, io, aiohttp
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

print("Hello! Starting ServerVault 6.0 (Tiered & Monetized)...")
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN: raise RuntimeError("DISCORD_TOKEN missing from .env.")

GUILD_ID = int(os.getenv("GUILD_ID", "925681527579349042"))
SKU_PRO = int(os.getenv("SKU_PRO", "0"))
SKU_BUSINESS = int(os.getenv("SKU_BUSINESS", "0"))
SKU_ENTERPRISE = int(os.getenv("SKU_ENTERPRISE", "0"))

BASE_DIR = Path(__file__).resolve().parent
BACKUP_DIR, LOG_DIR, CONFIG_FILE = BASE_DIR / "backups", BASE_DIR / "logs", BASE_DIR / "servervault_config.json"
BACKUP_DIR.mkdir(exist_ok=True); LOG_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "retention": 3, "automatic_enabled": True, "automatic_interval_minutes": 720,
    "announce_backups": False, "announcement_channel_id": None, "backup_assets": True,
    "max_asset_mb": 8, "deduplicate_backups": True, "create_pre_restore_backup": True, "guilds": {},
}

TIER_LIMITS = {
    0: {"name": "Free", "retention": 3, "interval_min": 0, "messages": 0, "assets": False, "roles": 0},
    1: {"name": "Pro ($3.99)", "retention": 30, "interval_min": 720, "messages": 50, "assets": True, "roles": 2500},
    2: {"name": "Business ($6.99)", "retention": 80, "interval_min": 360, "messages": 100, "assets": True, "roles": 25000},
    3: {"name": "Enterprise ($11.99)", "retention": 200, "interval_min": 120, "messages": 250, "assets": True, "roles": 500000},
}

def load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists(): save_config(DEFAULT_CONFIG.copy()); return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f: cfg = json.load(f)
        merged = DEFAULT_CONFIG.copy(); merged.update(cfg); return merged
    except Exception: return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict[str, Any]):
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=4, ensure_ascii=False)
    tmp.replace(CONFIG_FILE)

config = load_config()

def get_guild_config(guild_id: int) -> Dict[str, Any]:
    guilds = config.setdefault("guilds", {})
    key = str(guild_id)
    settings = {k: deepcopy(v) for k, v in DEFAULT_CONFIG.items() if k != "guilds"}
    for k, v in config.items():
        if k != "guilds": settings[k] = v
    if isinstance(guilds.get(key), dict): settings.update(guilds[key])
    return settings

def save_guild_config(guild_id: int, settings: Dict[str, Any]):
    config.setdefault("guilds", {})[str(guild_id)] = dict(settings); save_config(config)

def migrate_guild_config(guild: discord.Guild):
    key = str(guild.id); guilds = config.setdefault("guilds", {})
    if key not in guilds:
        guilds[key] = {k: deepcopy(v) for k, v in get_guild_config(guild.id).items() if k != "guilds"}
        save_config(config)
    return guilds[key]

intents = discord.Intents.default()
intents.guilds, intents.members, intents.emojis_and_stickers = True, True, True

class ServerVaultBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.session: Optional[aiohttp.ClientSession] = None
        self.guild_tiers: Dict[int, int] = {}

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        target = [discord.Object(id=GUILD_ID)] if GUILD_ID else []
        for g in target:
            self.tree.copy_global_to(guild=g)
            synced = await self.tree.sync(guild=g)
            print(f"Synced {len(synced)} commands to guild {g.id}.")
        if not automatic_backup_loop.is_running(): automatic_backup_loop.start()

    async def close(self):
        if self.session: await self.session.close()
        await super().close()

bot = ServerVaultBot()
backup_lock = asyncio.Lock()
last_automatic_backup = {}

def utc_now() -> datetime: return datetime.now(timezone.utc)
def timestamp() -> str: return utc_now().strftime("%Y-%m-%d_%H-%M-%S")
def safe_filename(name: str) -> str: return re.sub(r"[^a-zA-Z0-9._-]", "_", str(name))

def get_backup_path(filename: str) -> Path:
    filename = os.path.basename(filename)
    if not filename.endswith(".zip"): filename += ".zip"
    p = (BACKUP_DIR / filename).resolve()
    if p.parent != BACKUP_DIR.resolve(): raise ValueError("Invalid filename.")
    return p

def log_operation(guild: discord.Guild, msg: str):
    try:
        with open(LOG_DIR / f"{guild.id}.log", "a", encoding="utf-8") as f:
            f.write(f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}\n")
    except Exception: pass

def sha256_backup_contents(files: Dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        if name == "manifest.json": continue
        digest.update(name.encode("utf-8") + b"\0" + files[name] + b"\0")
    return digest.hexdigest()

def json_bytes(data: Any) -> bytes:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")

async def download_asset(url: str, max_bytes: int) -> bytes:
    if not bot.session: raise RuntimeError("No web session.")
    async with bot.session.get(str(url)) as r:
        if r.status != 200: raise Exception(f"HTTP {r.status}")
        data = await r.read()
        if len(data) > max_bytes: raise Exception("Asset too large")
        return data

def get_guild_tier(interaction: Optional[discord.Interaction] = None, guild: Optional[discord.Guild] = None) -> int:
    gid = interaction.guild_id if interaction and interaction.guild else (guild.id if guild else 0)
    if not gid: return 0
    if interaction and hasattr(interaction, "entitlements"):
        active = {e.sku_id for e in interaction.entitlements if getattr(e, "guild_id", 0) == gid and not e.is_expired()}
        tier = 3 if SKU_ENTERPRISE in active else (2 if SKU_BUSINESS in active else (1 if SKU_PRO in active else 0))
        bot.guild_tiers[gid] = tier
        return tier
    return bot.guild_tiers.get(gid, 0)

async def archive_channel_messages(channel: discord.TextChannel, limit: int) -> list:
    if limit <= 0 or not isinstance(channel, discord.TextChannel): return []
    msgs, seen = [], set()
    try:
        pins = await channel.pins()
        for m in pins:
            seen.add(m.id)
            msgs.append({"id": m.id, "author": str(m.author), "content": m.content, "pinned": True, "ts": m.created_at.isoformat()})
        async for m in channel.history(limit=limit):
            if m.id not in seen:
                msgs.append({"id": m.id, "author": str(m.author), "content": m.content, "pinned": False, "ts": m.created_at.isoformat()})
    except Exception: pass
    return msgs

async def snapshot_member_roles(guild: discord.Guild, max_members: int) -> Dict[str, list]:
    if max_members <= 0: return {}
    if guild.member_count > len(guild.members) and guild.member_count <= 25000:
        try: await guild.chunk(cache=True)
        except Exception: pass
    data = {}
    for idx, m in enumerate(guild.members):
        if idx >= max_members: break
        r_ids = [r.id for r in m.roles if not r.is_default()]
        if r_ids: data[str(m.id)] = r_ids
    return data

def serialize_permissions(ow: discord.PermissionOverwrite) -> Dict[str, int]:
    a, d = ow.pair()
    return {"allow": a.value, "deny": d.value}

def deserialize_overwrite(data: Dict[str, int]) -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite.from_pair(discord.Permissions(data.get("allow", 0)), discord.Permissions(data.get("deny", 0)))

def serialize_role(role: discord.Role) -> Dict[str, Any]:
    return {"id": role.id, "name": role.name, "position": role.position, "color": role.color.value,
            "hoist": role.hoist, "mentionable": role.mentionable, "permissions": role.permissions.value, "managed": role.managed}

def serialize_channel(c: discord.abc.GuildChannel) -> Dict[str, Any]:
    d = {"id": c.id, "name": c.name, "position": getattr(c, "position", 0), "type": str(c.type),
         "category_id": c.category.id if getattr(c, "category", None) else None,
         "permissions": [{"target_type": "role" if isinstance(t, discord.Role) else "member", "target_id": t.id,
                          "permissions": serialize_permissions(ow)} for t, ow in c.overwrites.items() if isinstance(t, (discord.Role, discord.Member))]}
    if isinstance(c, discord.TextChannel): d.update({"topic": c.topic, "nsfw": c.nsfw, "slowmode_delay": c.slowmode_delay})
    elif isinstance(c, (discord.VoiceChannel, discord.StageChannel)): d.update({"bitrate": c.bitrate, "user_limit": c.user_limit})
    return d

def _write_zip(path: Path, files: Dict[str, bytes]):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items(): z.writestr(name, data)

async def create_backup(guild: discord.Guild, reason="manual", tier: Optional[int] = None):
    async with backup_lock:
        try:
            tier = tier if tier is not None else get_guild_tier(guild=guild)
            limits = TIER_LIMITS[tier]
            settings = get_guild_config(guild.id)
            
            snapshot = {
                "server": {"id": guild.id, "name": guild.name, "member_count": guild.member_count},
                "roles": [serialize_role(r) for r in sorted(guild.roles, key=lambda r: r.position)],
                "channels": [serialize_channel(c) for c in sorted(guild.channels, key=lambda c: (getattr(c, "position", 0), c.id))],
                "emojis": [{"id": e.id, "name": e.name, "animated": e.animated, "url": str(e.url)} for e in guild.emojis],
                "stickers": [{"id": s.id, "name": s.name, "format_type": str(s.format_type), "url": str(s.url)} for s in guild.stickers]
            }
            files = {
                "server.json": json_bytes(snapshot["server"]), "roles.json": json_bytes(snapshot["roles"]),
                "channels.json": json_bytes(snapshot["channels"]), "emojis.json": json_bytes(snapshot["emojis"]),
                "stickers.json": json_bytes(snapshot["stickers"])
            }

            # 1. Tier-based Message Archiving
            if limits["messages"] > 0:
                messages_map = {}
                for ch in guild.text_channels:
                    m_data = await archive_channel_messages(ch, limits["messages"])
                    if m_data: messages_map[str(ch.id)] = m_data
                files["messages.json"] = json_bytes(messages_map)

            # 2. Tier-based Member Role Preservation
            if limits["roles"] > 0:
                member_roles = await snapshot_member_roles(guild, limits["roles"])
                files["member_roles.json"] = json_bytes(member_roles)

            # 3. Tier-based Assets (Emojis/Stickers)
            asset_idx = {"emojis": [], "stickers": []}
            if limits["assets"] and settings.get("backup_assets", True):
                max_bytes = max(1, int(settings.get("max_asset_mb", 8))) * 1024 * 1024
                for e in guild.emojis:
                    try:
                        d = await download_asset(e.url, max_bytes)
                        p = f"assets/emojis/{e.id}.{'gif' if e.animated else 'png'}"
                        files[p] = d; asset_idx["emojis"].append({"id": e.id, "name": e.name, "path": p})
                    except Exception: pass
                for s in guild.stickers:
                    try:
                        d = await download_asset(s.url, max_bytes)
                        p = f"assets/stickers/{s.id}.png"
                        files[p] = d; asset_idx["stickers"].append({"id": s.id, "name": s.name, "path": p})
                    except Exception: pass

            files["assets.json"] = json_bytes(asset_idx)
            content_hash = sha256_backup_contents(files)

            if settings.get("deduplicate_backups", True) and reason != "manual_priority":
                latest = resolve_backup(guild)
                if latest:
                    try:
                        if read_manifest(latest).get("content_sha256") == content_hash:
                            os.utime(latest, None)
                            log_operation(guild, f"BACKUP DEDUPLICATED | {latest.name}")
                            return latest
                    except Exception: pass

            manifest = {
                "servervault_version": "6.0", "server_id": guild.id, "server_name": guild.name,
                "tier": limits["name"], "created_at": utc_now().isoformat(), "reason": reason,
                "content_sha256": content_hash, "asset_counts": {"emojis": len(asset_idx["emojis"]), "stickers": len(asset_idx["stickers"])}
            }
            files["manifest.json"] = json_bytes(manifest)
            filename = f"{safe_filename(guild.name)}_{guild.id}_{timestamp()}.zip"
            path = BACKUP_DIR / filename
            await asyncio.to_thread(_write_zip, path, files)
            log_operation(guild, f"BACKUP CREATED | {filename} | tier={limits['name']}")
            await enforce_retention(guild, limits["retention"])
            return path
        except Exception as exc:
            log_operation(guild, f"BACKUP FAILED | {exc}"); raise

def list_backups(guild: discord.Guild) -> list[Path]:
    pfx = f"{safe_filename(guild.name)}_{guild.id}_"
    b = [p for p in BACKUP_DIR.glob("*.zip") if p.name.startswith(pfx)]
    b.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return b

async def enforce_retention(guild: discord.Guild, tier_max: int = 3):
    try:
        limit = min(int(get_guild_config(guild.id).get("retention", tier_max)), tier_max)
        b = list_backups(guild)
        if len(b) > limit:
            for old in b[limit:]:
                try: old.unlink(); log_operation(guild, f"RETENTION DELETE | {old.name}")
                except Exception: pass
    except Exception: pass

def validate_backup(p: Path) -> Tuple[bool, str]:
    if not p.exists(): return False, "Missing file."
    try:
        with zipfile.ZipFile(p, "r") as z:
            if z.testzip(): return False, "Corrupted archive."
            req = {"server.json", "roles.json", "channels.json", "manifest.json"}
            if req - set(z.namelist()): return False, "Missing core files."
            files = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
            m = json.loads(files["manifest.json"].decode("utf-8"))
            if m.get("content_sha256") != sha256_backup_contents(files): return False, "Hash mismatch."
            return True, f"Valid | Server: {m.get('server_name')} | Tier: {m.get('tier', 'Free')}"
    except Exception as exc: return False, str(exc)

def load_backup(p: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(p, "r") as z:
        snap = {k.replace(".json", ""): json.loads(z.read(k)) for k in z.namelist() if k.endswith(".json")}
        snap.setdefault("assets", {"emojis": [], "stickers": []})
    return snap

def read_manifest(p: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(p, "r") as z: return json.loads(z.read("manifest.json").decode("utf-8"))

def resolve_backup(guild: discord.Guild, filename: Optional[str] = None) -> Optional[Path]:
    b = list_backups(guild)
    if not b: return None
    if filename:
        try:
            p = get_backup_path(filename)
            return p if p in b else None
        except Exception: return None
    return b[0]

async def backup_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not interaction.guild: return []
    res = []
    for b in list_backups(interaction.guild):
        if current.lower() in b.name.lower():
            res.append(app_commands.Choice(name=f"{b.name} ({b.stat().st_size/1024/1024:.2f} MB)"[:100], value=b.name))
        if len(res) >= 25: break
    return res

async def restore_roles(guild: discord.Guild, snapshot: Dict) -> Dict[int, discord.Role]:
    role_map = {guild.default_role.id: guild.default_role}
    for r in sorted(snapshot["roles"], key=lambda x: x.get("position", 0)):
        if r["name"] == "@everyone" or r.get("managed"): continue
        existing = discord.utils.get(guild.roles, name=r["name"])
        args = {"permissions": discord.Permissions(r.get("permissions", 0)), "colour": discord.Colour(r.get("color", 0)),
                "hoist": r.get("hoist", False), "mentionable": r.get("mentionable", False), "reason": "ServerVault restore"}
        try: role_map[r["id"]] = await existing.edit(**args) if existing else await guild.create_role(name=r["name"], **args)
        except Exception: pass
    return role_map

async def restore_channels(guild: discord.Guild, snapshot: Dict):
    cat_map = {}
    for d in sorted([c for c in snapshot["channels"] if c["type"] == "category"], key=lambda c: c.get("position", 0)):
        ex = discord.utils.get(guild.categories, name=d["name"])
        cat_map[d["id"]] = ex if ex else await guild.create_category(name=d["name"])
    for d in sorted([c for c in snapshot["channels"] if c["type"] != "category"], key=lambda c: c.get("position", 0)):
        ex = discord.utils.get(guild.channels, name=d["name"])
        cat = cat_map.get(d.get("category_id"))
        if not ex:
            try:
                if d["type"] == "text": await guild.create_text_channel(name=d["name"], category=cat)
                elif d["type"] == "voice": await guild.create_voice_channel(name=d["name"], category=cat)
            except Exception: pass

async def perform_restore(guild: discord.Guild, snap: Dict):
    log_operation(guild, "RESTORE STARTED")
    await restore_roles(guild, snap)
    await restore_channels(guild, snap)
    log_operation(guild, "RESTORE FINISHED")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}. ServerVault 6.0 is online! 🛡️")
    if target := bot.get_guild(GUILD_ID): migrate_guild_config(target)

@tasks.loop(minutes=15)
async def automatic_backup_loop():
    try:
        guilds = [bot.get_guild(GUILD_ID)] if GUILD_ID else bot.guilds
        for g in [g for g in guilds if g]:
            tier = get_guild_tier(guild=g)
            limits = TIER_LIMITS[tier]
            if limits["interval_min"] <= 0: continue
            last = last_automatic_backup.get(g.id)
            if last and (utc_now() - last).total_seconds() / 60 < limits["interval_min"]: continue
            try:
                p = await create_backup(g, reason="automatic", tier=tier)
                last_automatic_backup[g.id] = utc_now()
                print(f"Auto backup complete: {g.name} ({limits['name']})")
            except Exception: pass
    except Exception: pass

@automatic_backup_loop.before_loop
async def before_automatic_backup_loop(): await bot.wait_until_ready()

def vault_embed(title, description=None):
    embed = discord.Embed(title=f"🛡️ {title}", description=description, colour=discord.Colour.blurple(), timestamp=utc_now())
    embed.set_footer(text="ServerVault 6.0 • Enterprise Disaster Recovery")
    return embed

# ============================================================
# COMMANDS
# ============================================================

@bot.tree.command(name="subscribe", description="View ServerVault Pro plans and upgrade this server.")
async def subscribe_command(i: discord.Interaction):
    cur_tier = get_guild_tier(interaction=i)
    emb = vault_embed("ServerVault Subscriptions", f"Current Server Status: **{TIER_LIMITS[cur_tier]['name']}**")
    emb.add_field(name="Free ($0)", value="• 3 Backups\n• Manual Only\n• Structure Only", inline=True)
    emb.add_field(name="Pro ($3.99/mo)", value="• 30 Backups\n• 12h Auto Backup\n• 50 Msgs / Channel\n• 2,500 Member Roles", inline=True)
    emb.add_field(name="Business ($6.99/mo)", value="• 80 Backups\n• 6h Auto Backup\n• 100 Msgs / Channel\n• 25,000 Member Roles\n• Direct ZIP Download", inline=True)
    emb.add_field(name="Enterprise ($11.99/mo)", value="• 200 Backups\n• 2h Auto Backup\n• 250 Msgs / Channel\n• Unlimited (100k+) Member Roles\n• Priority Restore", inline=False)
    
    view = discord.ui.View()
    app_id = bot.user.id
    if SKU_PRO: view.add_item(discord.ui.Button(label="Upgrade to Pro", url=f"https://discord.com/application-directory/{app_id}/store/{SKU_PRO}", style=discord.ButtonStyle.link))
    if SKU_BUSINESS: view.add_item(discord.ui.Button(label="Upgrade to Business", url=f"https://discord.com/application-directory/{app_id}/store/{SKU_BUSINESS}", style=discord.ButtonStyle.link))
    if SKU_ENTERPRISE: view.add_item(discord.ui.Button(label="Upgrade to Enterprise", url=f"https://discord.com/application-directory/{app_id}/store/{SKU_ENTERPRISE}", style=discord.ButtonStyle.link))
    await i.response.send_message(embed=emb, view=view, ephemeral=True)

@bot.tree.command(name="backup", description="Create a server backup.")
@app_commands.checks.has_permissions(administrator=True)
async def backup_command(i: discord.Interaction):
    if not i.guild: return
    await i.response.defer(ephemeral=True)
    tier = get_guild_tier(interaction=i)
    try:
        p = await create_backup(i.guild, reason="manual", tier=tier)
        await i.followup.send(f"✅ **Backup completed!**\n`{p.name}`\nPlan: **{TIER_LIMITS[tier]['name']}**", ephemeral=True)
    except Exception as exc: await i.followup.send(f"❌ Backup failed: `{exc}`", ephemeral=True)

@bot.tree.command(name="backups", description="List available server backups.")
@app_commands.checks.has_permissions(administrator=True)
async def backups_command(i: discord.Interaction):
    if not i.guild: return
    b = list_backups(i.guild)
    if not b: return await i.response.send_message("No backups found.", ephemeral=True)
    lines = [f"**{idx}.** `{p.name}` ({p.stat().st_size/1024/1024:.2f} MB)" for idx, p in enumerate(b[:20], 1)]
    await i.response.send_message("## ServerVault Backups\n\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="backupdownload", description="Download a ServerVault backup file (Business+ tier).")
@app_commands.describe(filename="Choose a backup from the list.")
@app_commands.autocomplete(filename=backup_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def backupdownload_command(i: discord.Interaction, filename: Optional[str] = None):
    if not i.guild: return
    if get_guild_tier(interaction=i) < 2:
        return await i.response.send_message("⭐ Direct ZIP downloads require **Business Tier ($6.99/mo)** or above. Use `/subscribe` to upgrade.", ephemeral=True)
    p = resolve_backup(i.guild, filename)
    if not p: return await i.response.send_message("Backup not found.", ephemeral=True)
    await i.response.send_message(file=discord.File(p, filename=p.name), ephemeral=True)

@bot.tree.command(name="dashboard", description="Open the ServerVault control dashboard.")
@app_commands.checks.has_permissions(administrator=True)
async def dashboard_command(i: discord.Interaction):
    if not i.guild: return
    tier = get_guild_tier(interaction=i)
    limits = TIER_LIMITS[tier]
    b = list_backups(i.guild)
    emb = vault_embed("ServerVault Dashboard")
    emb.add_field(name="Current Tier", value=f"**{limits['name']}**", inline=True)
    emb.add_field(name="Backups Stored", value=f"{len(b)} / {limits['retention']}", inline=True)
    emb.add_field(name="Auto Schedule", value=f"Every {limits['interval_min']}m" if limits['interval_min'] else "Manual Only", inline=True)
    emb.add_field(name="Messages Saved", value=f"{limits['messages']} / channel", inline=True)
    emb.add_field(name="Roles Saved", value=f"Up to {limits['roles']:,} members" if limits['roles'] else "Disabled", inline=True)
    emb.add_field(name="Latest Backup", value=f"`{b[0].name}`" if b else "None", inline=False)
    await i.response.send_message(embed=emb, ephemeral=True)

@bot.tree.command(name="restorepreview", description="Preview what a restore would change.")
@app_commands.describe(filename="Choose a backup from the list.")
@app_commands.autocomplete(filename=backup_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def restorepreview_command(i: discord.Interaction, filename: str | None = None):
    if not i.guild: return
    p = resolve_backup(i.guild, filename)
    if not p: return await i.response.send_message("Backup not found.", ephemeral=True)
    val, msg = validate_backup(p)
    if not val: return await i.response.send_message(f"❌ Validation failed: {msg}", ephemeral=True)
    snap = load_backup(p)
    await i.response.send_message(f"## Restore Preview: `{p.name}`\nRoles in Backup: {len(snap.get('roles', []))} | Channels: {len(snap.get('channels', []))}", ephemeral=True)

@bot.tree.command(name="restore", description="Restore server structure from a backup.")
@app_commands.describe(confirm="Set to True to execute.", filename="Choose a backup from the list.")
@app_commands.autocomplete(filename=backup_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def restore_command(i: discord.Interaction, confirm: bool = False, filename: str | None = None):
    if not i.guild: return
    if not confirm: return await i.response.send_message("⚠️ Run `/restore confirm:True` to execute.", ephemeral=True)
    p = resolve_backup(i.guild, filename)
    if not p: return await i.response.send_message("Backup not found.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        val, msg = validate_backup(p)
        if not val: return await i.followup.send(f"❌ Validation failed: {msg}", ephemeral=True)
        await perform_restore(i.guild, load_backup(p))
        await i.followup.send(f"✅ **Restore completed.**\nRestored from: `{p.name}`", ephemeral=True)
    except Exception as exc: await i.followup.send(f"❌ Error: `{exc}`", ephemeral=True)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    msg = "❌ Administrator permission required." if isinstance(error, app_commands.errors.MissingPermissions) else "❌ ServerVault error."
    try:
        if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
        else: await interaction.response.send_message(msg, ephemeral=True)
    except Exception: pass

if __name__ == "__main__":
    print("Connecting to Discord...")
    bot.run(TOKEN)