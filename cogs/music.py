import discord
import yt_dlp as youtube_dl
import spotipy
import asyncio
import copy
import math
from enum import Enum

from discord.ext import commands
from spotipy.oauth2 import SpotifyClientCredentials
from discord.ui import View, Button, button

# --- Enums and Helpers ---

class LoopMode(Enum):
    NONE = 0
    SONG = 1
    QUEUE = 2

def _extract_info_blocking(query, ydl_opts):
    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)

async def extract_info_async(query, ydl_opts):
    loop = asyncio.get_event_loop()
    opts = copy.deepcopy(ydl_opts)
    return await loop.run_in_executor(None, lambda: _extract_info_blocking(query, opts))

def _get_stream_url_from_info(info):
    if info.get('url'):
        return info['url']
    formats = info.get('formats') or []
    for f in formats:
        url = f.get('url')
        if not url: continue
        acodec = f.get('acodec')
        if acodec and acodec != 'none':
            return url
    if formats:
        return formats[0].get('url')
    return None

# --- UI Views ---

class QueuePaginatorView(View):
    def __init__(self, pages: list, total_songs: int, author: discord.User):
        super().__init__(timeout=180)
        self.pages = pages
        self.total_songs = total_songs
        self.author = author
        self.current_page = 0
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Chỉ người yêu cầu mới có thể lật trang.", ephemeral=True)
            return False
        return True

    def update_buttons(self):
        # self.children is a list of the buttons in the order they are added
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.pages) - 1

    def create_embed(self):
        embed = discord.Embed(
            title=f"📜 Hàng Đợi ({self.total_songs} bài) - Trang {self.current_page + 1}/{len(self.pages)}",
            color=discord.Color.blue(),
            description=self.pages[self.current_page]
        )
        return embed

    @button(label="◀️ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    @button(label="Next ▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: Button):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self.update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    @button(label="Close", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: Button):
        await interaction.message.delete()


