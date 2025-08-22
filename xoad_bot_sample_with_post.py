# xoad_bot_sample_with_post.py
# -*- coding: utf-8 -*-
"""
XOAD Discord Bot - 最小サンプル（SQLite/discord.py） + 投稿機能
機能:
- /xp add|remove|give|check
- /report <type> <details> 申請登録
- /report approve|reject（運営のみ）
- 毎日0:00: サブスクロールに応じてXP付与
- 15分ごと: 24h経過申請の自動承認
- 毎月1日0:00: XP税(10%)、0:05: 前月/累計の消費ランキング投稿
- /post xoad, /post collab（承認なしで即公開・自動スレッド）★追加
"""

import os, sqlite3, asyncio, math, datetime as dt, time
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ====== 設定 ======
# 既存の Tokensetting.env を利用（なければ .env に置き換えも可）
load_dotenv("Tokensetting.env")
TOKEN = os.getenv("BOT_TOKEN")               # .env に BOT_TOKEN=xxxx
GUILD_ID = int(os.getenv("GUILD_ID", "0"))   # 対象ギルドID（開発時は1つに限定）
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "運営") # 運営ロール名

# 新規: 投稿先チャンネルとクールダウン（任意）
AD_CHANNEL_ID = int(os.getenv("AD_CHANNEL_ID", "0"))          # /post xoad の公開先
COLLAB_CHANNEL_ID = int(os.getenv("COLLAB_CHANNEL_ID", "0"))  # /post collab の公開先
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "0"))            # 同一ユーザーの再投稿間隔（秒）0で無効
REPORT_NOTIFY_CHANNEL_ID = int(os.getenv("REPORT_NOTIFY_CHANNEL_ID", "0"))

# 課金プランをロール名で判定（環境差はここを差し替え）
PLAN_ROLE_MAP = {
    "SEED": None,          # 無課金は特定ロールなしでも良い
    "CORE": "CORE",
    "NEXUS": "NEXUS",      # 環境でPLATINUMをNEXUS相当にする場合はここを編集
}
DAILY_XP = {"SEED": 0, "CORE": 75, "NEXUS": 200}

DB_PATH = os.getenv("DB_PATH", "xoad.sqlite3")
TZ = dt.timezone(dt.timedelta(hours=9))  # Asia/Tokyo (JST)

# ====== ドメイン ======
@dataclass
class User:
    user_id: int
    username: str
    joined_at: dt.datetime

@dataclass
class Report:
    id: Optional[int]
    user_id: int
    rtype: str      # e.g., "adult_coop", "party", "bbs_post", ...
    details: str
    status: str     # "pending"/"approved"/"rejected"/"auto_approved"
    submitted_at: dt.datetime
    decided_at: Optional[dt.datetime]
    decided_by: Optional[int]

@dataclass
class Ledger:
    id: Optional[int]
    user_id: int
    delta: int
    reason: str     # "DAILY_CORE", "REPORT_APPROVED", "XP_TAX", "TRANSFER_OUT", ...
    ref_type: str   # "report" / "transfer" / "system" / "manual"
    ref_id: Optional[int]
    created_at: dt.datetime

@dataclass
class Transfer:
    id: Optional[int]
    from_user_id: int
    to_user_id: int
    amount: int
    fee: int
    created_at: dt.datetime

# ====== DBユーティリティ ======
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        c = conn.cursor()
        c.executescript("""
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            joined_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_totals(
            user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            current_points INTEGER NOT NULL DEFAULT 0,
            total_points   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS points_ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL,
            ref_type TEXT NOT NULL,
            ref_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            rtype TEXT NOT NULL,
            details TEXT NOT NULL,
            status TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by INTEGER
        );

        CREATE TABLE IF NOT EXISTS xp_transfers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            to_user_id   INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            amount INTEGER NOT NULL,
            fee    INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS job_runs(
            job_key TEXT PRIMARY KEY,
            last_run_at TEXT NOT NULL
        );
        """)
        conn.commit()

def ensure_user(u: discord.Member):
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users(user_id, username, joined_at) VALUES(?,?,?)",
                  (u.id, str(u), dt.datetime.now(TZ).isoformat()))
        c.execute("INSERT OR IGNORE INTO user_totals(user_id, current_points, total_points) VALUES(?,?,?)",
                  (u.id, 0, 0))
        conn.commit()

