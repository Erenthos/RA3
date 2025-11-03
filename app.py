# app.py
import os
import time
import io
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
import streamlit as st
from werkzeug.security import generate_password_hash, check_password_hash
from fpdf import FPDF
from streamlit_autorefresh import st_autorefresh

# ---------- Configuration ----------
DATABASE_URL = st.secrets.get('NEON_URL')  # Neon connection string stored in Streamlit Secrets
if not DATABASE_URL:
    st.warning('NEON_URL not set in Streamlit Secrets.')

REFRESH_INTERVAL_MS = 2000  # how often pages auto refresh in ms

# ---------- DB helpers ----------

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

# ---------- Auth ----------

def create_user(username, password, role='supplier'):
    pw_hash = generate_password_hash(password)
    run_query(
        "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
        (username, pw_hash, role), commit=True
    )


def authenticate(username, password):
    rows = run_query("SELECT * FROM users WHERE username=%s", (username,), fetch=True)
    if not rows:
        return None
    user = rows[0]
    if check_password_hash(user['password_hash'], password):
        return dict(id=user['id'], username=user['username'], role=user['role'])
    return None

# ---------- Auction logic ----------

def create_auction(buyer_id, title, decrement_step, duration_minutes, start_price):
    now = datetime.utcnow()
    end_time = now + timedelta(minutes=duration_minutes)
    row = run_query(
        "INSERT INTO auctions (buyer_id, title, decrement_step, duration_minutes, start_price, start_time, end_time, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (buyer_id, title, decrement_step, duration_minutes, start_price, now, end_time, 'SCHEDULED'), fetch=True, commit=True
    )
    return row[0]['id']


def add_item(auction_id, description, quantity, uom):
    run_query(
        "INSERT INTO items (auction_id, description, quantity, uom) VALUES (%s,%s,%s,%s)",
        (auction_id, description, quantity, uom), commit=True
    )


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
    if not row:
        return
    end_time = row[0]['end_time']
    status = row[0]['status']
    if status == 'LIVE' and end_time and now >= end_time:
        run_query("UPDATE auctions SET status='CLOSED' WHERE id=%s", (auction_id,), commit=True)


def place_bid(supplier_id, item_id, bid_value):
    row = run_query(
        "SELECT a.decrement_step, a.status, COALESCE((SELECT MIN(b.bid_value) FROM bids b WHERE b.item_id=%s), i.start_price) as current_min, i.auction_id FROM items i JOIN auctions a ON i.auction_id=a.id WHERE i.id=%s",
        (item_id, item_id), fetch=True
    )
    if not row:
        return False, 'Invalid item'
    data = row[0]
    if data['status'] != 'LIVE':
        return False, 'Auction is not live'

    decrement = data['decrement_step']
    current_min = data['current_min'] if data['current_min'] is not None else data.get('start_price')
    if current_min is None:
        auction_row = run_query("SELECT start_price FROM auctions WHERE id=%s", (data['auction_id'],), fetch=True)
        current_min = auction_row[0]['start_price'] if auction_row else None

    if current_min is None:
        return False, 'Cannot determine current price'

    diff = current_min - bid_value
    if diff <= 0:
        return False, 'Bid must be less than current minimum (reverse auction)'
    if diff < decrement:
        return False, f'Bid must reduce price by at least the decrement step ({decrement})'
    if (diff % decrement) != 0:
        return False, f'Bid decrease must be in multiples of {decrement}'

    run_query("INSERT INTO bids (item_id, supplier_id, bid_value, bid_time) VALUES (%s,%s,%s,%s)", (item_id, supplier_id, bid_value, datetime.utcnow()), commit=True)
    return True, 'Bid placed'


def get_items(auction_id):
    return run_query("SELECT i.*, COALESCE((SELECT MIN(b.bid_value) FROM bids b WHERE b.item_id=i.id), a.start_price) as current_min FROM items i JOIN auctions a ON i.auction_id=a.id WHERE i.auction_id=%s", (auction_id,), fetch=True)


