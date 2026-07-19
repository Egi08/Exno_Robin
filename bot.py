"""Bot Telegram LP Robin — screening token + analisis AI + deploy LP Uniswap v3.

Workflow (dari trik CT):
  /screen            -> ambil trending pools (GeckoTerminal), filter 5m vol & likuiditas,
                        deteksi bundler (tx >> jumlah wallet unik), skor AI
  /analyze <CA>      -> analisis AI mendalam 1 token + rekomendasi range LP
  /lp <CA> <eth> <pct> -> deploy posisi one-sided lower di Uniswap v3
"""
import asyncio
import json
import logging
import math
import os
import re

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lp-robin")

NETWORK = os.environ.get("NETWORK_SLUG", "eth")
MIN_VOL_5M = float(os.environ.get("MIN_VOL_5M_USD", 100_000))
MIN_LIQ = float(os.environ.get("MIN_LIQUIDITY_USD", 50_000))
# filter ala @0xyunss (tf 24 jam): mcap > $500k, vol > $1m, hindari flap.fun
MIN_VOL_24H = float(os.environ.get("MIN_VOL_24H_USD", 1_000_000))
MIN_MCAP = float(os.environ.get("MIN_MCAP_USD", 500_000))
# metode Yunus (GMGN): total fee 24h pool minimal segini (ETH) — token harus
# MENGHASILKAN fee nyata, bukan cuma volume
MIN_FEES_ETH = float(os.environ.get("MIN_FEES_24H_ETH", 0.4))
DEX_BLACKLIST = {d.strip().lower() for d in
                 os.environ.get("DEX_BLACKLIST", "flap,klik").split(",") if d.strip()}
MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", 3))
# mode auto ala Meridian: screening -> analisis -> deploy tiap N menit (0 = mati)
AUTO_MIN = float(os.environ.get("AUTO_INTERVAL_MIN", 30))
AUTO_ETH = float(os.environ.get("AUTO_ETH", 0.01))
# sizing berdasarkan saldo (ala Meridian positionSizePct): tiap deploy = % saldo wallet.
# 0 = pakai AUTO_ETH tetap. Butuh PRIVATE_KEY terisi untuk baca saldo.
AUTO_SIZE_PCT = float(os.environ.get("AUTO_SIZE_PCT", 30))
GAS_RESERVE_ETH = float(os.environ.get("GAS_RESERVE_ETH", 0.002))
# quote token untuk LP: WETH (default) atau USDG (meta LP stabil ala lp-terminal)
QUOTE = os.environ.get("QUOTE_TOKEN", "WETH").upper()
# fokus satu batch: pause screening+deploy selama masih ada posisi terbuka
AUTO_PAUSE_OPEN = os.environ.get("AUTO_PAUSE_WHEN_OPEN", "true").lower() != "false"
# dua sisi: swap ~50% ke token + range ±pct/2 mengapit harga -> fee sejak awal,
# tapi ikut menanggung naik-turun harga token (disetujui user 2026-07-18)
TWO_SIDED = os.environ.get("TWO_SIDED", "false").lower() == "true"
# management agent ala Meridian: awasi posisi tiap N menit (0 = mati)
MGMT_MIN = float(os.environ.get("MGMT_INTERVAL_MIN", 10))
# heartbeat: kirim status PnL tiap siklus management walau tidak ada perubahan
MGMT_HEARTBEAT = os.environ.get("MGMT_HEARTBEAT", "true").lower() != "false"
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", -15))
# take-profit fee: auto-close saat fee terkumpul >= % modal posisi ini (0 = mati)
FEE_TP_PCT = float(os.environ.get("FEE_TP_PCT", 30))
# ala Meridian outOfRangeWaitMinutes: harga di ATAS range terus N menit ->
# fee mati, jual semua, cari koin baru (0 = mati)
OUT_RANGE_CLOSE_MIN = float(os.environ.get("OUT_OF_RANGE_CLOSE_MIN", 30))
# token yang baru ditutup kena cooldown N jam — jangan main koin yang sama terus
TOKEN_COOLDOWN_H = float(os.environ.get("TOKEN_COOLDOWN_H", 6))
# kedalaman minimal sisi USDG pool supaya dianggap layak LP
MIN_USDG_POOL_USD = float(os.environ.get("MIN_USDG_POOL_USD", 500))
# ala Meridian minHolders/maxTop10Pct: distribusi holder sehat (dari GoPlus)
MIN_HOLDERS = int(os.environ.get("MIN_HOLDERS", 500))
MAX_TOP10_PCT = float(os.environ.get("MAX_TOP10_PCT", 60))
# anti beli pucuk: tunda deploy kalau token lagi pump aktif — akar penyebab
# posisi cepat kabur ke atas range (timing ala Yunus: masuk setelah koreksi)
MAX_CHG_5M = float(os.environ.get("MAX_DEPLOY_CHG_5M_PCT", 3))
MAX_CHG_1H = float(os.environ.get("MAX_DEPLOY_CHG_1H_PCT", 10))
BLACKLIST_FILE = os.path.join(os.path.dirname(__file__), "token-blacklist.json")
# auto-rebalance: harga >= entry*(1+pct/100) & di atas range -> geser range naik (0 = mati)
REBAL_PCT = float(os.environ.get("REBAL_ABOVE_PCT", 5))
# target compounding: saldo quote >= nilai ini -> berhenti deploy, lapor 🎯 (0 = tanpa target)
TARGET_USD = float(os.environ.get("TARGET_USD", 100))
TARGET_FLAG = os.path.join(os.path.dirname(__file__), "TARGET_REACHED")
MIN_POOL_VOL_ALERT = float(os.environ.get("MIN_POOL_VOL_ALERT_USD", 500_000))
POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
GT = "https://api.geckoterminal.com/api/v2"

AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.siliconflow.com/v1/chat/completions")
AI_MODEL = os.environ.get("AI_MODEL", "tencent/Hy3")
# AI cadangan: dipakai kalau AI utama lambat (> timeout) / error — kombinasi
# glm (pintar tapi antri) + Hy3 (responsif)
AI2_BASE_URL = os.environ.get("AI2_BASE_URL", "")
AI2_API_KEY = os.environ.get("AI2_API_KEY", "")
AI2_MODEL = os.environ.get("AI2_MODEL", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"   # default aman: simulasi
CAPITAL_USD = float(os.environ.get("DRY_RUN_CAPITAL_USD", 50))
DECISION_LOG = os.path.join(os.path.dirname(__file__), "decision-log.json")


POS_LOCK = asyncio.Lock()  # serialisasi baca-tulis positions.json antar siklus/command


def _write_json(path: str, data):
    """Tulis atomik (temp + rename): crash di tengah tulis tidak mengkorup file."""
    tmp = path + ".tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def log_decision(entry: dict):
    """Decision log ala Meridian: catat tiap deploy/skip biar bisa dievaluasi."""
    import time
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    hist = []
    if os.path.exists(DECISION_LOG):
        try:
            hist = json.load(open(DECISION_LOG, encoding="utf-8"))
        except json.JSONDecodeError:
            log.error("decision-log korup, mulai baru (backup: .bak)")
            os.replace(DECISION_LOG, DECISION_LOG + ".bak")
    hist.append(entry)
    _write_json(DECISION_LOG, hist)


def load_positions() -> list:
    if os.path.exists(POSITIONS_FILE):
        try:
            return json.load(open(POSITIONS_FILE, encoding="utf-8"))
        except json.JSONDecodeError:
            log.error("positions.json korup, mulai kosong (backup: .bak)")
            os.replace(POSITIONS_FILE, POSITIONS_FILE + ".bak")
    return []


def save_positions(pos: list):
    _write_json(POSITIONS_FILE, pos)


def pos_pnl(p: dict, price_now: float):
    """Estimasi PnL % (belum termasuk fee) + zona harga + kunci zona.
    One-sided lower: range [entry*(1-pct), entry], awal 100% quote di pb.
    Two-sided: range entry ± pct/2, awal campuran sesuai harga entry.
    Matematika nilai posisi Uniswap v3: v(P) generik untuk kedua bentuk."""
    entry = p.get("entry_price") or 0
    if not entry or not price_now:
        return None, "harga tidak tersedia", "unknown"
    if p.get("two_sided"):
        half = p["range_pct"] / 200
        pa, pb = entry * (1 - half), entry * (1 + half)
    else:
        pb, pa = entry, entry * (1 - p["range_pct"] / 100)

    def v(P):  # nilai posisi (unit quote) untuk 1 unit liquidity
        Pc = min(max(P, pa), pb)
        return (P * (1 / math.sqrt(Pc) - 1 / math.sqrt(pb))
                + math.sqrt(Pc) - math.sqrt(pa))
    pnl = (v(price_now) / v(entry) - 1) * 100
    if price_now >= pb:
        return pnl, f"🔵 di atas range · 100% {QUOTE}", "above"
    if price_now <= pa:
        return pnl, "🔴 tembus bawah · 100% token", "below"
    return pnl, "🟢 dalam range · fee jalan", "in"


ZONE_TEXT = {"above": f"🔵 di atas range · 100% {QUOTE}",
             "below": "🔴 tembus bawah · 100% token",
             "in": "🟢 dalam range · fee jalan"}


def _usd(n: float) -> str:
    """$1.4M / $288k / $530 — angka uang enak dibaca."""
    if n >= 1e6:
        return f"${n / 1e6:.1f}M"
    if n >= 1e3:
        return f"${n / 1e3:.0f}k"
    return f"${n:.0f}"


async def pos_zone(p: dict, now: float):
    """pos_pnl + koreksi zona dari tick on-chain untuk posisi live v4.
    Kasus VIRTUAL 2026-07-19: estimasi harga bilang 'in', chain bilang 'above'
    -> auto-close 30-menit-di-atas-range tidak pernah jalan."""
    pnl, zone, key = pos_pnl(p, now)
    if (p.get("mode") == "live" and p.get("versi") == "v4"
            and p.get("token_id") is not None):
        try:
            from lp4 import v4_zone
            k2 = await asyncio.to_thread(v4_zone, p)
            if k2 != key:
                key, zone = k2, ZONE_TEXT[k2]
        except Exception:
            log.exception("v4_zone gagal, pakai estimasi harga")
    return pnl, zone, key


def allowed(update: Update) -> bool:
    return not ALLOWED or (update.effective_user and update.effective_user.id in ALLOWED)


async def safe_edit(msg, text: str):
    """Edit pesan sebagai markdown; teks AI sering punya entity tak seimbang -> fallback polos."""
    text = text[:4090]  # limit pesan Telegram 4096
    try:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception:
        await msg.edit_text(text, disable_web_page_preview=True)


async def safe_send(bot, chat_id: int, text: str):
    text = text[:4090]
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN,
                               disable_web_page_preview=True)
    except Exception:
        await bot.send_message(chat_id, text, disable_web_page_preview=True)


