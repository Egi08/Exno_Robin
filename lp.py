"""Deploy LP one-sided Uniswap v3 (strategi 'Onesided Lower' ala CT).

Posisi 100% WETH ditaruh di range sebelah harga sekarang, jadi ETH kamu
terpakai "nangkep" token saat harga turun ke range (limit-buy + dapet fee).
"""
import math
import os

from web3 import Web3

FEE_TIERS = [3000, 10000, 500]  # urutan coba: 0.3%, 1%, 0.05%
TICK_SPACING = {500: 10, 3000: 60, 10000: 200}

FACTORY_ABI = [{"name": "getPool", "type": "function", "stateMutability": "view",
                "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}],
                "outputs": [{"type": "address"}]}]
POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint160"}, {"type": "int24"}, {"type": "uint16"},
                 {"type": "uint16"}, {"type": "uint16"}, {"type": "uint8"}, {"type": "bool"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint128"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
]
WETH_ABI = [
    {"name": "deposit", "type": "function", "stateMutability": "payable", "inputs": [], "outputs": []},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
]
NPM_ABI = [{
    "name": "mint", "type": "function", "stateMutability": "payable",
    "inputs": [{"components": [
        {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"},
        {"name": "tickUpper", "type": "int24"}, {"name": "amount0Desired", "type": "uint256"},
        {"name": "amount1Desired", "type": "uint256"}, {"name": "amount0Min", "type": "uint256"},
        {"name": "amount1Min", "type": "uint256"}, {"name": "recipient", "type": "address"},
        {"name": "deadline", "type": "uint256"}], "name": "params", "type": "tuple"}],
    "outputs": [{"type": "uint256"}, {"type": "uint128"}, {"type": "uint256"}, {"type": "uint256"}],
}]


def _cfg(name: str) -> str:
    v = os.environ.get(name, "")
    if not v or v.startswith("0x..."):
        raise RuntimeError(f"Config {name} belum diisi di .env")
    return v


def _quote_cfg():
    """(alamat, simbol, wrap_native) token quote sesuai .env: WETH (default) atau USDG."""
    q = os.environ.get("QUOTE_TOKEN", "WETH").upper()
    if q == "USDG":
        return _cfg("USDG_ADDRESS"), "USDG", False
    return _cfg("WETH_ADDRESS"), "WETH", True


def _w3():
    kw = {"headers": {"User-Agent": "Mozilla/5.0",
                      "Content-Type": "application/json"}, "timeout": 15}
    fallback = os.environ.get("RPC_URL_FALLBACK", "")
    if not fallback:
        return Web3(Web3.HTTPProvider(_cfg("RPC_URL"), request_kwargs=kw))
    for url in (_cfg("RPC_URL"), fallback):
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs=kw))
        try:
            w3.eth.block_number
            return w3
        except Exception:
            continue
    raise RuntimeError("RPC utama & fallback dua-duanya gagal")


def wallet_balance_quote() -> float:
    """Saldo quote token wallet bot (WETH: ETH natif + WETH; USDG: saldo USDG)."""
    w3 = _w3()
    acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
    quote_addr, _, wrap_native = _quote_cfg()
    c = w3.eth.contract(Web3.to_checksum_address(quote_addr), abi=WETH_ABI)
    bal = c.functions.balanceOf(acct.address).call()
    if wrap_native:
        bal += w3.eth.get_balance(acct.address)
    return bal / 10 ** c.functions.decimals().call()



def quote_pool_liquidity(token: str) -> float:
    """Saldo quote token terbesar (dalam unit token, USDG = USD) yang tersimpan di
    pool quote/token on-chain di antara fee tier. 0 = pool tidak ada / kosong."""
    w3 = _w3()
    quote_addr, _, _ = _quote_cfg()
    quote = Web3.to_checksum_address(quote_addr)
    factory = w3.eth.contract(Web3.to_checksum_address(_cfg("UNIV3_FACTORY")), abi=FACTORY_ABI)
    erc = w3.eth.contract(quote, abi=WETH_ABI)
    best = 0
    for fee in FEE_TIERS:
        addr = factory.functions.getPool(quote, Web3.to_checksum_address(token), fee).call()
        if int(addr, 16):
            best = max(best, erc.functions.balanceOf(addr).call())
    v3 = best / 10 ** erc.functions.decimals().call()
    try:
        from lp4 import v4_quote_depth
        return max(v3, v4_quote_depth(token))
    except Exception:
        return v3

