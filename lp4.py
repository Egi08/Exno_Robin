"""Uniswap v4 one-sided LP — Robinhood Chain (PoolManager singleton + Permit2).

Alamat resmi dari docs.uniswap.org/contracts/v4/deployments (chain 4663).
Pool v4 tidak punya kontrak sendiri: dicari lewat event Initialize di PoolManager,
lalu mint lewat PositionManager.modifyLiquidities (actions MINT_POSITION + SETTLE_PAIR).
"""
import math

from eth_abi import decode, encode
from eth_utils import keccak
from web3 import Web3

from lp import FACTORY_ABI, FEE_TIERS, POOL_ABI, WETH_ABI, _cfg, _quote_cfg, _w3

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POSM_V4 = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
Q96 = 2 ** 96
INIT_TOPIC = "0x" + keccak(
    text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()

STATE_VIEW_ABI = [
    {"name": "getSlot0", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "bytes32"}],
     "outputs": [{"type": "uint160"}, {"type": "int24"}, {"type": "uint24"}, {"type": "uint24"}]},
    {"name": "getLiquidity", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "bytes32"}], "outputs": [{"type": "uint128"}]},
    {"name": "getPositionInfo", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "bytes32"}, {"type": "address"}, {"type": "int24"},
                {"type": "int24"}, {"type": "bytes32"}],
     "outputs": [{"type": "uint128"}, {"type": "uint256"}, {"type": "uint256"}]},
    {"name": "getFeeGrowthInside", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "bytes32"}, {"type": "int24"}, {"type": "int24"}],
     "outputs": [{"type": "uint256"}, {"type": "uint256"}]},
]
POSM_ABI = [{"name": "modifyLiquidities", "type": "function", "stateMutability": "payable",
             "inputs": [{"type": "bytes"}, {"type": "uint256"}], "outputs": []}]
PERMIT2_ABI = [{"name": "approve", "type": "function", "stateMutability": "nonpayable",
                "inputs": [{"type": "address"}, {"type": "address"},
                           {"type": "uint160"}, {"type": "uint48"}], "outputs": []},
               {"name": "allowance", "type": "function", "stateMutability": "view",
                "inputs": [{"type": "address"}, {"type": "address"},
                           {"type": "address"}],
                "outputs": [{"type": "uint160"}, {"type": "uint48"},
                            {"type": "uint48"}]}]
ALLOWANCE_ABI = [{"name": "allowance", "type": "function", "stateMutability": "view",
                  "inputs": [{"type": "address"}, {"type": "address"}],
                  "outputs": [{"type": "uint256"}]}]
TRANSFER_TOPIC = keccak(text="Transfer(address,address,uint256)")


def _topic_addr(a: str) -> str:
    return "0x" + "0" * 24 + a[2:].lower()


_PAIR_CACHE = {}  # (c0,c1) -> [pool dict]; kunci pool immutable, cache seumur proses
# ponytail: pool v4 pasangan BARU yang muncul setelah cache terisi tidak kelihatan
# sampai bot restart — acceptable, pool baru jarang & restart harian wajar


def _get_logs_chunked(w3, flt: dict, frm: int, to: int) -> list:
    """get_logs; kalau RPC timeout, belah dua range-nya rekursif."""
    try:
        return w3.eth.get_logs({**flt, "fromBlock": frm, "toBlock": to})
    except Exception:
        if to - frm < 200_000:
            raise
        mid = (frm + to) // 2
        return (_get_logs_chunked(w3, flt, frm, mid)
                + _get_logs_chunked(w3, flt, mid + 1, to))


def _find_pools(w3, quote: str, token: str) -> list[dict]:
    c0, c1 = (quote, token) if int(quote, 16) < int(token, 16) else (token, quote)
    key = (c0.lower(), c1.lower())
    if key in _PAIR_CACHE:
        return _PAIR_CACHE[key]
    logs = _get_logs_chunked(w3, {
        "address": Web3.to_checksum_address(POOL_MANAGER),
        "topics": [INIT_TOPIC, None, _topic_addr(c0), _topic_addr(c1)]},
        0, w3.eth.block_number)
    pools = []
    for lg in logs:
        fee, spacing, hooks, _, _ = decode(
            ["uint24", "int24", "address", "uint160", "int24"], bytes(lg["data"]))
        pools.append({"id": bytes(lg["topics"][1]), "c0": c0, "c1": c1,
                      "fee": fee, "spacing": spacing, "hooks": hooks})
    _PAIR_CACHE[key] = pools
    return pools


def best_v4_pool(token: str, for_lp: bool = False) -> dict | None:
    """Pool v4 quote/token. Default (swap): liquidity aktif terbesar.
    for_lp=True (ala Yunus): fee tier TERENDAH di antara pool yang liquidity-nya
    masih layak (>= 25% pool terdalam) — pool murah dapat routing volume lebih
    deras, fee income lebih konsisten."""
    quote_addr, _, _ = _quote_cfg()
    return _best_pool_pair(quote_addr, token, for_lp=for_lp)