async def gt_get(path: str) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{GT}{path}", headers={"accept": "application/json"}) as r:
            r.raise_for_status()
            return await r.json()


GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/"


async def goplus_security(cas: list[str]) -> dict:
    """Cek keamanan token batch via GoPlus (honeypot, tax, renounce). Key: CA lowercase."""
    url = (GOPLUS + os.environ.get("CHAIN_ID", "4663")
           + "?contract_addresses=" + ",".join(cas))
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()
    return {k.lower(): v for k, v in (data.get("result") or {}).items()}


def pool_row(p: dict) -> dict:
    """Ringkas atribut pool GeckoTerminal jadi dict pendek untuk filter + AI."""
    a = p["attributes"]
    tx5 = (a.get("transactions") or {}).get("m5") or {}
    buys, sells = tx5.get("buys", 0), tx5.get("sells", 0)
    buyers, sellers = tx5.get("buyers", 0), tx5.get("sellers", 0)
    wallets = buyers + sellers
    txs = buys + sells
    ca = (p.get("relationships", {}).get("base_token", {}).get("data", {})
          .get("id", "")).split("_")[-1]
    dex = p.get("relationships", {}).get("dex", {}).get("data", {}).get("id", "")
    # fee tier dari nama pool ("Index / USDG 1%"); est fee APR ala lp-terminal:
    # vol24h * fee% * 365 / TVL
    fm = re.search(r"(\d+(?:\.\d+)?)%", a.get("name") or "")
    fee_pct = float(fm.group(1)) if fm else None
    liq = float(a.get("reserve_in_usd") or 0)
    vol24 = float((a.get("volume_usd") or {}).get("h24") or 0)
    apr = round(vol24 * (fee_pct / 100) * 365 / liq * 100, 1) \
        if fee_pct and liq > 0 else None
    return {
        "name": a.get("name"),
        "fee_pct": fee_pct,
        "est_fee_apr_pct": apr,
        # ekuivalen kolom "Total Fees" GMGN: fee riil yang dihasilkan pool 24h
        "fees_24h_usd": round(vol24 * fee_pct / 100) if fee_pct else None,
        "ca": ca,
        "dex": dex,
        "price_usd": a.get("base_token_price_usd"),
        "liq_usd": float(a.get("reserve_in_usd") or 0),
        "mcap_usd": float(a.get("market_cap_usd") or a.get("fdv_usd") or 0),
        "vol_5m": float((a.get("volume_usd") or {}).get("m5") or 0),
        "vol_1h": float((a.get("volume_usd") or {}).get("h1") or 0),
        "vol_24h": float((a.get("volume_usd") or {}).get("h24") or 0),
        "chg_5m": (a.get("price_change_percentage") or {}).get("m5"),
        "chg_1h": (a.get("price_change_percentage") or {}).get("h1"),
        "chg_24h": (a.get("price_change_percentage") or {}).get("h24"),
        "txs_5m": txs,
        "wallets_5m": wallets,
        # trik Hesz: wallet unik >> jumlah tx = sehat; tx >> wallet = indikasi bundler
        "bundler_suspect": wallets > 0 and txs / wallets > 3,
        "created_at": a.get("pool_created_at"),
    }


def links(ca: str) -> str:
    return (f"[GMGN](https://gmgn.ai/{NETWORK}/token/{ca}) | "
            f"[DexScreener](https://dexscreener.com/{NETWORK}/{ca}) | "
            f"[GeckoTerminal](https://www.geckoterminal.com/{NETWORK}/pools/{ca})")


async def ai_analyze(rows: list[dict], mode: str, extra: str = "") -> str:
    """Kirim data pool ke AI (SiliconFlow, OpenAI-compatible), minta skor & rekomendasi LP."""
    strat = (
        (f"Bot deploy DUA SISI (two-sided) dengan quote {QUOTE}: sebagian modal "
         "di-swap jadi token, likuiditas dipasang MENGAPIT harga sekarang "
         "(range ±pct/2). Konsekuensi: posisi IN RANGE & makan fee sejak awal, "
         "TAPI dana ikut naik-turun harga token. Maka UTAMAKAN token volume "
         "tinggi + harga relatif sideways/support kuat; HINDARI yang rawan dump "
         "dalam (kamu ikut rugi kalau jatuh).\n")
        if TWO_SIDED else
        (f"Bot deploy one-sided lower dengan quote {QUOTE}: modal 100% {QUOTE}, "
         "range di BAWAH harga, nunggu dip. Aman (100% quote selagi nunggu).\n"))
    prompt = (
        "Kamu analis DeFi untuk LP di Robinhood Chain. Data pool DEX (JSON) di bawah.\n"
        + strat +
        "Data pool GeckoTerminal umumnya pair WETH — itu BUKAN masalah dan BUKAN "
        "alasan menolak: bot deploy ke pool TOKEN/USDG on-chain. Field "
        "`punya_pool_usdg` sudah dicek on-chain; pilih kandidat dengan "
        "Bot support Uniswap v3 DAN v4 (otomatis pilih pool USDG terdalam) — field `dex` v2/v3/v4 di data BUKAN alasan tolak selama punya_pool_usdg=true.\n"
        "Field `keamanan` (GoPlus on-chain): honeypot/cannot_sell/tax>10% sudah otomatis dicoret dari lolos_filter; renounced & open_source = nilai plus, mintable = risiko.\n"
        "Field `gmgn` (GMGN.ai): `smart_money`/`kol` = jumlah wallet pintar & KOL yang pegang (banyak = validasi kuat); `rug_ratio`/`bundler_rate`/`entrapment_ratio`/`sniper_count` tinggi = bahaya; `wash_trading`=true berarti volume palsu — coret; `sosmed_dup` tinggi = indikasi akun daur ulang — waspada tapi BUKAN auto-coret (token populer wajar ditiru; timbang bersama sinyal lain). Honeypot & launchpad blacklist versi GMGN sudah otomatis dicoret.\n"
        "punya_pool_usdg=true (usdg_pool_usd = kedalaman sisi USDG pool on-chain; makin besar makin aman).\n"
        "`est_fee_apr_pct` = estimasi fee APR pool (vol24h*fee*365/TVL, ala lp-terminal): "
        "makin tinggi makin besar peluang fee > IL; tapi APR ekstrem di TVL kecil = jebakan. "
        "PENTING: angka itu dari pool TERBESAR token (biasanya WETH) — posisi kita dibuka di "
        "pool USDG, jadi ekspektasi fee yang BENAR adalah `usdg_pool_vol_24h` + "
        "`usdg_fee_apr_pct` (kalau ada). Vol pool USDG kecil = fee nyata kecil walau "
        "pool WETH-nya ramai — turunkan skor kandidat seperti itu.\n"
        "`fees_24h_usd` = total fee riil yang dihasilkan pool 24h (kolom 'Total Fees' GMGN) — "
        "INTI metode Yunus: token wajib menghasilkan fee nyata, bukan cuma volume.\n"
        "Checklist screening (metode Yunus / @0xyunss, tf 24 jam):\n"
        "- tiap kandidat punya `lolos_filter` + `alasan_gagal` dari filter keras "
        "(mcap > $500k, vol 24h > $1m, liq cukup, fees 24h >= 0.4 ETH). Prioritaskan yang lolos. "
        "Yang gagal boleh kamu selamatkan HANYA jika gagalnya tipis dan ada alasan kuat "
        "(sebut alasannya); yang lolos tapi mencurigakan boleh kamu coret.\n"
        "- JANGAN terpaku kandidat #1: bandingkan SEMUA 10 teratas lalu pilih profil terbaik — "
        "trending dengan fee riil konsisten, mcap moderat, tidak sedang dump; "
        "token peringkat bawah (contoh riil: RWA, GRID) sering lebih layak daripada #1 yang sudah overheated.\n"
        "- Meme coin BOLEH dan LAYAK deploy asal volume organik, likuiditas cukup, dan tidak sedang dump parah; jangan tolak hanya karena meme\n"
        "- JANGAN pilih pool USDG / WETH (stable pair) — bukan target LP bot ini\n"
        "- tebak dari nama/data apakah token launchpad murahan (mis. flap.fun) -> tolak\n"
        "- nilai komunitas & fomo: jelas thesisnya atau tidak (sebut apa yang harus dicek manual)\n"
        "Untuk tiap kandidat beri:\n"
        "- skor 0-100 (kelayakan jadi LP)\n"
        "- risiko utama (bundler? volume organik? likuiditas cukup?)\n"
        + ("- rekomendasi range TOTAL (dibelah ±setengah mengapit harga): "
           "20-40% untuk kebanyakan token (makin sempit makin padat fee, tapi makin "
           "gampang tembus batah bawah = 100% token); MAKS 40%. Karena dua sisi, "
           "range sempit hanya untuk token yang harganya stabil/support kuat.\n"
           if TWO_SIDED else
           "- rekomendasi range ala Yunus (range = jarak dari harga sekarang ke batas bawah): "
           "UTAMAKAN LEBAR -30% s/d -50% untuk meme runner volatil (nunggu dip, jarang kena stop-loss, "
           "tetap makan fee tiap harga masuk range); -10% s/d -15% HANYA untuk token yang sudah "
           "terbukti di support kuat; kalau bigcap runner sarankan full range (deploy manual)\n")
        + f"Aturan posisi: maks {MAX_POSITIONS} posisi aktif, compound profit, "
        "target $2k per posisi. Gas kalau ekspektasi fee > IL.\n"
        f"Mode: {mode}. Jawab ringkas bahasa Indonesia, format Telegram markdown, "
        "max 5 kandidat terbaik, urutkan dari skor tertinggi. "
        "JANGAN menyingkat contract address.\n"
        + extra + "\n\n" + json.dumps(rows, ensure_ascii=False)
    )
    # kombinasi model: utama (glm, pintar tapi free tier suka antri/503) dapat
    # 1 percobaan 4 menit; gagal/lambat -> cadangan (Hy3, responsif) 2 percobaan
    endpoints = [(AI_BASE_URL, os.environ["AI_API_KEY"], AI_MODEL, 1, 240)]
    if AI2_BASE_URL and AI2_API_KEY:
        endpoints.append((AI2_BASE_URL, AI2_API_KEY, AI2_MODEL, 2, 300))
    last = "AI error: tidak diketahui"
    for url, key, model, attempts, tmo in endpoints:
        body = {"model": model, "max_tokens": 12000,
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"Authorization": f"Bearer {key}",
                   "Content-Type": "application/json"}
        for attempt in range(attempts):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(url, json=body, headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=tmo)) as r:
                        data = await r.json()
                        if r.status == 200:
                            m = data["choices"][0]["message"]
                            out = m.get("content") or m.get("reasoning_content", "")
                            if out:
                                return f"{out}\n\n_model: {model}_"
                            last = f"AI jawab kosong ({model})"
                        else:
                            last = f"AI error {r.status} ({model}): {str(data)[:200]}"
                            if r.status < 500:
                                break  # error permanen -> langsung endpoint berikut
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = f"AI {model} timeout/koneksi: {e!r}"
            if attempt < attempts - 1:
                await asyncio.sleep(10)
        log.warning("AI %s gagal (%s) — lanjut endpoint berikutnya", model, last)
    raise RuntimeError(last)