def deploy_one_sided_lp(token: str, amount: float, range_pct: float) -> dict:
    """Mint posisi Uniswap v3 100% quote (WETH/USDG sesuai QUOTE_TOKEN),
    range `range_pct`% di sisi harga sekarang. Raise RuntimeError kalau gagal."""
    w3 = _w3()
    acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
    quote_addr, quote_sym, wrap_native = _quote_cfg()
    quote = Web3.to_checksum_address(quote_addr)
    token = Web3.to_checksum_address(token)
    npm_addr = Web3.to_checksum_address(_cfg("UNIV3_POSITION_MANAGER"))
    factory = w3.eth.contract(Web3.to_checksum_address(_cfg("UNIV3_FACTORY")), abi=FACTORY_ABI)

    # cari pool quote/token dengan liquidity terbesar di antara fee tier
    best = None
    for fee in FEE_TIERS:
        addr = factory.functions.getPool(quote, token, fee).call()
        if int(addr, 16) == 0:
            continue
        pool = w3.eth.contract(addr, abi=POOL_ABI)
        liq = pool.functions.liquidity().call()
        if best is None or liq > best[2]:
            best = (fee, pool, liq)
    # pilih v3 vs v4: pakai yang kedalaman sisi quote-nya lebih besar
    from lp4 import deploy_v4, v4_quote_depth
    try:
        v4_depth = v4_quote_depth(token)
    except Exception:
        v4_depth = 0.0
    erc_q = w3.eth.contract(quote, abi=WETH_ABI)
    v3_depth = (erc_q.functions.balanceOf(best[1].address).call()
                / 10 ** erc_q.functions.decimals().call()) if best else 0.0
    if v4_depth > v3_depth:
        return deploy_v4(token, amount, range_pct)
    if best is None:
        raise RuntimeError(f"Pool {quote_sym}/token tidak ditemukan (v3 maupun v4)")
    fee, pool, _ = best

    _, current_tick, *_ = pool.functions.slot0().call()
    token0 = pool.functions.token0().call()
    quote_is_token0 = token0.lower() == quote.lower()
    spacing = TICK_SPACING[fee]

    # range yang 100% berisi quote:
    #  - quote = token1 -> range DI BAWAH tick sekarang
    #  - quote = token0 -> range DI ATAS tick sekarang
    # lebar range = range_pct% dalam harga => delta_tick = ln(1+pct)/ln(1.0001)
    width = int(math.log(1 + range_pct / 100) / math.log(1.0001))
    width = max(width - width % spacing, spacing)
    base = current_tick - current_tick % spacing
    if quote_is_token0:
        tick_lower, tick_upper = base + spacing, base + spacing + width
    else:
        tick_lower, tick_upper = base - width, base

    quote_c = w3.eth.contract(quote, abi=WETH_ABI)
    amount_units = int(amount * 10 ** quote_c.functions.decimals().call())
    npm = w3.eth.contract(npm_addr, abi=NPM_ABI)
    nonce = w3.eth.get_transaction_count(acct.address)

    def send(fn, value=0, n=0):
        tx = fn.build_transaction({"from": acct.address, "nonce": nonce + n,
                                   "value": value, "chainId": int(_cfg("CHAIN_ID"))})
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if r.status != 1:
            raise RuntimeError(f"Tx gagal: {h.hex()}")
        return h.hex()

    n = 0
    bal = quote_c.functions.balanceOf(acct.address).call()
    if bal < amount_units:
        if not wrap_native:
            raise RuntimeError(f"Saldo {quote_sym} kurang ({bal/1e6:.2f})")
        # WETH: wrap hanya kekurangannya dari ETH natif
        deficit = amount_units - bal
        if w3.eth.get_balance(acct.address) < deficit:
            raise RuntimeError("Saldo ETH+WETH tidak cukup untuk posisi + gas")
        send(quote_c.functions.deposit(), value=deficit, n=n); n += 1
    send(quote_c.functions.approve(npm_addr, amount_units), n=n); n += 1

    a0, a1 = (amount_units, 0) if quote_is_token0 else (0, amount_units)
    t0, t1 = (quote, token) if quote_is_token0 else (token, quote)
    deadline = w3.eth.get_block("latest").timestamp + 600
    # ponytail: amountMin=0 (posisi one-sided, slippage risk kecil); tambah cek kalau perlu
    tx_hash = send(npm.functions.mint((t0, t1, fee, tick_lower, tick_upper,
                                       a0, a1, 0, 0, acct.address, deadline)), n=n)
    token_id = None
    try:
        m = w3.eth.contract(npm_addr, abi=NPM_MGMT_ABI)
        nbal = m.functions.balanceOf(acct.address).call()
        token_id = m.functions.tokenOfOwnerByIndex(acct.address, nbal - 1).call()
    except Exception:
        pass
    return {"tx": tx_hash, "token_id": token_id, "fee": fee, "tick_lower": tick_lower, "tick_upper": tick_upper,
            "pool": pool.address, "eth": amount, "quote": quote_sym, "range_pct": range_pct}