class ControlPanelView(View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("Bạn phải ở trong một kênh thoại để sử dụng các nút này.", ephemeral=True)
            return False
        if interaction.guild.voice_client and interaction.user.voice.channel != interaction.guild.voice_client.channel:
            await interaction.response.send_message("Bạn phải ở cùng kênh thoại với bot.", ephemeral=True)
            return False
        return True

    @button(label="Skip ⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: Button):
        await self.cog.skip_logic(interaction=interaction)

    @button(label="Loop 🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop(self, interaction: discord.Interaction, button: Button):
        await self.cog.loop_logic(interaction=interaction)

    @button(label="Stop ⏹", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: Button):
        await self.cog.stop_logic(interaction=interaction)

    @button(label="Queue 📜", style=discord.ButtonStyle.primary, custom_id="music:queue")
    async def queue(self, interaction: discord.Interaction, button: Button):
        await self.cog.queue_logic(interaction=interaction)

# --- Main Music Cog ---

async def is_in_same_channel(ctx: commands.Context) -> bool:
    """Check if the user is in the same voice and text channel as the bot."""
    vc = ctx.guild.voice_client
    # If the bot is not in a voice channel, allow the command
    if not vc:
        return True

    # Check if the user is in a voice channel
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Bạn phải ở trong một kênh thoại để dùng lệnh này.", delete_after=10)
        return False

    # Check if the user is in the same voice channel as the bot
    if ctx.author.voice.channel.id != vc.channel.id:
        await ctx.send(f"Bot đang bận ở kênh thoại `{vc.channel}`.", delete_after=10)
        return False

    # Check if the command is used in the same text channel as the control panel
    cog = ctx.cog
    if cog:
        data = cog.get_guild_data(ctx.guild.id)
        if data.get("control_panel") and data["control_panel"].channel.id != ctx.channel.id:
            await ctx.send(f"Vui lòng sử dụng lệnh ở kênh `{data['control_panel'].channel.name}`.", delete_after=10)
            return False
            
    return True

class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot):
        self.bot = bot
        self.music_data = {}
        self.sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id="86eaf26b30144cf5b353ab7b8937c7b8",
            client_secret="4fbf01a835994141870c97342e6dbc40"
        ))

    def get_guild_data(self, guild_id):
        if guild_id not in self.music_data:
            self.music_data[guild_id] = {
                "playlist": [], "current_index": -1, "loop_mode": LoopMode.NONE,
                "playing": False, "control_panel": None, "last_ctx": None
            }
        return self.music_data[guild_id]

    async def _create_control_embed(self, data, status_override=None):
        current_song = data["playlist"][data["current_index"]] if 0 <= data["current_index"] < len(data["playlist"]) else None
        ctx = data.get("last_ctx")
        vc = ctx.guild.voice_client if ctx else None

        state = "Đang phát"
        if status_override:
            state = status_override
        elif not vc or not vc.is_connected():
            state = "Đã ngắt kết nối"
        elif vc.is_paused():
            state = "Đã tạm dừng"
        elif not current_song:
            state = "Đã dừng"

        embed = discord.Embed(title="Bảng Điều Khiển Nhạc", color=discord.Color.green() if state == "Đang phát" else discord.Color.orange())
        
        if current_song:
            embed.add_field(name=f"🎶 Đang phát ({state})", value=f"**{current_song['title']}**", inline=False)
        else:
            embed.add_field(name=f"💤 Trạng thái: {state}", value="Sử dụng `!play` để thêm bài hát.", inline=False)

        next_song_index = data["current_index"] + 1
        if data["loop_mode"] == LoopMode.QUEUE and len(data["playlist"]) > 0:
             next_song_index = (data["current_index"] + 1) % len(data["playlist"])
        
        if 0 <= next_song_index < len(data["playlist"]):
            next_title = data["playlist"][next_song_index]['title']
            embed.add_field(name="⏭️ Tiếp theo", value=next_title, inline=True)

        loop_map = {
            LoopMode.NONE: "❌ Tắt",
            LoopMode.SONG: "🔂 Lặp bài",
            LoopMode.QUEUE: "🔁 Lặp danh sách"
        }
        embed.add_field(name="Lặp lại", value=loop_map[data["loop_mode"]], inline=True)
        embed.set_footer(text=f"Tổng cộng {len(data['playlist'])} bài trong hàng đợi.")
        return embed

    async def _update_control_panel(self, guild_id, status_override=None, interaction: discord.Interaction = None):
        data = self.get_guild_data(guild_id)
        # Determine the channel to send the new panel to
        channel = None
        if interaction:
            channel = interaction.channel
        elif data.get("last_ctx"):
            channel = data["last_ctx"].channel
        
        if not channel:
            # If we can't determine a channel, we can't create a new panel.
            # This might happen if the bot restarts and hasn't received a command yet.
            return

        # Delete the old control panel if it exists
        if data.get("control_panel"):
            try:
                await data["control_panel"].delete()
            except (discord.NotFound, discord.HTTPException):
                pass  # Ignore if it's already gone

        view = ControlPanelView(self)
        view.clear_items()
        
        vc = data.get("last_ctx").guild.voice_client if data.get("last_ctx") else None
        is_paused = vc and vc.is_paused()

        if is_paused:
            pr_button = Button(label="Resume ▶️", style=discord.ButtonStyle.secondary, custom_id="music:pause_resume")
        else:
            pr_button = Button(label="Pause ⏸️", style=discord.ButtonStyle.success, custom_id="music:pause_resume")
        
        async def pause_resume_callback(interaction: discord.Interaction):
            await self.pause_resume_logic(interaction=interaction)
        pr_button.callback = pause_resume_callback
        
        view.add_item(pr_button)
        view.add_item(view.skip)
        view.add_item(view.loop)
        view.add_item(view.stop)
        view.add_item(view.queue)

        try:
            embed = await self._create_control_embed(data, status_override)
            new_panel = await channel.send(embed=embed, view=view)
            data["control_panel"] = new_panel # Store the new panel message
        except discord.HTTPException as e:
            print(f"Failed to create new control panel for guild {guild_id}: {e}")
            data["control_panel"] = None

    async def play_current(self, guild_id):
        data = self.get_guild_data(guild_id)
        ctx = data.get("last_ctx")
        if not ctx: return

        if data["loop_mode"] != LoopMode.SONG:
            if data["current_index"] >= len(data["playlist"]):
                if data["loop_mode"] == LoopMode.QUEUE and len(data["playlist"]) > 0:
                    data["current_index"] = 0
                else:
                    data["playing"] = False
                    await self._update_control_panel(guild_id, "Đã hết bài")
                    return
        
        song = data["playlist"][data["current_index"]]
        if not song.get('url'):
            YDL_SINGLE = {'format': 'bestaudio/best', 'quiet': True, 'noplaylist': True}
            try:
                query = song.get('webpage_url') or song.get('query') or song.get('title')
                info = await extract_info_async(query, YDL_SINGLE)
                if 'entries' in info: info = info['entries'][0]
                song['url'] = _get_stream_url_from_info(info)
                song['title'] = info.get('title', song['title'])
            except Exception as e:
                await ctx.send(f"⚠️ Lỗi khi lấy stream cho **{song['title']}**: {e}. Bỏ qua.", delete_after=5)
                if data["loop_mode"] != LoopMode.SONG: data["current_index"] += 1
                return await self.play_current(guild_id)

        if not song.get('url'):
            await ctx.send(f"⚠️ Không tìm thấy URL cho **{song['title']}**. Bỏ qua.", delete_after=5)
            if data["loop_mode"] != LoopMode.SONG: data["current_index"] += 1
            return await self.play_current(guild_id)

        vc = ctx.guild.voice_client
        if not vc: return

        data["playing"] = True
        try:
            source = await discord.FFmpegOpusAudio.from_probe(song['url'], before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', options='-vn')
        except Exception as e:
            await ctx.send(f"⚠️ Lỗi FFMPEG: {e}", delete_after=5)
            if data["loop_mode"] != LoopMode.SONG: data["current_index"] += 1
            return await self.play_current(guild_id)

        def after_play(error):
            if error: print(f"Player error: {error}")
            if data["playing"]:
                if data["loop_mode"] != LoopMode.SONG:
                    data["current_index"] += 1
                asyncio.run_coroutine_threadsafe(self.play_current(guild_id), self.bot.loop)

        vc.play(source, after=after_play)
        await self._update_control_panel(guild_id)

    async def add_ydl_playlist_to_data(self, ctx, url, data, max_tracks=200):
        YDL_PL_OPTS = {'format': 'bestaudio/best', 'quiet': True, 'default_search': 'auto', 'geo_bypass': True, 'noplaylist': False, 'extract_flat': True}
        try:
            info = await extract_info_async(url, YDL_PL_OPTS)
        except Exception as e:
            await ctx.send(f"❌ Lỗi khi lấy playlist từ URL: {e}", delete_after=5)
            return 0
        entries = info.get('entries') or []
        added = 0
        for entry in entries:
            if added >= max_tracks: break
            entry_id = entry.get('url') or entry.get('id')
            if not entry_id: continue
            webpage = f"https://www.youtube.com/watch?v={entry_id}" if not entry_id.startswith('http') else entry_id
            title = entry.get('title') or "Không rõ tiêu đề"
            data['playlist'].append({'title': title, 'webpage_url': webpage, 'url': None, 'requester': ctx.author})
            added += 1
        return added

    async def add_spotify_playlist_to_data(self, ctx, url, data, max_tracks=500):
        try:
            playlist_id = url.split("/")[-1].split("?")[0]
        except Exception:
            await ctx.send("Không parse được playlist Spotify.", delete_after=5)
            return 0
        added = 0
        try:
            offset = 0
            while True:
                items = self.sp.playlist_items(playlist_id, offset=offset, limit=100)
                for item in items.get('items', []):
                    track = item.get('track')
                    if not track: continue
                    query = f"{track.get('name')} {track.get('artists', [{}])[0].get('name', '')}"
                    data['playlist'].append({'title': track.get('name'), 'query': query, 'url': None, 'requester': ctx.author})
                    added += 1
                    if added >= max_tracks: break
                if added >= max_tracks or not items.get('next'): break
                offset += 100
        except Exception as e:
            await ctx.send(f"❌ Lỗi khi lấy playlist Spotify: {e}", delete_after=5)
        return added

    async def add_spotify_album_to_data(self, ctx, url, data, max_tracks=100):
        try:
            album_id = url.split("/")[-1].split("?")[0]
        except Exception:
            await ctx.send("Không parse được album Spotify.", delete_after=5)
            return 0
        added = 0
        try:
            offset = 0
            while True:
                items = self.sp.album_tracks(album_id, offset=offset, limit=50)
                for track in items.get('items', []):
                    query = f"{track.get('name')} {track.get('artists', [{}])[0].get('name', '')}"
                    data['playlist'].append({'title': track.get('name'), 'query': query, 'url': None, 'requester': ctx.author})
                    added += 1
                    if added >= max_tracks: break
                if added >= max_tracks or not items.get('next'): break
                offset += 50
        except Exception as e:
            await ctx.send(f"❌ Lỗi khi lấy album Spotify: {e}", delete_after=5)
        return added

    # --- Command Logic ---

    @commands.command(aliases=['p'])
    @commands.check(is_in_same_channel)
    async def play(self, ctx, *, query: str):
        gid = ctx.guild.id
        data = self.get_guild_data(gid)
        data["last_ctx"] = ctx

        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("Bạn phải ở trong kênh thoại để dùng lệnh này.", delete_after=5)
                return
        
        initial_playlist_size = len(data["playlist"])
        added = 0
        is_spotify_track = "open.spotify.com/track" in query
        is_spotify_playlist = "open.spotify.com/playlist" in query
        is_spotify_album = "open.spotify.com/album" in query
        is_ydl_playlist = any(x in query for x in ("youtube.com/playlist", "list=", "soundcloud.com/sets/"))

        temp_msg = None
        if is_spotify_playlist:
            temp_msg = await ctx.send(f"🔁 Đang tải playlist Spotify...")
            added = await self.add_spotify_playlist_to_data(ctx, query, data)
        elif is_spotify_album:
            temp_msg = await ctx.send(f"🔁 Đang tải album Spotify...")
            added = await self.add_spotify_album_to_data(ctx, query, data)
        elif is_ydl_playlist:
            temp_msg = await ctx.send(f"🔁 Đang tải playlist...")
            added = await self.add_ydl_playlist_to_data(ctx, query, data)
        else:
            search_query = query
            if is_spotify_track:
                try:
                    track = self.sp.track(query.split("/")[-1].split("?")[0])
                    search_query = f"{track['name']} {track['artists'][0]['name']}"
                except Exception as e:
                    await ctx.send(f"Không thể lấy dữ liệu Spotify: {e}", delete_after=5)
                    return
            YDL_SINGLE = {'format': 'bestaudio/best', 'quiet': True, 'noplaylist': True, 'default_search': 'ytsearch'}
            try:
                info = await extract_info_async(search_query, YDL_SINGLE)
                if 'entries' in info: info = info['entries'][0]
                title = info.get('title', 'Không rõ tiêu đề')
                webpage = info.get('webpage_url')
                data['playlist'].append({'title': title, 'webpage_url': webpage, 'url': None, 'requester': ctx.author})
                added = 1
                await ctx.send(f"✅ Đã thêm **{title}** vào hàng đợi.", delete_after=5)
            except Exception as e:
                await ctx.send(f"❌ Lỗi khi tìm bài hát: {e}", delete_after=5)
                return
        
        if temp_msg:
            await temp_msg.edit(content=f"✅ Đã thêm {added} bài.", delete_after=5)

        if not data["playing"] and added > 0:
            data["current_index"] = initial_playlist_size
            # No need to check for control panel, _update will create it
            await self.play_current(gid)
        elif added > 0:
            await self._update_control_panel(gid)
        
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    async def pause_resume_logic(self, *, interaction: discord.Interaction = None, ctx: commands.Context = None):
        gid = interaction.guild.id if interaction else ctx.guild.id
        vc = interaction.guild.voice_client if interaction else ctx.guild.voice_client
        
        if not vc:
            msg = "Bot không ở trong kênh thoại."
            if interaction: await interaction.response.send_message(msg, ephemeral=True)
            else: await ctx.send(msg, delete_after=5)
            return

        if vc.is_playing():
            vc.pause()
            await self._update_control_panel(gid, "Đã tạm dừng", interaction=interaction)
        elif vc.is_paused():
            vc.resume()
            await self._update_control_panel(gid, "Đang phát", interaction=interaction)
        
        if interaction: 
            await interaction.response.defer()
        else: 
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    async def skip_logic(self, *, interaction: discord.Interaction = None, ctx: commands.Context = None):
        gid = interaction.guild.id if interaction else ctx.guild.id
        vc = interaction.guild.voice_client if interaction else ctx.guild.voice_client
        msg = ""
        ephemeral = False
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            msg = "⏭ Đã bỏ qua bài hát."
            ephemeral = True
        else:
            msg = "Không có gì để bỏ qua."
            ephemeral = True
        
        if interaction: 
            await interaction.response.send_message(msg, ephemeral=ephemeral, delete_after=5)
        else: 
            await ctx.send(msg, delete_after=5)
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    async def loop_logic(self, *, interaction: discord.Interaction = None, ctx: commands.Context = None, mode: LoopMode = None):
        gid = interaction.guild.id if interaction else ctx.guild.id
        data = self.get_guild_data(gid)
        
        if mode is None: # Cycle through modes if called from button
            current_mode_val = data["loop_mode"].value
            next_mode_val = (current_mode_val + 1) % len(LoopMode)
            data["loop_mode"] = LoopMode(next_mode_val)
        else: # Set specific mode if called from command
            data["loop_mode"] = mode

        await self._update_control_panel(gid, interaction=interaction)
        if interaction: 
            await interaction.response.defer()
        else: 
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    async def stop_logic(self, *, interaction: discord.Interaction = None, ctx: commands.Context = None):
        gid = interaction.guild.id if interaction else ctx.guild.id
        data = self.get_guild_data(gid)
        vc = interaction.guild.voice_client if interaction else ctx.guild.voice_client
        
        data["playlist"].clear()
        data["current_index"] = -1
        data["playing"] = False
        data["loop_mode"] = LoopMode.NONE
        
        if vc:
            vc.stop()
            # Don't disconnect here, let it be handled by an inactivity timer later if desired
        
        # Update panel before potential disconnect to show "Stopped" state
        await self._update_control_panel(gid, "Đã dừng", interaction=interaction)

        if vc:
            await vc.disconnect()

        if interaction: 
            await interaction.response.defer()
        else: 
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    async def queue_logic(self, *, interaction: discord.Interaction = None, ctx: commands.Context = None):
        gid = interaction.guild.id if interaction else ctx.guild.id
        data = self.get_guild_data(gid)
        playlist = data["playlist"]
        if not playlist:
            msg = "Hàng đợi đang trống."
            if interaction: await interaction.response.send_message(msg, ephemeral=True)
            else: 
                await ctx.send(msg, delete_after=5)
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
            return

        # Create pages
        pages = []
        current_page = ""
        for i, song in enumerate(playlist):
            prefix = "🎶 **(Đang phát)**" if i == data["current_index"] else f"**#{i+1}**"
            line = f"{prefix} {song['title']}\n"
            if len(current_page) + len(line) > 1024: # Embed description limit
                pages.append(current_page)
                current_page = ""
            current_page += line
        if current_page: pages.append(current_page)

        if not pages:
            msg = "Hàng đợi đang trống."
            if interaction: await interaction.response.send_message(msg, ephemeral=True)
            else: 
                await ctx.send(msg, delete_after=5)
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
            return

        # Create paginator
        paginator = QueuePaginatorView(pages=pages, total_songs=len(playlist), author=interaction.user if interaction else ctx.author)
        embed = paginator.create_embed()
        
        if interaction: 
            await interaction.response.send_message(embed=embed, view=paginator, delete_after=120)
        else: 
            await ctx.send(embed=embed, view=paginator)
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    # --- Text Commands ---

    @commands.command()
    @commands.check(is_in_same_channel)
    async def pause(self, ctx: commands.Context):
        await self.pause_resume_logic(ctx=ctx)

    @commands.command()
    @commands.check(is_in_same_channel)
    async def resume(self, ctx: commands.Context):
        await self.pause_resume_logic(ctx=ctx)

    @commands.command(aliases=['j'])
    @commands.check(is_in_same_channel)
    async def join(self, ctx: commands.Context):
        """Allow the bot to join the user's voice channel."""
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        if not ctx.author.voice:
            await ctx.send("Bạn phải ở trong một kênh thoại để dùng lệnh này.", delete_after=5)
            return

        channel = ctx.author.voice.channel
        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                await ctx.send("Bot đã ở trong kênh thoại của bạn rồi.", delete_after=5)
                return
            # As per new requirement, do not move if already in a channel
            await ctx.send(f"Bot đang bận ở kênh thoại `{vc.channel}`.", delete_after=5)
            return
        else:
            await channel.connect()
            await ctx.send(f"Đã tham gia kênh: **{channel}**", delete_after=5)

    @commands.command(aliases=['lv'])
    @commands.check(is_in_same_channel)
    async def leave(self, ctx: commands.Context):
        """Make the bot leave the voice channel and clear the queue."""
        await self.stop_logic(ctx=ctx)

    @commands.command(aliases=['s'])
    @commands.check(is_in_same_channel)
    async def skip(self, ctx: commands.Context):
        await self.skip_logic(ctx=ctx)

    @commands.command(aliases=['st'])
    @commands.check(is_in_same_channel)
    async def skipto(self, ctx: commands.Context, position: int):
        """Nhảy đến một bài hát ở vị trí cụ thể trong hàng đợi."""
        gid = ctx.guild.id
        data = self.get_guild_data(gid)
        vc = ctx.guild.voice_client

        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        if not vc:
            # This check is technically redundant due to is_in_same_channel, but good for safety
            await ctx.send("Bot không ở trong kênh thoại.", delete_after=5)
            return

        if not 1 <= position <= len(data["playlist"]):
            await ctx.send(f"Vị trí không hợp lệ. Vui lòng chọn một số từ 1 đến {len(data['playlist'])}.", delete_after=5)
            return

        data["current_index"] = position - 2
        
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await ctx.send(f"⏭️ Đã nhảy đến bài hát ở vị trí {position}.", delete_after=5)
        else:
            await self.play_current(gid)
            await ctx.send(f"▶️ Bắt đầu phát từ bài hát ở vị trí {position}.", delete_after=5)
    
    @commands.command()
    @commands.check(is_in_same_channel)
    async def stop(self, ctx: commands.Context):
        await self.stop_logic(ctx=ctx)

    @commands.command(aliases=['q'])
    @commands.check(is_in_same_channel)
    async def queue(self, ctx: commands.Context):
        await self.queue_logic(ctx=ctx)

    @commands.command(aliases=['l'])
    @commands.check(is_in_same_channel)
    async def loop(self, ctx: commands.Context):
        await self.loop_logic(ctx=ctx, mode=LoopMode.SONG)

    @commands.command(aliases=['lq'])
    @commands.check(is_in_same_channel)
    async def loopqueue(self, ctx: commands.Context):
        await self.loop_logic(ctx=ctx, mode=LoopMode.QUEUE)
    
    @commands.command()
    @commands.check(is_in_same_channel)
    async def noloop(self, ctx: commands.Context):
        await self.loop_logic(ctx=ctx, mode=LoopMode.NONE)


async def setup(bot):
    cog = MusicCog(bot)
    bot.add_view(ControlPanelView(cog))
    await bot.add_cog(cog)