def _gmgn_token(ca: str) -> dict:
    """Info + security satu token dari GMGN (untuk /analyze). Best-effort."""
    import subprocess

    def run(sub):
        out = subprocess.run(
            ["gmgn-cli", "token", sub, "--chain", "robinhood",
             "--address", ca, "--raw"],
            capture_output=True, text=True, timeout=25)
        return json.loads(out.stdout) if out.returncode == 0 else {}

    info, sec = run("info"), run("security")
    return {k: v for k, v in {
        "holders": info.get("holder_count"),
        "launchpad": info.get("launchpad") or None,
        "image_dup": info.get("image_dup_count"),
        "top10_rate": sec.get("top_10_holder_rate"),
        "honeypot": bool(sec.get("is_honeypot")),
        "blacklist": bool(sec.get("is_blacklist")),
        "buy_tax": sec.get("buy_tax"), "sell_tax": sec.get("sell_tax"),
        "renounced": sec.get("is_renounced"),
        "open_source": sec.get("is_open_source"),
        "lock_detail": (sec.get("lock_summary") or {}).get("lock_detail"),
    }.items() if v is not None}


def _gmgn_trending() -> dict:
    """Trending 24h GMGN via gmgn-cli (ala FlipZ3ro, best-effort).
    Return {ca_lowercase: row}. Raise kalau CLI gagal — caller yang nangkap."""
    import subprocess
    out = subprocess.run(
        ["gmgn-cli", "market", "trending", "--chain", "robinhood",
         "--interval", "24h", "--limit", "60", "--raw"],
        capture_output=True, text=True, timeout=25)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout)[:200])
    data = json.loads(out.stdout)
    return {t["address"].lower(): t for t in data["data"]["rank"]
            if t.get("address")}


async def fetch_candidates():
    """Screening tahap 1: trending + top pool Uniswap v3 (ala lp-terminal),
    dedupe, lalu anotasi filter keras ala @0xyunss."""
    # harga ETH diambil DULUAN (sebelum 4 request sumber di bawah) biar tidak
    # kena rate limit GT; + 1x retry
    eth_usd = 0.0
    for _ in range(2):
        try:
            weth = os.environ.get("WETH_ADDRESS", "").lower()
            data = await gt_get(f"/simple/networks/{NETWORK}/token_price/{weth}")
            eth_usd = float(data["data"]["attributes"]["token_prices"][weth])
            break
        except Exception:
            await asyncio.sleep(5)
    if not eth_usd:
        log.warning("harga ETH gagal diambil — filter fees 24h dilewati")
    raw = []
    # sumber ala GMGN "Sedang Tren 24h": trending + SEMUA pool diurut volume 24h
    # (2 halaman biar token kecil macam RWA/GRID/VEX ikut kesaring)
    for path in (f"/networks/{NETWORK}/trending_pools?page=1",
                 f"/networks/{NETWORK}/pools?page=1&sort=h24_volume_usd_desc",
                 f"/networks/{NETWORK}/pools?page=2&sort=h24_volume_usd_desc",
                 f"/networks/{NETWORK}/pools?page=3&sort=h24_volume_usd_desc",
                 f"/networks/{NETWORK}/dexes/uniswap-v3-{NETWORK}/pools?page=1"):
        try:
            raw += (await gt_get(path)).get("data", [])
        except Exception:
            log.warning("fetch %s gagal, lanjut sumber lain", path)
    seen, rows = set(), []
    for p in raw:
        r = pool_row(p)
        if r["ca"] not in seen:
            seen.add(r["ca"])
            rows.append(r)
    rows = [r for r in rows if not any(b in (r["dex"] or "").lower() for b in DEX_BLACKLIST)]
    skip_ca = {os.environ.get("USDG_ADDRESS", "").lower(),
               os.environ.get("WETH_ADDRESS", "").lower()}
    rows = [r for r in rows if r["ca"].lower() not in skip_ca]
    for r in rows:
        gagal = []
        # mcap 0 = GeckoTerminal tidak punya datanya (bukan berarti kecil,
        # contoh: PONS $13M di GMGN tapi 0 di GT) -> jangan digagalkan, AI yang nilai
        if 0 < r["mcap_usd"] < MIN_MCAP:
            gagal.append(f"mcap < ${MIN_MCAP:,.0f}")
        elif not r["mcap_usd"]:
            r["mcap_tidak_tersedia"] = True
        if r["vol_24h"] < MIN_VOL_24H:
            gagal.append(f"vol24h < ${MIN_VOL_24H:,.0f}")
        if r["liq_usd"] < MIN_LIQ:
            gagal.append(f"liq < ${MIN_LIQ:,.0f}")
        if (eth_usd and r["fees_24h_usd"] is not None
                and r["fees_24h_usd"] < MIN_FEES_ETH * eth_usd):
            gagal.append(f"fees24h < {MIN_FEES_ETH:g} ETH "
                         f"(${MIN_FEES_ETH * eth_usd:,.0f})")
        r["lolos_filter"] = not gagal
        r["alasan_gagal"] = gagal
    try:
        cas = [r["ca"] for r in rows
               if re.fullmatch(r"0x[a-fA-F0-9]{40}", r["ca"] or "")]
        sec = await goplus_security(cas) if cas else {}
    except Exception:
        log.exception("GoPlus gagal — lanjut tanpa cek keamanan")
        sec = {}
    for r in rows:
        s = sec.get((r["ca"] or "").lower())
        if not s:
            continue
        bahaya = []
        if s.get("is_honeypot") == "1":
            bahaya.append("HONEYPOT")
        if s.get("cannot_sell_all") == "1":
            bahaya.append("cannot_sell_all")
        if s.get("cannot_buy") == "1":
            bahaya.append("cannot_buy")
        if float(s.get("sell_tax") or 0) > 0.10:
            bahaya.append(f"sell_tax {float(s['sell_tax']) * 100:.0f}%")
        if bahaya:
            r["lolos_filter"] = False
            r["alasan_gagal"] = r["alasan_gagal"] + ["GoPlus: " + ", ".join(bahaya)]
        # ala Meridian minHolders + maxTop10Pct (holder kontrak/pool dikecualikan)
        try:
            hc = int(s.get("holder_count") or 0)
            if 0 < hc < MIN_HOLDERS:
                r["lolos_filter"] = False
                r["alasan_gagal"] = r["alasan_gagal"] + [f"holders {hc} < {MIN_HOLDERS}"]
            top10 = sum(float(h.get("percent") or 0)
                        for h in (s.get("holders") or [])[:10]
                        if not int(h.get("is_contract") or 0)) * 100
            if top10 > MAX_TOP10_PCT:
                r["lolos_filter"] = False
                r["alasan_gagal"] = r["alasan_gagal"] + [
                    f"top10 holder {top10:.0f}% > {MAX_TOP10_PCT:.0f}%"]
            r["holders"] = hc or None
            r["top10_pct"] = round(top10, 1)
        except (TypeError, ValueError):
            pass
        r["keamanan"] = {
            "open_source": s.get("is_open_source") == "1",
            "renounced": (s.get("owner_address") or "").lower() in
                         ("", "0x0000000000000000000000000000000000000000",
                          "0x000000000000000000000000000000000000dead"),
            "mintable": s.get("is_mintable") == "1",
            "buy_tax": s.get("buy_tax"), "sell_tax": s.get("sell_tax"),
        }
    # enrichment GMGN: smart money/KOL/rug/bundler/launchpad per token.
    # Honeypot & launchpad blacklist = hard fail; sisanya bahan nilai AI.
    try:
        gm = await asyncio.to_thread(_gmgn_trending)
    except Exception:
        log.exception("GMGN gagal — lanjut tanpa enrichment")
        gm = {}
    for r in rows:
        g = gm.get((r["ca"] or "").lower())
        if not g:
            continue
        gm_mc = float(g.get("market_cap") or 0)
        if gm_mc and (not r["mcap_usd"] or
                      abs(gm_mc - r["mcap_usd"]) / max(gm_mc, r["mcap_usd"]) > 0.5):
            # GT kadang kosong (PONS) atau salah besar — pair kebalik bikin mcap
            # WETH kepakai (contoh "WETH / AnsemCat" $4.6B) — angka GMGN menang
            r["mcap_usd"] = gm_mc
            r.pop("mcap_tidak_tersedia", None)
            if r["mcap_usd"] < MIN_MCAP and r["lolos_filter"]:
                r["lolos_filter"] = False
                r["alasan_gagal"] = r["alasan_gagal"] + [
                    f"mcap (GMGN) < ${MIN_MCAP:,.0f}"]
        if g.get("is_honeypot"):
            r["lolos_filter"] = False
            r["alasan_gagal"] = r["alasan_gagal"] + ["GMGN: honeypot"]
        lp_pad = (g.get("launchpad") or "").lower()
        if any(b in lp_pad for b in DEX_BLACKLIST):
            r["lolos_filter"] = False
            r["alasan_gagal"] = r["alasan_gagal"] + [f"GMGN: launchpad {lp_pad}"]
        r["gmgn"] = {
            "smart_money": g.get("smart_degen_count"),
            "kol": g.get("renowned_count"),
            "rug_ratio": g.get("rug_ratio"),
            "bundler_rate": g.get("bundler_rate"),
            "entrapment_ratio": g.get("entrapment_ratio"),
            "sniper_count": g.get("sniper_count"),
            "launchpad": g.get("launchpad") or None,
            "holders": g.get("holder_count"),
            "top10_rate": g.get("top_10_holder_rate"),
            "wash_trading": bool(g.get("is_wash_trading")),
            "sosmed_dup": {"tw": g.get("twitter_dup"),
                           "web": g.get("website_dup")},
        }
    hits = sorted(rows, key=lambda r: (not r["lolos_filter"], -r["vol_24h"]))
    if QUOTE == "USDG":
        from lp import quote_pool_liquidity
        for r in hits[:12]:
            if r["lolos_filter"] and re.fullmatch(r"0x[a-fA-F0-9]{40}", r["ca"] or ""):
                try:
                    usd = await asyncio.to_thread(quote_pool_liquidity, r["ca"])
                    r["usdg_pool_usd"] = round(usd)
                    r["punya_pool_usdg"] = usd >= MIN_USDG_POOL_USD
                    if r["punya_pool_usdg"]:
                        # est_fee_apr_pct dihitung dari pool TERBESAR (WETH) —
                        # posisi kita di pool USDG: ambil vol & APR pool USDG asli
                        data = await gt_get(
                            f"/networks/{NETWORK}/tokens/{r['ca']}/pools?page=1")
                        up = [pool_row(q) for q in data.get("data", [])]
                        up = [u for u in up if "USDG" in (u["name"] or "").upper()]
                        if up:
                            u = max(up, key=lambda x: x["vol_24h"])
                            r["usdg_pool_vol_24h"] = round(u["vol_24h"])
                            r["usdg_fee_apr_pct"] = u["est_fee_apr_pct"]
                except Exception:
                    log.exception("cek pool USDG gagal: %s", r["name"])
                    r["punya_pool_usdg"] = None
    note = "" if any(r["lolos_filter"] for r in hits) else \
        "\n_(tidak ada yang lolos filter keras; AI menilai semua kandidat)_"
    return hits, note


