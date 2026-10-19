"""Technical indicators over a daily OHLCV frame (columns Open, High, Low, Close, Volume)."""

import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return line - line.ewm(span=signal, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's average true range."""
    prev_close = df["Close"].shift()
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands and %B (0 at the lower band, 1 at the upper)."""
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    pct_b = (close - lower) / (upper - lower)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "pct_b": pct_b})


def stochastic(df: pd.DataFrame, n: int = 14, d: int = 3) -> pd.DataFrame:
    """Stochastic oscillator: %K over ``n`` days, %D its ``d``-day average (0-100)."""
    low_n = df["Low"].rolling(n).min()
    high_n = df["High"].rolling(n).max()
    k = 100 * (df["Close"] - low_n) / (high_n - low_n)
    return pd.DataFrame({"k": k, "d": k.rolling(d).mean()})


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's ADX: trend strength 0-100 (above ~25 is a strong trend)."""
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - df["Close"].shift()).abs(),
         (df["Low"] - df["Close"].shift()).abs()], axis=1).max(axis=1)
    atr_n = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_n
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