def _best_pool_pair(a: str, b: str, for_lp: bool = False) -> dict | None:
    w3 = _w3()
    pools = _find_pools(w3, Web3.to_checksum_address(a),
                        Web3.to_checksum_address(b))
    sv = w3.eth.contract(Web3.to_checksum_address(STATE_VIEW), abi=STATE_VIEW_ABI)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:  # 1 pair bisa punya 60+ pool; serial = ~50 dtk
        liqs = list(ex.map(lambda p: sv.functions.getLiquidity(p["id"]).call(), pools))
    live = [{**p, "liq": liq} for p, liq in zip(pools, liqs) if liq]
    if not live:
        return None
    best = max(live, key=lambda x: x["liq"])
    if for_lp:
        layak = [x for x in live if x["liq"] >= best["liq"] * 0.25]
        best = min(layak, key=lambda x: (x["fee"], -x["liq"]))
    sqrt_p, tick, *_ = sv.functions.getSlot0(best["id"]).call()
    best.update(sqrt_p=sqrt_p, tick=tick)
    return best


def v4_quote_depth(token: str) -> float:
    """Estimasi kedalaman sisi quote pool v4 terbaik dalam rentang ±20% harga
    (unit quote; USDG = USD). 0 = tidak ada pool v4 aktif."""
    p = best_v4_pool(token)
    if not p:
        return 0.0
    quote_addr, _, _ = _quote_cfg()
    quote_is_c0 = p["c0"].lower() == quote_addr.lower()
    L, sp = p["liq"], p["sqrt_p"]
    if sp == 0:
        return 0.0
    if quote_is_c0:
        amt = L * Q96 * (1 / sp - 1 / (sp * math.sqrt(1.2)))
    else:
        amt = L * sp * (1 - 1 / math.sqrt(1.2)) / Q96
    w3 = _w3()
    dec = w3.eth.contract(Web3.to_checksum_address(quote_addr),
                          abi=WETH_ABI).functions.decimals().call()
    return amt / 10 ** dec


def deploy_v4(token: str, amount: float, range_pct: float) -> dict:
    """Mint posisi v4 100% quote, range range_pct% di sisi harga sekarang.
    Simulasi (eth_call) dulu sebelum kirim; raise RuntimeError kalau gagal."""
    w3 = _w3()
    acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
    quote_addr, quote_sym, _ = _quote_cfg()
    quote = Web3.to_checksum_address(quote_addr)
    token = Web3.to_checksum_address(token)
    p = best_v4_pool(token, for_lp=True)
    if not p:
        raise RuntimeError(f"Pool v4 {quote_sym}/token tidak ditemukan")
    quote_is_c0 = p["c0"].lower() == quote.lower()
    spacing, tick = p["spacing"], p["tick"]

    width = int(math.log(1 + range_pct / 100) / math.log(1.0001))
    width = max(width - width % spacing, spacing)
    base = tick - tick % spacing
    if quote_is_c0:  # quote = currency0 -> range DI ATAS harga
        tick_lower, tick_upper = base + spacing, base + spacing + width
    else:            # quote = currency1 -> range DI BAWAH harga
        tick_lower, tick_upper = base - width, base

    erc = w3.eth.contract(quote, abi=WETH_ABI + ALLOWANCE_ABI)
    dec = erc.functions.decimals().call()
    amount_units = int(amount * 10 ** dec)
    if erc.functions.balanceOf(acct.address).call() < amount_units:
        raise RuntimeError(f"Saldo {quote_sym} kurang")

    def sqrtp(t):
        return 1.0001 ** (t / 2) * Q96
    sa, sb = sqrtp(tick_lower), sqrtp(tick_upper)
    if quote_is_c0:
        liq = amount_units * sa * sb / (Q96 * (sb - sa))
    else:
        liq = amount_units * Q96 / (sb - sa)
    liq = int(liq * 0.995)  # margin pembulatan float -> jangan sampai butuh > saldo
    if liq <= 0:
        raise RuntimeError("Jumlah terlalu kecil untuk liquidity > 0")

    nonce = w3.eth.get_transaction_count(acct.address)

    def send(fn, n=0):
        tx = fn.build_transaction({"from": acct.address, "nonce": nonce + n,
                                   "chainId": int(_cfg("CHAIN_ID"))})
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if r.status != 1:
            raise RuntimeError(f"Tx gagal: {h.hex()}")
        return h.hex()

    # approval 2 lapis khas v4: quote -> Permit2, lalu Permit2 -> PositionManager
    n = 0
    if erc.functions.allowance(acct.address, PERMIT2).call() < amount_units:
        send(erc.functions.approve(PERMIT2, 2 ** 256 - 1), n=n); n += 1
    permit2 = w3.eth.contract(Web3.to_checksum_address(PERMIT2), abi=PERMIT2_ABI)
    send(permit2.functions.approve(quote, Web3.to_checksum_address(POSM_V4),
                                   2 ** 160 - 1, 2 ** 48 - 1), n=n); n += 1

    actions = bytes([0x02, 0x0d])  # MINT_POSITION, SETTLE_PAIR
    key = (Web3.to_checksum_address(p["c0"]), Web3.to_checksum_address(p["c1"]),
           p["fee"], p["spacing"], Web3.to_checksum_address(p["hooks"]))
    amax = int(amount_units * 101 // 100)
    mint_params = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24",
         "uint256", "uint128", "uint128", "address", "bytes"],
        [key, tick_lower, tick_upper, liq,
         amax if quote_is_c0 else 0, 0 if quote_is_c0 else amax,
         acct.address, b""])
    settle_params = encode(["address", "address"],
                           [Web3.to_checksum_address(p["c0"]),
                            Web3.to_checksum_address(p["c1"])])
    unlock = encode(["bytes", "bytes[]"], [actions, [mint_params, settle_params]])
    deadline = w3.eth.get_block("latest").timestamp + 600

    posm = w3.eth.contract(Web3.to_checksum_address(POSM_V4), abi=POSM_ABI)
    fn = posm.functions.modifyLiquidities(unlock, deadline)
    # simulasi dulu: revert di sini = tidak ada gas terbuang untuk mint
    w3.eth.call({"from": acct.address, "to": posm.address,
                 "data": fn._encode_transaction_data()})
    tx_hash = send(fn, n=n)
    token_id = None
    for lg in w3.eth.get_transaction_receipt(tx_hash)["logs"]:
        if (lg["address"].lower() == POSM_V4.lower() and len(lg["topics"]) == 4
                and bytes(lg["topics"][0]) == TRANSFER_TOPIC):
            token_id = int.from_bytes(bytes(lg["topics"][3]), "big")
    return {"tx": tx_hash, "fee": p["fee"], "tick_lower": tick_lower,
            "tick_upper": tick_upper, "pool": "v4:" + p["id"].hex(),
            "eth": amount, "quote": quote_sym, "range_pct": range_pct, "versi": "v4",
            "token_id": token_id, "c0": p["c0"], "c1": p["c1"]}