def get_bids_for_auction(auction_id):
    return run_query("SELECT b.*, u.username as supplier_name, i.description FROM bids b JOIN users u ON b.supplier_id=u.id JOIN items i ON b.item_id=i.id WHERE i.auction_id=%s ORDER BY b.bid_time DESC", (auction_id,), fetch=True)


def get_auctions_by_buyer(buyer_id):
    return run_query("SELECT * FROM auctions WHERE buyer_id=%s ORDER BY id DESC", (buyer_id,), fetch=True)


def get_live_auctions():
    now = datetime.utcnow()
    return run_query("SELECT * FROM auctions WHERE status='LIVE' ORDER BY id DESC", fetch=True)

# ---------- PDF generation ----------

def generate_pdf_summary(auction_id):
    items = run_query("SELECT i.id, i.description, i.quantity, i.uom, a.title FROM items i JOIN auctions a ON i.auction_id=a.id WHERE i.auction_id=%s", (auction_id,), fetch=True)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font('Arial', 'B', 14)
    pdf.cell(0, 10, f'Auction Summary: {items[0]["title"] if items else auction_id}', ln=True)
    pdf.ln(4)
    pdf.set_font('Arial', '', 12)
    for item in items:
        winner = run_query("SELECT b.bid_value, u.username FROM bids b JOIN users u ON b.supplier_id=u.id WHERE b.item_id=%s ORDER BY b.bid_value ASC LIMIT 1", (item['id'],), fetch=True)
        if winner:
            bid_value = winner[0]['bid_value']
            supplier = winner[0]['username']
        else:
            bid_value = 'No bids'
            supplier = 'N/A'
        pdf.multi_cell(0, 8, f"Item: {item['description']} | Qty: {item['quantity']} {item['uom']} | Winner: {supplier} | Price: {bid_value}")
        pdf.ln(1)

    return pdf.output(dest='S').encode('latin-1')

# ---------- Streamlit UI ----------

st.set_page_config(page_title='Reverse Auction (Neon + Streamlit)', layout='wide')

if 'user' not in st.session_state:
    st.session_state.user = None

st_autorefresh(interval=REFRESH_INTERVAL_MS, key="autorefresh")

st.title('Reverse Auction Platform')

menu = st.sidebar.selectbox('Go to', ['Home', 'Buyer', 'Supplier', 'Admin (dev)'])

if menu == 'Home':
    st.markdown('Welcome. Use the sidebar to login as Buyer or Supplier.')

# ---------- Buyer Page ----------
if menu == 'Buyer':
    st.header('Buyer Portal')
    if st.session_state.user and st.session_state.user['role'] == 'buyer':
        st.success(f"Logged in as {st.session_state.user['username']}")
    else:
        with st.form('buyer_login'):
            buser = st.text_input('Username')
            bpw = st.text_input('Password', type='password')
            submitted = st.form_submit_button('Login')
            if submitted:
                user = authenticate(buser, bpw)
                if user and user['role'] == 'buyer':
                    st.session_state.user = user
                    st.rerun()
                else:
                    st.error('Invalid buyer credentials')

    if st.session_state.user and st.session_state.user['role'] == 'buyer':
        st.subheader('Create Auction')
        with st.form('create_auction_form'):
            title = st.text_input('Auction Title')
            decrement_step = st.number_input('Minimum decrement step (X)', min_value=1, value=1)
            duration_minutes = st.number_input('Duration (minutes)', min_value=1, value=10)
            start_price = st.number_input('Start price (per item)', min_value=0.0, value=1000.0)
            items_text = st.text_area('Items (one per line: description,qty,uom)\nExample: Solar Cable 4mm,100,MTR')
            create_submit = st.form_submit_button('Create Auction')
            if create_submit:
                auction_id = create_auction(st.session_state.user['id'], title, decrement_step, duration_minutes, start_price)
                for line in items_text.splitlines():
                    if not line.strip():
                        continue
                    parts = [p.strip() for p in line.split(',')]
                    desc = parts[0]
                    qty = float(parts[1]) if len(parts) > 1 else 1
                    uom = parts[2] if len(parts) > 2 else 'NOS'
                    add_item(auction_id, desc, qty, uom)
                st.success(f'Auction {auction_id} created successfully.')

        st.subheader('Your Auctions')
        auctions = get_auctions_by_buyer(st.session_state.user['id'])
        for a in auctions:
            st.markdown(f"**Auction {a['id']} — {a['title']}**  — Status: {a['status']}")
            cols = st.columns(3)
            with cols[0]:
                if st.button('Start Auction', key=f'start_{a['id']}'):
                    start_auction(a['id'])
                    st.success('Auction started')
            with cols[1]:
                if st.button('View Live Status', key=f'view_{a['id']}'):
                    st.session_state.view_auction = a['id']
            with cols[2]:
                if st.button('Download Summary PDF', key=f'pdf_{a['id']}'):
                    pdf_bytes = generate_pdf_summary(a['id'])
                    st.download_button('Download PDF', data=pdf_bytes, file_name=f'auction_{a['id']}_summary.pdf', mime='application/pdf')

        if 'view_auction' in st.session_state:
            aid = st.session_state.view_auction
            close_if_expired(aid)
            st.markdown('---')
            st.subheader(f'Live Auction View — {aid}')
            bids = get_bids_for_auction(aid)
            st.table(bids)

