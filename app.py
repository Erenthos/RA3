import os
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
import streamlit as st
from fpdf import FPDF
from streamlit_autorefresh import st_autorefresh
from werkzeug.security import generate_password_hash, check_password_hash

# ---------- Configuration ----------
DATABASE_URL = st.secrets.get('NEON_URL')  # Neon connection string from Streamlit Secrets
if not DATABASE_URL:
    st.warning('NEON_URL not set in Streamlit Secrets.')

REFRESH_INTERVAL_MS = 2000  # Refresh interval for live auctions

# ---------- Database Helpers ----------

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')


def run_query(query, args=None, fetch=False, commit=False):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, args or ())
            if commit:
                conn.commit()
            if fetch:
                return cur.fetchall()

# ---------- Authentication ----------

def create_user(username, password, role):
    pw_hash = generate_password_hash(password)
    run_query("INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)", (username, pw_hash, role), commit=True)


def authenticate(username, password):
    rows = run_query("SELECT * FROM users WHERE username=%s", (username,), fetch=True)
    if not rows:
        return None
    user = rows[0]
    if check_password_hash(user['password_hash'], password):
        return dict(id=user['id'], username=user['username'], role=user['role'])
    return None

# ---------- Auction Logic ----------

def create_auction(buyer_id, title, decrement_step, duration_minutes, start_price):
    now = datetime.utcnow()
    end_time = now + timedelta(minutes=duration_minutes)
    row = run_query(
        """INSERT INTO auctions (buyer_id, title, decrement_step, duration_minutes, start_price, start_time, end_time, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (buyer_id, title, decrement_step, duration_minutes, start_price, now, end_time, 'SCHEDULED'), fetch=True, commit=True
    )
    return row[0]['id']


def add_item(auction_id, description, quantity, uom):
    run_query("INSERT INTO items (auction_id, description, quantity, uom) VALUES (%s,%s,%s,%s)", (auction_id, description, quantity, uom), commit=True)


def start_auction(auction_id):
    now = datetime.utcnow()
    run_query("UPDATE auctions SET status='LIVE', start_time=%s WHERE id=%s", (now, auction_id), commit=True)
    row = run_query("SELECT duration_minutes FROM auctions WHERE id=%s", (auction_id,), fetch=True)
    if row:
        end_time = now + timedelta(minutes=row[0]['duration_minutes'])
        run_query("UPDATE auctions SET end_time=%s WHERE id=%s", (end_time, auction_id), commit=True)


def close_if_expired(auction_id):
    now = datetime.utcnow()
    row = run_query("SELECT end_time, status FROM auctions WHERE id=%s", (auction_id,), fetch=True)
    if row and row[0]['status'] == 'LIVE' and row[0]['end_time'] and now >= row[0]['end_time']:
        run_query("UPDATE auctions SET status='CLOSED' WHERE id=%s", (auction_id,), commit=True)


def place_bid(supplier_id, item_id, bid_value):
    row = run_query(
        """SELECT a.decrement_step, a.status, COALESCE((SELECT MIN(b.bid_value) FROM bids b WHERE b.item_id=%s), a.start_price) as current_min, i.auction_id
             FROM items i JOIN auctions a ON i.auction_id=a.id WHERE i.id=%s""",
        (item_id, item_id), fetch=True
    )
    if not row:
        return False, 'Invalid item'
    data = row[0]
    if data['status'] != 'LIVE':
        return False, 'Auction not live'
    decrement = data['decrement_step']
    current_min = data['current_min']
    diff = current_min - bid_value
    if diff <= 0:
        return False, 'Bid must be lower than current minimum.'
    if diff < decrement or diff % decrement != 0:
        return False, f'Bid must reduce by multiples of {decrement}.'
    run_query("INSERT INTO bids (item_id, supplier_id, bid_value, bid_time) VALUES (%s,%s,%s,%s)", (item_id, supplier_id, bid_value, datetime.utcnow()), commit=True)
    return True, 'Bid placed'


def get_items(auction_id):
    return run_query(
        """SELECT i.*, COALESCE((SELECT MIN(b.bid_value) FROM bids b WHERE b.item_id=i.id), a.start_price) AS current_min
           FROM items i JOIN auctions a ON i.auction_id=a.id WHERE i.auction_id=%s""",
        (auction_id,), fetch=True
    )


def get_bids_for_auction(auction_id):
    return run_query(
        """SELECT b.*, u.username as supplier_name, i.description FROM bids b JOIN users u ON b.supplier_id=u.id JOIN items i ON b.item_id=i.id WHERE i.auction_id=%s ORDER BY b.bid_time DESC""",
        (auction_id,), fetch=True
    )


def get_auctions_by_buyer(buyer_id):
    return run_query("SELECT * FROM auctions WHERE buyer_id=%s ORDER BY id DESC", (buyer_id,), fetch=True)


def get_live_auctions():
    return run_query("SELECT * FROM auctions WHERE status='LIVE' ORDER BY id DESC", fetch=True)

# ---------- PDF Generation ----------

def generate_pdf_summary(auction_id):
    items = run_query("SELECT i.id, i.description, i.quantity, i.uom, a.title FROM items i JOIN auctions a ON i.auction_id=a.id WHERE i.auction_id=%s", (auction_id,), fetch=True)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font('Arial', 'B', 14)
    pdf.cell(0, 10, f'Auction Summary: {items[0]["title"] if items else auction_id}', ln=True)
    pdf.set_font('Arial', '', 12)
    for item in items:
        winner = run_query("SELECT b.bid_value, u.username FROM bids b JOIN users u ON b.supplier_id=u.id WHERE b.item_id=%s ORDER BY b.bid_value ASC LIMIT 1", (item['id'],), fetch=True)
        if winner:
            pdf.multi_cell(0, 8, f"{item['description']} | Winner: {winner[0]['username']} | Bid: {winner[0]['bid_value']}")
        else:
            pdf.multi_cell(0, 8, f"{item['description']} | No bids")
    return pdf.output(dest='S').encode('latin-1')

# ---------- Streamlit UI ----------

st.set_page_config(page_title='Reverse Auction Platform', layout='wide')
if 'user' not in st.session_state:
    st.session_state.user = None

st.title('Reverse Auction Platform')
menu = st.sidebar.selectbox('Navigation', ['Home', 'Buyer', 'Supplier'])

if menu == 'Home':
    st.write('Welcome! Please sign up or log in as a Buyer or Supplier to continue.')

# Buyer Page
if menu == 'Buyer':
    st.header('Buyer Portal')
    if not (st.session_state.user and st.session_state.user['role'] == 'buyer'):
        with st.form('buyer_form'):
            username = st.text_input('Username')
            password = st.text_input('Password', type='password')
            signup = st.checkbox('Sign up as new Buyer')
            submit = st.form_submit_button('Submit')
        if submit:
            if signup:
                try:
                    create_user(username, password, 'buyer')
                    st.success('Buyer account created successfully. Please log in.')
                except Exception as e:
                    st.error(str(e))
            else:
                user = authenticate(username, password)
                if user and user['role'] == 'buyer':
                    st.session_state.user = user
                    st.rerun()
                else:
                    st.error('Invalid credentials.')
    else:
        st.success(f"Logged in as {st.session_state.user['username']}")
        with st.form('create_auction_form'):
            title = st.text_input('Auction Title')
            decrement_step = st.number_input('Minimum Decrement', min_value=1, value=1)
            duration = st.number_input('Duration (minutes)', min_value=1, value=10)
            start_price = st.number_input('Starting Price', min_value=0.0, value=1000.0)
            items_text = st.text_area('Items (one per line: desc,qty,uom)')
            submit = st.form_submit_button('Create Auction')
            if submit:
                a_id = create_auction(st.session_state.user['id'], title, decrement_step, duration, start_price)
                for line in items_text.splitlines():
                    if not line.strip():
                        continue
                    parts = [p.strip() for p in line.split(',')]
                    desc, qty, uom = parts[0], float(parts[1]) if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 'NOS'
                    add_item(a_id, desc, qty, uom)
                st.success(f'Auction {a_id} created successfully!')

        st.subheader('Your Auctions')
        for a in get_auctions_by_buyer(st.session_state.user['id']):
            st.write(f"{a['title']} - {a['status']}")
            if st.button('Start Auction', key=f'start_{a['id']}'):
                start_auction(a['id'])
                st.success('Auction started!')
            if st.button('View Live', key=f'view_{a['id']}'):
                st.session_state['view_aid'] = a['id']

        if 'view_aid' in st.session_state:
            st_autorefresh(interval=REFRESH_INTERVAL_MS, key='buyer_refresh')
            bids = get_bids_for_auction(st.session_state['view_aid'])
            st.table(bids)

# Supplier Page
if menu == 'Supplier':
    st.header('Supplier Portal')
    if not (st.session_state.user and st.session_state.user['role'] == 'supplier'):
        with st.form('supplier_form'):
            username = st.text_input('Username')
            password = st.text_input('Password', type='password')
            signup = st.checkbox('Sign up as new Supplier')
            submit = st.form_submit_button('Submit')
        if submit:
            if signup:
                try:
                    create_user(username, password, 'supplier')
                    st.success('Supplier account created successfully. Please log in.')
                except Exception as e:
                    st.error(str(e))
            else:
                user = authenticate(username, password)
                if user and user['role'] == 'supplier':
                    st.session_state.user = user
                    st.rerun()
                else:
                    st.error('Invalid credentials.')
    else:
        st.success(f"Logged in as {st.session_state.user['username']}")
        st_autorefresh(interval=REFRESH_INTERVAL_MS, key='supplier_refresh')
        live = get_live_auctions()
        for a in live:
            st.subheader(a['title'])
            items = get_items(a['id'])
            with st.form(f'bid_form_{a['id']}'):
                bids = {}
                for it in items:
                    st.write(f"{it['description']} | Current Min: {it['current_min']}")
                    bids[it['id']] = st.number_input(f"Your bid for {it['description']}", min_value=0.0, key=f"bid_{it['id']}")
                submit = st.form_submit_button('Place Bids')
                if submit:
                    count = 0
                    for iid, val in bids.items():
                        if val > 0:
                            ok, msg = place_bid(st.session_state.user['id'], iid, val)
                            if ok:
                                count += 1
                            else:
                                st.error(msg)
                    if count:
                        st.success(f'{count} bid(s) placed successfully!')