def _two_sided_ticks(tick: int, spacing: int, range_pct: float) -> tuple[int, int]:
    """Range ±(range_pct/2)% mengapit tick sekarang, dibulatkan ke spacing."""
    half = int(math.log(1 + range_pct / 200) / math.log(1.0001))
    half = max(half - half % spacing, spacing)
    base = tick - tick % spacing
    return base - half, base + spacing + half


def _token_value_frac(sp: float, sa: float, sb: float, token_is_c0: bool) -> float:
    """Porsi nilai (dalam quote) sisi token untuk range [sa,sb] pada harga sp."""
    v_c0 = sp * (sb - sp) / sb   # nilai sisi c0 (faktor L/Q96 coret)
    v_c1 = sp - sa               # nilai sisi c1
    tot = v_c0 + v_c1
    if tot <= 0:
        return 0.5
    return (v_c0 if token_is_c0 else v_c1) / tot


def buy_token(token: str, quote_units: int) -> dict:
    """Beli token pakai quote_units (raw) via Universal Router v4, pool terdalam.
    Simulasi dulu; raise RuntimeError kalau gagal."""
    import time
    w3 = _w3()
    acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
    quote_addr, quote_sym, _ = _quote_cfg()
    quote = Web3.to_checksum_address(quote_addr)
    token = Web3.to_checksum_address(token)
    import os
    state = {"n": w3.eth.get_transaction_count(acct.address)}

    def send(fn, value=0):
        tx = fn.build_transaction({"from": acct.address, "nonce": state["n"],
                                   "value": value,
                                   "chainId": int(_cfg("CHAIN_ID"))})
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if r.status != 1:
            raise RuntimeError(f"Tx gagal: {h.hex()}")
        state["n"] += 1
        return h.hex()

    erc = w3.eth.contract(quote, abi=WETH_ABI + ALLOWANCE_ABI)
    if erc.functions.allowance(acct.address, PERMIT2).call() < quote_units:
        send(erc.functions.approve(PERMIT2, 2 ** 256 - 1))
    permit2 = w3.eth.contract(Web3.to_checksum_address(PERMIT2), abi=PERMIT2_ABI)
    amt, exp, _ = permit2.functions.allowance(
        acct.address, quote, Web3.to_checksum_address(UNIVERSAL_ROUTER)).call()
    if amt < quote_units or exp <= time.time():
        send(permit2.functions.approve(
            quote, Web3.to_checksum_address(UNIVERSAL_ROUTER),
            2 ** 160 - 1, 2 ** 48 - 1))

    ur = w3.eth.contract(Web3.to_checksum_address(UNIVERSAL_ROUTER), abi=UR_ABI)

    def v4_leg(p, tok_in, amt_in):
        """Simulasi + kirim satu swap v4 exact-in di pool p. Return tx hash.
        Mirror rute jual yang terbukti; USDG pool token sering tipis -> 2-hop
        lewat ETH native yang likuiditasnya dalam."""
        zf1 = tok_in.lower() == p["c0"].lower()
        tok_out = p["c1"] if zf1 else p["c0"]
        price = (p["sqrt_p"] / Q96) ** 2
        est_out = amt_in * price if zf1 else amt_in / price
        min_out = max(1, min(int(est_out * 0.85), 2 ** 128 - 1))
        key = (Web3.to_checksum_address(p["c0"]), Web3.to_checksum_address(p["c1"]),
               p["fee"], p["spacing"], Web3.to_checksum_address(p["hooks"]))
        acts = bytes([0x06, 0x0c, 0x0f])  # SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL
        swp = encode(
            ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
            [(key, zf1, amt_in, min_out, b"")])
        settle = encode(["address", "uint256"],
                        [Web3.to_checksum_address(tok_in), amt_in])
        take = encode(["address", "uint256"],
                      [Web3.to_checksum_address(tok_out), min_out])
        inputs = [encode(["bytes", "bytes[]"], [acts, [swp, settle, take]])]
        deadline = w3.eth.get_block("latest").timestamp + 600
        fn = ur.functions.execute(b"\x10", inputs, deadline)  # V4_SWAP
        value = amt_in if tok_in.lower() == NATIVE else 0
        w3.eth.call({"from": acct.address, "to": ur.address, "value": value,
                     "data": fn._encode_transaction_data()})
        return send(fn, value=value)

    gagal = []

    # Rute 1: USDG -> token langsung (pool USDG/token terdalam)
    p = best_v4_pool(token)
    if p:
        try:
            return {"tx": v4_leg(p, quote, quote_units), "fee": p["fee"],
                    "rute": "v4 langsung"}
        except Exception as e:
            gagal.append(f"v4 langsung: {e}")
    else:
        gagal.append("v4 langsung: tidak ada pool USDG/token")

    # Rute 2: USDG -> ETH native -> token (pool native dalam)
    # ponytail: leg1 (USDG->ETH) eksekusi dulu; kalau leg2 (ETH->token) gagal,
    # dana nyangkut jadi ETH (bukan USDG). Risiko sama seperti jalur jual
    # swap_all_to_quote yang sudah dipakai di produksi — kedua leg pakai pool
    # native yang dalam jadi jarang gagal. Upgrade: state-override sim leg2
    # sebelum kirim leg1 kalau ini pernah kejadian.
    p1 = _best_pool_pair(quote, NATIVE)
    p2 = _best_pool_pair(NATIVE, token)
    if p1 and p2:
        reserve = int(float(os.environ.get("GAS_RESERVE_ETH", 0.002)) * 10 ** 18)
        try:
            v4_leg(p1, quote, quote_units)  # USDG -> native
        except Exception as e:
            gagal.append(f"v4 2-hop USDG->ETH: {e}")
        else:
            spend_nat = w3.eth.get_balance(acct.address) - reserve
            if spend_nat <= 0:
                raise RuntimeError("USDG->ETH sukses tapi hasil <= cadangan gas")
            tx = v4_leg(p2, NATIVE, spend_nat)  # native -> token
            return {"tx": tx, "fee": p2["fee"], "rute": "v4 2-hop via ETH"}
    else:
        gagal.append("v4 2-hop: pool native tidak lengkap")

    raise RuntimeError("Semua rute beli token gagal: " + " | ".join(gagal))