# ---------- Supplier Page ----------
if menu == 'Supplier':
    st.header('Supplier Portal')
    if not (st.session_state.user and st.session_state.user['role'] == 'supplier'):
        with st.form('supplier_login'):
            suser = st.text_input('Username')
            spw = st.text_input('Password', type='password')
            register = st.checkbox('Register new account')
            submit = st.form_submit_button('Proceed')
            if submit:
                if register:
                    try:
                        create_user(suser, spw, role='supplier')
                        st.success('Supplier created. Please login.')
                    except Exception as e:
                        st.error(f'Unable to create user: {e}')
                else:
                    user = authenticate(suser, spw)
                    if user and user['role'] == 'supplier':
                        st.session_state.user = user
                        st.rerun()
                    else:
                        st.error('Invalid supplier credentials')

    if st.session_state.user and st.session_state.user['role'] == 'supplier':
        st.success(f"Logged in as {st.session_state.user['username']}")
        st.subheader('Live Auctions')
        live = get_live_auctions()
        for a in live:
            st.markdown(f"**Auction {a['id']} — {a['title']}**")
            items = get_items(a['id'])
            for it in items:
                st.write(f"Item {it['id']}: {it['description']} | Qty: {it['quantity']} {it['uom']} | Current Min: {it['current_min']}")
            st.markdown('---')
            with st.form(f'bid_form_{a['id']}'):
                st.write('Place bids (leave blank to skip an item)')
                bid_inputs = {}
                for it in items:
                    bid_inputs[it['id']] = st.number_input(f"Bid for Item {it['id']} ({it['description']})", min_value=0.0, value=0.0, key=f"bid_{a['id']}_{it['id']}")
                submit_bids = st.form_submit_button('Submit Bids')
                if submit_bids:
                    placed = 0
                    for iid, val in bid_inputs.items():
                        if val and val > 0:
                            ok, msg = place_bid(st.session_state.user['id'], iid, val)
                            if ok:
                                placed += 1
                            else:
                                st.error(f'Item {iid}: {msg}')
                    st.success(f'Placed {placed} bids')

# ---------- Admin (dev) ----------
if menu == 'Admin (dev)':
    st.header('Admin / Dev')
    st.subheader('Create default admin user')
    if st.button('Create admin'):
        try:
            create_user('admin', 'adminpass', role='buyer')
            st.success('Admin created')
        except Exception as e:
            st.error(f'Error creating admin: {e}')

    st.subheader('DB Quick View')
    try:
        st.write('Auctions:')
        st.table(run_query('SELECT * FROM auctions', fetch=True) or [])
        st.write('Items:')
        st.table(run_query('SELECT * FROM items', fetch=True) or [])
        st.write('Bids:')
        st.table(run_query('SELECT * FROM bids', fetch=True) or [])
    except Exception as e:
        st.error(f'Unable to read DB: {e}')
