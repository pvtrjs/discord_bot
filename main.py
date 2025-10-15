import discord
import asyncio
import os
from discord.ext import commands

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged on as {bot.user}!')

@bot.command()
async def ping(ctx):
    """Kiểm tra bot có hoạt động không"""
    await ctx.send('Pong!')

# --- Cog Loading ---
async def load_cogs():
    """Tải tất cả các cogs từ thư mục /cogs"""
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
                print(f'Loaded cog: {filename}')
            except Exception as e:
                print(f'Failed to load cog {filename}: {e}')

async def main():
    async with bot:
        await load_cogs()
        await bot.start('')

if __name__ == "__main__":
    asyncio.run(main())
