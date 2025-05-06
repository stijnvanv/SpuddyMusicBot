import discord
from discord.ext import commands
from discord import Embed, Color
from yt_dlp import YoutubeDL
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import os
from discord.ext.commands import cooldown, BucketType
import requests
import re
import difflib
from urllib.parse import quote_plus

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

song_queue = {}
playback_tasks = {}
last_played = {}
karaoke_mode = {}
issued_commands = []
bot_sent_messages = []
session_log = []
audio_executor = ThreadPoolExecutor(max_workers=2)

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
    'executable': 'C:/ffmpeg/bin/ffmpeg.exe'
}

def get_commands_overview():
    return """
**Available Commands:**
- `!join`: Join the voice channel
- `!play <song>`: Search and play a song
- `!skip [index]`: Skip the current or a queued song
- `!leave`: Leave voice channel
- `!queue`: Show the current queue
- `!forward [seconds]`: Fast forward the current song
- `!repeat`: Repeat the last played song
- `!switch [from] [to]`: Swap songs in the queue
- `!karaoke`: Toggle karaoke mode on/off
- `!info`: Show this help message
- `!session`: Show session history
"""

def extract_song_info(search):
    opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'skip_download': True,
        'default_search': 'ytsearch',
        'extract_flat': False,
        'socket_timeout': 10
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(search, download=False)
        if 'entries' in info:
            info = info['entries'][0]
        return info['url'], info['title'], info.get('duration', 0)

async def get_song_url(search):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(audio_executor, extract_song_info, search)

def extract_artist_title(video_title):
    cleaned = re.sub(r'[\[\(].*?[\]\)]', '', video_title).strip()
    match = re.match(r'^(.*?)(?:\s*[-–—:]\s*)(.*)$', cleaned)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return None, cleaned

async def send_temp_message(ctx, content):
    msg = await ctx.send(content)
    bot_sent_messages.append(msg)
    session_log.append((ctx.author.name, ctx.command.name if ctx.command else 'message', content))
    await asyncio.sleep(5)
    try:
        await msg.delete()
    except:
        pass

@bot.command()
async def session(ctx):
    if not session_log:
        return await ctx.send("📭 No session history yet.")
    log = '\n'.join(f"**{u}** - `{cmd}`: {out}" for u, cmd, out in session_log[-10:])
    await ctx.send(f"📝 **Session History:**\n{log}")

async def start_karaoke(ctx, title, vc):
    if not karaoke_mode.get(ctx.guild.id, False):
        return
    artist, song = extract_artist_title(title)
    if not artist:
        return await ctx.send("❌ Could not extract artist from title.")
    search_url = f"https://lrclib.net/api/search?track_name={quote_plus(song)}&artist_name={quote_plus(artist)}"
    resp = requests.get(search_url)
    if resp.status_code != 200:
        return await ctx.send("❌ Search API error.")
    try:
        hits = resp.json()
        if isinstance(hits, dict) and 'data' in hits:
            hits = hits['data']
    except ValueError:
        return await ctx.send("❌ Invalid search response.")
    if not isinstance(hits, list) or not hits:
        return await ctx.send("❌ No results found.")
    candidates = []
    for h in hits:
        ti = h.get('track') if isinstance(h.get('track'), dict) else h
        name = ti.get('name') or ti.get('trackName') or ti.get('title')
        if name:
            candidates.append((name, h))
    if not candidates:
        return await ctx.send("❌ No valid track entries.")
    query = f"{artist} {song}"
    best = difflib.get_close_matches(query, [n for n,_ in candidates], n=1, cutoff=0.4)
    entry = next((e for n,e in candidates if n==best[0]), candidates[0][1]) if best else candidates[0][1]
    track_id = entry.get('track', {}).get('id') or entry.get('id')
    if not track_id:
        return await ctx.send("❌ Could not determine track ID.")
    lr = requests.get(f"https://lrclib.net/api/get/{track_id}")
    if lr.status_code != 200:
        return await ctx.send("❌ Lyrics API error.")
    try:
        data = lr.json()
    except ValueError:
        return await ctx.send("❌ Invalid lyrics response.")
    synced = data.get('syncedLyrics')
    if not synced:
        return await ctx.send("⚠️ No synced lyrics available.")
    lines = []
    for ln in synced.split('\n'):
        m = re.match(r'\[(\d+):(\d+\.\d+)\](.*)', ln)
        if m:
            ts = int(m.group(1))*60 + float(m.group(2))
            lines.append((ts, m.group(3).strip()))
    if not lines:
        return await ctx.send("❌ No valid lyrics lines found.")
    msg = await ctx.send("🎤 **Karaoke:**\n ")
    start_ts = vc.start_time.timestamp()
    for idx, (ts, txt) in enumerate(lines):
        if not vc.is_playing():
            break
        now = discord.utils.utcnow().timestamp()
        delay = ts - (now - start_ts) - 0.5
        if delay > 0:
            await asyncio.sleep(delay)
        next_txt = lines[idx + 1][1] if idx + 1 < len(lines) else ""
        embed = Embed(title=f"\n{txt}\n{next_txt}\n", description="\u200B", color=Color.blurple())
        await msg.edit(content="🎤 **Karaoke:**", embed=embed)
    await msg.edit(content="🎤 **Karaoke:**\n✅ Done!", embed=None)
    await asyncio.sleep(5)
    try:
        await msg.delete()
    except discord.errors.Forbidden:
        pass

