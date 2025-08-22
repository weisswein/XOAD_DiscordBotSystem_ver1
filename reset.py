# clear_commands_pointsystem.py
import os, discord
from discord import app_commands
from dotenv import load_dotenv
load_dotenv("Tokensetting.env")
BOT_TOKEN = os.environ["BOT_TOKEN"]
GUILD_ID  = int(os.environ["GUILD_ID"])

intents = discord.Intents.none()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    print("✅ cleared guild commands")
    # await bot.close()

bot.run(BOT_TOKEN)
