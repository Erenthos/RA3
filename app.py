import streamlit as st
import pandas as pd
import psycopg2
from datetime import datetime, timedelta
from psycopg2.extras import RealDictCursor

st.set_page_config(page_title="Reverse Auction Platform", layout="wide")

# =========================================================
# 🔗 DATABASE CONNECTION (uses Streamlit secret NEON_URL)
# =========================================================
def get_conn():
    conn_str = st.secrets["NEON_URL"]
    return psycopg2.connect(conn_str)

def execute_query(query, params=None, fetch=False):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(query, params or ())
    data = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return data

# =========================================================
# 🧩 AUTHENTICATION
# =========================================================
def login(username, password):
    user = execute_query(
        "SELECT * FROM users WHERE username=%s AND password=%s",
        (username, password),
        fetch=True
    )
    return user[0] if user else None

def user_exists(username):
    user = execute_query("SELECT id FROM users WHERE username=%s", (username,), fetch=True)
    return len(user) > 0

def signup(username, password, role):
    execute_query(
        "INSERT INTO users (username, password, role) VALUES (%s, %s, %s)",
        (username, password, role)
    )

# =========================================================
# 🏗️ AUCTION MANAGEMENT
# =========================================================
def create_auction(title, base_price, min_dec, duration, buyer_id):
    execute_query("""
        INSERT INTO auctions (title, base_price, min_decrement, current_price, duration_minutes, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (title, base_price, min_dec, base_price, duration, buyer_id))

def get_auctions():
    return execute_query("SELECT * FROM auctions ORDER BY id DESC", fetch=True)

def get_duration(auction_id):
    dur = execute_query("SELECT duration_minutes FROM auctions WHERE id=%s", (auction_id,), fetch=True)
    return dur[0]['duration_minutes']

def start_auction(auction_id):
    start = datetime.now()
    end = start + timedelta(minutes=get_duration(auction_id))
    execute_query("""
        UPDATE auctions 
        SET status='running', start_time=%s, end_time=%s 
        WHERE id=%s
    """, (start, end, auction_id))

def stop_auction(auction_id):
    execute_query("UPDATE auctions SET status='ended' WHERE id=%s", (auction_id,))

def get_bids(auction_id):
    return execute_query("""
        SELECT b.id, u.username AS supplier, b.bid_amount, b.bid_time
        FROM bids b
        JOIN users u ON u.id = b.supplier_id
        WHERE b.auction_id=%s
        ORDER BY b.bid_amount ASC
    """, (auction_id,), fetch=True)

def place_bid(auction_id, supplier_id, bid_amount):
    auction = execute_query("SELECT * FROM auctions WHERE id=%s", (auction_id,), fetch=True)[0]
    min_allowed = float(auction['current_price']) - float(auction['min_decrement'])

    if auction['status'] != 'running':
        st.warning("⚠️ Auction not running.")
        return False

    if bid_amount >= auction['current_price']:
        st.warning("❌ Bid must be lower than current price.")
        return False
    if bid_amount > min_allowed:
        st.warning(f"⚠️ Minimum decrement is {auction['min_decrement']}. You must bid ≤ {min_allowed:.2f}")
        return False

    execute_query("""
        INSERT INTO bids (auction_id, supplier_id, bid_amount) VALUES (%s, %s, %s)
    """, (auction_id, supplier_id, bid_amount))
    execute_query("UPDATE auctions SET current_price=%s WHERE id=%s", (bid_amount, auction_id))
    st.success("✅ Bid placed successfully!")
    return True

# =========================================================
# 🧭 STREAMLIT APP
# =========================================================
def main():
    st.title("💰 Reverse Auction Platform")

    if "user" not in st.session_state:
        st.session_state.user = None
    if "show_signup" not in st.session_state:
        st.session_state.show_signup = False

    # ---------------------------------------------------------
    # SIGNUP SCREEN
    # ---------------------------------------------------------
    if st.session_state.show_signup:
        st.subheader("🆕 Create a New Account")
        username = st.text_input("Choose Username")
        password = st.text_input("Choose Password", type="password")
        role = st.radio("Select Role", ["buyer", "supplier"], horizontal=True)

        if st.button("Sign Up"):
            if not username or not password:
                st.warning("Please fill all fields.")
            elif user_exists(username):
                st.error("Username already exists.")
            else:
                signup(username, password, role)
                st.success("✅ Account created successfully! You can now log in.")
                st.session_state.show_signup = False
                st.rerun()

        if st.button("🔙 Back to Login"):
            st.session_state.show_signup = False
            st.rerun()
        return

    # ---------------------------------------------------------
    # LOGIN SCREEN
    # ---------------------------------------------------------
    if not st.session_state.user:
        st.subheader("🔐 Login to Continue")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.button("Login"):
            user = login(username, password)
            if user:
                st.session_state.user = user
                st.success(f"Welcome {user['username']} ({user['role']})")
                st.rerun()
            else:
                st.error("Invalid credentials.")
        st.write("Don't have an account?")
        if st.button("📝 Sign Up"):
            st.session_state.show_signup = True
            st.rerun()
        return

    # ---------------------------------------------------------
    # DASHBOARDS
    # ---------------------------------------------------------
    user = st.session_state.user
    st.sidebar.write(f"👤 Logged in as: {user['username']} ({user['role']})")
    if st.sidebar.button("Logout"):
        st.session_state.user = None
        st.rerun()

    # ---------------------------------------------------------
    # BUYER DASHBOARD
    # ---------------------------------------------------------
    if user["role"] == "buyer":
        st.header("🧾 Buyer Dashboard")

        with st.expander("➕ Create New Auction", expanded=True):
            title = st.text_input("Auction Title")
            base_price = st.number_input("Base Price", min_value=0.0, step=100.0)
            min_dec = st.number_input("Minimum Bid Decrement (X)", min_value=1.0, step=1.0)
            duration = st.number_input("Auction Duration (Y minutes)", min_value=1, step=1)
            if st.button("Create Auction"):
                if not title:
                    st.warning("Please provide a title.")
                else:
                    create_auction(title, base_price, min_dec, duration, user["id"])
                    st.success("✅ Auction created successfully!")
                    st.rerun()

        auctions = get_auctions()
        st.subheader("📦 Your Auctions")
        for a in auctions:
            if a["created_by"] != user["id"]:
                continue
            with st.container(border=True):
                st.markdown(
                    f"**{a['title']}** | Base ₹{a['base_price']} | Current ₹{a['current_price']} | Status: {a['status']}"
                )
                if a["status"] == "not_started":
                    if st.button(f"▶️ Start Auction #{a['id']}", key=f"start{a['id']}"):
                        start_auction(a['id'])
                        st.rerun()
                elif a["status"] == "running":
                    if st.button(f"⏹ Stop Auction #{a['id']}", key=f"stop{a['id']}"):
                        stop_auction(a['id'])
                        st.rerun()
                bids = get_bids(a["id"])
                if bids:
                    st.dataframe(pd.DataFrame(bids))
                else:
                    st.info("No bids yet.")

    # ---------------------------------------------------------
    # SUPPLIER DASHBOARD
    # ---------------------------------------------------------
    elif user["role"] == "supplier":
        st.header("🏷 Supplier Dashboard")
        auctions = get_auctions()
        running_auctions = [a for a in auctions if a["status"] == "running"]

        if not running_auctions:
            st.info("No running auctions right now.")
            return

        for a in running_auctions:
            with st.container(border=True):
                st.markdown(
                    f"**{a['title']}** | Current Price: ₹{a['current_price']} | Min Decrement: ₹{a['min_decrement']}"
                )
                bid = st.number_input(
                    f"Your Bid for Auction #{a['id']}",
                    key=f"bid{a['id']}",
                    min_value=0.0,
                    step=1.0,
                )
                if st.button(f"💸 Place Bid #{a['id']}", key=f"btn{a['id']}"):
                    place_bid(a['id'], user['id'], bid)
                    st.rerun()

                bids = get_bids(a["id"])
                if bids:
                    st.dataframe(pd.DataFrame(bids))

# =========================================================
# 🚀 RUN
# =========================================================
if __name__ == "__main__":
    main()