async def playback_loop(ctx, guild_id):
    vc = ctx.voice_client
    try:
        while song_queue.get(guild_id):
            url, title, user, duration = song_queue[guild_id].pop(0)
            last_played[guild_id] = (url, title, user, duration)
            if not vc or not url.startswith('http'):
                continue
            def play():
                vc.play(discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS))
                vc.start_time = discord.utils.utcnow()
            await bot.loop.run_in_executor(audio_executor, play)
            np_msg = await ctx.send(f"🎵 Now playing: **{title}**")
            await start_karaoke(ctx, title, vc)

            while vc.is_playing():
                await asyncio.sleep(1)
            try:
                await np_msg.delete()
            except discord.errors.Forbidden:
                pass
            await start_karaoke(ctx, title, vc)
            while vc.is_playing():
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        playback_tasks[guild_id] = None

@bot.command()
async def join(ctx):
    if ctx.author.voice:
        await ctx.author.voice.channel.connect()
        await send_temp_message(ctx, "🔊 Joined. Type !info for commands.")
    else:
        await send_temp_message(ctx, "❌ You must be in a voice channel.")

@bot.command()
@cooldown(5, 5, BucketType.user)
async def play(ctx, *, search: str):
    gid = ctx.guild.id
    issued_commands.append(ctx)
    await send_temp_message(ctx, f"🔍 Searching for **{search}**...")
    try:
        url, title, duration = await get_song_url(search)
    except Exception as e:
        return await send_temp_message(ctx, f"❌ Error: {e}")
    song_queue.setdefault(gid, []).append((url, title, ctx.author.mention, duration))
    await send_temp_message(ctx, f"➕ Queued: **{title}**")
    if not ctx.voice_client:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            return await send_temp_message(ctx, "❌ Join a voice channel first.")
    if playback_tasks.get(gid) is None:
        playback_tasks[gid] = bot.loop.create_task(playback_loop(ctx, gid))

@bot.command()
async def skip(ctx, index: int=None):
    if not ctx.voice_client:
        return await send_temp_message(ctx, "❌ Not in a voice channel.")
    gid = ctx.guild.id
    if index is None:
        ctx.voice_client.stop()
        return await send_temp_message(ctx, "⏭️ Skipped.")
    queue = song_queue.get(gid, [])
    if not queue or index < 1 or index > len(queue):
        return await send_temp_message(ctx, "❌ Invalid index.")
    removed = queue.pop(index - 1)
    await send_temp_message(ctx, f"🗑️ Removed **{removed[1]}** from queue.")

