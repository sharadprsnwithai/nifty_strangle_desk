import yfinance as yf
import pandas as pd
import numpy as np

def run():
    universe = [
        'NIFTYBEES.NS', 'AUTOBEES.NS', 'PSUBNKBEES.NS', 'CPSEETF.NS', 
        'SILVERBEES.NS', 'HNGSNGBEES.NS', 'MON100.NS', 'BANKBEES.NS', 'GOLDBEES.NS'
    ]

    print("Fetching actual market historical data from Yahoo Finance (NSE)...")
    daily_data = {}
    for t in universe:
        df = yf.download(t, start='2021-01-01', end='2024-11-21', auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        daily_data[t] = df

    nifty = yf.download('^NSEI', start='2021-01-01', end='2024-11-21', auto_adjust=True, progress=False)
    if isinstance(nifty.columns, pd.MultiIndex):
        nifty.columns = nifty.columns.get_level_values(0)

    weekly_dict = {}
    for t, df in daily_data.items():
        m = df.resample('ME').agg({'High': 'max', 'Low': 'min', 'Close': 'last'})
        m_prev = m.shift(1)
        m['Pivot'] = (m_prev['High'] + m_prev['Low'] + m_prev['Close']) / 3.0
        m['Range'] = m_prev['High'] - m_prev['Low']
        m['S1'] = m['Pivot'] - 0.382 * m['Range']
        m['YearMonth'] = m.index.to_period('M')
        
        w = df.resample('W-FRI').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
        }).dropna()
        w['YearMonth'] = w.index.to_period('M')
        pivot_map = m.set_index('YearMonth')[['Pivot', 'S1']]
        w = w.join(pivot_map, on='YearMonth').dropna(subset=['Pivot', 'S1'])
        w['Mom12'] = w['Close'].pct_change(12).fillna(0)
        w['Next_Open'] = w['Open'].shift(-1)
        weekly_dict[t] = w

    test_start = pd.Timestamp('2021-11-01')
    all_dates = sorted(list(weekly_dict['NIFTYBEES.NS'][weekly_dict['NIFTYBEES.NS'].index >= test_start].index))

    INITIAL_CAPITAL = 1_000_000.0
    cash = INITIAL_CAPITAL
    max_slots = 3
    positions = {}
    portfolio_history = []
    trade_log = []

    for i in range(len(all_dates) - 1):
        curr_date = all_dates[i]
        next_date = all_dates[i + 1]
        
        tickers_to_exit = []
        for t, pos in positions.items():
            w_df = weekly_dict[t]
            if curr_date in w_df.index:
                row = w_df.loc[curr_date]
                if row['Close'] < row['S1']:
                    tickers_to_exit.append(t)
                    
        for t in tickers_to_exit:
            pos = positions[t]
            next_open = weekly_dict[t].loc[curr_date, 'Next_Open']
            if pd.isna(next_open):
                next_open = weekly_dict[t].loc[next_date, 'Open']
            proceeds = pos['units'] * next_open
            ret_pct = (next_open / pos['entry_price'] - 1) * 100
            weeks_held = (next_date - pos['entry_date']).days // 7
            cash += proceeds
            trade_log.append({
                'Ticker': t,
                'Entry Date': pos['entry_date'].strftime('%Y-%m-%d'),
                'Entry Price': round(pos['entry_price'], 2),
                'Exit Date': next_date.strftime('%Y-%m-%d'),
                'Exit Price': round(next_open, 2),
                'Return %': round(ret_pct, 2),
                'Weeks Held': weeks_held,
                'Year': next_date.year
            })
            del positions[t]
            
        open_slots = max_slots - len(positions)
        if open_slots > 0:
            candidates = []
            for t, w_df in weekly_dict.items():
                if t in positions:
                    continue
                if curr_date in w_df.index:
                    row = w_df.loc[curr_date]
                    if row['Close'] > row['Pivot']:
                        candidates.append((t, row['Mom12'], row['Next_Open']))
                        
            candidates.sort(key=lambda x: x[1], reverse=True)
            chosen = candidates[:open_slots]
            
            if len(chosen) > 0:
                current_equity = cash
                for t, pos in positions.items():
                    if curr_date in weekly_dict[t].index:
                        current_equity += pos['units'] * weekly_dict[t].loc[curr_date, 'Close']
                target_alloc = current_equity / max_slots
                for t, mom, nxt_open in chosen:
                    alloc = min(cash, target_alloc)
                    if alloc > 1000 and not pd.isna(nxt_open) and nxt_open > 0:
                        units = alloc / nxt_open
                        cash -= alloc
                        positions[t] = {
                            'units': units,
                            'entry_price': nxt_open,
                            'entry_date': next_date
                        }
                        
        port_val = cash
        for t, pos in positions.items():
            if next_date in weekly_dict[t].index:
                val = pos['units'] * weekly_dict[t].loc[next_date, 'Close']
            else:
                val = pos['units'] * pos['entry_price']
            port_val += val
            
        portfolio_history.append({
            'Date': next_date,
            'Portfolio_Value': port_val,
            'Year': next_date.year
        })

    port_df = pd.DataFrame(portfolio_history).set_index('Date')
    port_df['Equity_Peak'] = port_df['Portfolio_Value'].cummax()
    port_df['Drawdown'] = (port_df['Portfolio_Value'] - port_df['Equity_Peak']) / port_df['Equity_Peak']

    total_return = (port_df['Portfolio_Value'].iloc[-1] / INITIAL_CAPITAL - 1) * 100
    years = (port_df.index[-1] - port_df.index[0]).days / 365.25
    cagr = ((port_df['Portfolio_Value'].iloc[-1] / INITIAL_CAPITAL) ** (1 / years) - 1) * 100
    max_dd = port_df['Drawdown'].min() * 100

    nifty_w = nifty.resample('W-FRI').agg({'Close': 'last'}).dropna()
    nifty_w = nifty_w[(nifty_w.index >= port_df.index[0]) & (nifty_w.index <= port_df.index[-1])]
    nifty_tot_ret = (nifty_w['Close'].iloc[-1] / nifty_w['Close'].iloc[0] - 1) * 100
    nifty_cagr = ((nifty_w['Close'].iloc[-1] / nifty_w['Close'].iloc[0]) ** (1 / years) - 1) * 100
    nifty_peak = nifty_w['Close'].cummax()
    nifty_max_dd = ((nifty_w['Close'] - nifty_peak) / nifty_peak).min() * 100

    trade_df = pd.DataFrame(trade_log)
    win_trades = trade_df[trade_df['Return %'] > 0]
    loss_trades = trade_df[trade_df['Return %'] <= 0]
    win_rate = len(win_trades) / len(trade_df) * 100 if len(trade_df) > 0 else 0
    avg_win = win_trades['Return %'].mean() if len(win_trades) > 0 else 0
    avg_loss = loss_trades['Return %'].mean() if len(loss_trades) > 0 else 0
    profit_factor = win_trades['Return %'].sum() / abs(loss_trades['Return %'].sum()) if len(loss_trades) > 0 else 999.0

    print("==================================================================")
    print(f"ACTUAL HISTORICAL BACKTEST (2021-11-05 to 2024-11-15):")
    print("==================================================================")
    print(f"Ending Capital:          INR {port_df['Portfolio_Value'].iloc[-1]:,.0f} (from 1,000,000)")
    print(f"Strategy Total Return:   {total_return:.2f}%")
    print(f"Strategy CAGR:           {cagr:.2f}%")
    print(f"Strategy Max Drawdown:   {max_dd:.2f}%")
    print("------------------------------------------------------------------")
    print(f"Nifty 50 Benchmark:")
    print(f"Nifty 50 Total Return:   {nifty_tot_ret:.2f}%")
    print(f"Nifty 50 CAGR:           {nifty_cagr:.2f}%")
    print(f"Nifty 50 Max Drawdown:   {nifty_max_dd:.2f}%")
    print("------------------------------------------------------------------")
    print(f"Trade Stats:")
    print(f"Total Trades:            {len(trade_df)}")
    print(f"Win Rate:                {win_rate:.1f}%")
    print(f"Profit Factor:           {profit_factor:.2f}")
    print(f"Average Win:             +{avg_win:.2f}%")
    print(f"Average Loss:            {avg_loss:.2f}%")
    print(f"Risk-Reward:             1 : {abs(avg_win/avg_loss):.2f}")
    print(f"Average Holding:         {trade_df['Weeks Held'].mean():.1f} weeks")
    print("==================================================================")
    print("\nTrades Summary by Asset:")
    if len(trade_df) > 0:
        asset_summary = trade_df.groupby('Ticker').agg({
            'Return %': ['count', lambda x: (x > 0).mean() * 100, 'mean', 'min', 'max']
        })
        asset_summary.columns = ['Trades', 'Win Rate %', 'Avg Ret %', 'Worst %', 'Best %']
        print(asset_summary.round(2).to_string())

if __name__ == '__main__':
    run()
