import streamlit as st
import pandas as pd
import shioaji as sj
import os
import base64
import time
import requests
from io import StringIO
from datetime import datetime, timedelta

# 頁面設定
st.set_page_config(
    page_title="即時殖利率排行",
    page_icon="📊",
    layout="wide",
)

# 參數設定
UPDATE_INTERVAL = 30
SHOW_TOP_N = 0
CSV_URL = "https://raw.githubusercontent.com/bennyhuurl-code/divident-monitor/main/divident.csv"

TIME_0900 = datetime.strptime("09:00", "%H:%M").time()
TIME_1330 = datetime.strptime("13:30", "%H:%M").time()

@st.cache_data(ttl=60)
def load_csv_from_github():
    try:
        response = requests.get(CSV_URL)
        response.raise_for_status()
        df = pd.read_csv(StringIO(response.text), dtype={'code': str})
        df = df.dropna(how='all')
        df['ex_date'] = df['ex_date'].astype(str).str.strip().replace('', None)
        df['dividend'] = df['dividend'].fillna(0)
        return df
    except Exception as e:
        st.error(f"讀取 CSV 失敗: {e}")
        return None

@st.cache_resource
def login_shioaji():
    try:
        api = sj.Shioaji()
        api_key = os.environ.get("API_KEY")
        secret_key = os.environ.get("SECRET_KEY")
        if not api_key or not secret_key:
            st.error("環境變數未設定")
            return None
        api.login(api_key=api_key, secret_key=secret_key)
        ca_base64 = os.environ.get("CA_BASE64")
        if ca_base64:
            ca_data = base64.b64decode(ca_base64)
            ca_path = "/tmp/Sinopac.pfx"
            with open(ca_path, "wb") as f:
                f.write(ca_data)
            api.activate_ca(ca_path=ca_path, ca_passwd=os.environ.get("CA_PASS"))
        return api
    except Exception as e:
        st.error(f"登入失敗: {e}")
        return None

def get_trading_mode():
    now = datetime.now()
    current_time = now.time()
    is_weekday = now.weekday() < 5
    return "realtime" if is_weekday and (current_time >= TIME_0900 and current_time <= TIME_1330) else "single"

def build_contracts(api, df):
    contracts = []
    for code in df['code']:
        try:
            code_str = str(code)
            try:
                stock = api.Contracts.Stocks[code_str]
            except:
                try:
                    stock = api.Contracts.Stocks[(code_str, 'OTC')]
                except:
                    continue
            if stock:
                contracts.append(stock)
        except:
            continue
    return contracts

def fetch_prices(api, contracts):
    price_map, name_map = {}, {}
    if not contracts:
        return price_map, name_map
    try:
        snapshots = api.snapshots(contracts)
        if snapshots:
            for s in snapshots:
                code = str(getattr(s, 'code', ''))
                if not code:
                    continue
                price = getattr(s, 'close', None) or getattr(s, 'price', None)
                price_map[code] = price
                name = getattr(s, 'name', None)
                if not name:
                    try:
                        stock = api.Contracts.Stocks[code]
                        name = stock.name if hasattr(stock, 'name') else code
                    except:
                        name = code
                name_map[code] = name
    except Exception as e:
        st.warning(f"取得股價失敗: {e}")
    return price_map, name_map

def calculate_yield(df, price_map, name_map, top_n=0):
    df_copy = df.copy()
    df_copy['price'] = df_copy['code'].map(price_map)
    df_copy['name'] = df_copy['code'].map(name_map)
    df_copy['name'] = df_copy['name'].fillna(df_copy['code'])
    df_copy['yield'] = df_copy.apply(
        lambda row: row['dividend'] / row['price'] * 100 if row['price'] and row['price'] > 0 and row['dividend'] > 0 else 0,
        axis=1
    )
    df_result = df_copy.sort_values('yield', ascending=False)
    df_result['ex_date_display'] = df_result['ex_date'].fillna('未定')
    output_cols = ['code', 'name', 'dividend', 'price', 'yield', 'ex_date_display']
    df_output = df_result[output_cols].copy()
    df_output.columns = ['代碼', '名稱', '股利', '股價', '殖利率(%)', '除息日']
    if top_n > 0:
        df_output = df_output.head(top_n)
    return df_output

def main():
    st.title("📊 即時殖利率排行")
    api = login_shioaji()
    if api is None:
        return
    df = load_csv_from_github()
    if df is None:
        return
    contracts = build_contracts(api, df)
    if not contracts:
        st.warning("無有效合約")
        return
    price_map, name_map = fetch_prices(api, contracts)
    df_output = calculate_yield(df, price_map, name_map, SHOW_TOP_N)
    mode = get_trading_mode()
    has_data = any(p is not None for p in price_map.values())
    if has_data and mode == "realtime":
        st.info("🟢 盤中模式，自動更新中")
    else:
        st.info("🔵 盤後模式")
    st.dataframe(df_output, use_container_width=True)
    if mode == "realtime":
        time.sleep(UPDATE_INTERVAL)
        st.rerun()

if __name__ == "__main__":
    main()