def deploy_v4_two_sided(token: str, amount: float, range_pct: float) -> dict:
    """Mint posisi v4 DUA SISI: range ±(range_pct/2)% mengapit harga sekarang.
    Sebagian quote di-swap jadi token sesuai rasio nilai range, lalu mint
    keduanya. In range (dan menghasilkan fee) sejak detik pertama."""
    w3 = _w3()
    acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
    quote_addr, quote_sym, _ = _quote_cfg()
    quote = Web3.to_checksum_address(quote_addr)
    token = Web3.to_checksum_address(token)
    p = best_v4_pool(token, for_lp=True)
    if not p:
        raise RuntimeError(f"Pool v4 {quote_sym}/token tidak ditemukan")
    quote_is_c0 = p["c0"].lower() == quote.lower()
    tick_lower, tick_upper = _two_sided_ticks(p["tick"], p["spacing"], range_pct)

    def sqrtp(t):
        return 1.0001 ** (t / 2) * Q96
    sa, sb = sqrtp(tick_lower), sqrtp(tick_upper)

    erc_q = w3.eth.contract(quote, abi=WETH_ABI + ALLOWANCE_ABI)
    erc_t = w3.eth.contract(token, abi=WETH_ABI + ALLOWANCE_ABI)
    dec = erc_q.functions.decimals().call()
    units = int(amount * 10 ** dec)
    if erc_q.functions.balanceOf(acct.address).call() < units:
        raise RuntimeError(f"Saldo {quote_sym} kurang")

    # beli sisi token sesuai porsi nilai range
    frac = _token_value_frac(p["sqrt_p"], sa, sb, token_is_c0=not quote_is_c0)
    spend = int(units * frac)
    tok_before = erc_t.functions.balanceOf(acct.address).call()
    swap = buy_token(token, spend)
    tok_avail = erc_t.functions.balanceOf(acct.address).call() - tok_before
    if tok_avail <= 0:
        raise RuntimeError("Swap beli token tidak menghasilkan saldo")
    quote_avail = units - spend

    # harga bergeser setelah swap sendiri -> baca ulang, clamp ke dalam range
    sv = w3.eth.contract(Web3.to_checksum_address(STATE_VIEW), abi=STATE_VIEW_ABI)
    sp, *_ = sv.functions.getSlot0(p["id"]).call()
    sp = min(max(sp, sa * 1.000001), sb * 0.999999)

    amt0, amt1 = ((quote_avail, tok_avail) if quote_is_c0
                  else (tok_avail, quote_avail))
    liq0 = amt0 * sb * sp / (Q96 * (sb - sp))
    liq1 = amt1 * Q96 / (sp - sa)
    liq = int(min(liq0, liq1) * 0.995)
    if liq <= 0:
        raise RuntimeError("Jumlah terlalu kecil untuk liquidity > 0")

    nonce = w3.eth.get_transaction_count(acct.address)

    def send(fn, n=0):
        tx = fn.build_transaction({"from": acct.address, "nonce": nonce + n,
                                   "chainId": int(_cfg("CHAIN_ID"))})
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if r.status != 1:
            raise RuntimeError(f"Tx gagal: {h.hex()}")
        return h.hex()

    # approval 2 lapis untuk KEDUA token
    permit2 = w3.eth.contract(Web3.to_checksum_address(PERMIT2), abi=PERMIT2_ABI)
    n = 0
    for erc, need in ((erc_q, quote_avail), (erc_t, tok_avail)):
        if erc.functions.allowance(acct.address, PERMIT2).call() < need:
            send(erc.functions.approve(PERMIT2, 2 ** 256 - 1), n=n); n += 1
        amt, _, _ = permit2.functions.allowance(
            acct.address, erc.address, Web3.to_checksum_address(POSM_V4)).call()
        if amt < need:
            send(permit2.functions.approve(
                erc.address, Web3.to_checksum_address(POSM_V4),
                2 ** 160 - 1, 2 ** 48 - 1), n=n); n += 1

    actions = bytes([0x02, 0x0d])  # MINT_POSITION, SETTLE_PAIR
    key = (Web3.to_checksum_address(p["c0"]), Web3.to_checksum_address(p["c1"]),
           p["fee"], p["spacing"], Web3.to_checksum_address(p["hooks"]))
    mint_params = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24",
         "uint256", "uint128", "uint128", "address", "bytes"],
        [key, tick_lower, tick_upper, liq, amt0, amt1, acct.address, b""])
    settle_params = encode(["address", "address"],
                           [Web3.to_checksum_address(p["c0"]),
                            Web3.to_checksum_address(p["c1"])])
    unlock = encode(["bytes", "bytes[]"], [actions, [mint_params, settle_params]])
    deadline = w3.eth.get_block("latest").timestamp + 600
    posm = w3.eth.contract(Web3.to_checksum_address(POSM_V4), abi=POSM_ABI)
    fn = posm.functions.modifyLiquidities(unlock, deadline)
    w3.eth.call({"from": acct.address, "to": posm.address,
                 "data": fn._encode_transaction_data()})
    tx_hash = send(fn, n=n)
    token_id = None
    for lg in w3.eth.get_transaction_receipt(tx_hash)["logs"]:
        if (lg["address"].lower() == POSM_V4.lower() and len(lg["topics"]) == 4
                and bytes(lg["topics"][0]) == TRANSFER_TOPIC):
            token_id = int.from_bytes(bytes(lg["topics"][3]), "big")
    return {"tx": tx_hash, "fee": p["fee"], "tick_lower": tick_lower,
            "tick_upper": tick_upper, "pool": "v4:" + p["id"].hex(),
            "eth": amount, "quote": quote_sym, "range_pct": range_pct,
            "versi": "v4", "token_id": token_id, "c0": p["c0"], "c1": p["c1"],
            "two_sided": True, "swap_tx": swap["tx"]}


