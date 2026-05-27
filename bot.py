#!/usr/bin/env python3
"""Factorio Discord Bot - runs on VPS alongside Factorio server."""
import asyncio
import logging
import os
import re
import socket
import struct
import subprocess
import time
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
RCON_HOST = os.getenv("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.getenv("RCON_PORT", "34198"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")
SERVER_LOG = os.path.expanduser(os.getenv("SERVER_LOG", "~/factorio/server.log"))
FACTORIO_LOG = os.path.expanduser(os.getenv("FACTORIO_LOG", "~/factorio/factorio/factorio-current.log"))

# FIXED - use token directly from env variable
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL = int(os.getenv("CHANNEL_ID", "0"))
R_PASS = os.getenv("RCON_PASSWORD")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot_log = logging.getLogger("factorio-bot")


# --- RCON (for executing commands) ---
class RCONClient:
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password.encode() if password else b""
        self.sock = None
        self._req_id = 0

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((self.host, self.port))
        self._auth()

    def _auth(self):
        body = self.password
        plen = 4 + 4 + len(body) + 2
        pkt = struct.pack("<ii", plen, 0) + struct.pack("<i", 3) + body + b"\x00\x00"
        self.sock.sendall(pkt)
        time.sleep(0.2)
        resp = self._recv_all()
        if len(resp) >= 12:
            req_id = struct.unpack("<i", resp[4:8])[0]
            if req_id == -1:
                raise PermissionError("RCON auth failed")

    def _recv_all(self, timeout=2):
        self.sock.settimeout(timeout)
        data = b""
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        return data

    def command(self, cmd_str):
        if not self.sock:
            self.connect()
        body = cmd_str.encode() + b"\x00"
        clen = 4 + 4 + len(body) + 2
        self._req_id += 1
        pkt = struct.pack("<ii", clen, self._req_id) + struct.pack("<i", 2) + body + b"\x00\x00"
        try:
            self.sock.sendall(pkt)
            resp = self._recv_all()
            idx = 0
            texts = []
            while idx + 12 <= len(resp):
                pkt_len = struct.unpack("<i", resp[idx:idx+4])[0]
                body_text = resp[idx+12:idx+4+pkt_len]
                text = body_text.rstrip(b"\x00").decode("utf-8", errors="replace") if len(body_text) > 2 else ""
                if text:
                    texts.append(text)
                idx += 4 + pkt_len
            return texts if texts else None
        except Exception as e:
            self.close()
            raise

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


# --- Lua query via screen (for reading game state) ---
def _screen_lua(lua_code, tag="bot_query"):
    """Send a /c Lua command via screen and read output from factorio-current.log."""
    try:
        full_cmd = f'/c log("{tag}:" .. {lua_code})\n'
        subprocess.run(
            ["screen", "-S", "factorio", "-X", "stuff", full_cmd],
            capture_output=True, timeout=5
        )
        time.sleep(1.5)
        r = subprocess.run(
            ["grep", "-a", tag, FACTORIO_LOG],
            capture_output=True, text=True, timeout=5
        )
        lines = r.stdout.strip().split("\n")
        for line in reversed(lines):
            if tag in line:
                parts = line.split(tag + ":")
                if len(parts) > 1:
                    return parts[-1].strip()
        return None
    except Exception as e:
        bot_log.error(f"screen_lua error: {e}")
        return None


def _screen_lua_foreach(tag_base, lua_iter_code):
    """Send a /c command that logs multiple values via a loop."""
    try:
        full_cmd = f"/c {lua_iter_code}\n"
        subprocess.run(
            ["screen", "-S", "factorio", "-X", "stuff", full_cmd],
            capture_output=True, timeout=5
        )
        time.sleep(1.5)
        r = subprocess.run(
            ["grep", "-a", tag_base, FACTORIO_LOG],
            capture_output=True, text=True, timeout=5
        )
        results = []
        for line in r.stdout.strip().split("\n"):
            if tag_base in line:
                parts = line.split(tag_base + ":")
                if len(parts) > 1:
                    results.append(parts[-1].strip())
        return results
    except Exception as e:
        bot_log.error(f"screen_lua_foreach error: {e}")
        return []