def ca_lines(hits: list, n: int = 5) -> str:
    # CA full dicetak kode (AI suka menyingkat), monospace biar gampang di-copy
    return "\n".join(f"• {r['name']}\n  `{r['ca']}`\n  {links(r['ca'])}" for r in hits[:n])


async def cmd_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("🔎 Screening trending pools…")
    try:
        hits, note = await fetch_candidates()
        analysis = await ai_analyze(hits[:12], "screening trending")
        await safe_edit(msg, f"{analysis}\n\n*CA & Links:*\n{ca_lines(hits)}{note}")
    except Exception as e:
        log.exception("screen gagal")
        await msg.edit_text(f"❌ Gagal: {e}")


async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if not ctx.args:
        await update.message.reply_text("Pakai: /analyze <contract_address>")
        return
    ca = ctx.args[0]
    msg = await update.message.reply_text("🧠 Analisis token…")
    try:
        data = await gt_get(f"/networks/{NETWORK}/tokens/{ca}/pools?page=1")
        rows = [pool_row(p) for p in data.get("data", [])][:5]
        if not rows:
            await msg.edit_text("Pool tidak ditemukan untuk CA itu.")
            return
        try:
            rows[0]["gmgn_token"] = await asyncio.to_thread(_gmgn_token, ca)
        except Exception:
            log.exception("GMGN token gagal — analisis lanjut tanpanya")
        analysis = await ai_analyze(rows, "deep-dive satu token")
        await safe_edit(msg, f"{analysis}\n\n`{ca}`\n{links(ca)}")
    except Exception as e:
        log.exception("analyze gagal")
        await msg.edit_text(f"❌ Gagal: {e}")


async def token_price(ca: str) -> float:
    try:
        d = await gt_get(f"/networks/{NETWORK}/tokens/{ca}")
        return float(d["data"]["attributes"].get("price_usd") or 0)
    except Exception:
        return 0.0


async def do_deploy(ca: str, eth_amt: float, pct: float, actor: str,
                    name: str = "", entry_price: float = 0, reason: str = "") -> str:
    """Deploy (atau simulasi kalau DRY_RUN) + catat posisi & decision log."""
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", ca):
        return f"⛔ CA tidak valid: {ca}"
    if not 1 <= pct <= 90:
        return f"⛔ Range {pct}% di luar batas wajar (1-90%)."
    if eth_amt <= 0:
        return "⛔ Jumlah ETH harus > 0."
    async with POS_LOCK:
        return await _do_deploy_locked(ca, eth_amt, pct, actor, name, entry_price, reason)


