import ccxt
import pandas as pd
import numpy as np
import telegram
import asyncio
import time
from datetime import datetime
import uuid
import os

# Binance sozlamalari (API kalitsiz)
binance = ccxt.binance()

# Telegram bot sozlamalari
TELEGRAM_TOKEN = os.getenv('7227161030:AAHcsRcMeDzvmbheEGfXqs1B6gHOfvQKDBI')
TELEGRAM_CHANNEL = os.getenv('-1002354472675')

# Botni ishga tushirish
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# Strategiya parametrlari
SYMBOL = os.getenv('SYMBOL', 'BTC/USDT')  # Agar o'zgaruvchi berilmasa, BTC/USDT
TIMEFRAME = '15m'    # Vaqt oralig'i (15 daqiqa)
LIMIT = 100          # Ma'lumotlar soni
BB_PERIOD = 20       # Bollinger Bands davri
BB_STD = 2           # Bollinger Bands standart og'ish
RSI_PERIOD = 14      # RSI davri
MACD_FAST = 12       # MACD tez liniya
MACD_SLOW = 26       # MACD sekin liniya
MACD_SIGNAL = 9      # MACD signal liniyasi
VOLUME_MA = 20       # Hajm o'rtacha davri
RISK_REWARD_RATIO = 1.5  # TP/SL nisbati (1.5:1)
CHECK_INTERVAL = 300  # 5 daqiqa (sekundda)

# Ma'lumotlarni olish funksiyasi
def fetch_ohlcv(symbol, timeframe, limit):
    ohlcv = binance.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

# Texnik ko'rsatkichlarni hisoblash
def calculate_indicators(df):
    # Bollinger Bands
    df['ma'] = df['close'].rolling(window=BB_PERIOD).mean()
    df['std'] = df['close'].rolling(window=BB_PERIOD).std()
    df['upper_bb'] = df['ma'] + (df['std'] * BB_STD)
    df['lower_bb'] = df['ma'] - (df['std'] * BB_STD)
    
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # MACD
    exp1 = df['close'].ewm(span=MACD_FAST, adjust=False).mean()
    exp2 = df['close'].ewm(span=MACD_SLOW, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    # Hajm o'rtachasi
    df['volume_ma'] = df['volume'].rolling(window=VOLUME_MA).mean()
    
    return df

# TP va SL hisoblash
def calculate_tp_sl(price, signal_type, atr, risk_reward_ratio=RISK_REWARD_RATIO):
    sl_distance = atr * 1.5  # SL ATR asosida
    if signal_type == 'BUY':
        stop_loss = price - sl_distance
        take_profit = price + (sl_distance * risk_reward_ratio)
    else:  # SELL
        stop_loss = price + sl_distance
        take_profit = price - (sl_distance * risk_reward_ratio)
    return take_profit, stop_loss

# Signal generatsiyasi
def generate_signals(df):
    signals = []
    # ATR hisoblash (TP/SL uchun)
    df['atr'] = df['high'].sub(df['low']).rolling(window=14).mean()
    
    for i in range(2, len(df)):
        # Hajm tasdiqlovi
        volume_confirm = df['volume'].iloc[i] > df['volume_ma'].iloc[i]
        
        # Sotib olish signali
        if (df['close'].iloc[i] < df['lower_bb'].iloc[i] and
            df['rsi'].iloc[i] < 35 and
            df['macd_hist'].iloc[i] > df['macd_hist'].iloc[i-1] and
            volume_confirm):
            tp, sl = calculate_tp_sl(df['close'].iloc[i], 'BUY', df['atr'].iloc[i])
            signals.append({
                'id': str(uuid.uuid4()),
                'time': df['timestamp'].iloc[i],
                'symbol': SYMBOL,
                'type': 'BUY',
                'price': df['close'].iloc[i],
                'rsi': df['rsi'].iloc[i],
                'macd': df['macd'].iloc[i],
                'take_profit': tp,
                'stop_loss': sl,
                'details': 'Price below Lower BB, RSI near oversold, MACD histogram bullish, Volume confirmed'
            })
        # Sotish signali
        elif (df['close'].iloc[i] > df['upper_bb'].iloc[i] and
              df['rsi'].iloc[i] > 65 and
              df['macd_hist'].iloc[i] < df['macd_hist'].iloc[i-1] and
              volume_confirm):
            tp, sl = calculate_tp_sl(df['close'].iloc[i], 'SELL', df['atr'].iloc[i])
            signals.append({
                'id': str(uuid.uuid4()),
                'time': df['timestamp'].iloc[i],
                'symbol': SYMBOL,
                'type': 'SELL',
                'price': df['close'].iloc[i],
                'rsi': df['rsi'].iloc[i],
                'macd': df['macd'].iloc[i],
                'take_profit': tp,
                'stop_loss': sl,
                'details': 'Price above Upper BB, RSI near overbought, MACD histogram bearish, Volume confirmed'
            })
    return signals

# Telegramga signal yuborish
async def send_signal(signal):
    message = (
        f"

📡 *New Signal* (ID: {signal['id']})\n"
        f"Time: {signal['time']}\n"
        f"Symbol: {signal['symbol']}\n"
        f"Type: {signal['type']}\n"
        f"Price: {signal['price']:.2f}\n"
        f"Take Profit: {signal['take_profit']:.2f}\n"
        f"Stop Loss: {signal['stop_loss']:.2f}\n"
        f"RSI: {signal['rsi']:.2f}\n"
        f"MACD: {signal['macd']:.4f}\n"
        f"Details: {signal['details']}"
    )
    await bot.send_message(chat_id=TELEGRAM_CHANNEL, text=message, parse_mode='Markdown')

# Asosiy tsikl
async def main():
    while True:
        try:
            # Ma'lumotlarni olish
            df = fetch_ohlcv(SYMBOL, TIMEFRAME, LIMIT)
            
            # Ko'rsatkichlarni hisoblash
            df = calculate_indicators(df)
            
            # Signallarni generatsiya qilish
            signals = generate_signals(df)
            
            # Signallarni Telegramga yuborish
            for signal in signals:
                await send_signal(signal)
                print(f"Signal sent: {signal['id']}")
            
            # 5 daqiqa kutish
            await asyncio.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(60)  # Xato yuz bersa, 1 daqiqa kutish

# Botni ishga tushirish
if __name__ == "__main__":
    asyncio.run(main())