def server_running():
    """Check if Factorio server process is running."""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "bin/x64/factorio"],
            capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except:
        return False


def get_player_info():
    """Get player count and names via Lua query."""
    qid = str(int(time.time() * 1000))  # unique millisecond timestamp
    count = _screen_lua("#game.connected_players", f"bot_pc{qid}")
    if count is None:
        return 0, []
    count = int(count) if count.isdigit() else 0

    # Clean old bot_pname entries before querying
    names = _screen_lua_foreach(
        f"bot_pn{qid}",
        f'for _, p in pairs(game.connected_players) do log("bot_pn{qid}:" .. p.name) end'
    )
    return count, names


def get_server_uptime():
    """Get server uptime in ticks."""
    tick = _screen_lua("game.tick", "bot_tick")
    if tick and tick.isdigit():
        minutes = int(tick) // 3600
        return minutes
    return None


# --- Discord Bot ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Track last log position for chat polling
_last_log_pos = {}


def _send_to_factorio(text):
    """Send a message to Factorio game chat via screen."""
    try:
        escaped = text.replace("'", "'\\''")
        subprocess.run(
            ["screen", "-S", "factorio", "-X", "stuff", escaped + "\n"],
            capture_output=True, timeout=5
        )
        return True
    except Exception as e:
        bot_log.error(f"send_to_factorio error: {e}")
        return False


async def poll_factorio_chat():
    """Background task: poll server.log for new [CHAT] entries."""
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL)
    if not channel:
        bot_log.error(f"Channel {CHANNEL} not found for chat polling")
        return

    # Track file position
    pos = 0
    try:
        if os.path.exists(SERVER_LOG):
            pos = os.path.getsize(SERVER_LOG)
    except:
        pass

    while not bot.is_closed():
        await asyncio.sleep(5)
        try:
            if not os.path.exists(SERVER_LOG):
                continue
            current_size = os.path.getsize(SERVER_LOG)
            if current_size <= pos:
                pos = current_size
                continue

            with open(SERVER_LOG, "rb") as f:
                f.seek(pos)
                new_data = f.read()
                pos = f.tell()

            for line in new_data.decode("utf-8", errors="replace").split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Parse [CHAT] entries from players
                # Format: [CHAT] <server>: msg  OR  [CHAT] PlayerName: msg
                m = re.search(r"\[CHAT\]\s*(?:<([^>]+)>|([^:]+?)):\s*(.+)", line)
                if m:
                    player = m.group(1) or m.group(2)
                    msg = m.group(3).rstrip("\x00")
                    # Skip server-originated messages and our own relay messages
                    if player == "server" or msg.startswith("[Discord]"):
                        continue
                    await channel.send(f"**{player}**: {msg}")
        except Exception as e:
            bot_log.error(f"Chat poll error: {e}")
            await asyncio.sleep(10)


async def monitor_server_status():
    """Background task: notify when server starts/stops."""
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL)
    if not channel:
        bot_log.error(f"Channel {CHANNEL} not found for status monitor")
        return

    was_running = server_running()
    while not bot.is_closed():
        await asyncio.sleep(10)
        is_running = server_running()
        if was_running and not is_running:
            await channel.send("🛑 **Servidor desligado!**")
            bot_log.info("Server went offline")
        elif not was_running and is_running:
            await channel.send("🟢 **Servidor ligado!**")
            bot_log.info("Server came online")
        was_running = is_running


@bot.event
async def on_ready():
    bot_log.info(f"Bot logged in as {bot.user}")
    channel = bot.get_channel(CHANNEL)
    if channel:
        await channel.send("🚂 **Factorio Bot online!**")


