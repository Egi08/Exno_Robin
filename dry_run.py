"""Dry run satu siklus: screening trending pools Robinhood Chain -> analisis AI -> rencana alokasi modal.
Stdlib only (urllib) biar bisa jalan tanpa install dependency bot.
Pakai: python dry_run.py  (baca AI_API_KEY dkk dari .env)
"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")  # console Windows default cp1252

# load .env manual (tanpa python-dotenv)
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    for line in open(env_path, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

NETWORK = os.environ.get("NETWORK_SLUG", "robinhood")
CAPITAL = float(os.environ.get("DRY_RUN_CAPITAL_USD", 50))
MIN_VOL_5M = float(os.environ.get("MIN_VOL_5M_USD", 100_000))
MIN_LIQ = float(os.environ.get("MIN_LIQUIDITY_USD", 50_000))
# filter ala @0xyunss (tf 24 jam)
MIN_VOL_24H = float(os.environ.get("MIN_VOL_24H_USD", 1_000_000))
MIN_MCAP = float(os.environ.get("MIN_MCAP_USD", 500_000))
DEX_BLACKLIST = {d.strip().lower() for d in
                 os.environ.get("DEX_BLACKLIST", "flap,klik").split(",") if d.strip()}


def get_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"accept": "application/json",
                                          "User-Agent": "Mozilla/5.0 (lp-robin-bot)",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def pool_row(p):
    a = p["attributes"]
    tx5 = (a.get("transactions") or {}).get("m5") or {}
    txs = tx5.get("buys", 0) + tx5.get("sells", 0)
    wallets = tx5.get("buyers", 0) + tx5.get("sellers", 0)
    return {
        "name": a.get("name"),
        "ca": p.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "").split("_")[-1],
        "dex": p.get("relationships", {}).get("dex", {}).get("data", {}).get("id", ""),
        "price_usd": a.get("base_token_price_usd"),
        "liq_usd": float(a.get("reserve_in_usd") or 0),
        "mcap_usd": float(a.get("market_cap_usd") or a.get("fdv_usd") or 0),
        "vol_5m": float((a.get("volume_usd") or {}).get("m5") or 0),
        "vol_1h": float((a.get("volume_usd") or {}).get("h1") or 0),
        "vol_24h": float((a.get("volume_usd") or {}).get("h24") or 0),
        "chg_1h": (a.get("price_change_percentage") or {}).get("h1"),
        "chg_24h": (a.get("price_change_percentage") or {}).get("h24"),
        "txs_5m": txs, "wallets_5m": wallets,
        "bundler_suspect": wallets > 0 and txs / wallets > 3,
        "created_at": a.get("pool_created_at"),
    }


def main():
    print(f"=== DRY RUN | network={NETWORK} | modal=${CAPITAL:,.0f} ===\n")
    data = get_json(f"https://api.geckoterminal.com/api/v2/networks/{NETWORK}/trending_pools?page=1")
    rows = [pool_row(p) for p in data.get("data", [])]
    rows = [r for r in rows if not any(b in (r["dex"] or "").lower() for b in DEX_BLACKLIST)]
    # filter keras ala @0xyunss dihitung sebagai anotasi; AI lihat semua kandidat
    for r in rows:
        gagal = []
        if r["mcap_usd"] < MIN_MCAP:
            gagal.append(f"mcap < ${MIN_MCAP:,.0f}")
        if r["vol_24h"] < MIN_VOL_24H:
            gagal.append(f"vol24h < ${MIN_VOL_24H:,.0f}")
        if r["liq_usd"] < MIN_LIQ:
            gagal.append(f"liq < ${MIN_LIQ:,.0f}")
        r["lolos_filter"] = not gagal
        r["alasan_gagal"] = gagal
    hits = sorted(rows, key=lambda r: (not r["lolos_filter"], -r["vol_24h"]))

    for r in hits[:10]:
        flag = (" ✅" if r["lolos_filter"] else " ❌" + ",".join(r["alasan_gagal"]))
        flag += " ⚠️bundler?" if r["bundler_suspect"] else ""
        print(f"  {r['name']:<24} mcap ${r['mcap_usd']:>12,.0f}  liq ${r['liq_usd']:>11,.0f}  "
              f"vol24h ${r['vol_24h']:>11,.0f}  tx5m {r['txs_5m']:>3}/{r['wallets_5m']:>3} wallet{flag}")

    prompt = (
        f"Kamu agent LP DeFi. Modal saya HANYA ${CAPITAL:.0f} (dry run, ETH di Robinhood Chain, "
        "LP Uniswap v3 one-sided lower). Data pool trending (JSON) di bawah.\n"
        "Checklist ala @0xyunss: tiap kandidat punya `lolos_filter` + `alasan_gagal` "
        "dari filter keras (mcap > $500k, vol24h > $1m, liq cukup). Prioritaskan yang lolos; "
        "yang gagal tipis boleh diselamatkan dengan alasan kuat, yang lolos tapi mencurigakan coret. "
        "Prioritaskan token utilitas (meme lagi lemah), tolak token launchpad murahan, "
        "nilai kejelasan komunitas & thesis fomo, gas kalau ekspektasi fee > IL.\n"
        "Buat RENCANA ALOKASI konkret:\n"
        "- pilih max 2 pool terbaik (skor 0-100 + alasan singkat)\n"
        "- bagi $50 (boleh sisakan cash), range -10/-20/-30% per posisi\n"
        "- sebut risiko utama & kapan harus keluar\n"
        "Jawab ringkas bahasa Indonesia. Langsung ke jawaban, jangan bertele-tele.\n\n"
        + json.dumps(hits[:12], ensure_ascii=False)
    )
    resp = get_json(
        os.environ.get("AI_BASE_URL", "https://api.siliconflow.com/v1/chat/completions"),
        data={"model": os.environ.get("AI_MODEL", "tencent/Hy3"), "max_tokens": 12000,
              "messages": [{"role": "user", "content": prompt}]},
        headers={"Authorization": f"Bearer {os.environ['AI_API_KEY']}",
                 "Content-Type": "application/json"})
    m = resp["choices"][0]["message"]
    print("\n=== RENCANA AI ===\n")
    print(m.get("content") or m.get("reasoning_content", ""))


if __name__ == "__main__":
    main()
