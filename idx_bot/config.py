"""Settings agreed with the account owner for the paper-trading trial."""

from dataclasses import dataclass

# LQ45 constituents for 3 Aug - 30 Oct 2026 (INDY and NCKL in, SMGR and TOWR
# out). Compiled without access to the IDX announcement itself: check it against
# idx.co.id, and replace it when the November 2026 review takes effect.
LQ45 = [
    "ACES", "ADMR", "ADRO", "AKRA", "AMMN", "AMRT", "ANTM", "ARTO", "ASII",
    "BBCA", "BBNI", "BBRI", "BBTN", "BMRI", "BRIS", "BRPT", "CPIN", "CTRA",
    "ESSA", "EXCL", "GOTO", "ICBP", "INCO", "INDF", "INDY", "INKP", "ISAT",
    "ITMG", "JPFA", "JSMR", "KLBF", "MAPA", "MAPI", "MBMA", "MDKA", "MEDC",
    "NCKL", "PGAS", "PGEO", "PTBA", "SIDO", "SMRA", "TLKM", "UNTR", "UNVR",
]

EQUITY = 10_000_000          # paper capital, IDR
RISK_PER_TRADE = 0.02        # Elder's 2% rule: loss if the stop is hit
MONTHLY_RISK_CAP = 0.06      # Elder's 6% rule: month's losses + open risk
LOT_SIZE = 100               # shares per lot on IDX
ATR_STOP_MULTIPLE = 2.0      # stop = entry - 2 x ATR(14)
REWARD_RISK = 2.0            # target = entry + 2 x (entry - stop)
BUY_FEE = 0.0015             # typical retail broker fee; confirm with Mirae
SELL_FEE = 0.0025            # includes 0.1% final income tax on sales
MIN_DAILY_VALUE = 10_000_000_000  # Rp 10 bn average daily turnover
TOP_N = 5                    # candidates passed on to the analyst team


@dataclass(frozen=True)
class Market:
    """What sizing and the ledger need to know about where a trade happens."""
    name: str
    equity: float           # paper capital, in ``currency``
    currency: str
    lot_size: float         # units bought per lot (IDX: 100 shares; crypto: smallest step)
    buy_fee: float
    sell_fee: float
    ticks: str              # "idx" for BEI price fractions, "sig5" for five significant digits
    extreme_move: float     # a daily move this large blocks a candidate
    min_daily_value: float  # average daily turnover, in ``currency``


IDX = Market("idx", EQUITY, "Rp", LOT_SIZE, BUY_FEE, SELL_FEE, "idx", 0.10, MIN_DAILY_VALUE)

# Rp 10 jt at the 17 Oct 2026 rate of Rp 18,000 per USD. Binance spot, no leverage,
# standard 0.1% fee each way. Crypto moves more, so the extreme-move bar is 15%.
# Turnover is the one Binance USDT pair's (Yahoo's fallback sums many venues, so
# it clears the bar more easily); USD 10 mn a day dwarfs orders of a few hundred USDT.
CRYPTO = Market("crypto", 555.56, "USDT", 0.000001, 0.001, 0.001, "sig5", 0.15, 10_000_000)

# Large coins traded on Binance spot against USDT, without stablecoins or meme coins.
# The daily run ranks these by market cap and keeps the top CRYPTO_TOP_N.
CRYPTO_POOL = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "TRX", "AVAX", "LINK", "DOT",
    "LTC", "BCH", "XLM", "HBAR", "NEAR", "UNI", "APT", "ATOM", "ETC", "ICP", "FIL",
]
CRYPTO_TOP_N = 15
