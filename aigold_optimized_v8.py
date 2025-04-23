import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import numpy as np
import time
import datetime
import asyncio
import telegram
from telegram.error import NetworkError
import logging
import os
from datetime import timezone

# Logging sozlamalari
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Sozlamalar
RISK_PERCENT = 0.05
DAILY_TARGET_PERCENT = 1.0
MIN_ATR = 0.015
MIN_ADX = 15.0
TIMEZONE_OFFSET = 3  # Broker UTC farqi (soat)
USE_TIME_FILTER = True
TRADING_START_HOUR = 8  # UTC, London sessiyasi
TRADING_END_HOUR = 16   # UTC, NY sessiyasi
MAX_DRAWDOWN_PERCENT = 10.0
TRAILING_STEP = 1.5
MIN_MARGIN_LEVEL = 500.0
MAX_DAILY_TRADES = 3
TELEGRAM_BOT_TOKEN = "8107287816:AAGH3N80O4pWgI6dM74HTkhkglpSGNdwrVI"
TELEGRAM_CHAT_ID = "1112793157"
SYMBOL = "GOLD#"  # XAUUSD o'rniga GOLD#
TIMEFRAME_M1 = mt5.TIMEFRAME_M1
TIMEFRAME_M5 = mt5.TIMEFRAME_M5
RETRY_ATTEMPTS = 3
RETRY_DELAY = 10

# Global o'zgaruvchilar
start_equity = 0
daily_trades = 0
current_day = None
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
position_open = False
position_type = None
position_open_price = 0
position_sl = 0
position_tp = 0
position_ticket = 0

async def send_telegram_message(message):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')
        logger.info(f"Telegram xabar yuborildi: {message}")
        return True
    except NetworkError as e:
        logger.error(f"Telegram xabar yuborishda xato: {e}")
        return False

def reconnect_mt5():
    logger.info("MT5 ga qayta ulanmoqda...")
    mt5.shutdown()
    for attempt in range(RETRY_ATTEMPTS):
        if mt5.initialize(login=int(os.getenv("MT5_LOGIN", 0)), 
                         password=os.getenv("MT5_PASSWORD", ""), 
                         server=os.getenv("MT5_SERVER", "")):
            logger.info("MT5 qayta ulandi")
            return True
        logger.error(f"Qayta ulanish xatosi, urinish {attempt + 1}/{RETRY_ATTEMPTS}: {mt5.last_error()}")
        time.sleep(RETRY_DELAY)
    logger.critical("MT5 ga qayta ulanib bo'lmadi")
    return False

def ensure_symbol_selected():
    if not mt5.symbol_select(SYMBOL, True):
        logger.error(f"Symbol {SYMBOL} ni tanlashda xato: {mt5.last_error()}")
        return False
    logger.info(f"Symbol {SYMBOL} tanlandi")
    return True

def calculate_dynamic_stop_loss(atr):
    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} ma'lumotlarini olishda xato")
        return 50.0
    point = symbol_info.point
    sl_points = max(atr * 2.0 / point, 50.0)
    return sl_points

def calculate_lot_size(stop_loss_points):
    account_info = mt5.account_info()
    if not account_info:
        logger.error("Hisob ma'lumotlarini olishda xato")
        return 0.01
    balance = account_info.balance
    risk_amount = balance * (RISK_PERCENT / 100.0)
    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} ma'lumotlarini olishda xato")
        return 0.01
    pip_value = symbol_info.trade_tick_value * 100
    lot_size = risk_amount / (stop_loss_points * pip_value)
    lot_size = round(lot_size, 2)
    if lot_size < 0.01:
        lot_size = 0.01
    logger.info(f"Lot hajmi: {lot_size} | Risk: {risk_amount} | SL Points: {stop_loss_points}")
    return lot_size

def check_max_drawdown():
    account_info = mt5.account_info()
    if not account_info:
        logger.error("Hisob ma'lumotlarini olishda xato")
        return False
    current_equity = account_info.equity
    if (start_equity - current_equity) / start_equity * 100 > MAX_DRAWDOWN_PERCENT:
        asyncio.run(send_telegram_message(
            f"! Maksimal drawdown yetdi!\nEquity: {current_equity:.2f}"))
        if mt5.positions_total() > 0:
            mt5.Close(SYMBOL)
        logger.critical("Maksimal drawdown yetdi, dastur to'xtatildi")
        return False
    return True

def check_margin_level():
    account_info = mt5.account_info()
    if not account_info:
        logger.error("Hisob ma'lumotlarini olishda xato")
        return False
    margin_level = account_info.margin_level
    if margin_level > 0 and margin_level < MIN_MARGIN_LEVEL:
        asyncio.run(send_telegram_message(
            f"! Margin level past!\nLevel: {margin_level:.2f}"))
        return False
    return True