@bot.command(name="switch")
async def switch(ctx, from_idx: int, to_idx: int):
    gid = ctx.guild.id
    queue = song_queue.get(gid, [])
    if not queue:
        return await send_temp_message(ctx, "❌ The queue is empty.")
    fi, ti = from_idx - 1, to_idx - 1
    if fi < 0 or fi >= len(queue) or ti < 0 or ti >= len(queue):
        return await send_temp_message(ctx, f"❌ Indices must be 1-{len(queue)}.")
    song = queue.pop(fi)
    queue.insert(ti, song)
    await send_temp_message(ctx, f"🔀 Moved **{song[1]}** to position {to_idx}.")

@bot.command()
async def leave(ctx):
    gid = ctx.guild.id
    if playback_tasks.get(gid):
        playback_tasks[gid].cancel()
    song_queue[gid] = []
    last_played[gid] = None
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await send_temp_message(ctx, "👋 Left voice channel.")
    else:
        await send_temp_message(ctx, "❌ Not connected.")

@bot.command(name="queue")
async def show_queue(ctx):
    q = song_queue.get(ctx.guild.id, [])
    if not q:
        return await send_temp_message(ctx, "📭 Queue is empty.")
    lines = [f"{i+1}. **{item[1]}** by {item[2]}" for i, item in enumerate(q)]
    await send_temp_message(ctx, "🎶 Current Queue:\n" + "\n".join(lines))

@bot.command()
async def forward(ctx, seconds: int = 15):
    vc = ctx.voice_client
    gid = ctx.guild.id
    data = last_played.get(gid)
    if not vc or not vc.is_playing() or not data or not hasattr(vc, 'start_time'):
        return await send_temp_message(ctx, "❌ Cannot forward.")
    url, title, _, duration = data
    elapsed = (discord.utils.utcnow() - vc.start_time).total_seconds()
    new_pos = elapsed + seconds
    if new_pos >= duration:
        return await send_temp_message(ctx, "⚠️ Beyond end.")
    opts = FFMPEG_OPTIONS.copy()
    opts['before_options'] += f" -ss {int(new_pos)}"
    vc.stop()
    vc.play(discord.FFmpegPCMAudio(url, **opts))
    vc.start_time = discord.utils.utcnow()
    await send_temp_message(ctx, f"⏩ Forwarded {seconds}s in **{title}**.")

@bot.command()
async def repeat(ctx):
    gid = ctx.guild.id
    data = last_played.get(gid)
    if not data:
        return await send_temp_message(ctx, "❌ Nothing to repeat.")
    song_queue.setdefault(gid, []).insert(0, data)
    if playback_tasks.get(gid) is None:
        playback_tasks[gid] = bot.loop.create_task(playback_loop(ctx, gid))
    await send_temp_message(ctx, f"🔁 Repeating **{data[1]}**")

@bot.command()
async def karaoke(ctx):
    gid = ctx.guild.id
    karaoke_mode[gid] = not karaoke_mode.get(gid, False)
    state = 'enabled' if karaoke_mode[gid] else 'disabled'
    await send_temp_message(ctx, f"🎤 Karaoke mode {state}!")

@bot.command()
async def info(ctx):
    await ctx.send(get_commands_overview())

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await send_temp_message(ctx, f"⏳ Try again in {error.retry_after:.1f}s.")
        return
    raise error

@bot.event
async def on_message(msg):
    if msg.author.bot:
        bot_sent_messages.append(msg)
    elif msg.content.startswith('!'):
        issued_commands.append(msg)
        await bot.process_commands(msg)
        await asyncio.sleep(5)
        try:
            await msg.delete()
        except discord.errors.Forbidden:
            pass  # Bot lacks permissions
    else:
        await bot.process_commands(msg)


bot.run(TOKEN)
