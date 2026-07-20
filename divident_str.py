import streamlit as st
import pandas as pd
import shioaji as sj
import os
import base64
import time
import requests
from io import StringIO
from datetime import datetime, timedelta

# ===== 頁面設定 =====
st.set_page_config(
    page_title="即時殖利率排行",
    page_icon="📊",
    layout="wide",
)

# ===== 參數設定 =====
UPDATE_INTERVAL = 30
SHOW_TOP_N = 0
MAX_RETRY = 3  # 最大重試次數
RETRY_WAIT = 30  # 重試間隔（秒）
CSV_URL = "https://raw.githubusercontent.com/bennyhuurl-code/divident-monitor/main/divident.csv"

TIME_0900 = datetime.strptime("09:00", "%H:%M").time()
TIME_1330 = datetime.strptime("13:30", "%H:%M").time()

# ===== 初始化 Session =====
if "df" not in st.session_state:
    st.session_state.df = None
if "data_source" not in st.session_state:
    st.session_state.data_source = "GitHub"

# ===== 快取函式 =====
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

def is_trading_day():
    """判斷是否為交易日（週一～週五）"""
    now = datetime.now()
    return now.weekday() < 5

def is_trading_hours():
    """判斷是否在交易時段（09:00~13:30）"""
    now = datetime.now()
    current_time = now.time()
    return current_time >= TIME_0900 and current_time <= TIME_1330

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

def fetch_realtime_prices(api, contracts):
    """抓取即時資料（snapshot）"""
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
        st.warning(f"即時資料抓取失敗: {e}")
    return price_map, name_map

def fetch_historical_prices(api, contracts):
    """抓取歷史收盤價（備用）"""
    price_map, name_map = {}, {}
    if not contracts:
        return price_map, name_map
    try:
        # 嘗試用 history 或 kbars 抓昨日收盤
        # 註：此為示意，實際用法需依 Shioaji API 調整
        for stock in contracts:
            try:
                # 抓最近一筆歷史資料
                data = api.kbars(stock, "1d", "2026-07-17")  # 示意日期
                if data and not data.empty:
                    price = data['close'].iloc[-1]
                    price_map[str(stock.code)] = price
                    name_map[str(stock.code)] = stock.name
            except:
                pass
    except Exception as e:
        st.warning(f"歷史資料抓取失敗: {e}")
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

# ============================================
# CSV 驗證與上傳功能（與之前相同）
# ============================================
def validate_csv_row(row):
    try:
        code = str(row.get('code', '')).strip()
        if not code or not code.isdigit():
            return False, "股票代碼無效"
        name = str(row.get('name', '')).strip()
        if not name:
            return False, "股票名稱無效"
        try:
            dividend = float(row.get('dividend', -1))
            if dividend < 0:
                return False, "股利不可為負數"
        except:
            return False, "股利格式無效"
        ex_date = str(row.get('ex_date', '')).strip()
        if ex_date:
            try:
                pd.to_datetime(ex_date)
            except:
                return False, "日期格式無效 (需 YYYY-MM-DD)"
        return True, "有效"
    except Exception as e:
        return False, str(e)

def process_uploaded_csv(uploaded_file):
    try:
        df_upload = pd.read_csv(uploaded_file, dtype={'code': str})
        required_cols = ['code', 'name', 'dividend', 'ex_date']
        missing_cols = [col for col in required_cols if col not in df_upload.columns]
        if missing_cols:
            return {
                "success": False,
                "message": f"❌ 缺少必要欄位: {missing_cols}",
                "valid_rows": [],
                "invalid_rows": []
            }
        valid_rows = []
        invalid_rows = []
        for idx, row in df_upload.iterrows():
            is_valid, msg = validate_csv_row(row)
            row_data = row.to_dict()
            row_data['_index'] = idx + 1
            if is_valid:
                valid_rows.append(row_data)
            else:
                row_data['_error'] = msg
                invalid_rows.append(row_data)
        total_rows = len(df_upload)
        valid_count = len(valid_rows)
        invalid_count = len(invalid_rows)
        if valid_count == 0:
            return {
                "success": False,
                "message": f"❌ 上傳失敗：{total_rows} 筆資料全部無效，保留原檔",
                "valid_rows": [],
                "invalid_rows": invalid_rows,
                "total_rows": total_rows,
                "valid_count": 0,
                "invalid_count": invalid_count
            }
        elif invalid_count == 0:
            return {
                "success": True,
                "message": f"✅ 全部 {total_rows} 筆合格，將完全取代舊檔",
                "valid_rows": valid_rows,
                "invalid_rows": [],
                "total_rows": total_rows,
                "valid_count": valid_count,
                "invalid_count": 0,
                "action": "replace_all"
            }
        else:
            return {
                "success": True,
                "message": f"⚠️ {valid_count} 筆合格，{invalid_count} 筆無效（已剔除），將更新合格資料",
                "valid_rows": valid_rows,
                "invalid_rows": invalid_rows,
                "total_rows": total_rows,
                "valid_count": valid_count,
                "invalid_count": invalid_count,
                "action": "replace_valid_only"
            }
    except Exception as e:
        return {
            "success": False,
            "message": f"❌ 讀取失敗: {e}",
            "valid_rows": [],
            "invalid_rows": []
        }