@bot.event
async def on_message(msg):
    """Relay Discord messages to Factorio game chat."""
    if msg.author.bot:
        return
    if msg.channel.id != CHANNEL:
        return

    # Let commands be handled by the command system
    await bot.process_commands(msg)

    # If it's a command, don't relay
    if msg.content.startswith("!"):
        return

    # Relay to Factorio
    text = msg.content.strip()
    if text:
        relay = f"[Discord] {msg.author.display_name}: {text}"
        _send_to_factorio(relay)


@bot.command(name="status")
async def cmd_status(ctx):
    """Mostra status do servidor."""
    if ctx.channel.id != CHANNEL:
        return
    if not server_running():
        await ctx.send("❌ **Servidor OFFLINE**")
        return
    count, names = get_player_info()
    uptime = get_server_uptime()
    lines = ["✅ **Servidor ONLINE**", "📍 `45.157.16.64:34197`"]
    if count > 0:
        lines.append(f"👥 **{count} jogador(es) conectado(s)**")
        if names:
            lines.append("└ " + ", ".join(f"**{n}**" for n in names))
    else:
        lines.append("👻 Nenhum jogador conectado")
    if uptime is not None:
        hours = uptime // 60
        mins = uptime % 60
        lines.append(f"⏱ {hours}h{mins:02d}min de jogo")
    lines.append("📦 Factorio 2.0.76 + Space Age")
    await ctx.send("\n".join(lines))


@bot.command(name="players")
async def cmd_players(ctx):
    """Mostra jogadores conectados."""
    if ctx.channel.id != CHANNEL:
        return
    if not server_running():
        await ctx.send("❌ Servidor offline.")
        return
    count, names = get_player_info()
    if count > 0:
        msg = f"👥 **{count} jogador(es) conectado(s)**"
        if names:
            msg += "\n" + "\n".join(f"• **{n}**" for n in names)
        await ctx.send(msg)
    else:
        await ctx.send("👻 Nenhum jogador conectado no momento.")


@bot.command(name="cmd")
async def cmd_factorio(ctx, *, cmd: str):
    """Executa comando no console do servidor via RCON."""
    if ctx.channel.id != CHANNEL:
        return
    if not server_running():
        await ctx.send("❌ Servidor offline.")
        return
    await ctx.send(f"⚙️ Executando: `{cmd}`")
    try:
        rcon = RCONClient(RCON_HOST, RCON_PORT, R_PASS)
        rcon.connect()
        result = rcon.command(cmd)
        rcon.close()
        if result:
            await ctx.send(f"```\n{chr(10).join(result)}\n```")
        else:
            await ctx.send("✅ Comando executado.")
    except Exception as e:
        await ctx.send(f"❌ Erro RCON: {e}")


@bot.command(name="help")
async def cmd_help(ctx):
    """Mostra comandos disponíveis."""
    if ctx.channel.id != CHANNEL:
        return
    embed = discord.Embed(
        title="🚂 Factorio Bot",
        description="Comandos do servidor de Factorio",
        color=0xE67E22,
    )
    embed.add_field(name="!status", value="Status completo do servidor", inline=False)
    embed.add_field(name="!players", value="Lista jogadores conectados", inline=False)
    embed.add_field(name="!cmd <comando>", value="Executa comando RCON (ex: `!cmd help`)", inline=False)
    embed.set_footer(text="45.157.16.64:34197 | VPS Dante")
    await ctx.send(embed=embed)


def main():
    if not TOKEN:
        bot_log.error("DISCORD_TOKEN not set")
        return
    if not CHANNEL:
        bot_log.error("CHANNEL_ID not set")
        return
    if not R_PASS:
        bot_log.error("RCON_PASSWORD not set")
        return

    @bot.event
    async def setup_hook():
        bot.loop.create_task(poll_factorio_chat())
        bot.loop.create_task(monitor_server_status())

    bot.run(TOKEN)


if __name__ == "__main__":
    main()
