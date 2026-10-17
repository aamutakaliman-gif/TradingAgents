"""Settings agreed with the account owner for the paper-trading trial."""

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