def v4_position_state(p: dict) -> dict:
    """Isi posisi v4 sekarang (raw): jumlah c0/c1 tersuplai + fee belum dipungut."""
    w3 = _w3()
    sv = w3.eth.contract(Web3.to_checksum_address(STATE_VIEW), abi=STATE_VIEW_ABI)
    pool_id = bytes.fromhex(p["pool"].split(":", 1)[1])
    tl, tu = p["tick_lower"], p["tick_upper"]
    liq, l0, l1 = sv.functions.getPositionInfo(
        pool_id, Web3.to_checksum_address(POSM_V4), tl, tu,
        int(p["token_id"]).to_bytes(32, "big")).call()
    sqrt_p, tick_now, *_ = sv.functions.getSlot0(pool_id).call()
    sa, sb = int(1.0001 ** (tl / 2) * Q96), int(1.0001 ** (tu / 2) * Q96)
    sp = min(max(sqrt_p, sa), sb)
    amt0 = liq * Q96 * (sb - sp) // (sb * sp) if sp < sb else 0
    amt1 = liq * (sp - sa) // Q96
    g0, g1 = sv.functions.getFeeGrowthInside(pool_id, tl, tu).call()
    return {"liq": liq, "amt0": amt0, "amt1": amt1, "tick": tick_now,
            "sqrt_p": sqrt_p,
            "fee0": liq * ((g0 - l0) % 2 ** 256) >> 128,
            "fee1": liq * ((g1 - l1) % 2 ** 256) >> 128}