NPM_MGMT_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "tokenOfOwnerByIndex", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
    {"name": "positions", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "uint96"}, {"type": "address"}, {"type": "address"},
                 {"type": "address"}, {"type": "uint24"}, {"type": "int24"},
                 {"type": "int24"}, {"type": "uint128"}, {"type": "uint256"},
                 {"type": "uint256"}, {"type": "uint128"}, {"type": "uint128"}]},
    {"name": "decreaseLiquidity", "type": "function", "stateMutability": "payable",
     "inputs": [{"components": [
         {"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
         {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
         {"name": "deadline", "type": "uint256"}], "name": "params", "type": "tuple"}],
     "outputs": [{"type": "uint256"}, {"type": "uint256"}]},
    {"name": "collect", "type": "function", "stateMutability": "payable",
     "inputs": [{"components": [
         {"name": "tokenId", "type": "uint256"}, {"name": "recipient", "type": "address"},
         {"name": "amount0Max", "type": "uint128"}, {"name": "amount1Max", "type": "uint128"}],
         "name": "params", "type": "tuple"}],
     "outputs": [{"type": "uint256"}, {"type": "uint256"}]},
]


def withdraw_v3(token: str, token_id=None) -> dict:
    """Tarik semua likuiditas + fee posisi v3 token ini ke wallet."""
    w3 = _w3()
    acct = w3.eth.account.from_key(_cfg("PRIVATE_KEY"))
    npm = w3.eth.contract(Web3.to_checksum_address(_cfg("UNIV3_POSITION_MANAGER")),
                          abi=NPM_MGMT_ABI)
    token = Web3.to_checksum_address(token)
    if token_id is None:  # fallback: cari NFT posisi milik wallet yang cocok
        for i in range(npm.functions.balanceOf(acct.address).call()):
            tid = npm.functions.tokenOfOwnerByIndex(acct.address, i).call()
            q = npm.functions.positions(tid).call()
            if token.lower() in (q[2].lower(), q[3].lower()) and q[7] > 0:
                token_id = tid
                break
    if token_id is None:
        raise RuntimeError("Posisi v3 untuk token ini tidak ditemukan di wallet")
    liq = npm.functions.positions(token_id).call()[7]
    nonce = w3.eth.get_transaction_count(acct.address)
    deadline = w3.eth.get_block("latest").timestamp + 600
    txs = []
    for i, fn in enumerate([
            npm.functions.decreaseLiquidity((token_id, liq, 0, 0, deadline)),
            npm.functions.collect((token_id, acct.address, 2**128 - 1, 2**128 - 1))]):
        tx = fn.build_transaction({"from": acct.address, "nonce": nonce + i,
                                   "chainId": int(_cfg("CHAIN_ID"))})
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if r.status != 1:
            raise RuntimeError(f"Tx withdraw v3 gagal: {h.hex()}")
        txs.append(h.hex())
    return {"tx": txs[-1]}


def withdraw_position(p: dict) -> dict:
    """Dispatch withdraw sesuai versi pool posisi."""
    if str(p.get("pool", "")).startswith("v4:"):
        from lp4 import withdraw_v4
        return withdraw_v4(p["token_id"], p["c0"], p["c1"])
    return withdraw_v3(p["ca"], p.get("token_id"))


if __name__ == "__main__":
    # self-check matematika tick (tanpa RPC)
    width = int(math.log(1.20) / math.log(1.0001))
    assert 1750 < width < 1900, width
    assert (width - width % 60) % 60 == 0
    print("ok")
