import yfinance as yf
import pandas as pd
import numpy as np

def calculate_monthly_pivots(daily_df):
    """
    Computes Monthly Fibonacci Pivot (P) and S1 from daily data.
    """
    # Group by calendar year and month
    monthly = daily_df.resample('ME').agg({
        'High': 'max',
        'Low': 'min',
        'Close': 'last'
    })
    
    # Previous month values
    prev = monthly.shift(1)
    monthly['Pivot'] = (prev['High'] + prev['Low'] + prev['Close']) / 3.0
    monthly['Range'] = prev['High'] - prev['Low']
    monthly['S1'] = monthly['Pivot'] - 0.382 * monthly['Range']
    
    return monthly

def backtest_single_etf(ticker, start_date="2023-01-01", test_start="2023-11-01"):
    df_daily = yf.download(ticker, start=start_date, auto_adjust=True)
    if isinstance(df_daily.columns, pd.MultiIndex):
        df_daily.columns = df_daily.columns.get_level_values(0)
    
    if len(df_daily) == 0:
        return None, None
    
    monthly = calculate_monthly_pivots(df_daily)
    
    # Weekly resample ending Friday
    weekly = df_daily.resample('W-FRI').agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }).dropna()
    
    # Assign month to weekly candle based on Friday date
    weekly['YearMonth'] = weekly.index.to_period('M')
    monthly_lookup = monthly.copy()
    monthly_lookup['YearMonth'] = monthly_lookup.index.to_period('M')
    
    pivot_map = monthly_lookup.set_index('YearMonth')[['Pivot', 'S1']]
    weekly = weekly.join(pivot_map, on='YearMonth')
    weekly.dropna(subset=['Pivot', 'S1'], inplace=True)
    
    # Filter to test period
    weekly = weekly[weekly.index >= pd.Timestamp(test_start)].copy()
    if len(weekly) < 5:
        return None, None

    # Trade simulation
    in_position = False
    entry_date = None
    entry_price = 0.0
    trades = []
    
    weekly['In_Position'] = 0
    weekly['Strategy_Return'] = 0.0
    
    # Next bar's Open is the execution price
    weekly['Next_Open'] = weekly['Open'].shift(-1)
    
    for i in range(len(weekly)):
        curr_bar = weekly.iloc[i]
        curr_date = weekly.index[i]
        next_open = curr_bar['Next_Open']
        
        if not in_position:
            if curr_bar['Close'] > curr_bar['Pivot']:
                # Buy signal for next bar open
                if not pd.isna(next_open) and i + 1 < len(weekly):
                    in_position = True
                    entry_date = weekly.index[i+1] # effectively Monday open of next week
                    entry_price = next_open
        else:
            weekly.loc[curr_date, 'In_Position'] = 1
            if curr_bar['Close'] < curr_bar['S1']:
                # Exit signal for next bar open
                if not pd.isna(next_open) and i + 1 < len(weekly):
                    exit_date = weekly.index[i+1]
                    exit_price = next_open
                    ret = (exit_price / entry_price - 1) * 100
                    weeks_held = (exit_date - entry_date).days // 7
                    trades.append({
                        'Ticker': ticker,
                        'Entry Date': entry_date.strftime('%Y-%m-%d'),
                        'Entry Price': round(entry_price, 2),
                        'Exit Date': exit_date.strftime('%Y-%m-%d'),
                        'Exit Price': round(exit_price, 2),
                        'Return %': round(ret, 2),
                        'Weeks Held': weeks_held,
                        'Status': 'Closed'
                    })
                    in_position = False
                    entry_date = None
                    entry_price = 0.0
                    
    # If still in position at the end
    if in_position and entry_price > 0:
        last_close = weekly['Close'].iloc[-1]
        last_date = weekly.index[-1]
        ret = (last_close / entry_price - 1) * 100
        weeks_held = (last_date - entry_date).days // 7
        trades.append({
            'Ticker': ticker,
            'Entry Date': entry_date.strftime('%Y-%m-%d'),
            'Entry Price': round(entry_price, 2),
            'Exit Date': last_date.strftime('%Y-%m-%d'),
            'Exit Price': round(last_close, 2),
            'Return %': round(ret, 2),
            'Weeks Held': weeks_held,
            'Status': 'Open'
        })
        
    trades_df = pd.DataFrame(trades)
    return weekly, trades_df

if __name__ == '__main__':
    universe = ['NIFTYBEES.NS', 'AUTOBEES.NS', 'PSUBNKBEES.NS', 'CPSEETF.NS', 'SILVERBEES.NS', 'HNGSNGBEES.NS', 'MON100.NS', 'BANKBEES.NS', 'GOLDBEES.NS']
    all_trades = []
    
    print("Testing individual ETFs for last 3 years (Nov 2023 - Nov 2026)...")
    for t in universe:
        weekly, trades_df = backtest_single_etf(t)
        if trades_df is not None and len(trades_df) > 0:
            all_trades.append(trades_df)
            wins = trades_df[trades_df['Return %'] > 0]
            win_rate = len(wins) / len(trades_df) * 100
            total_ret = trades_df['Return %'].sum()
            avg_ret = trades_df['Return %'].mean()
            print(f"\n--- {t} ---")
            print(f"Total Trades: {len(trades_df)} | Win Rate: {win_rate:.1f}% | Avg Trade Ret: {avg_ret:.2f}% | Total Sum Ret: {total_ret:.2f}%")
            print(trades_df.to_string(index=False))