async def _do_deploy_locked(ca, eth_amt, pct, actor, name, entry_price, reason="") -> str:
    import time
    pos = load_positions()
    if len(pos) >= MAX_POSITIONS:
        return f"⛔ Slot penuh ({len(pos)}/{MAX_POSITIONS}). Tutup posisi dulu (/close <no>)."
    if any(p["ca"].lower() == ca.lower() for p in pos):
        return "⛔ Token ini sudah ada posisinya."
    if not entry_price:
        entry_price = await token_price(ca)
    entry = {"ca": ca, "name": name or ca[:10], "eth": eth_amt, "quote": QUOTE,
             "range_pct": pct, "entry_price": entry_price,
             "mode": "dry" if DRY_RUN else "live",
             "actor": actor, "reason": reason[:500],
             "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    reason_txt = f"\n📝 Alasan: {reason[:300]}" if reason else ""
    if DRY_RUN:
        log_decision({"actor": actor, "action": "deploy_simulated", **entry})
        pos.append(entry)
        save_positions(pos)
        return (f"🧪 *DRY RUN* posisi #{len(pos)}: {eth_amt} {QUOTE} one-sided lower "
                f"-{pct:.0f}% pada {entry['name']}\n`{ca}`\n"
                f"Entry ${entry_price:.6g}.{reason_txt}\nPantau: /positions")
    if TWO_SIDED:
        try:
            from lp4 import deploy_v4_two_sided
            res = await asyncio.to_thread(deploy_v4_two_sided, ca, eth_amt, pct)
        except RuntimeError as e:
            # pool USDG tak ada / entry swap gagal (pool tipis) -> fallback
            # one-sided lower yang selalu bisa (100% USDG, tanpa swap)
            if not any(s in str(e) for s in ("tidak ditemukan", "beli token gagal")):
                raise
            log.warning("two-sided gagal (%s) -> fallback one-sided", e)
            from lp import deploy_one_sided_lp
            res = await asyncio.to_thread(deploy_one_sided_lp, ca, eth_amt, pct)
    else:
        from lp import deploy_one_sided_lp
        res = await asyncio.to_thread(deploy_one_sided_lp, ca, eth_amt, pct)
    log_decision({"actor": actor, "action": "deploy_live", "ca": ca, **res})
    entry.update(res)
    # nama = pool yang BENERAN dipakai (TOKEN / USDG + fee tier asli),
    # bukan nama pair GeckoTerminal (yang biasanya TOKEN / WETH)
    sym = (name or "").split(" /")[0].strip() or ca[:10]
    entry["name"] = f"{sym} / {QUOTE} {res['fee'] / 10000:g}%"
    pos.append(entry)
    save_positions(pos)
    if res.get("two_sided"):
        lo = entry_price * (1 - pct / 200)
        hi = entry_price * (1 + pct / 200)
        bentuk = (f"dua sisi ±{pct / 2:.0f}%, in range langsung\n"
                  f"📐 Range: ${lo:.6g} ↔ ${hi:.6g}")
    else:
        lo = entry_price * (1 - pct / 100)
        bentuk = (f"one-sided lower\n"
                  f"📐 Range: ${lo:.6g} → ${entry_price:.6g} (-{pct:.0f}% dari entry)")
    return (f"✅ *Posisi #{len(pos)}: {entry['name']}* ({res.get('versi', 'v3')})\n"
            f"💰 Modal: {eth_amt:.2f} {QUOTE} — {bentuk}\n"
            f"Tx: `{res['tx']}`{reason_txt}\nPantau: /positions")


async def cmd_lp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        ca, eth_amt, pct = ctx.args[0], float(ctx.args[1]), float(ctx.args[2])
    except (IndexError, ValueError):
        await update.message.reply_text(
            f"Pakai: /lp <CA> <jumlah_{QUOTE}> <range_pct>\n"
            f"Contoh: /lp 0xabc... {'0.1' if QUOTE == 'WETH' else '50'} 20  (one-sided lower -20%)")
        return
    msg = await update.message.reply_text("🚀 Memproses LP…")
    try:
        text = await do_deploy(ca, eth_amt, pct, actor="user")
        await safe_edit(msg, text)
    except Exception as e:
        log.exception("lp gagal")
        await msg.edit_text(f"❌ Deploy gagal: {e}")


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    pos = load_positions()
    if not pos:
        await update.message.reply_text("Tidak ada posisi terbuka.")
        return
    lines = []
    for i, p in enumerate(pos, 1):
        now = await token_price(p["ca"])
        pnl, zone, _ = await pos_zone(p, now)
        pnl_s = f"{pnl:+.1f}%" if pnl is not None else "?"
        chg = f"{(now / p['entry_price'] - 1) * 100:+.1f}%" if p.get("entry_price") and now else "?"
        isi = ""
        if (p["mode"] == "live" and p.get("versi") == "v4"
                and p.get("token_id") is not None):
            try:
                # isi on-chain ala wallet: SUPPLIED + REWARDS per token
                from lp4 import v4_position_state
                st = await asyncio.to_thread(v4_position_state, p)
                quote_ca = os.environ.get("USDG_ADDRESS", "").lower()
                sym = p["name"].split(" /")[0].strip()
                q_is_c1 = p["c1"].lower() == quote_ca
                # ponytail: desimal diasumsikan token=18 USDG=6 (berlaku semua
                # token chain ini sejauh ini); baca on-chain kalau ada yang aneh
                tok_amt = (st["amt0"] if q_is_c1 else st["amt1"]) / 1e18
                q_amt = (st["amt1"] if q_is_c1 else st["amt0"]) / 1e6
                tok_fee = (st["fee0"] if q_is_c1 else st["fee1"]) / 1e18
                q_fee = (st["fee1"] if q_is_c1 else st["fee0"]) / 1e6
                tok_usd = tok_amt * (now or 0)
                fee_usd = tok_fee * (now or 0) + q_fee
                isi = (f"\n  📦 Isi: {tok_amt:,.2f} {sym} (${tok_usd:.2f}) "
                       f"+ {q_amt:,.2f} USDG — total ${tok_usd + q_amt:.2f}\n"
                       f"  🎁 Fee: {tok_fee:,.2f} {sym} + {q_fee:.4f} USDG "
                       f"(~${fee_usd:.2f})")
            except Exception:
                log.exception("baca isi posisi %s gagal", p.get("name"))
        lines.append(
            f"*#{i} {p['name']}* ({p['mode']}, {p['eth']:.2f} {p.get('quote', QUOTE)}, "
            + (f"±{p['range_pct'] / 2:.0f}%)\n" if p.get("two_sided")
               else f"-{p['range_pct']:.0f}%)\n")
            + f"  `{p['ca']}`\n"
            f"  entry ${p.get('entry_price', 0):.6g} → now ${now:.6g} ({chg})\n"
            f"  {zone} | est PnL (tanpa fee): {pnl_s} | sejak {p['ts']}"
            + isi
            + (f"\n  📝 {p['reason'][:200]}" if p.get("reason") else ""))
    lines.append(f"\nSlot: {len(pos)}/{MAX_POSITIONS} | "
                 "/close <no> = tarik likuiditas + jual ke USDG otomatis")
    await safe_send(ctx.bot, update.effective_chat.id, "\n".join(lines))


PNL_BASELINE = os.path.join(os.path.dirname(__file__), "pnl-baseline.json")


async def _total_value_usd() -> tuple[float, list[str]]:
    """Nilai total portofolio USD (wallet + posisi + fee) + rincian baris."""
    from lp import _cfg, _w3, wallet_balance_quote

    def _wallet():
        w3 = _w3()
        acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
        return wallet_balance_quote(), w3.eth.get_balance(acct.address) / 1e18

    usdg, eth = await asyncio.to_thread(_wallet)
    eth_usd = 0.0
    try:
        weth = os.environ.get("WETH_ADDRESS", "").lower()
        data = await gt_get(f"/simple/networks/{NETWORK}/token_price/{weth}")
        eth_usd = float(data["data"]["attributes"]["token_prices"][weth])
    except Exception:
        log.warning("harga ETH gagal diambil untuk /pnl")
    total = usdg + eth * eth_usd
    lines = [f"USDG wallet: ${usdg:.2f}",
             f"ETH wallet: {eth:.5f} (~${eth * eth_usd:.2f}, cadangan gas)"]
    for p in load_positions():
        now = await token_price(p["ca"])
        pnl, _, _ = pos_pnl(p, now)
        cap = p.get("eth") or 0
        fee = None
        if (p["mode"] == "live" and p.get("versi") == "v4"
                and p.get("token_id") is not None):
            try:
                fee = await asyncio.to_thread(_v4_fee_usd, p, now)
            except Exception:
                log.exception("fee /pnl gagal: %s", p.get("name"))
        val = cap * (1 + (pnl or 0) / 100) + (fee or 0)
        total += val
        lines.append(f"Posisi {p['name']}: ~${val:.2f}"
                     + (f" (incl. fee ${fee:.2f})" if fee else ""))
    return total, lines


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pnl = laporan untung/rugi total; /pnl set <usd> = catat modal masuk."""
    if not allowed(update):
        return
    if ctx.args and ctx.args[0].lower() == "set":
        try:
            base = float(ctx.args[1])
        except (IndexError, ValueError):
            await update.message.reply_text("Pakai: /pnl set <total_modal_usd>")
            return
        json.dump({"modal_usd": base}, open(PNL_BASELINE, "w"))
        await update.message.reply_text(
            f"✅ Modal dasar dicatat: ${base:.2f} — tambah lagi kalau setor lagi.")
        return
    msg = await update.message.reply_text("📊 Menghitung nilai portofolio…")
    total, lines = await _total_value_usd()
    try:
        base = float(json.load(open(PNL_BASELINE))["modal_usd"])
    except Exception:
        base = 0.0
    txt = ("📊 *PnL Portofolio*\n" + "\n".join(f"• {x}" for x in lines)
           + f"\n\n*Total: ${total:.2f}*")
    if base > 0:
        d = total - base
        txt += (f"\nModal masuk: ${base:.2f}\n"
                f"{'🟢 Untung' if d >= 0 else '🔴 Rugi'}: ${d:+.2f} "
                f"({d / base * 100:+.1f}%)")
    else:
        txt += "\n_Catat modal dulu: /pnl set <usd> (total yang pernah kamu setor)_"
    await safe_edit(msg, txt)


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    async with POS_LOCK:
        pos = load_positions()
        try:
            i = int(ctx.args[0]) - 1
            if not 0 <= i < len(pos):
                raise IndexError
            p = pos[i]
        except (IndexError, ValueError):
            await update.message.reply_text(f"Pakai: /close <no 1-{len(pos)}>")
            return
        # jalur sama dengan auto-close: tarik likuiditas on-chain + jual token
        # sisa ke USDG (aturan user: dana wajib berakhir di USDG)
        msg = await update.message.reply_text("📕 Menutup posisi on-chain…")
        try:
            txt = await _auto_close(pos, p, "ditutup manual via /close")
        except Exception as e:
            log.exception("close manual gagal")
            txt = f"⚠️ Close {p['name']} GAGAL: {e} — coba lagi atau tutup di app.uniswap.org"
        await safe_edit(msg, txt)


async def manage_cycle(ctx: ContextTypes.DEFAULT_TYPE):
    """Management agent ala Meridian: awasi posisi, alert kalau butuh tindakan.
    Alert hanya saat keadaan BERUBAH (anti-spam): zona pindah, stop-loss, vol pool mati."""
    chat_id = next(iter(ALLOWED), None)
    if not chat_id:
        return
    async with POS_LOCK:
        alerts, status = await _manage_locked()
        alerts += await _sweep_stray_tokens()
    if alerts:
        await safe_send(ctx.bot, chat_id, "👁 *Management Agent*\n" + "\n".join(alerts))
    elif MGMT_HEARTBEAT and status:
        # heartbeat real-time, tapi skip kalau teksnya identik dengan kiriman
        # terakhir — tidak ada info baru = tidak usah bunyi
        txt = "\n".join(status)
        if txt != getattr(manage_cycle, "_last_hb", None):
            manage_cycle._last_hb = txt
            await safe_send(ctx.bot, chat_id, "👁 *Posisi (update berkala)*\n" + txt)


def _recent_lessons(n: int = 5) -> str:
    """Ringkasan n penutupan terakhir — ala lessons.js Meridian: AI belajar
    dari histori sendiri supaya tidak mengulang kesalahan."""
    try:
        hist = json.load(open(DECISION_LOG, encoding="utf-8"))
    except Exception:
        return ""
    out = []
    for e in reversed(hist):
        if e.get("action") in ("auto_close", "close") and len(out) < n:
            out.append(f"- {e.get('name') or e.get('ca', '?')}: ditutup "
                       f"{e.get('ts', '?')} — {e.get('why') or 'manual'}")
    return "\n".join(out)


def _recent_closed_cas() -> set[str]:
    """CA terlarang: blacklist permanen + yang ditutup < TOKEN_COOLDOWN_H jam."""
    import time
    out = set()
    try:  # ala Meridian token-blacklist: sekali masuk, tidak pernah dipilih lagi
        out |= {str(x).lower() for x in json.load(open(BLACKLIST_FILE))}
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("baca blacklist gagal")
    if TOKEN_COOLDOWN_H <= 0:
        return out
    try:
        cutoff = time.time() - TOKEN_COOLDOWN_H * 3600
        for e in json.load(open(DECISION_LOG, encoding="utf-8"))[-100:]:
            if e.get("action") in ("auto_close", "close") and e.get("ca"):
                try:
                    ts = time.mktime(time.strptime(e["ts"], "%Y-%m-%d %H:%M:%S"))
                except (KeyError, ValueError):
                    continue
                if ts >= cutoff:
                    out.add(e["ca"].lower())
    except Exception:
        log.exception("baca cooldown gagal")
    return out


def _wallet_token_bal(ca: str) -> tuple[int, float]:
    """Saldo token di wallet (raw, qty desimal)."""
    from web3 import Web3
    from lp import WETH_ABI, _cfg, _w3
    w3 = _w3()
    acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
    erc = w3.eth.contract(Web3.to_checksum_address(ca), abi=WETH_ABI)
    bal = erc.functions.balanceOf(acct.address).call()
    if not bal:
        return 0, 0.0
    return bal, bal / 10 ** erc.functions.decimals().call()


async def _sweep_stray_tokens() -> list[str]:
    """Self-healing aturan 'dana wajib berakhir USDG': jual sisa token posisi
    lama yang nyangkut di wallet (mis. bot mati di antara withdraw dan swap)."""
    try:
        hist = json.load(open(DECISION_LOG, encoding="utf-8"))
    except Exception:
        return []
    skip = {os.environ.get("USDG_ADDRESS", "").lower(),
            os.environ.get("WETH_ADDRESS", "").lower()}
    skip |= {p["ca"].lower() for p in load_positions()}
    cas = []
    for e in hist[-50:]:
        ca = (e.get("ca") or "").lower()
        if (e.get("action") in ("auto_close", "close", "withdraw_above_range")
                and re.fullmatch(r"0x[a-f0-9]{40}", ca)
                and ca not in skip and ca not in cas):
            cas.append(ca)
    alerts = []
    for ca in cas[:5]:  # maks 5 token per siklus, cukup
        try:
            raw, qty = await asyncio.to_thread(_wallet_token_bal, ca)
            if not raw:
                continue
            usd = qty * (await token_price(ca) or 0)
            if usd < 0.5:  # debu — tidak sepadan gas
                continue
            from lp4 import swap_all_to_quote
            r = await asyncio.to_thread(swap_all_to_quote, ca)
            log_decision({"actor": "sweeper", "action": "sweep_sell",
                          "ca": ca, "usd": round(usd, 2)})
            alerts.append(f"🧹 Sisa token lama (~${usd:.2f}) terjual balik ke "
                          f"{QUOTE} (rute {r.get('rute', '-')})")
        except Exception:
            log.exception("sweep %s gagal", ca)
    return alerts


def _v4_real(p: dict) -> dict:
    """Snapshot on-chain 1x RPC: harga pool (USD/token), nilai isi posisi (USD),
    fee unclaimed (USD), zona dari tick. Asumsi desimal token=18 USDG=6
    (sama dengan cmd_positions). Untuk keputusan SL/TP — bukan estimasi kurva."""
    from lp import _quote_cfg
    from lp4 import v4_position_state
    st = v4_position_state(p)
    quote_addr, _, _ = _quote_cfg()
    q_is_c0 = p["c0"].lower() == quote_addr.lower()
    raw = (st["sqrt_p"] / 2 ** 96) ** 2          # c1_raw per c0_raw
    price = 1e12 / raw if q_is_c0 else raw * 1e12   # USD per token
    tok_amt = (st["amt1"] if q_is_c0 else st["amt0"]) / 1e18
    q_amt = (st["amt0"] if q_is_c0 else st["amt1"]) / 1e6
    tok_fee = (st["fee1"] if q_is_c0 else st["fee0"]) / 1e18
    q_fee = (st["fee0"] if q_is_c0 else st["fee1"]) / 1e6
    if st["tick"] < p["tick_lower"]:
        zone = "above" if q_is_c0 else "below"
    elif st["tick"] >= p["tick_upper"]:
        zone = "below" if q_is_c0 else "above"
    else:
        zone = "in"
    return {"price": price, "value": tok_amt * price + q_amt,
            "fee_usd": tok_fee * price + q_fee, "zone": zone}


def _v4_fee_usd(p: dict, price_now) -> float | None:
    """Nilai USD fee belum dipungut posisi v4 (None kalau tak bisa dihitung).
    Sisi quote (USDG) dihitung 1:1 USD, sisi token pakai harga sekarang."""
    # ponytail: v4 saja — bot selalu pilih pool v4 (terdalam); tambah jalur v3
    # via NPM.collect static call kalau suatu saat ada posisi v3 live
    from web3 import Web3
    from lp import WETH_ABI, _quote_cfg, _w3
    from lp4 import v4_pending_fees
    f0, f1 = v4_pending_fees(p)
    if f0 == 0 and f1 == 0:
        return 0.0
    w3 = _w3()
    quote_addr, _, _ = _quote_cfg()
    total = 0.0
    for cur, amt in ((p["c0"], f0), (p["c1"], f1)):
        if amt == 0:
            continue
        dec = w3.eth.contract(Web3.to_checksum_address(cur),
                              abi=WETH_ABI).functions.decimals().call()
        if cur.lower() == quote_addr.lower():
            total += amt / 10 ** dec
        elif price_now:
            total += amt / 10 ** dec * price_now
        else:
            return None  # ada fee sisi token tapi harga tak diketahui
    return total


async def _manage_locked():
    pos = load_positions()
    if not pos:
        return [], []
    alerts, status, changed = [], [], False
    for i, p in enumerate(pos, 1):
        try:
            head = f"#{i} {p['name']}"
            real, fee_usd, now = None, None, 0.0
            if (p["mode"] == "live" and p.get("versi") == "v4"
                    and p.get("token_id") is not None):
                try:
                    real = await asyncio.to_thread(_v4_real, p)
                    now, fee_usd = real["price"], real["fee_usd"]
                except Exception:
                    log.exception("snapshot on-chain %s gagal, fallback GT/estimasi",
                                  p.get("name"))
            if not now:
                now = await token_price(p["ca"])
            cap_r = p.get("eth") or 0
            if real and cap_r > 0:
                # PnL REAL: nilai isi posisi on-chain vs modal masuk
                pnl = (real["value"] / cap_r - 1) * 100
                key = real["zone"]
                zone = ZONE_TEXT.get(key, key)
            else:
                pnl, zone, key = await pos_zone(p, now)
                if fee_usd is None and (p["mode"] == "live"
                                        and p.get("versi") == "v4"
                                        and p.get("token_id") is not None):
                    try:
                        fee_usd = await asyncio.to_thread(_v4_fee_usd, p, now)
                    except Exception:
                        log.exception("cek fee posisi %s gagal", p.get("name"))
            chg = (f"{(now / p['entry_price'] - 1) * 100:+.1f}%"
                   if p.get("entry_price") and now else "?")
            cap = p.get("eth") or 0  # modal posisi (unit quote = USDG)
            fee_s = ""
            if fee_usd is not None and cap > 0:
                fee_s = f" · fee ${fee_usd:.2f} ({fee_usd / cap * 100:.0f}% modal"
                try:  # laju fee (ala FlipZ3ro earnings/hour): sinyal vol mengering
                    import time as _t
                    age_h = (_t.time() - _t.mktime(
                        _t.strptime(p["ts"], "%Y-%m-%d %H:%M:%S"))) / 3600
                    if age_h >= 1:
                        fee_s += f", ${fee_usd / age_h:.2f}/jam"
                except Exception:
                    pass
                fee_s += ")"
            status.append(f"*{head}* — {zone}\n"
                          f"      ${now:.6g} ({chg} dr entry)"
                          + (f" · PnL {pnl:+.1f}%" if pnl is not None else "")
                          + fee_s)
            if (FEE_TP_PCT > 0 and fee_usd and cap > 0 and "_close" not in p
                    and fee_usd >= cap * FEE_TP_PCT / 100):
                p["_close"] = (f"take-profit: fee terkumpul ${fee_usd:.2f} = "
                               f"{fee_usd / cap * 100:.0f}% dari modal "
                               f"(target {FEE_TP_PCT:.0f}%)")
            if key != p.get("last_zone"):
                # anti flip-flop: harga menari di tepi range bikin alert spam.
                # Zona baru harus bertahan 2 siklus berturut baru diumumkan;
                # heartbeat & logika auto-close tetap pakai zona real-time.
                if p.get("_zone_cand") == key or not p.get("last_zone"):
                    if p.get("last_zone"):
                        alerts.append(f"*{head}*\n{zone}"
                                      + (f" · PnL {pnl:+.1f}%"
                                         if pnl is not None else ""))
                    p["last_zone"] = key
                    p.pop("_zone_cand", None)
                else:
                    p["_zone_cand"] = key
                changed = True
            elif p.pop("_zone_cand", None) is not None:
                changed = True
            # ala Meridian outOfRangeWaitMinutes: nganggur di atas range terlalu
            # lama = fee mati -> jual, kasih slot ke koin lain
            import time as _time
            if key == "above":
                if "above_since" not in p:
                    p["above_since"] = _time.time()
                    changed = True
            elif p.pop("above_since", None) is not None:
                changed = True
            if (OUT_RANGE_CLOSE_MIN > 0 and "_close" not in p
                    and p.get("above_since")
                    and _time.time() - p["above_since"] >= OUT_RANGE_CLOSE_MIN * 60):
                p["_close"] = (f"di atas range {OUT_RANGE_CLOSE_MIN:.0f}+ menit "
                               f"tanpa fee — jual, cari koin baru")
            if (REBAL_PCT > 0 and key == "above" and now and p.get("entry_price")
                    and now >= p["entry_price"] * (1 + REBAL_PCT / 100)):
                p["_reb"] = now
            # stop-loss dari PnL BERSIH: rugi harga dikurangi fee yang sudah
            # terkumpul (ala Yunus — fee adalah untungnya)
            net = pnl
            if pnl is not None and fee_usd and cap > 0:
                net = pnl + fee_usd / cap * 100
            if net is not None and net <= STOP_LOSS_PCT and "_close" not in p:
                p["_close"] = (f"net PnL {net:+.1f}% (harga {pnl:+.1f}%"
                               + (f" + fee {fee_usd / cap * 100:.1f}%"
                                  if fee_usd and cap > 0 else "")
                               + f") tembus stop loss {STOP_LOSS_PCT:.0f}%")
            # vol pool mati = fee mati; ambil vol 24h pool terbesar token ini
            data = await gt_get(f"/networks/{NETWORK}/tokens/{p['ca']}/pools?page=1")
            vols = [float((q["attributes"].get("volume_usd") or {}).get("h24") or 0)
                    for q in data.get("data", [])]
            vol = max(vols, default=0)
            if vol < MIN_POOL_VOL_ALERT and "_close" not in p:
                p["_close"] = (f"vol 24h pool tinggal ${vol:,.0f} "
                               f"(< ${MIN_POOL_VOL_ALERT:,.0f}) — fee mengering")
        except Exception:
            log.exception("manage_cycle: posisi %s gagal dicek", p.get("name"))
    cls = [(x, x.pop("_close")) for x in list(pos) if "_close" in x]
    for x, _ in cls:
        x.pop("_reb", None)
    reb = [(x, x.pop("_reb")) for x in list(pos) if "_reb" in x]
    if changed or cls or reb:
        save_positions(pos)
    for p, why in cls:
        try:
            alerts.append(await _auto_close(pos, p, why))
        except Exception as e:
            log.exception("auto-close gagal")
            alerts.append(f"⚠️ Auto-close {p['name']} GAGAL: {e} — tutup manual!")
    for p, now in reb:
        try:
            alerts.append(await _rebalance(pos, p, now))
        except Exception as e:
            log.exception("rebalance gagal")
            alerts.append(f"⚠️ Rebalance {p['name']} gagal: {e}")
    if cls:  # ala Meridian: evaluasi threshold tiap 5 posisi tertutup
        try:
            n = sum(1 for e in json.load(open(DECISION_LOG, encoding="utf-8"))
                    if e.get("action") in ("auto_close", "close"))
            if n and n % 5 == 0:
                alerts.append(f"📚 Sudah {n} posisi tertutup — ketik /evolve: "
                              "AI evaluasi histori & saran perbaikan threshold.")
        except Exception:
            log.exception("hitung posisi tertutup gagal")
    return alerts, status


async def _auto_close(pos, p, why):
    """Tutup posisi otomatis: tarik likuiditas, jual token balik ke quote, compound."""
    from lp import wallet_balance_quote, withdraw_position
    txt = f"🛑 *Auto-close {p['name']}*: {why}\n"
    if p["mode"] == "live":
        await asyncio.to_thread(withdraw_position, p)
        try:
            from lp4 import swap_all_to_quote
            r = await asyncio.to_thread(swap_all_to_quote, p["ca"])
            txt += f"Token dijual balik ke {QUOTE} (rute {r.get('rute', '-')}).\n"
        except Exception as e:
            log.exception("swap gagal")
            txt += f"⚠️ Jual token GAGAL ({e}) — token masih di wallet, jual manual!\n"
        bal = await asyncio.to_thread(wallet_balance_quote)
        txt += f"Saldo: {bal:.4f} {QUOTE}."
    pos.remove(p)
    save_positions(pos)
    log_decision({"actor": "auto", "action": "auto_close", "why": why, **p})
    return txt


async def _rebalance(pos, p, now):
    """Harga naik jauh di atas range: posisi 100% quote, fee mati -> tarik
    likuiditas (quote utuh, cuma gas) lalu deploy ulang dekat harga sekarang."""
    if p["mode"] == "live":
        from lp import withdraw_position
        await asyncio.to_thread(withdraw_position, p)
    pos.remove(p)
    save_positions(pos)
    log_decision({"actor": "rebalance", "action": "withdraw_above_range", **p,
                  "exit_price": now})
    eth_size = p["eth"]
    if p["mode"] == "live" and AUTO_SIZE_PCT > 0:
        from lp import wallet_balance_quote
        bal = await asyncio.to_thread(wallet_balance_quote)
        if TARGET_USD > 0 and QUOTE == "USDG" and bal >= TARGET_USD:
            open(TARGET_FLAG, "w").write(f"{bal}")
            return (f"🎯 *TARGET ${TARGET_USD:.0f} TERCAPAI!* Saldo {bal:.2f} {QUOTE}. "
                    f"Posisi ditutup, auto-deploy berhenti. "
                    f"Hapus file TARGET_REACHED di VPS untuk lanjut.")
        eth_size = round(bal * AUTO_SIZE_PCT / 100, 6)
    msg = await _do_deploy_locked(
        p["ca"], eth_size, p["range_pct"], "rebalance", p["name"], now,
        f"auto-rebalance: harga +{REBAL_PCT:.0f}% di atas range, range digeser naik")
    return f"🔁 *Auto-rebalance {p['name']}* -> range baru dekat ${now:.6g}\n{msg}"


async def cmd_evolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ala Meridian `evolve`: AI pelajari histori posisi tertutup, sarankan threshold baru."""
    if not allowed(update):
        return
    hist = []
    if os.path.exists(DECISION_LOG):
        hist = [h for h in json.load(open(DECISION_LOG, encoding="utf-8"))
                if h.get("action") == "close"]
    if len(hist) < 5:
        await update.message.reply_text(
            f"Baru {len(hist)} posisi tertutup — butuh ≥5 biar polanya kebaca. "
            "Lanjut dulu, nanti coba /evolve lagi.")
        return
    msg = await update.message.reply_text("🧬 Menganalisis histori posisi…")
    try:
        prompt_extra = (
            "TUGAS KHUSUS EVOLVE: di bawah histori posisi LP yang sudah ditutup "
            "(entry, exit, est PnL, range). Analisis pola menang/kalah, lalu sarankan "
            f"penyesuaian konkret untuk threshold saat ini: MIN_MCAP=${MIN_MCAP:,.0f}, "
            f"MIN_VOL_24H=${MIN_VOL_24H:,.0f}, MIN_LIQ=${MIN_LIQ:,.0f}, "
            f"STOP_LOSS={STOP_LOSS_PCT:.0f}%, range default. Jawab: nilai baru + alasan.")
        analysis = await ai_analyze(hist[-20:], "evolve threshold", prompt_extra)
        await safe_edit(msg, analysis[:4000])
    except Exception as e:
        log.exception("evolve gagal")
        await msg.edit_text(f"❌ Gagal: {e}")


# terima variasi tulisan AI: "DEPLOY: 0x... 20", "-20%", "− 20 %"
AUTO_PICK_RE = re.compile(r"DEPLOY:\s*(0x[a-fA-F0-9]{40})[\s,]*[-–−]?\s*(\d+(?:\.\d+)?)\s*%?")


async def auto_cycle(ctx: ContextTypes.DEFAULT_TYPE):
    """Siklus otomatis ala Meridian: screening -> analisis AI -> deploy -> lapor."""
    chat_id = next(iter(ALLOWED), None)
    if not chat_id:
        return
    if os.path.exists(TARGET_FLAG):
        return
    if TARGET_USD > 0 and QUOTE == "USDG" and not load_positions():
        try:
            from lp import wallet_balance_quote
            bal = await asyncio.to_thread(wallet_balance_quote)
            if bal >= TARGET_USD:
                open(TARGET_FLAG, "w").write(f"{bal}")
                await safe_send(ctx.bot, chat_id,
                    f"🎯 *TARGET ${TARGET_USD:.0f} TERCAPAI!* Saldo {bal:.2f} {QUOTE}. "
                    "Auto-deploy berhenti. Hapus file TARGET_REACHED di VPS untuk lanjut.")
                return
        except Exception:
            log.exception("cek target gagal")
    if AUTO_PAUSE_OPEN and load_positions():
        # masih ada posisi terbuka -> skip screening & deploy sampai di-/close.
        # management agent tetap mengawasi tiap MGMT_INTERVAL_MIN.
        log.info("auto_cycle pause: masih ada posisi terbuka")
        return
    try:
        # ── Fase 1: screening (lapor real-time, sebelum nunggu AI) ──
        hits, note = await fetch_candidates()
        lolos = [r for r in hits if r["lolos_filter"]]
        await safe_send(ctx.bot, chat_id,
            "🔎 *Fase 1 — Screening*\n"
            + (f"{len(lolos)} kandidat lolos filter 24h:\n" + "\n".join(
                f"• *{r['name']}*\n"
                f"   mcap {_usd(r['mcap_usd'])} · vol {_usd(r['vol_24h'])} · "
                f"liq {_usd(r['liq_usd'])}"
                + (f" · fee24h {_usd(r['fees_24h_usd'])}"
                   if r.get("fees_24h_usd") else "")
                + (f" · APR ~{min(r['est_fee_apr_pct'], 9999):,.0f}%"
                   if r.get("est_fee_apr_pct") else "")
                for r in lolos[:8])
               if lolos else "Tidak ada yang lolos filter keras.") + note
            + "\n\n_Fase 2 (analisis AI) berjalan… ±1-3 menit._")
        # ── Fase 2: analisis AI (wajib jelaskan alasan keputusan) ──
        pos = load_positions()
        slot = MAX_POSITIONS - len(pos)
        held = {p["ca"].lower() for p in pos}
        cooldown = _recent_closed_cas()
        extra = (f"POSISI TERBUKA: {len(pos)}/{MAX_POSITIONS} "
                 f"({', '.join(p['name'] for p in pos) or 'tidak ada'}).\n")
        if cooldown:
            extra += ("JANGAN pilih CA berikut (baru ditutup < "
                      f"{TOKEN_COOLDOWN_H:.0f} jam, cooldown — cari koin LAIN): "
                      + ", ".join(sorted(cooldown)) + "\n")
        les = _recent_lessons()
        if les:
            extra += ("PELAJARAN posisi sebelumnya (hindari mengulang pola "
                      "yang berakhir stop-loss/nganggur):\n" + les + "\n")
        if slot > 0:
            extra += (
                "Di akhir jawabanmu WAJIB ada bagian:\n"
                "ALASAN KEPUTUSAN: 2-4 kalimat kenapa kandidat terpilih layak dieksekusi "
                "SEKARANG (ekspektasi fee vs IL, volume, thesis util/komun) — atau kenapa "
                "kamu memilih TIDAK deploy siklus ini.\n"
                "Lalu SATU baris persis: DEPLOY: <ca_lengkap> <range_pct> "
                "(bukan token yang sudah dipegang), atau DEPLOY: NONE. Konservatif > FOMO.\n"
                f"TIMING (anti beli pucuk): JANGAN deploy token yang chg_5m > +{MAX_CHG_5M:g}% "
                f"atau chg_1h > +{MAX_CHG_1H:g}% — lagi pump aktif, posisi bakal langsung "
                "kabur ke atas range; tunggu koreksi. Ideal: chg_1h merah tipis, chg_24h hijau.\n"
                "RANGE: sekitar 1.5x |chg_24h| token, MAKSIMAL 40% (range sempit = fee "
                "lebih padat; kalau token butuh range > 40%, dia terlalu volatil -> pilih "
                "kandidat lain). Posisi dipasang DUA SISI mengapit harga (±range/2): "
                "fee jalan sejak awal, tapi dana ikut harga token — hindari token yang "
                "rawan dump dalam; prioritas volume tinggi + harga sideways.")
        else:
            extra += "Slot penuh — akhiri dengan DEPLOY: NONE."
        analysis = await ai_analyze(hits[:12], "auto-screening berkala", extra)
        rm = re.search(r"ALASAN KEPUTUSAN:\s*(.+?)(?=\n\s*DEPLOY:|\Z)", analysis, re.S)
        reason = rm.group(1).strip() if rm else ""
        m = AUTO_PICK_RE.search(analysis)
        deploy_msg = ""
        if m and slot > 0:
            ca, pct = m.group(1), float(m.group(2))
            if ca.lower() in held:
                deploy_msg = "\n\n🤖 AI pilih token yang sudah dipegang — skip."
            elif ca.lower() in cooldown:
                deploy_msg = (f"\n\n🤖 AI pilih token yang baru ditutup "
                              f"(cooldown {TOKEN_COOLDOWN_H:.0f} jam) — skip, "
                              "siklus depan cari koin lain.")
            elif ca.lower() not in {x["ca"].lower() for x in hits}:
                # AI menyebut CA di luar daftar kandidat (halusinasi) -> tolak
                deploy_msg = f"\n\n🤖 AI pilih CA di luar kandidat screening — skip.\n`{ca}`"
                log_decision({"actor": "auto", "action": "skip_hallucinated_ca", "ca": ca})
            else:
                # sizing: % saldo wallet (compound otomatis); fallback AUTO_ETH tetap
                eth_size, size_note = AUTO_ETH, ""
                if AUTO_SIZE_PCT > 0:
                    try:
                        from lp import wallet_balance_quote
                        bal = await asyncio.to_thread(wallet_balance_quote)
                        reserve = GAS_RESERVE_ETH if QUOTE == "WETH" else 0
                        bagi = 1 if AUTO_PAUSE_OPEN else max(slot, 1)
                        eth_size = round(max(bal - reserve, 0)
                                         * AUTO_SIZE_PCT / 100 / bagi, 6)
                        size_note = (f" ({AUTO_SIZE_PCT:.0f}% dari saldo {bal:.4f} {QUOTE}"
                                     + (f", dibagi {bagi} slot" if bagi > 1 else "") + ")")
                    except Exception:
                        size_note = " (saldo tak terbaca, pakai AUTO_ETH tetap)"
                r = next((x for x in hits if x["ca"].lower() == ca.lower()), None)
                chg5 = float((r or {}).get("chg_5m") or 0)
                chg1h = float((r or {}).get("chg_1h") or 0)
                if eth_size <= 0:
                    deploy_msg = "\n\n🤖 Saldo tidak cukup untuk deploy — skip."
                elif chg5 > MAX_CHG_5M or chg1h > MAX_CHG_1H:
                    # anti beli pucuk: pump aktif = posisi langsung kabur ke atas
                    # range; tunggu koreksi, coba lagi siklus depan
                    deploy_msg = (f"\n\n🤖 {(r or {}).get('name', ca)}: pump aktif "
                                  f"(5m {chg5:+.1f}%, 1h {chg1h:+.1f}%) — tunda, "
                                  "tunggu koreksi (anti beli pucuk).")
                    log_decision({"actor": "auto", "action": "skip_pump",
                                  "ca": ca, "chg_5m": chg5, "chg_1h": chg1h})
                else:
                    # range 1.5x volatilitas 24h, cap 40 (fee density > lebar)
                    vol24 = abs(float((r or {}).get("chg_24h") or 0))
                    pct = min(40.0, max(pct, round(1.5 * vol24)))
                    deploy_msg = f"\n\n🤖 *Auto-deploy*{size_note}:\n" + await do_deploy(
                        ca, eth_size, pct, actor="auto",
                        name=r["name"] if r else "",
                        entry_price=float(r["price_usd"] or 0) if r else 0,
                        reason=reason)
        elif slot > 0:
            deploy_msg = "🤖 AI memilih TIDAK deploy siklus ini (lihat alasan di analisis)."
        await safe_send(ctx.bot, chat_id,
                        f"🧠 *Fase 2 — Analisis*\n{analysis}\n\n*CA & Links:*\n"
                        f"{ca_lines(hits, 3)}")
        # ── Fase 3: deploy ──
        if deploy_msg:
            await safe_send(ctx.bot, chat_id, f"🚀 *Fase 3 — Deploy*\n{deploy_msg.strip()}")
    except Exception as e:
        log.exception("auto_cycle gagal")
        try:
            await ctx.bot.send_message(chat_id, f"❌ Auto-cycle gagal: {e}")
        except Exception:
            pass


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *LP Robin*\n"
        "/screen — screening trending pools + skor AI\n"
        "/analyze <CA> — analisis mendalam 1 token\n"
        "/lp <CA> <eth> <pct> — deploy LP one-sided lower\n"
        "/positions — posisi terbuka + est PnL\n"
        "/close <no> — tarik likuiditas + jual ke USDG\n"
        "/pnl — untung/rugi total vs modal (/pnl set <usd> catat modal)\n"
        "/evolve — AI evaluasi histori & saran threshold (≥5 posisi tertutup)\n"
        f"\nNetwork: `{NETWORK}` | Filter 24h (metode Yunus): mcap ≥ ${MIN_MCAP:,.0f}, "
        f"vol ≥ ${MIN_VOL_24H:,.0f}, liq ≥ ${MIN_LIQ:,.0f}, "
        f"fees ≥ {MIN_FEES_ETH:g} ETH | Maks {MAX_POSITIONS} posisi\n"
        + (f"⏰ Auto-cycle tiap {AUTO_MIN:.0f} menit (quote {QUOTE}, "
           f"{'DRY RUN' if DRY_RUN else '🔴 LIVE'})"
           + ("; pause selama ada posisi terbuka" if AUTO_PAUSE_OPEN else "")
           if AUTO_MIN > 0 else "Auto-cycle: mati")
        + (f"\n👁 Management agent tiap {MGMT_MIN:.0f} menit (stop loss {STOP_LOSS_PCT:.0f}%)"
           if MGMT_MIN > 0 else ""),
        parse_mode=ParseMode.MARKDOWN)