def add_ledger(user_id: int, delta: int, reason: str, ref_type: str, ref_id: Optional[int]=None):
    now = dt.datetime.now(TZ).isoformat()
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO points_ledger(user_id, delta, reason, ref_type, ref_id, created_at) VALUES(?,?,?,?,?,?)",
                  (user_id, delta, reason, ref_type, ref_id, now))
        # 現在XP・累計反映
        if delta >= 0:
            c.execute("UPDATE user_totals SET current_points=current_points+?, total_points=total_points+? WHERE user_id=?",
                      (delta, delta, user_id))
        else:
            c.execute("UPDATE user_totals SET current_points=current_points+? WHERE user_id=?",
                      (delta, user_id))
        conn.commit()

def get_points(user_id: int) -> Tuple[int, int]:
    with db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT current_points, total_points FROM user_totals WHERE user_id=?", (user_id,)).fetchone()
        if not row: return (0, 0)
        return (row["current_points"], row["total_points"])

def record_transfer(frm: int, to: int, amount: int, fee: int):
    now = dt.datetime.now(TZ).isoformat()
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO xp_transfers(from_user_id,to_user_id,amount,fee,created_at) VALUES(?,?,?,?,?)",
                  (frm, to, amount, fee, now))
        tid = c.lastrowid
        conn.commit()
        return tid

def create_report(user_id: int, rtype: str, details: str) -> int:
    now = dt.datetime.now(TZ).isoformat()
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO reports(user_id, rtype, details, status, submitted_at) VALUES(?,?,?,?,?)",
                  (user_id, rtype, details, "pending", now))
        rid = c.lastrowid
        conn.commit()
        return rid

def decide_report(rid: int, approver_id: int, approve: bool) -> Optional[sqlite3.Row]:
    now = dt.datetime.now(TZ).isoformat()
    with db() as conn:
        c = conn.cursor()
        status = "approved" if approve else "rejected"
        c.execute("UPDATE reports SET status=?, decided_at=?, decided_by=? WHERE id=? AND status='pending'",
                  (status, now, approver_id, rid))
        if c.rowcount == 0:
            return None
        row = c.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        conn.commit()
        return row

def auto_approve_reports():
    """24時間未対応は自動承認"""
    with db() as conn:
        c = conn.cursor()
        now = dt.datetime.now(TZ)
        limit = now - dt.timedelta(hours=24)
        rows = c.execute(
            "SELECT id FROM reports WHERE status='pending' AND submitted_at <= ?",
            (limit.isoformat(),)
        ).fetchall()
        for r in rows:
            c.execute("UPDATE reports SET status='auto_approved', decided_at=?, decided_by=NULL WHERE id=?",
                      (now.isoformat(), r["id"]))
        conn.commit()
        return [r["id"] for r in rows]

# ====== 付与ルール（例） ======
REPORT_XP = {
    "adult_coop": 200,
    "party": 150,
    "bbs_post": 30,
    "voice_15m": 40,
    "xoad_sns": 80,
    "invite_join": 500,
    "special": 500,      # スペシャル広告
    "general": 100,      # 全年齢広告
}

def xp_for_plan(member: discord.Member) -> int:
    # ロール名に基づき判定。NEXUS優先、次にCORE
    names = {r.name for r in member.roles}
    if PLAN_ROLE_MAP["NEXUS"] and PLAN_ROLE_MAP["NEXUS"] in names:
        return DAILY_XP["NEXUS"]
    if PLAN_ROLE_MAP["CORE"] and PLAN_ROLE_MAP["CORE"] in names:
        return DAILY_XP["CORE"]
    return DAILY_XP["SEED"]

def is_admin(member: discord.Member) -> bool:
    return any(r.name == ADMIN_ROLE for r in member.roles)

# ====== Discord Bot ======
intents = discord.Intents.default()
intents.members = True  # Server Members Intent
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ====== 追加: /post（xoad / collab）即時公開コマンド ======

# 簡易クールダウン: { (user_id, kind): last_epoch }
_last_post_at = {}

def role_mention_or_text(guild: discord.Guild, raw: str) -> str:
    """'@ロール名' or 'ロール名' を役職メンションに変換。見つからなければ入力文字列を返す。"""
    if not guild or not raw:
        return raw or ""
    name = raw.strip()
    name = name[1:] if name.startswith("@") else name
    role = discord.utils.get(guild.roles, name=name)
    return role.mention if role else raw