def write_to_github(content):
    try:
        token = st.secrets["GITHUB_TOKEN"]
        repo = "bennyhuurl-code/divident-monitor"
        path = "divident.csv"
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        response = requests.get(url, headers={"Authorization": f"token {token}"})
        if response.status_code == 200:
            sha = response.json().get('sha')
        else:
            sha = None
        payload = {
            "message": "更新股票清單（透過 Streamlit App）",
            "content": base64.b64encode(content.encode()).decode(),
            "sha": sha
        }
        response = requests.put(url, json=payload, headers={"Authorization": f"token {token}"})
        if response.status_code in [200, 201]:
            return True, "✅ 已成功寫入 GitHub"
        else:
            return False, f"❌ GitHub API 錯誤"
    except Exception as e:
        return False, f"❌ 寫入失敗: {e}"

# ============================================
# 主程式
# ============================================
def main():
    # ===== 側邊欄 =====
    with st.sidebar:
        st.header("⚙️ 設定")
        update_interval = st.slider(
            "⏱️ 更新間隔（秒）",
            min_value=10,
            max_value=60,
            value=UPDATE_INTERVAL,
            step=5
        )
        top_n = st.number_input(
            "📊 顯示前 N 筆",
            min_value=0,
            value=SHOW_TOP_N,
            step=5,
            help="0 表示全部顯示"
        )
        st.divider()
        
        # ===== 上傳 CSV 功能 =====
        st.subheader("📤 更新股票清單")
        st.caption("上傳 CSV（須含 code, name, dividend, ex_date）")
        
        uploaded_file = st.file_uploader("選擇 CSV 檔案", type="csv", key="csv_uploader")
        
        if uploaded_file:
            with st.spinner("處理中..."):
                result = process_uploaded_csv(uploaded_file)
            
            if result["success"]:
                st.info(f"📊 總筆數: {result['total_rows']}")
                st.info(f"✅ 合格: {result['valid_count']} 筆")
                if result['invalid_count'] > 0:
                    st.warning(f"❌ 無效: {result['invalid_count']} 筆")
                    with st.expander("查看無效資料"):
                        for row in result['invalid_rows']:
                            st.write(f"行 {row['_index']}: {row.get('_error', '未知錯誤')}")
                
                if st.button("📤 確認寫入 GitHub", use_container_width=True):
                    valid_df = pd.DataFrame(result['valid_rows'])
                    valid_df = valid_df.drop(columns=['_index'], errors='ignore')
                    if '_error' in valid_df.columns:
                        valid_df = valid_df.drop(columns=['_error'], errors='ignore')
                    csv_content = valid_df.to_csv(index=False)
                    success, msg = write_to_github(csv_content)
                    if success:
                        st.success(msg)
                        st.info("🔄 請點擊下方按鈕重新整理")
                        if st.button("🔄 重新整理", use_container_width=True):
                            st.session_state.df = None
                            st.rerun()
                    else:
                        st.error(msg)
            else:
                st.error(result["message"])
                if result.get("invalid_rows"):
                    with st.expander("查看無效資料"):
                        for row in result['invalid_rows']:
                            st.write(f"行 {row['_index']}: {row.get('_error', '未知錯誤')}")
        
        st.divider()
        
        # ===== 強制更新 =====
        if st.button("🔄 強制更新", use_container_width=True):
            st.session_state.df = None
            st.rerun()
        
        st.divider()
        
        # ===== 狀態顯示 =====
        st.caption("📌 狀態")
        mode = st.session_state.get("mode", "未知")
        mode_icon = "🟢" if mode == "realtime" else "🔵"
        st.write(f"{mode_icon} 模式: {mode}")
        st.write(f"📂 資料來源: {st.session_state.get('data_source', 'GitHub')}")
        last_update = st.session_state.get("last_update", "---")
        st.write(f"🕐 最後更新: {last_update}")
    
    # ===== 主內容 =====
    st.title("📊 即時殖利率排行")
    st.caption("台股除息監視器")
    
    # ===== 讀取資料 =====
    if st.session_state.df is None:
        with st.spinner("📊 載入資料..."):
            df = load_csv_from_github()
            if df is not None:
                st.session_state.df = df
                st.session_state.data_source = "GitHub"
            else:
                st.error("❌ 讀取資料失敗")
                return
    else:
        df = st.session_state.df
    
    # ===== 登入 Shioaji =====
    api = login_shioaji()
    if api is None:
        return
    
    # ===== 建立合約 =====
    contracts = build_contracts(api, df)
    if not contracts:
        st.warning("⚠️ 無有效合約，請檢查股票代碼")
        return
    
    # ============================================================
    # 核心邏輯：先抓即時，失敗重試，再不行才抓歷史
    # ============================================================
    price_map, name_map = {}, {}
    mode = "single"
    retry_count = 0
    is_trading = is_trading_day() and is_trading_hours()
    
    with st.spinner("📊 正在取得股價資料..."):
        # 步驟 1：抓即時資料
        price_map, name_map = fetch_realtime_prices(api, contracts)
        has_data = any(p is not None for p in price_map.values())
        
        # 步驟 2：如果無資料且為交易日，重試
        while not has_data and is_trading and retry_count < MAX_RETRY:
            retry_count += 1
            st.info(f"⏳ 等待即時資料（第 {retry_count}/{MAX_RETRY} 次重試）...")
            time.sleep(RETRY_WAIT)
            price_map, name_map = fetch_realtime_prices(api, contracts)
            has_data = any(p is not None for p in price_map.values())
        
        # 步驟 3：最終決定
        if has_data:
            mode = "realtime"
            st.session_state.data_source = "🟢 即時資料"
        else:
            # 無即時資料，抓歷史收盤價
            st.info("📂 抓取歷史收盤價...")
            price_map, name_map = fetch_historical_prices(api, contracts)
            has_historical = any(p is not None for p in price_map.values())
            if has_historical:
                mode = "single"
                st.session_state.data_source = "🔵 歷史收盤價"
            else:
                st.warning("⚠️ 無任何股價資料")
    
    # ===== 計算殖利率 =====
    df_output = calculate_yield(df, price_map, name_map, top_n)
    
    st.session_state.mode = mode
    st.session_state.last_update = datetime.now().strftime("%H:%M:%S")
    
    # ===== 顯示統計 =====
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("📊 總筆數", len(df_output))
    with col2:
        valid_count = len(df_output[df_output['殖利率(%)'] > 0])
        st.metric("📈 有殖利率", valid_count)
    with col3:
        max_yield = df_output['殖利率(%)'].max()
        st.metric("🏆 最高殖利率", f"{max_yield:.2f}%" if max_yield > 0 else "N/A")
    with col4:
        mode_icon = "🟢" if mode == "realtime" else "🔵"
        mode_text = "盤中即時" if mode == "realtime" else "盤後單次"
        st.metric("📌 模式", f"{mode_icon} {mode_text}")
    
    # ===== 顯示表格 =====
    st.dataframe(
        df_output,
        use_container_width=True,
        hide_index=True,
        column_config={
            "代碼": st.column_config.TextColumn("代碼", width="small"),
            "名稱": st.column_config.TextColumn("名稱", width="medium"),
            "股利": st.column_config.NumberColumn("股利", format="%.2f", width="small"),
            "股價": st.column_config.NumberColumn("股價", format="%.2f", width="small"),
            "殖利率(%)": st.column_config.NumberColumn("殖利率", format="%.2f%%", width="small"),
            "除息日": st.column_config.TextColumn("除息日", width="medium"),
        },
        height=600
    )
    
    # ===== 底部資訊 =====
    if mode == "realtime":
        st.info(f"🟢 盤中模式：每 {update_interval} 秒自動更新 | 下次更新：{(datetime.now() + timedelta(seconds=update_interval)).strftime('%H:%M:%S')}")
        time.sleep(update_interval)
        st.rerun()
    else:
        st.info("🔵 盤後模式：股價已固定，如需更新請按側邊欄「強制更新」按鈕")

if __name__ == "__main__":
    main()