def v4_zone(p: dict) -> str:
    """Zona posisi dari tick pool on-chain — akurat, beda dengan estimasi harga
    GT + entry yang meleset karena range asli digeser pembulatan tick spacing.
    'above' = harga token (USD) di atas range = posisi 100% quote."""
    w3 = _w3()
    sv = w3.eth.contract(Web3.to_checksum_address(STATE_VIEW), abi=STATE_VIEW_ABI)
    _, tick, *_ = sv.functions.getSlot0(
        bytes.fromhex(p["pool"].split(":", 1)[1])).call()
    quote_addr, _, _ = _quote_cfg()
    q_is_c0 = p["c0"].lower() == quote_addr.lower()
    if tick < p["tick_lower"]:
        return "above" if q_is_c0 else "below"
    if tick >= p["tick_upper"]:
        return "below" if q_is_c0 else "above"
    return "in"


def v4_pending_fees(p: dict) -> tuple[int, int]:
    """Fee posisi v4 yang belum dipungut (raw unit c0, c1).
    Posisi milik bot lewat POSM: owner = POSM, salt = token_id."""
    w3 = _w3()
    sv = w3.eth.contract(Web3.to_checksum_address(STATE_VIEW), abi=STATE_VIEW_ABI)
    pool_id = bytes.fromhex(p["pool"].split(":", 1)[1])
    salt = int(p["token_id"]).to_bytes(32, "big")
    liq, l0, l1 = sv.functions.getPositionInfo(
        pool_id, Web3.to_checksum_address(POSM_V4),
        p["tick_lower"], p["tick_upper"], salt).call()
    g0, g1 = sv.functions.getFeeGrowthInside(
        pool_id, p["tick_lower"], p["tick_upper"]).call()
    # feeGrowth sengaja wrap mod 2^256 (desain core); selisih tetap benar
    return (liq * ((g0 - l0) % 2 ** 256) >> 128,
            liq * ((g1 - l1) % 2 ** 256) >> 128)


def withdraw_v4(token_id: int, c0: str, c1: str) -> dict:
    """Burn posisi v4: tarik semua likuiditas + fee ke wallet. Simulasi dulu."""
    w3 = _w3()
    acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
    actions = bytes([0x03, 0x11])  # BURN_POSITION, TAKE_PAIR
    burn = encode(["uint256", "uint128", "uint128", "bytes"], [token_id, 0, 0, b""])
    take = encode(["address", "address", "address"],
                  [Web3.to_checksum_address(c0), Web3.to_checksum_address(c1),
                   acct.address])
    unlock = encode(["bytes", "bytes[]"], [actions, [burn, take]])
    deadline = w3.eth.get_block("latest").timestamp + 600
    posm = w3.eth.contract(Web3.to_checksum_address(POSM_V4), abi=POSM_ABI)
    fn = posm.functions.modifyLiquidities(unlock, deadline)
    w3.eth.call({"from": acct.address, "to": posm.address,
                 "data": fn._encode_transaction_data()})
    tx = fn.build_transaction({"from": acct.address,
                               "nonce": w3.eth.get_transaction_count(acct.address),
                               "chainId": int(_cfg("CHAIN_ID"))})
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if r.status != 1:
        raise RuntimeError(f"Tx withdraw v4 gagal: {h.hex()}")
    return {"tx": h.hex()}