def cooldown_ok(user_id: int, kind: str) -> Tuple[bool, int]:
    """クールダウン判定。True/False と 残り秒数を返す"""
    if COOLDOWN_SEC <= 0:
        return True, 0
    key = (user_id, kind)
    now = time.time()
    last = _last_post_at.get(key, 0.0)
    remain = int(COOLDOWN_SEC - (now - last))
    if remain > 0:
        return False, remain
    _last_post_at[key] = now
    return True, 0

# /post グループ定義（承認なしで即公開）
post = app_commands.Group(name="post", description="XOADの広告/コラボ投稿（承認なしで即公開）")

@post.command(name="xoad", description="XOAD広告を公開チャンネルに投稿します")
@app_commands.describe(
    title="広告タイトル",
    cluster="クラスター名（例: @イラストレーター）",
    content="広告本文",
    link="外部リンク（任意）",
    request="協力してほしい内容・要望（任意）"
)
async def post_xoad(
    interaction: discord.Interaction,
    title: str,
    cluster: str,
    content: str,
    link: Optional[str] = None,
    request: Optional[str] = None,
):
    ok, remain = cooldown_ok(interaction.user.id, "xoad")
    if not ok:
        await interaction.response.send_message(
            f"⏳ 投稿間隔の制限中です。あと **{remain}秒** お待ちください。", ephemeral=True
        )
        return

    guild = interaction.guild
    channel = guild.get_channel(AD_CHANNEL_ID) if guild else None
    if not channel:
        await interaction.response.send_message("広告チャンネルが見つかりません。管理者に連絡してください。", ephemeral=True)
        return

    cluster_text = role_mention_or_text(guild, cluster)

    embed = discord.Embed(title=title, description=content, color=discord.Color.green())
    embed.add_field(name="クラスター", value=cluster_text, inline=False)
    if link:
        embed.add_field(name="外部リンク", value=link, inline=False)
    if request:
        embed.add_field(name="協力・要望", value=request, inline=False)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)

    msg = await channel.send(
        content=cluster_text,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )
    # 返信をスレッドに誘導するため自動スレッド作成（失敗しても無視）
    try:
        await msg.create_thread(name=f"広告: {title}"[:90], auto_archive_duration=1440)  # 24h
    except Exception:
        pass

    # ★XP連携したい場合はここで add_ledger(...) を呼ぶ（承認なし運用なら即時付与など）
    await interaction.response.send_message("✅ 広告を投稿しました！", ephemeral=True)

@post.command(name="collab", description="コラボ相手募集を公開チャンネルに投稿します")
@app_commands.describe(
    title="コラボ企画タイトル",
    cluster="クラスター名（例: @動画編集者）",
    content="どんなコラボを誰としたいか（詳細）",
    link="外部リンク（任意）"
)
async def post_collab(
    interaction: discord.Interaction,
    title: str,
    cluster: str,
    content: str,
    link: Optional[str] = None,
):
    ok, remain = cooldown_ok(interaction.user.id, "collab")
    if not ok:
        await interaction.response.send_message(
            f"⏳ 投稿間隔の制限中です。あと **{remain}秒** お待ちください。", ephemeral=True
        )
        return

    guild = interaction.guild
    channel = guild.get_channel(COLLAB_CHANNEL_ID) if guild else None
    if not channel:
        await interaction.response.send_message("コラボ募集チャンネルが見つかりません。管理者に連絡してください。", ephemeral=True)
        return

    cluster_text = role_mention_or_text(guild, cluster)

    embed = discord.Embed(
        title=f"[コラボ募集] {title}",
        description=content,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="クラスター", value=cluster_text, inline=False)
    if link:
        embed.add_field(name="外部リンク", value=link, inline=False)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)

    msg = await channel.send(
        content=cluster_text,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )
    try:
        await msg.create_thread(name=f"コラボ: {title}"[:90], auto_archive_duration=1440)
    except Exception:
        pass

    # ★必要ならここでXP付与フック
    await interaction.response.send_message("✅ コラボ募集を投稿しました！", ephemeral=True)

# ツリーへ登録（on_ready の sync 前に行う）
try:
    if GUILD_ID:
        tree.add_command(post, guild=discord.Object(id=GUILD_ID))
    else:
        tree.add_command(post)
except Exception:
    # 起動順などで失敗しても on_ready の sync 前に再登録される想定
    pass