def apply_trailing_stop(df):
    global position_sl, position_ticket
    if not position_open:
        return
    atr = df['atr'].iloc[-1]
    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} ma'lumotlarini olishda xato")
        return
    point = symbol_info.point
    trailing_distance = atr * TRAILING_STEP / point
    min_profit = atr * 2.0 / point
    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick:
        logger.error(f"Tik ma'lumotlarini olishda xato: {mt5.last_error()}")
        return

    if position_type == "buy" and tick.bid > position_open_price + min_profit * point:
        new_sl = tick.bid - trailing_distance * point
        if new_sl > position_sl:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": position_ticket,
                "sl": new_sl,
                "tp": position_tp
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                position_sl = new_sl
                asyncio.run(send_telegram_message(
                    f"Trailing Stop (Buy)!\nNew SL: {new_sl:.2f}"))
                logger.info(f"Trailing stop yangilandi: SL={new_sl}")
            else:
                logger.error(f"Trailing stop xatosi: {result.comment}")

    elif position_type == "sell" and tick.ask < position_open_price - min_profit * point:
        new_sl = tick.ask + trailing_distance * point
        if new_sl < position_sl:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": position_ticket,
                "sl": new_sl,
                "tp": position_tp
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                position_sl = new_sl
                asyncio.run(send_telegram_message(
                    f"Trailing Stop (Sell)!\nNew SL: {new_sl:.2f}"))
                logger.info(f"Trailing stop yangilandi: SL={new_sl}")
            else:
                logger.error(f"Trailing stop xatosi: {result.comment}")

def get_indicators():
    for attempt in range(RETRY_ATTEMPTS):
        rates_m1 = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME_M1, 0, 20)
        rates_m5 = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME_M5, 0, 20)
        if rates_m1 is not None and rates_m5 is not None:
            df_m1 = pd.DataFrame(rates_m1)
            df_m5 = pd.DataFrame(rates_m5)

            # M1 indikatorlari
            df_m1['ema_fast'] = ta.ema(df_m1['close'], length=5)
            df_m1['ema_slow'] = ta.ema(df_m1['close'], length=10)
            df_m1['atr'] = ta.atr(df_m1['high'], df_m1['low'], df_m1['close'], length=20)
            df_m1['adx'] = ta.adx(df_m1['high'], df_m1['low'], df_m1['close'], length=14)['ADX_14']
            df_m1['rsi'] = ta.rsi(df_m1['close'], length=14)

            # M5 indikatorlari
            df_m5['ema_fast'] = ta.ema(df_m5['close'], length=5)
            df_m5['ema_slow'] = ta.ema(df_m5['close'], length=10)

            return df_m1, df_m5
        else:
            logger.error(f"Ma'lumotlar olishda xato, urinish {attempt + 1}/{RETRY_ATTEMPTS}: {mt5.last_error()}")
            if not reconnect_mt5():
                return None
            time.sleep(RETRY_DELAY)
    logger.error("Ma'lumotlar olishda doimiy xato")
    return None

