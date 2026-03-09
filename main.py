import discord
from discord import app_commands
from discord.ext import commands
import os
import aiohttp
from io import BytesIO
from PIL import Image, ImageDraw, ImageOps
from flask import Flask
from threading import Thread
from motor.motor_asyncio import AsyncIOMotorClient

# --- DATABASE SETUP ---
MONGO_URL = os.environ.get('MONGO_URL')
cluster = AsyncIOMotorClient(MONGO_URL)
db = cluster["BountyBot"]
collection_active = db["active_bounty"]
collection_leaderboard = db["leaderboard"]

# --- WEB SERVER ---
app = Flask('')
@app.route('/')
def home(): return "Bounty Board is Online!"
def run(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
def keep_alive(): Thread(target=run).start()

class BountyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True 
        super().__init__(command_prefix="!", intents=intents, activity=discord.Game(name="Hunting Outlaws..."))

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}")

bot = BountyBot()

# --- HELPER: POSTER ---
async def create_wanted_poster(avatar_url, mc_name):
    try:
        async with bot.session.get(avatar_url) as resp:
            data = await resp.read()
        avatar = Image.open(BytesIO(data)).convert("RGBA")
        poster = Image.new('RGB', (400, 550), color=(225, 198, 153))
        draw = ImageDraw.Draw(poster)
        draw.rectangle([15, 15, 385, 25], fill=(60, 40, 0))   
        draw.text((145, 45), "WANTED", fill=(60, 40, 0), stroke_width=1)
        draw.text((175, 75), "DEAD", fill=(60, 40, 0))
        avatar = avatar.resize((260, 260))
        avatar = ImageOps.grayscale(avatar).convert("RGB")
        avatar = ImageOps.colorize(avatar, black=(45, 35, 15), white=(255, 245, 210))
        avatar = ImageOps.expand(avatar, border=10, fill=(60, 40, 0)) 
        poster.paste(avatar, (60, 120))
        draw.text((135, 450), f"{mc_name.upper()}", fill=(100, 0, 0), stroke_width=1)
        buffer = BytesIO()
        poster.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer
    except: return None

# --- COMMANDS ---

@bot.tree.command(name="set_bounty")
async def set_bounty(interaction: discord.Interaction, target_discord: discord.Member, target_mc: str, reward: str):
    if await collection_active.find_one({"type": "current"}):
        return await interaction.response.send_message("❌ A bounty is already active!", ephemeral=True)
    
    await interaction.response.defer()
    
    poster_buffer = await create_wanted_poster(target_discord.display_avatar.url, target_mc)
    
    bounty_data = {
        "type": "current",
        "target_id": target_discord.id,
        "target_mc": target_mc,
        "reward": reward,
        "setter_id": interaction.user.id,
        "proof_url": None
    }
    await collection_active.insert_one(bounty_data)

    embed = discord.Embed(title="⚔️ WANTED DEAD", color=discord.Color.dark_red())
    embed.add_field(name="Target", value=f"`{target_mc}` ({target_discord.mention})", inline=False)
    embed.add_field(name="Reward", value=f"💰 {reward}", inline=False)
    
    if poster_buffer:
        file = discord.File(fp=poster_buffer, filename="poster.png")
        embed.set_image(url="attachment://poster.png")
        await interaction.followup.send(file=file, embed=embed)
    else:
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="status")
async def status(interaction: discord.Interaction):
    active = await collection_active.find_one({"type": "current"})
    if not active: return await interaction.response.send_message("The board is empty.", ephemeral=True)
    
    embed = discord.Embed(title="📜 Current Contract", color=discord.Color.blue())
    embed.add_field(name="Target", value=f"`{active['target_mc']}`", inline=True)
    embed.add_field(name="Reward", value=active['reward'], inline=True)
    if active["proof_url"]:
        embed.set_image(url=active["proof_url"])
        embed.set_footer(text="⚠️ Proof submitted!")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="claim")
async def claim(interaction: discord.Interaction, mc_username: str, proof_image: discord.Attachment):
    active = await collection_active.find_one({"type": "current"})
    if not active: return await interaction.response.send_message("No active bounty.", ephemeral=True)
    if interaction.user.id == active["target_id"]: return await interaction.response.send_message("Nice try.", ephemeral=True)

    await collection_active.update_one({"type": "current"}, {"$set": {"proof_url": proof_image.url}})
    await interaction.response.send_message(f"🚩 {interaction.user.mention} (`{mc_username}`) claimed it! Check `/status`.")

@bot.tree.command(name="leaderboard")
async def leaderboard(interaction: discord.Interaction):
    cursor = collection_leaderboard.find().sort("kills", -1).limit(10)
    users = await cursor.to_list(length=10)
    
    if not users: return await interaction.response.send_message("No legends yet.", ephemeral=True)
    
    description = ""
    for i, user in enumerate(users, 1):
        description += f"**{i}.** <@{user['_id']}> — {user['kills']} Kills\n"
    
    embed = discord.Embed(title="🏆 Top Headhunters", description=description, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="finalize")
@app_commands.checks.has_permissions(manage_messages=True)
async def finalize(interaction: discord.Interaction, winner_discord: discord.Member, winner_mc: str):
    active = await collection_active.find_one({"type": "current"})
    if not active: return await interaction.response.send_message("No bounty active.", ephemeral=True)

    # Update Leaderboard
    await collection_leaderboard.update_one(
        {"_id": winner_discord.id},
        {"$inc": {"kills": 1}},
        upsert=True
    )

    # History Log
    history_channel = discord.utils.get(interaction.guild.channels, name="bounty-history")
    if history_channel:
        log = discord.Embed(title="💀 Hunt Log", color=discord.Color.dark_grey())
        log.add_field(name="Target", value=active['target_mc'], inline=True)
        log.add_field(name="Hunter", value=f"{winner_mc} ({winner_discord.mention})", inline=True)
        log.add_field(name="Payout", value=active['reward'], inline=False)
        if active["proof_url"]: log.set_image(url=active["proof_url"])
        await history_channel.send(embed=log)

    await collection_active.delete_one({"type": "current"})
    await interaction.response.send_message(f"🏆 Bounty on **{active['target_mc']}** finalized!")

@bot.tree.command(name="cancel")
async def cancel(interaction: discord.Interaction):
    active = await collection_active.find_one({"type": "current"})
    if not active: return await interaction.response.send_message("Nothing to cancel.", ephemeral=True)
    if interaction.user.id == active["setter_id"] or interaction.user.guild_permissions.manage_messages:
        await collection_active.delete_one({"type": "current"})
        await interaction.response.send_message("🚫 Bounty revoked.")
    else:
        await interaction.response.send_message("No permission.", ephemeral=True)

if __name__ == "__main__":
    keep_alive()
    bot.run(os.environ.get('DISCORD_TOKEN'))