# 起動時
@bot.event
async def on_ready():
    init_db()
    try:
        guild = discord.Object(id=GUILD_ID) if GUILD_ID else None
        if guild:
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        print(f"Logged in as {bot.user} (synced)")
    except Exception as e:
        print("Sync error:", e)
    daily_grant.start()
    auto_approve.start()
    monthly_tax_and_ranking.start()

# ====== Slash Commands ======
@tree.command(name="xp_check", description="現在XPと累計XPを表示します。", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(user="対象ユーザー（省略で自分）")
async def xp_check(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    ensure_user(user)
    cur, total = get_points(user.id)
    await interaction.response.send_message(
        f"**{user.mention}** のXP\n現在: **{cur}**  | 累計: **{total}**",
        ephemeral=False
    )

@tree.command(name="xp_add", description="（運営）XPを付与します。", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.checks.has_role(ADMIN_ROLE)
@app_commands.describe(user="対象ユーザー", amount="付与XP（整数）")
async def xp_add(interaction: discord.Interaction, user: discord.Member, amount: int):
    ensure_user(user)
    add_ledger(user.id, amount, "MANUAL_ADD", "manual", None)
    await interaction.response.send_message(f"{user.mention} に **+{amount}XP** 付与しました。")

@tree.command(name="xp_remove", description="（運営）XPを減算します。", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.checks.has_role(ADMIN_ROLE)
@app_commands.describe(user="対象ユーザー", amount="減算XP（整数）")
async def xp_remove(interaction: discord.Interaction, user: discord.Member, amount: int):
    ensure_user(user)
    add_ledger(user.id, -abs(amount), "MANUAL_REMOVE", "manual", None)
    await interaction.response.send_message(f"{user.mention} から **-{abs(amount)}XP** 減算しました。")

@tree.command(name="xp_give", description="自分のXPを他者へ譲渡します（10%手数料）。", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(recipient="受取ユーザー", amount="譲渡XP（整数）")
async def xp_give(interaction: discord.Interaction, recipient: discord.Member, amount: int):
    sender = interaction.user
    if amount <= 0:
        await interaction.response.send_message("譲渡額は正の整数で指定してください。", ephemeral=True)
        return
    ensure_user(sender)
    ensure_user(recipient)
    cur, _ = get_points(sender.id)
    fee = math.floor(amount * 0.10)  # 手数料10%（小数切り捨て）
    if cur < (amount + fee):
        await interaction.response.send_message(f"残高不足です。必要: {amount+fee}XP / 現在: {cur}XP", ephemeral=True)
        return
    # 記帳（送信者: -amount - fee / 受信者: +amount - fee）
    tid = record_transfer(sender.id, recipient.id, amount, fee)
    add_ledger(sender.id, -(amount + fee), "TRANSFER_OUT", "transfer", tid)
    add_ledger(recipient.id, +(amount - fee), "TRANSFER_IN", "transfer", tid)
    await interaction.response.send_message(
        f"譲渡完了: {sender.mention} → {recipient.mention} へ **{amount}XP**（手数料 {fee}XP）",
        ephemeral=False
    )
    try:
        await sender.send(f"[通知] {recipient} へ {amount}XP 譲渡しました（手数料 {fee}XP）。")
        await recipient.send(f"[通知] {sender} から {amount-fee}XP を受領しました（手数料 {fee}XP控除）。")
    except:
        pass

@tree.command(name="report", description="XP獲得申請を登録します。", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(rtype="申請タイプ（例: adult_coop, party, ...）", details="詳細（URLなど）")
async def report_cmd(interaction: discord.Interaction, rtype: str, details: str):
    user = interaction.user
    ensure_user(user)
    rid = create_report(user.id, rtype, details)
    # 申請通知
    channel = interaction.guild.get_channel(REPORT_NOTIFY_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            title=f"新規申請: {rtype}",
            description=details,
            color=discord.Color.orange()
        )
        embed.add_field(name="申請者", value=interaction.user.mention)
        embed.add_field(name="申請ID", value=str(rid))
        await channel.send(embed=embed)
    await interaction.response.send_message("申請を登録しました。", ephemeral=True)

@tree.command(name="report_approve", description="（運営）申請を承認します。", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.checks.has_role(ADMIN_ROLE)
@app_commands.describe(report_id="申請ID")
async def report_approve(interaction: discord.Interaction, report_id: int):
    row = decide_report(report_id, interaction.user.id, True)
    if not row:
        await interaction.response.send_message("承認できません（既に処理済みかID不正）。", ephemeral=True)
        return
    xp = REPORT_XP.get(row["rtype"], 0)
    add_ledger(row["user_id"], xp, "REPORT_APPROVED", "report", row["id"])
    await interaction.response.send_message(f"ID {report_id} を承認し **+{xp}XP** を付与しました。", ephemeral=False)

@tree.command(name="report_reject", description="（運営）申請を否認します。", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.checks.has_role(ADMIN_ROLE)
@app_commands.describe(report_id="申請ID", reason="否認理由")
async def report_reject(interaction: discord.Interaction, report_id: int, reason: str):
    row = decide_report(report_id, interaction.user.id, False)
    if not row:
        await interaction.response.send_message("否認できません（既に処理済みかID不正）。", ephemeral=True)
        return
    await interaction.response.send_message(f"ID {report_id} を否認しました。", ephemeral=False)
    # DM通知
    try:
        member = await interaction.guild.fetch_member(row["user_id"])
        await member.send(f"[申請否認] ID {report_id} は否認されました。理由: {reason}")
    except:
        pass

class ReportActionView(discord.ui.View):
    def __init__(self, report_id: int, user_id: int, rtype: str, details: str):
        super().__init__(timeout=300)
        self.report_id = report_id
        self.user_id = user_id
        self.rtype = rtype
        self.details = details

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = decide_report(self.report_id, interaction.user.id, True)
        if not row:
            await interaction.response.send_message("承認できません（既に処理済みかID不正）。", ephemeral=True)
            return
        xp = REPORT_XP.get(self.rtype, 0)
        add_ledger(self.user_id, xp, "REPORT_APPROVED", "report", self.report_id)
        await interaction.response.send_message(f"ID {self.report_id} を承認し **+{xp}XP** を付与しました。", ephemeral=True)
        self.stop()

    @discord.ui.button(label="否認", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = decide_report(self.report_id, interaction.user.id, False)
        if not row:
            await interaction.response.send_message("否認できません（既に処理済みかID不正）。", ephemeral=True)
            return
        await interaction.response.send_message(f"ID {self.report_id} を否認しました。", ephemeral=True)
        self.stop()

@tree.command(name="report_list", description="（運営）未処理申請を一覧表示（ボタン承認）", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.checks.has_role(ADMIN_ROLE)
async def report_list(interaction: discord.Interaction):
    # 未処理申請を最大5件表示（必要に応じてページング可）
    with db() as conn:
        rows = conn.execute("SELECT * FROM reports WHERE status='pending' ORDER BY submitted_at ASC LIMIT 5").fetchall()
    if not rows:
        await interaction.response.send_message("未処理の申請はありません。", ephemeral=True)
        return
    for r in rows:
        embed = discord.Embed(
            title=f"申請ID: {r['id']} / {r['rtype']}",
            description=r['details'],
            color=discord.Color.orange()
        )
        embed.add_field(name="申請者", value=f"<@{r['user_id']}>", inline=True)
        embed.add_field(name="申請日時", value=r['submitted_at'], inline=True)
        view = ReportActionView(r['id'], r['user_id'], r['rtype'], r['details'])
        await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("申請一覧を表示しました。", ephemeral=True)

# ====== バックグラウンド（スケジューラ） ======
def last_ran(job_key: str) -> Optional[dt.datetime]:
    with db() as conn:
        row = conn.execute("SELECT last_run_at FROM job_runs WHERE job_key=?", (job_key,)).fetchone()
        return dt.datetime.fromisoformat(row["last_run_at"]) if row else None

def mark_ran(job_key: str):
    now = dt.datetime.now(TZ).isoformat()
    with db() as conn:
        conn.execute("INSERT INTO job_runs(job_key,last_run_at) VALUES(?,?) ON CONFLICT(job_key) DO UPDATE SET last_run_at=excluded.last_run_at",
                     (job_key, now))
        conn.commit()

@tasks.loop(minutes=1)
async def daily_grant():
    """毎日0:00（JST）に1回だけ付与（job_runsで冪等）"""
    now = dt.datetime.now(TZ)
    if not (now.hour == 0 and now.minute == 0):
        return
    key = f"daily_grant:{now.date().isoformat()}"
    if last_ran(key):  # 冪等
        return
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if not guild:
        return
    # メンバー全員に対してロール→日次XP
    granted = 0
    async for m in guild.fetch_members(limit=None):
        ensure_user(m)
        xp = xp_for_plan(m)
        if xp > 0:
            add_ledger(m.id, xp, f"DAILY_{xp}", "system", None)
            # DM通知（任意）
            try:
                await m.send(f"[日次付与] {xp}XP を付与しました。")
            except:
                pass
            granted += 1
    mark_ran(key)
    print(f"[daily_grant] completed: members granted={granted}")

@tasks.loop(minutes=15)
async def auto_approve():
    """15分ごとに24h超過申請を自動承認→XP付与"""
    ids = auto_approve_reports()
    if not ids:
        return
    with db() as conn:
        rows = conn.execute("SELECT * FROM reports WHERE id IN (%s)" %
                            ",".join("?"*len(ids)), ids).fetchall()
    for r in rows:
        xp = REPORT_XP.get(r["rtype"], 0)
        add_ledger(r["user_id"], xp, "REPORT_AUTO_APPROVED", "report", r["id"])
        # DM通知
        guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
        if guild:
            try:
                member = await guild.fetch_member(r["user_id"])
                await member.send(f"[自動承認] 申請(ID {r['id']}) は自動承認され、{xp}XP 付与されました。")
            except:
                pass
    print(f"[auto_approve] auto-approved reports: {ids}")

@tasks.loop(minutes=1)
async def monthly_tax_and_ranking():
    """毎月1日 0:00: XP税（10%）
       毎月1日 0:05: 前月/累計の消費ランキングを投稿（簡易表示）"""
    now = dt.datetime.now(TZ)
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if not guild:
        return

    # XP税
    if now.day == 1 and now.hour == 0 and now.minute == 0:
        key = f"tax:{now.strftime('%Y-%m')}"
        if not last_ran(key):
            with db() as conn:
                rows = conn.execute("SELECT user_id, current_points FROM user_totals").fetchall()
                for r in rows:
                    tax = r["current_points"] // 10  # 10%切捨て
                    if tax > 0:
                        add_ledger(r["user_id"], -tax, "XP_TAX_10PCT", "system", None)
                        # DM通知
                        try:
                            m = await guild.fetch_member(r["user_id"])
                            await m.send(f"[XP税] 今月のXP税で {tax}XP 減少しました。")
                        except:
                            pass
            mark_ran(key)
            print("[tax] completed")

    # ランキング
    if now.day == 1 and now.hour == 0 and now.minute == 5:
        key = f"ranking:{now.strftime('%Y-%m')}"
        if last_ran(key):
            return
        # 前月期間
        first_this_month = dt.datetime(now.year, now.month, 1, tzinfo=TZ)
        last_month_end = first_this_month - dt.timedelta(seconds=1)
        last_month_start = dt.datetime(last_month_end.year, last_month_end.month, 1, tzinfo=TZ)

        # 消費 = 負の台帳合計
        with db() as conn:
            prev = conn.execute("""
                SELECT user_id, -SUM(delta) AS spent
                FROM points_ledger
                WHERE delta < 0 AND created_at >= ? AND created_at <= ?
                GROUP BY user_id
                ORDER BY spent DESC
                LIMIT 30
            """, (last_month_start.isoformat(), last_month_end.isoformat())).fetchall()

            # 累計（全期間）
            total = conn.execute("""
                SELECT user_id, -SUM(delta) AS spent
                FROM points_ledger
                WHERE delta < 0
                GROUP BY user_id
                ORDER BY spent DESC
                LIMIT 30
            """).fetchall()

        channel_id = os.getenv("RANKING_CHANNEL_ID")
        ch = guild.get_channel(int(channel_id)) if channel_id else None
        if ch:
            def fmt(rows):
                lines = []
                rank = 1
                for r in rows:
                    lines.append(f"{rank}位：<@{r['user_id']}> {int(r['spent'])}XP")
                    rank += 1
                return "\n".join(lines) if lines else "（該当なし）"

            await ch.send(f"【XOAD XP消費ランキング - 前月】\n{fmt(prev)}")
            await ch.send(f"【XOAD XP消費ランキング - 累計】\n{fmt(total)}")

        mark_ran(key)
        print("[ranking] posted")

# ====== 実行 ======
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("環境変数 BOT_TOKEN が未設定です(.env/Tokensetting.env を用意してください)")
    bot.run(TOKEN)
