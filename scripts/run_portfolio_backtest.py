import yfinance as yf
import pandas as pd
import numpy as np

def run_portfolio_backtest():
    universe = [
        'NIFTYBEES.NS', 'AUTOBEES.NS', 'PSUBNKBEES.NS', 'CPSEETF.NS', 
        'SILVERBEES.NS', 'HNGSNGBEES.NS', 'MON100.NS', 'BANKBEES.NS', 'GOLDBEES.NS'
    ]
    
    # 1. Fetch daily data
    print("Fetching historical data...")
    daily_data = {}
    for t in universe:
        df = yf.download(t, start='2023-01-01', auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        daily_data[t] = df
        
    # Also fetch Nifty 50 benchmark
    nifty = yf.download('^NSEI', start='2023-01-01', auto_adjust=True, progress=False)
    if isinstance(nifty.columns, pd.MultiIndex):
        nifty.columns = nifty.columns.get_level_values(0)
    
    # 2. Build weekly candles and monthly pivots for each ETF
    weekly_dict = {}
    for t, df in daily_data.items():
        # Monthly Pivots
        m = df.resample('ME').agg({'High': 'max', 'Low': 'min', 'Close': 'last'})
        m_prev = m.shift(1)
        m['Pivot'] = (m_prev['High'] + m_prev['Low'] + m_prev['Close']) / 3.0
        m['Range'] = m_prev['High'] - m_prev['Low']
        m['S1'] = m['Pivot'] - 0.382 * m['Range']
        m['YearMonth'] = m.index.to_period('M')
        
        # Weekly bars (Friday close)
        w = df.resample('W-FRI').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
        }).dropna()
        w['YearMonth'] = w.index.to_period('M')
        
        pivot_map = m.set_index('YearMonth')[['Pivot', 'S1']]
        w = w.join(pivot_map, on='YearMonth').dropna(subset=['Pivot', 'S1'])
        
        # 12-week Momentum (Rate of Change) for priority ranking
        w['Mom12'] = w['Close'].pct_change(12).fillna(0)
        w['Next_Open'] = w['Open'].shift(-1)
        weekly_dict[t] = w

    # Align dates across universe from 2023-11-01 to 2026-11-20
    test_start = pd.Timestamp('2023-11-01')
    all_dates = sorted(list(weekly_dict['NIFTYBEES.NS'][weekly_dict['NIFTYBEES.NS'].index >= test_start].index))
    
    # Portfolio simulation: 3 slots, initial capital 1,000,000 INR
    INITIAL_CAPITAL = 1_000_000.0
    cash = INITIAL_CAPITAL
    max_slots = 3
    positions = {} # ticker -> {'units': float, 'entry_price': float, 'entry_date': Timestamp}
    
    portfolio_history = []
    trade_log = []
    
    for i in range(len(all_dates) - 1):
        curr_date = all_dates[i]
        next_date = all_dates[i + 1]
        
        # Step A: Evaluate Exits at curr_date Friday close
        tickers_to_exit = []
        for t, pos in positions.items():
            w_df = weekly_dict[t]
            if curr_date in w_df.index:
                row = w_df.loc[curr_date]
                if row['Close'] < row['S1']:
                    tickers_to_exit.append(t)
                    
        # Execute exits at next_date Monday open
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
                'Status': 'Closed'
            })
            del positions[t]
            
        # Step B: Evaluate Entries at curr_date Friday close
        open_slots = max_slots - len(positions)
        if open_slots > 0:
            candidates = []
            for t, w_df in weekly_dict.items():
                if t in positions:
                    continue # already holding
                if curr_date in w_df.index:
                    row = w_df.loc[curr_date]
                    if row['Close'] > row['Pivot']:
                        candidates.append((t, row['Mom12'], row['Next_Open']))
                        
            # Sort candidates by 12-week momentum descending (Priority rule)
            candidates.sort(key=lambda x: x[1], reverse=True)
            chosen = candidates[:open_slots]
            
            # Execute entries at next_date Monday open
            if len(chosen) > 0:
                # Target allocation per position = 1/3 of total equity
                # Calculate current total equity
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
                        
        # Step C: Record Portfolio Value at next_date
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
            'Cash': cash,
            'Positions': list(positions.keys()),
            'Active_Count': len(positions)
        })

    # Portfolio metrics
    port_df = pd.DataFrame(portfolio_history).set_index('Date')
    port_df['Equity_Peak'] = port_df['Portfolio_Value'].cummax()
    port_df['Drawdown'] = (port_df['Portfolio_Value'] - port_df['Equity_Peak']) / port_df['Equity_Peak']
    
    total_return = (port_df['Portfolio_Value'].iloc[-1] / INITIAL_CAPITAL - 1) * 100
    years = (port_df.index[-1] - port_df.index[0]).days / 365.25
    cagr = ((port_df['Portfolio_Value'].iloc[-1] / INITIAL_CAPITAL) ** (1 / years) - 1) * 100
    max_dd = port_df['Drawdown'].min() * 100
    
    # Nifty 50 benchmark
    nifty_w = nifty.resample('W-FRI').agg({'Close': 'last'}).dropna()
    nifty_w = nifty_w[(nifty_w.index >= port_df.index[0]) & (nifty_w.index <= port_df.index[-1])]
    nifty_tot_ret = (nifty_w['Close'].iloc[-1] / nifty_w['Close'].iloc[0] - 1) * 100
    nifty_cagr = ((nifty_w['Close'].iloc[-1] / nifty_w['Close'].iloc[0]) ** (1 / years) - 1) * 100
    nifty_peak = nifty_w['Close'].cummax()
    nifty_max_dd = ((nifty_w['Close'] - nifty_peak) / nifty_peak).min() * 100

    # Trade stats
    trade_df = pd.DataFrame(trade_log)
    win_trades = trade_df[trade_df['Return %'] > 0]
    loss_trades = trade_df[trade_df['Return %'] <= 0]
    
    win_rate = len(win_trades) / len(trade_df) * 100 if len(trade_df) > 0 else 0
    avg_win = win_trades['Return %'].mean() if len(win_trades) > 0 else 0
    avg_loss = loss_trades['Return %'].mean() if len(loss_trades) > 0 else 0
    total_gains = win_trades['Return %'].sum()
    total_losses = abs(loss_trades['Return %'].sum())
    profit_factor = total_gains / total_losses if total_losses > 0 else 999.0
    
    print("==================================================================")
    print(f"3-YEAR PORTFOLIO BACKTEST RESULTS ({port_df.index[0].strftime('%Y-%m-%d')} to {port_df.index[-1].strftime('%Y-%m-%d')})")
    print("==================================================================")
    print(f"Initial Capital:         INR {INITIAL_CAPITAL:,.0f}")
    print(f"Ending Portfolio Value:  INR {port_df['Portfolio_Value'].iloc[-1]:,.0f}")
    print(f"Total Portfolio Return:  {total_return:.2f}%")
    print(f"Strategy CAGR:           {cagr:.2f}%")
    print(f"Strategy Max Drawdown:   {max_dd:.2f}%")
    print(f"Calmar Ratio (CAGR/MDD): {abs(cagr/max_dd):.2f}")
    print("------------------------------------------------------------------")
    print(f"Benchmark (Nifty 50):")
    print(f"Nifty 50 Total Return:   {nifty_tot_ret:.2f}%")
    print(f"Nifty 50 CAGR:           {nifty_cagr:.2f}%")
    print(f"Nifty 50 Max Drawdown:   {nifty_max_dd:.2f}%")
    print("------------------------------------------------------------------")
    print(f"Trade Performance Statistics:")
    print(f"Total Trades Closed:     {len(trade_df)}")
    print(f"Win Rate:                {win_rate:.1f}%")
    print(f"Profit Factor:           {profit_factor:.2f}")
    print(f"Average Win:             +{avg_win:.2f}%")
    print(f"Average Loss:            {avg_loss:.2f}%")
    print(f"Risk-Reward Ratio:       1 : {abs(avg_win / avg_loss):.2f}" if avg_loss != 0 else "N/A")
    print(f"Average Holding Period:  {trade_df['Weeks Held'].mean():.1f} weeks (~{trade_df['Weeks Held'].mean()/4.3:.1f} months)")
    print(f"Best Trade:              +{trade_df['Return %'].max():.2f}% ({trade_df.loc[trade_df['Return %'].idxmax(), 'Ticker']})")
    print(f"Worst Trade:             {trade_df['Return %'].min():.2f}% ({trade_df.loc[trade_df['Return %'].idxmin(), 'Ticker']})")
    print("==================================================================")
    print("\nRecent 10 Trades:")
    print(trade_df.tail(10).to_string(index=False))

    # Year-by-Year Breakdown
    port_df['Year'] = port_df.index.year
    year_groups = port_df.groupby('Year')['Portfolio_Value']
    yearly_stats = []
    prev_val = INITIAL_CAPITAL
    for y, group in year_groups:
        end_val = group.iloc[-1]
        y_ret = (end_val / prev_val - 1) * 100
        cum_max_y = group.cummax()
        y_dd = ((group - cum_max_y) / cum_max_y).min() * 100
        yearly_stats.append({
            'Year': y,
            'Starting Capital': round(prev_val, 0),
            'Ending Capital': round(end_val, 0),
            'Return %': round(y_ret, 2),
            'Max Drawdown %': round(y_dd, 2)
        })
        prev_val = end_val
    print("\n==================================================================")
    print("YEAR-BY-YEAR PORTFOLIO BREAKDOWN:")
    print("==================================================================")
    print(pd.DataFrame(yearly_stats).to_string(index=False))

    # Per-Asset Breakdown
    print("\n==================================================================")
    print("BREAKDOWN BY ASSET CLASS / ETF:")
    print("==================================================================")
    if len(trade_df) > 0:
        asset_summary = trade_df.groupby('Ticker').agg({
            'Return %': ['count', lambda x: (x > 0).mean() * 100, 'mean', 'min', 'max']
        })
        asset_summary.columns = ['Trades', 'Win Rate %', 'Avg Return %', 'Worst Trade %', 'Best Trade %']
        print(asset_summary.round(2).to_string())
    print("==================================================================")

if __name__ == '__main__':
    run_portfolio_backtest()