UNIVERSAL_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
UR_ABI = [{"name": "execute", "type": "function", "stateMutability": "payable",
           "inputs": [{"type": "bytes"}, {"type": "bytes[]"}, {"type": "uint256"}],
           "outputs": []}]


NATIVE = "0x0000000000000000000000000000000000000000"


def swap_all_to_quote(token: str) -> dict:
    """Jual SELURUH saldo token ke quote via Universal Router.
    Rute dicoba berurutan, tiap leg disimulasi eth_call dulu (revert = coba
    rute berikutnya, tanpa gas terbuang):
      1. v4 langsung token->quote
      2. v4 dua hop token->ETH native->quote. Pool USDG token kecil sering
         tipis/mati; likuiditas asli ada di pair native. Hop 2 menyisakan
         GAS_RESERVE_ETH di wallet sebagai gas.
      3. v3 langsung.
    Slippage min-out 15% per leg. Raise RuntimeError kalau semua rute gagal."""
    import os
    import time
    w3 = _w3()
    acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
    quote_addr, quote_sym, _ = _quote_cfg()
    quote = Web3.to_checksum_address(quote_addr)
    token = Web3.to_checksum_address(token)
    erc = w3.eth.contract(token, abi=WETH_ABI + ALLOWANCE_ABI)
    bal = erc.functions.balanceOf(acct.address).call()
    if bal == 0:
        return {"tx": None, "note": "saldo token 0, tidak ada yang dijual"}

    state = {"n": w3.eth.get_transaction_count(acct.address)}

    def send(fn, value=0):
        tx = fn.build_transaction({"from": acct.address, "nonce": state["n"],
                                   "value": value,
                                   "chainId": int(_cfg("CHAIN_ID"))})
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if r.status != 1:
            raise RuntimeError(f"Tx gagal: {h.hex()}")
        state["n"] += 1
        return h.hex()

    if erc.functions.allowance(acct.address, PERMIT2).call() < bal:
        send(erc.functions.approve(PERMIT2, 2 ** 256 - 1))
    permit2 = w3.eth.contract(Web3.to_checksum_address(PERMIT2), abi=PERMIT2_ABI)
    amt, exp, _ = permit2.functions.allowance(
        acct.address, token, Web3.to_checksum_address(UNIVERSAL_ROUTER)).call()
    if amt < bal or exp <= time.time():
        send(permit2.functions.approve(
            token, Web3.to_checksum_address(UNIVERSAL_ROUTER),
            2 ** 160 - 1, 2 ** 48 - 1))

    ur = w3.eth.contract(Web3.to_checksum_address(UNIVERSAL_ROUTER), abi=UR_ABI)

    def v4_leg(p, tok_in, amt_in):
        """Simulasi + kirim satu swap v4 exact-in di pool p. Return tx hash."""
        zf1 = tok_in.lower() == p["c0"].lower()
        tok_out = p["c1"] if zf1 else p["c0"]
        price = (p["sqrt_p"] / Q96) ** 2  # harga c1 per c0 (unit raw)
        est_out = amt_in * price if zf1 else amt_in / price
        min_out = max(1, min(int(est_out * 0.85), 2 ** 128 - 1))
        key = (Web3.to_checksum_address(p["c0"]), Web3.to_checksum_address(p["c1"]),
               p["fee"], p["spacing"], Web3.to_checksum_address(p["hooks"]))
        acts = bytes([0x06, 0x0c, 0x0f])  # SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL
        swp = encode(
            ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
            [(key, zf1, amt_in, min_out, b"")])
        settle = encode(["address", "uint256"],
                        [Web3.to_checksum_address(tok_in), amt_in])
        take = encode(["address", "uint256"],
                      [Web3.to_checksum_address(tok_out), min_out])
        inputs = [encode(["bytes", "bytes[]"], [acts, [swp, settle, take]])]
        deadline = w3.eth.get_block("latest").timestamp + 600
        fn = ur.functions.execute(b"\x10", inputs, deadline)  # V4_SWAP
        value = amt_in if tok_in.lower() == NATIVE else 0
        w3.eth.call({"from": acct.address, "to": ur.address, "value": value,
                     "data": fn._encode_transaction_data()})
        return send(fn, value=value)

    gagal = []

    # Rute 1: v4 langsung token->quote
    p = best_v4_pool(token)
    if p:
        try:
            tx = v4_leg(p, token, bal)
            return {"tx": tx, "rute": f"v4 fee {p['fee']/10000}%", "amount_in": bal}
        except Exception as e:
            gagal.append(f"v4 langsung: {e}")
    else:
        gagal.append("v4 langsung: tidak ada pool")

    # Rute 2: v4 dua hop lewat ETH native
    p1 = _best_pool_pair(NATIVE, token)
    p2 = _best_pool_pair(NATIVE, quote_addr)
    if p1 and p2:
        try:
            tx1 = v4_leg(p1, token, bal)
        except Exception as e:
            gagal.append(f"v4 2-hop: {e}")
        else:
            reserve = int(float(os.environ.get("GAS_RESERVE_ETH", 0.002)) * 10 ** 18)
            jual = w3.eth.get_balance(acct.address) - reserve
            if jual <= 0:
                return {"tx": tx1, "amount_in": bal,
                        "rute": f"v4 2-hop via ETH, hop 2 ditahan (hasil <= "
                                f"cadangan gas {reserve / 10 ** 18} ETH)"}
            try:
                tx2 = v4_leg(p2, NATIVE, jual)
            except Exception as e:
                raise RuntimeError(f"token sudah jadi ETH native (tx {tx1}), "
                                   f"tapi ETH->{quote_sym} gagal: {e}")
            return {"tx": tx2, "amount_in": bal,
                    "rute": f"v4 2-hop via ETH (fee {p1['fee']/10000}% "
                            f"+ {p2['fee']/10000}%)"}
    else:
        gagal.append("v4 2-hop: pool native tidak lengkap")

    # Rute 3: v3 langsung
    # ponytail: UR chain ini fork — layout input V3_SWAP_EXACT_IN beda (head 6
    # slot, bukan 5) jadi rute ini kemungkinan SliceOutOfBounds; dibiarkan
    # sebagai usaha terakhir karena sim menahan tx sampah. Reverse-engineer
    # layout fork kalau muncul token yang cuma punya pool v3.
    factory = w3.eth.contract(Web3.to_checksum_address(_cfg("UNIV3_FACTORY")),
                              abi=FACTORY_ABI)
    best = None
    for fee in FEE_TIERS:
        addr = factory.functions.getPool(quote, token, fee).call()
        if int(addr, 16):
            pool = w3.eth.contract(addr, abi=POOL_ABI)
            liq = pool.functions.liquidity().call()
            if best is None or liq > best[2]:
                best = (fee, pool, liq)
    if best:
        try:
            fee, pool, _ = best
            sqrt_p, *_ = pool.functions.slot0().call()
            price = (sqrt_p / Q96) ** 2
            token_is_0 = int(token, 16) < int(quote, 16)
            est_out = bal * price if token_is_0 else bal / price
            min_out = int(est_out * 0.85)
            path = (bytes.fromhex(token[2:]) + fee.to_bytes(3, "big")
                    + bytes.fromhex(quote[2:]))
            inputs = [encode(["address", "uint256", "uint256", "bytes", "bool"],
                             [acct.address, bal, min_out, path, True])]
            deadline = w3.eth.get_block("latest").timestamp + 600
            fn = ur.functions.execute(b"\x00", inputs, deadline)  # V3_SWAP_EXACT_IN
            w3.eth.call({"from": acct.address, "to": ur.address,
                         "data": fn._encode_transaction_data()})
            return {"tx": send(fn), "rute": f"v3 fee {fee/10000}%", "amount_in": bal}
        except Exception as e:
            gagal.append(f"v3: {e}")
    else:
        gagal.append("v3: tidak ada pool")

    raise RuntimeError("Semua rute jual gagal: " + " | ".join(gagal))


if __name__ == "__main__":
    # self-check matematika liquidity (tanpa RPC)
    sp = Q96  # harga 1.0, tick 0
    sa, sb = 1.0001 ** (-2000 / 2) * Q96, sp
    L = 1_000_000 * Q96 / (sb - sa)
    amt1 = L * (sb - sa) / Q96
    assert abs(amt1 - 1_000_000) < 2, amt1
    # self-check dua sisi: range simetris di harga tengah -> porsi token ~50%
    tl, tu = _two_sided_ticks(0, 60, 40)  # ±20%
    assert tl < 0 < tu and tl % 60 == 0 and tu % 60 == 0, (tl, tu)
    f = _token_value_frac(Q96, 1.0001 ** (tl / 2) * Q96,
                          1.0001 ** (tu / 2) * Q96, token_is_c0=True)
    assert 0.4 < f < 0.6, f
    assert abs(f + _token_value_frac(Q96, 1.0001 ** (tl / 2) * Q96,
                                     1.0001 ** (tu / 2) * Q96,
                                     token_is_c0=False) - 1) < 1e-9
    print("ok")