async def main():
    global start_equity, daily_trades, current_day, position_open, position_type, position_open_price, position_sl, position_tp, position_ticket

    # MetaTrader 5 ulanish
    if not mt5.initialize(login=int(os.getenv("MT5_LOGIN", 0)), 
                         password=os.getenv("MT5_PASSWORD", ""), 
                         server=os.getenv("MT5_SERVER", "")):
        logger.critical("MetaTrader 5 ulanmadi")
        await send_telegram_message("! MetaTrader 5 ulanmadi")
        return
    logger.info("MetaTrader 5 ulandi")

    # Symbol tanlash
    if not ensure_symbol_selected():
        logger.critical("Symbol tanlashda xato, dastur to'xtatildi")
        await send_telegram_message(f"! Symbol {SYMBOL} tanlashda xato")
        return

    account_info = mt5.account_info()
    if not account_info:
        logger.critical("Hisob ma'lumotlarini olishda xato")
        await send_telegram_message("! Hisob ma'lumotlarini olishda xato")
        return
    start_equity = account_info.equity
    await send_telegram_message(f"Bot ishga tushdi!\nSymbol: {SYMBOL}")

    current_day = datetime.datetime.now(timezone.utc).date()

    while True:
        now = datetime.datetime.now(timezone.utc)
        if now.date() != current_day:
            current_day = now.date()
            account_info = mt5.account_info()
            if not account_info:
                logger.error("Hisob ma'lumotlarini olishda xato")
                await asyncio.sleep(60)
                continue
            start_equity = account_info.equity
            daily_trades = 0
            await send_telegram_message(f"Yangi kun!\nStart Equity: {start_equity:.2f}")

        if not check_max_drawdown():
            break

        account_info = mt5.account_info()
        if not account_info:
            logger.error("Hisob ma'lumotlarini olishda xato")
            await asyncio.sleep(60)
            continue
        current_equity = account_info.equity
        daily_profit = current_equity - start_equity
        target_equity = start_equity * (1 + DAILY_TARGET_PERCENT / 100.0)
        if daily_profit >= target_equity - start_equity:
            await send_telegram_message(f"Kunlik maqsad yetdi!\nProfit: {daily_profit:.2f}")
            break

        if daily_trades >= MAX_DAILY_TRADES:
            logger.info(f"Kunlik savdo limiti yetdi: {daily_trades}")
            await asyncio.sleep(60)
            continue

        if USE_TIME_FILTER:
            utc_hour = now.hour
            if utc_hour < TRADING_START_HOUR or utc_hour >= TRADING_END_HOUR:
                logger.info(f"Faol bozor soatlari tashqarisida: {utc_hour}")
                await asyncio.sleep(60)
                continue

        indicators = get_indicators()
        if indicators is None:
            await asyncio.sleep(5)
            continue
        df_m1, df_m5 = indicators

        # Signallar
        buy_signal = (df_m1['ema_fast'].iloc[-2] < df_m1['ema_slow'].iloc[-2] and
                      df_m1['ema_fast'].iloc[-1] > df_m1['ema_slow'].iloc[-1] and
                      df_m5['ema_fast'].iloc[-2] < df_m5['ema_slow'].iloc[-2] and
                      df_m5['ema_fast'].iloc[-1] > df_m5['ema_slow'].iloc[-1] and
                      30 < df_m1['rsi'].iloc[-1] < 70 and
                      df_m1['atr'].iloc[-1] >= MIN_ATR and
                      df_m1['adx'].iloc[-1] >= MIN_ADX)
        sell_signal = (df_m1['ema_fast'].iloc[-2] > df_m1['ema_slow'].iloc[-2] and
                       df_m1['ema_fast'].iloc[-1] < df_m1['ema_slow'].iloc[-1] and
                       df_m5['ema_fast'].iloc[-2] > df_m5['ema_slow'].iloc[-2] and
                       df_m5['ema_fast'].iloc[-1] < df_m5['ema_slow'].iloc[-1] and
                       30 < df_m1['rsi'].iloc[-1] < 70 and
                       df_m1['atr'].iloc[-1] >= MIN_ATR and
                       df_m1['adx'].iloc[-1] >= MIN_ADX)

        logger.info(f"Buy Signal: {buy_signal}, Sell Signal: {sell_signal}")

        # Trailing stop
        apply_trailing_stop(df_m1)

        if position_open or not check_margin_level():
            await asyncio.sleep(5)
            continue

        tick = mt5.symbol_info_tick(SYMBOL)
        if not tick:
            logger.error(f"Tik ma'lumotlarini olishda xato: {mt5.last_error()}")
            await asyncio.sleep(5)
            continue

        if buy_signal:
            sl_points = calculate_dynamic_stop_loss(df_m1['atr'].iloc[-1])
            sl = tick.ask - sl_points * mt5.symbol_info(SYMBOL).point
            tp = tick.ask + sl_points * 3.0 * mt5.symbol_info(SYMBOL).point
            lot_size = calculate_lot_size(sl_points)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": SYMBOL,
                "volume": lot_size,
                "type": mt5.ORDER_TYPE_BUY,
                "price": tick.ask,
                "sl": sl,
                "tp": tp,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                position_open = True
                position_type = "buy"
                position_open_price = tick.ask
                position_sl = sl
                position_tp = tp
                position_ticket = result.order
                daily_trades += 1
                await send_telegram_message(
                    f"BUY ochildi!\nLot: {lot_size:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f}")
            else:
                await send_telegram_message(f"! BUY xatolik!\nXato: {result.comment}")

        elif sell_signal:
            sl_points = calculate_dynamic_stop_loss(df_m1['atr'].iloc[-1])
            sl = tick.bid + sl_points * mt5.symbol_info(SYMBOL).point
            tp = tick.bid - sl_points * 3.0 * mt5.symbol_info(SYMBOL).point
            lot_size = calculate_lot_size(sl_points)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": SYMBOL,
                "volume": lot_size,
                "type": mt5.ORDER_TYPE_SELL,
                "price": tick.bid,
                "sl": sl,
                "tp": tp,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                position_open = True
                position_type = "sell"
                position_open_price = tick.bid
                position_sl = sl
                position_tp = tp
                position_ticket = result.order
                daily_trades += 1
                await send_telegram_message(
                    f"SELL ochildi!\nLot: {lot_size:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f}")
            else:
                await send_telegram_message(f"! SELL xatolik!\nXato: {result.comment}")

        await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
