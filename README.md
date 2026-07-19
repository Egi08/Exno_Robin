# Exno_Robin

Bot LP (liquidity provider) otonom untuk **Robinhood Chain** — screening token, analisis AI, deploy posisi Uniswap v3/v4, dan manajemen posisi, semuanya dikendalikan lewat Telegram. Ditulis Python, jalan 24 jam di VPS.

## Cara kerja

Tiap siklus (default 5 menit) bot menjalankan tiga fase:

1. **Screening** — tarik kandidat dari GeckoTerminal (trending + top volume), filter keras metode Yunus (@0xyunss): mcap > $500k, vol 24h > $1M, fee riil 24h, likuiditas cukup, blacklist launchpad scam. Diperkaya data **GoPlus** (honeypot, tax, holders, top-10) dan **GMGN** (smart money, KOL, rug ratio, bundler rate, wash trading) — token berbahaya dicoret otomatis.
2. **Analisis AI** — kandidat dikirim ke LLM (endpoint utama + cadangan) yang memberi skor, risiko, rekomendasi range, dan keputusan akhir satu baris `DEPLOY: <ca> <range>` atau `DEPLOY: NONE`. Ekspektasi fee dihitung dari pool USDG tempat posisi benar-benar dibuka, bukan pool WETH terbesar.
3. **Deploy** — posisi Uniswap **v4** dua sisi (in-range langsung, beli token via Universal Router dengan fallback rute 2-hop lewat ETH native) atau one-sided lower v3/v4 sebagai fallback. Sizing = % saldo (compound otomatis).

**Management agent** mengawasi posisi tiap 5 menit dengan data on-chain real (tick pool, isi posisi, fee unclaimed — bukan estimasi): stop-loss dari net PnL (harga + fee), take-profit fee, auto-close saat lama di atas range atau volume pool mengering, sweeper token nyangkut, dan heartbeat Telegram yang hanya bunyi kalau ada info baru.

## Perintah Telegram

| Perintah | Fungsi |
|---|---|
| `/screen` | Screening manual + skor AI |
| `/analyze <CA>` | Analisis mendalam 1 token (+data GMGN) |
| `/lp <CA> <jumlah> <pct>` | Deploy LP manual |
| `/positions` | Posisi terbuka: isi on-chain, fee unclaimed, PnL |
| `/close <no>` | Tutup on-chain: tarik likuiditas + jual ke USDG otomatis |
| `/pnl` | PnL portofolio vs modal masuk |
| `/audit` | Cek tracking vs on-chain vs decision-log |
| `/evolve` | AI evaluasi histori posisi & saran tuning threshold |

## Setup

```bash
git clone https://github.com/Egi08/Exno_Robin.git && cd Exno_Robin
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # isi TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, AI_API_KEY, PRIVATE_KEY
venv/bin/python bot.py
```

Mulai dengan `DRY_RUN=true` (simulasi). Set `false` hanya kalau paham risikonya — bot transaksi uang asli tanpa konfirmasi.

**Opsional — enrichment GMGN** (butuh Node 20+):

```bash
npm install -g gmgn-cli
gmgn-cli config            # buka URL yang muncul, buat API key (read-only)
gmgn-cli config --apply <API_KEY>
```

Tanpa gmgn-cli bot tetap jalan penuh, hanya tanpa data smart money/KOL.

**Produksi (systemd):** jalankan sebagai service dengan `Restart=always`; bot punya single-instance lock, tulis file atomik, dan RPC fallback (`RPC_URL_FALLBACK`).

## Keamanan

- Pakai **wallet baru khusus bot** dengan saldo kecil. `PRIVATE_KEY` hanya di `.env` (tidak pernah di-commit — lihat `.gitignore`).
- `ALLOWED_USER_IDS` wajib diisi: hanya chat ID itu yang bisa perintah bot.
- Semua swap disimulasikan via `eth_call` sebelum dikirim; min-out slippage di semua rute jual.

## Disclaimer

Eksperimen pribadi, bukan saran finansial. LP token mikro-cap berisiko tinggi (IL, rug, honeypot). Gunakan dengan uang yang siap hilang.