async def _audit_text() -> str:
    """Ala FlipZ3ro ledger-rebuild: cek positions.json sinkron dengan on-chain,
    dan cari posisi 'yatim' (deploy live di decision-log tanpa tracking/close)."""
    from lp4 import v4_position_state
    pos = load_positions()
    lines = []
    for p in pos:
        if (p.get("mode") == "live" and p.get("versi") == "v4"
                and p.get("token_id") is not None):
            try:
                st = await asyncio.to_thread(v4_position_state, p)
                if st["liq"] == 0:
                    lines.append(f"⚠️ {p['name']} tercatat TERBUKA tapi liquidity "
                                 "on-chain 0 — sudah ditarik di luar bot? "
                                 "/close untuk bersihkan tracking.")
            except Exception as e:
                lines.append(f"⚠️ {p['name']} gagal dicek on-chain: {str(e)[:80]}")
    hist = []
    if os.path.exists(DECISION_LOG):
        try:
            hist = json.load(open(DECISION_LOG, encoding="utf-8"))
        except json.JSONDecodeError:
            lines.append("⚠️ decision-log.json tidak terbaca.")
    tracked = {p.get("token_id") for p in pos}
    closed = {h.get("token_id") for h in hist
              if h.get("action") in ("close", "auto_close", "withdraw_above_range")}
    for h in hist:
        tid = h.get("token_id")
        if (h.get("action") != "deploy_live" or tid is None
                or tid in tracked or tid in closed):
            continue
        try:  # masih ada isinya di chain? kalau 0 berarti sudah beres
            if h.get("versi") == "v4" and h.get("pool") and h.get("c0"):
                if (await asyncio.to_thread(v4_position_state, h))["liq"] == 0:
                    continue
        except Exception:
            pass
        lines.append(f"🚨 ORPHAN token_id {tid} ({h.get('name') or h.get('ca', '')[:10]}): "
                     "deploy live tapi tidak ada di tracking — cek app.uniswap.org.")
    return "\n".join(lines) if lines else \
        "✅ Audit: tracking sinkron dengan on-chain & decision-log."


