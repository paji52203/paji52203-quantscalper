#!/root/quantscalper/.venv/bin/python3
import sys, os, logging
sys.path.insert(0, '/root/quantscalper')
os.chdir('/root/quantscalper')

# load .env
for line in open('/root/quantscalper/.env'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        os.environ[k.strip()] = v.strip()

import configparser
cfg = configparser.ConfigParser()
cfg.read('config.ini')

from exchange.bybit_connector import BybitConnector
from risk.risk_manager import RiskManager
from data.market_data import MarketData

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger()

API_KEY    = os.environ.get('BYBIT_API_KEY', '')
API_SECRET = os.environ.get('BYBIT_API_SECRET', '')

conn = BybitConnector(API_KEY, API_SECRET, 'BTCUSDT', '15', logger=log)

# 1. Balance
balance = conn.get_balance()
print(f'[1] Balance:       ${balance:.4f} USDT')

# 2. Price
md = MarketData('BTCUSDT', '15')
price = md.get_current_price()
print(f'[2] Price:         ${price:,.2f}')

# 3. Qty
risk_cfg = dict(cfg['RISK']) | dict(cfg['EXCHANGE'])
rm = RiskManager(risk_cfg, log)
sl   = round(price * 0.995, 2)
tp   = round(price * 1.01,  2)
qty  = rm.calculate_qty(balance, price, sl)
print(f'[3] SL:            ${sl:,.2f}  (0.5% below)')
print(f'[3] TP:            ${tp:,.2f}  (1.0% above)')
print(f'[3] Qty:           {qty:.6f} BTC')
print(f'[3] Min Bybit qty: 0.001 BTC')
qty_ok = qty >= 0.001
print(f'[3] Qty OK:        {qty_ok}')

# 4. API auth test
print()
print('[4] Testing API auth (set_leverage 50x)...')
r = conn.set_leverage(50)
if r:
    code = r.get('retCode')
    if code in (0, 110043):
        print(f'    API OK  (retCode={code})')
    else:
        print(f'    API PROBLEM: {r}')
else:
    print('    API FAILED: no response')

# 5. Order simulation
print()
if qty_ok:
    print('[5] Order sim (DRY, NOT sent to exchange):')
    result = conn.place_order('Buy', qty, sl, tp, dry_run=True)
    print(f'    Result: {result}')
    print(f'    ORDER PIPELINE: OK - siap execute real order')
else:
    needed_margin = (price * 0.001) / 50
    print(f'[5] PROBLEM: qty {qty:.6f} BTC < 0.001 minimum!')
    print(f'    Butuh margin minimal: ${needed_margin:.2f} USDT untuk 1 kontrak')
    print(f'    Saldo saat ini: ${balance:.4f} USDT')
    if balance < needed_margin:
        print(f'    SALDO TIDAK CUKUP untuk 1 kontrak minimum!')
    else:
        print(f'    Saldo cukup tapi risk_pct terlalu kecil - perlu adjust')