async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("🔍 Audit tracking vs on-chain…")
    try:
        await safe_edit(msg, "🔍 *Audit posisi*\n" + await _audit_text())
    except Exception as e:
        log.exception("audit gagal")
        await msg.edit_text(f"❌ Audit gagal: {e}")


async def audit_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Sekali tiap start bot: lapor HANYA kalau ada yang tidak sinkron."""
    chat_id = next(iter(ALLOWED), None)
    if not chat_id:
        return
    try:
        txt = await _audit_text()
        if not txt.startswith("✅"):
            await safe_send(ctx.bot, chat_id, "🔍 *Audit posisi (startup)*\n" + txt)
    except Exception:
        log.exception("audit startup gagal")


def acquire_instance_lock():
    """Ala FlipZ3ro: cegah 2 proses bot (nonce clash + double deploy + polling conflict)."""
    import fcntl
    f = open(os.path.join(os.path.dirname(__file__), ".bot.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("Bot sudah jalan di proses lain (.bot.lock terkunci). Keluar.")
    return f  # simpan referensi biar lock tidak dilepas GC


def main():
    _lock = acquire_instance_lock()
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("lp", cmd_lp))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("evolve", cmd_evolve))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.job_queue.run_once(audit_job, 30)
    if AUTO_MIN > 0:
        app.job_queue.run_repeating(auto_cycle, interval=AUTO_MIN * 60, first=15)
        log.info("Auto-cycle aktif tiap %.0f menit", AUTO_MIN)
    if MGMT_MIN > 0:
        app.job_queue.run_repeating(manage_cycle, interval=MGMT_MIN * 60, first=60)
        log.info("Management agent aktif tiap %.0f menit", MGMT_MIN)
    log.info("Bot jalan…")
    app.run_polling()


if __name__ == "__main__":
    main()
